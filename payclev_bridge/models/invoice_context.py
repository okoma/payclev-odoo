"""Read-only Payclev collection context on Odoo customer invoices."""

from datetime import datetime, timezone
from urllib.parse import urlencode
from uuid import UUID

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError

COLLECTION_STATES = [
    ("not_started", "Not started"),
    ("active", "Active"),
    ("paused", "Paused"),
    ("promise_to_pay", "Promise to pay"),
    ("disputed", "Disputed"),
    ("payment_plan", "Payment plan"),
    ("manual_attention", "Needs attention"),
    ("completed", "Completed"),
]
PAYMENT_STATES = [
    ("none", "No Payclev payment"),
    ("received", "Payment received; accounting update pending"),
    ("reconciled", "Recorded in Odoo"),
    ("needs_review", "Accounting update needs review"),
]
EVENT_LABELS = {
    "invoice.sent": "Invoice send requested",
    "invoice.email_sent": "Invoice email sent",
    "invoice.email_failed": "Invoice email needs attention",
    "invoice.viewed": "Invoice viewed",
    "followup.sent": "Follow-up sent",
    "followup.paused": "Follow-ups paused",
    "followup.resumed": "Follow-ups resumed",
    "followup.failed": "Follow-up needs attention",
    "conversation.message_received": "Customer replied",
    "conversation.message_sent": "Message sent to customer",
    "payment.verified": "Payment verified",
    "invoice.payment_reconciled": "Payment reconciled",
    "invoice.payment_reversal_reconciled": "Payment reversal recorded",
    "payment_promise.proposed": "Payment promise proposed",
    "payment_promise.accepted": "Payment promise accepted",
    "payment_promise.declined": "Payment promise declined",
    "payment_promise.kept": "Payment promise kept",
    "payment_promise.missed": "Payment promise missed",
    "payment_plan.proposed": "Payment plan proposed",
    "payment_plan.approved": "Payment plan approved",
    "payment_plan.declined": "Payment plan declined",
    "payment_plan.defaulted": "Payment plan needs attention",
    "dispute.created": "Dispute opened",
    "dispute.resolved": "Dispute resolved",
    "dispute.rejected": "Dispute rejected",
}


def _utc_naive(value):
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError) as error:
        raise UserError(_("Payclev returned an invalid activity date.")) from error
    if parsed.tzinfo is None:
        raise UserError(_("Payclev activity dates must include a timezone."))
    return parsed.astimezone(timezone.utc).replace(tzinfo=None)


class AccountMove(models.Model):
    _inherit = "account.move"

    payclev_context_visible = fields.Boolean(compute="_compute_payclev_visibility")
    payclev_activity_visible = fields.Boolean(compute="_compute_payclev_visibility")
    payclev_context_id = fields.Many2one(
        "payclev.invoice.context",
        compute="_compute_payclev_context",
        readonly=True,
    )
    payclev_invoice_uuid = fields.Char(related="payclev_context_id.invoice_uuid")
    payclev_collection_status = fields.Selection(
        related="payclev_context_id.collection_status",
    )
    payclev_payment_status = fields.Selection(
        related="payclev_context_id.payment_status"
    )
    payclev_next_step = fields.Char(related="payclev_context_id.next_step")
    payclev_last_contact_at = fields.Datetime(
        related="payclev_context_id.last_contact_at"
    )
    payclev_snapshot_at = fields.Datetime(related="payclev_context_id.snapshot_at")
    payclev_activity_ids = fields.One2many(related="payclev_context_id.activity_ids")

    def _compute_payclev_context(self):
        contexts = (
            self.env["payclev.invoice.context"]
            .sudo()
            .search(
                [
                    ("invoice_id", "in", self.ids),
                ]
            )
        )
        by_invoice = {row.invoice_id.id: row for row in contexts}
        for move in self:
            move.payclev_context_id = by_invoice.get(move.id)

    @api.depends("company_id", "move_type")
    def _compute_payclev_visibility(self):
        company_ids = list(set(self.mapped("company_id").ids))
        connections = (
            self.env["payclev.connection"]
            .sudo()
            .search(
                [
                    ("company_id", "in", company_ids),
                    ("state", "=", "connected"),
                ]
            )
        )
        by_company = {row.company_id.id: row for row in connections}
        for move in self:
            connection = by_company.get(move.company_id.id)
            allowed = move.move_type == "out_invoice" and bool(connection)
            move.payclev_context_visible = allowed and bool(
                connection.show_invoice_panel
            )
            move.payclev_activity_visible = move.payclev_context_visible and bool(
                connection.show_invoice_activity
            )

    def action_open_payclev_invoice(self):
        self.ensure_one()
        if not self.payclev_context_visible or not self.payclev_invoice_uuid:
            raise UserError(_("Payclev context is not available for this invoice."))
        connection = (
            self.env["payclev.connection"]
            .sudo()
            .search(
                [
                    ("company_id", "=", self.company_id.id),
                    ("state", "=", "connected"),
                ],
                limit=1,
            )
        )
        if not connection or not connection.organization_id:
            raise AccessError(
                _("This invoice is not linked to a connected Payclev business.")
            )
        origin = connection._payclev_origin(
            connection.payclev_app_url,
            production_host="app.payclev.com",
            allow_local=connection.allow_local_testing,
        )
        return {
            "type": "ir.actions.act_url",
            "url": f"{origin}/invoices/{self.payclev_invoice_uuid}?"
            f"{urlencode({'organization_id': connection.organization_id})}",
            "target": "new",
        }


class PayclevInvoiceContext(models.Model):
    _name = "payclev.invoice.context"
    _description = "Payclev invoice collection snapshot"
    _check_company_auto = True

    company_id = fields.Many2one("res.company", required=True, index=True)
    invoice_id = fields.Many2one(
        "account.move",
        required=True,
        index=True,
        ondelete="cascade",
        check_company=True,
    )
    invoice_uuid = fields.Char(required=True)
    collection_status = fields.Selection(COLLECTION_STATES, required=True)
    payment_status = fields.Selection(PAYMENT_STATES, required=True)
    next_step = fields.Char()
    last_contact_at = fields.Datetime()
    snapshot_at = fields.Datetime(required=True)
    activity_ids = fields.One2many("payclev.invoice.event", "context_id")
    _unique_invoice = models.Constraint(
        "unique(company_id, invoice_id)",
        "Only one Payclev snapshot is allowed per invoice.",
    )


class PayclevInvoiceEvent(models.Model):
    _name = "payclev.invoice.event"
    _description = "Read-only Payclev invoice activity"
    _order = "occurred_at desc, id desc"
    _check_company_auto = True

    company_id = fields.Many2one("res.company", required=True, index=True)
    context_id = fields.Many2one(
        "payclev.invoice.context",
        required=True,
        index=True,
        ondelete="cascade",
        check_company=True,
    )
    event_uuid = fields.Char(required=True, index=True)
    label = fields.Char(required=True)
    occurred_at = fields.Datetime(required=True)
    _unique_event = models.Constraint(
        "unique(company_id, context_id, event_uuid)",
        "This Payclev invoice activity is already recorded.",
    )


class PayclevInvoiceContextBridge(models.Model):
    _inherit = "payclev.payment.bridge"

    @api.model
    def payclev_update_invoice_contexts(
        self,
        company_id,
        integration_id,
        organization_id,
        snapshots,
    ):
        company, connection = self._paired_connection(
            company_id,
            integration_id,
            organization_id,
        )
        if not connection.show_invoice_panel:
            raise AccessError(_("Payclev invoice visibility is off for this company."))
        if not isinstance(snapshots, list) or len(snapshots) > 100:
            raise UserError(
                _("Payclev invoice updates must contain at most 100 records.")
            )
        for snapshot in snapshots:
            if not isinstance(snapshot, dict):
                raise UserError(_("The Payclev invoice update is invalid."))
            try:
                external_id = int(snapshot["odoo_invoice_id"])
                invoice_uuid = str(UUID(str(snapshot["invoice_uuid"])))
            except (KeyError, ValueError, TypeError) as error:
                raise UserError(
                    _("The Payclev invoice identity is invalid.")
                ) from error
            invoice = (
                self.env["account.move"]
                .sudo()
                .search(
                    [
                        ("id", "=", external_id),
                        ("company_id", "=", company.id),
                        ("move_type", "=", "out_invoice"),
                    ],
                    limit=1,
                )
            )
            if not invoice:
                raise AccessError(
                    _("The invoice does not belong to this Odoo company.")
                )
            collection = snapshot.get("collection_status")
            payment = snapshot.get("payment_status")
            if collection not in dict(COLLECTION_STATES) or payment not in dict(
                PAYMENT_STATES
            ):
                raise UserError(_("The Payclev collection state is invalid."))
            snapshot_at = _utc_naive(snapshot.get("snapshot_at"))
            context = (
                self.env["payclev.invoice.context"]
                .sudo()
                .search(
                    [
                        ("company_id", "=", company.id),
                        ("invoice_id", "=", invoice.id),
                    ],
                    limit=1,
                )
            )
            if context and context.snapshot_at and snapshot_at < context.snapshot_at:
                continue
            events = (
                snapshot.get("events", []) if connection.show_invoice_activity else []
            )
            if not isinstance(events, list) or len(events) > 20:
                raise UserError(_("The Payclev activity list is too large."))
            next_step = snapshot.get("next_step")
            if next_step is not None and (
                not isinstance(next_step, str) or len(next_step) > 240
            ):
                raise UserError(_("The Payclev next step is invalid."))
            last_contact = snapshot.get("last_contact_at")
            values = {
                "company_id": company.id,
                "invoice_id": invoice.id,
                "invoice_uuid": invoice_uuid,
                "collection_status": collection,
                "payment_status": payment,
                "next_step": next_step or False,
                "last_contact_at": _utc_naive(last_contact) if last_contact else False,
                "snapshot_at": snapshot_at,
            }
            if context:
                if context.invoice_uuid != invoice_uuid:
                    raise AccessError(
                        _(
                            "This Odoo invoice is already linked to another Payclev invoice."
                        )
                    )
                context.write(values)
            else:
                context = self.env["payclev.invoice.context"].sudo().create(values)
            if connection.show_invoice_activity:
                new_events = []
                for event in events:
                    if (
                        not isinstance(event, dict)
                        or event.get("action") not in EVENT_LABELS
                    ):
                        raise UserError(_("The Payclev activity event is invalid."))
                    try:
                        event_uuid = str(UUID(str(event["id"])))
                    except (KeyError, ValueError, TypeError) as error:
                        raise UserError(
                            _("The Payclev activity identity is invalid.")
                        ) from error
                    new_events.append(
                        {
                            "company_id": company.id,
                            "context_id": context.id,
                            "event_uuid": event_uuid,
                            "label": EVENT_LABELS[event["action"]],
                            "occurred_at": _utc_naive(event.get("occurred_at")),
                        }
                    )
                self.env["payclev.invoice.event"].sudo().search(
                    [
                        ("company_id", "=", company.id),
                        ("context_id", "=", context.id),
                    ]
                ).unlink()
                if new_events:
                    self.env["payclev.invoice.event"].sudo().create(new_events)
        return {"updated": len(snapshots)}
