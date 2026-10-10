"""Durable, signed notifications for newly posted customer invoices."""

import hashlib
import hmac
import json
import logging
import uuid
from datetime import datetime, timedelta, timezone

import requests

from odoo import api, fields, models

_logger = logging.getLogger(__name__)


class AccountMove(models.Model):
    _inherit = "account.move"

    def _post(self, soft=True):
        posted = super()._post(soft=soft)
        invoices = posted.filtered(lambda move: move.move_type == "out_invoice")
        if not invoices:
            return posted
        connections = self.env["payclev.connection"].sudo().search([
            ("company_id", "in", invoices.mapped("company_id").ids),
            ("state", "=", "connected"),
        ])
        by_company = {item.company_id.id: item for item in connections}
        try:
            with self.env.cr.savepoint():
                outbox = self.env["payclev.invoice.notification"].sudo()
                for invoice in invoices:
                    if invoice.company_id.id in by_company and not outbox.search_count([
                        ("invoice_id", "=", invoice.id),
                        ("event_type", "=", "invoice.posted"),
                    ]):
                        outbox.create({
                            "company_id": invoice.company_id.id,
                            "invoice_id": invoice.id,
                            "event_type": "invoice.posted",
                        })
                if connections:
                    self.env.ref("payclev_bridge.ir_cron_invoice_notifications").sudo()._trigger()
        except Exception:
            # Independent incremental sync recovers the invoice. A broken
            # notification must never prevent Odoo from posting it.
            _logger.exception("Payclev invoice notification could not be queued")
        return posted


class PayclevInvoiceNotification(models.Model):
    _name = "payclev.invoice.notification"
    _description = "Payclev invoice notification outbox"
    _order = "create_date asc"

    company_id = fields.Many2one("res.company", required=True, index=True, ondelete="restrict")
    invoice_id = fields.Many2one("account.move", required=True, index=True, ondelete="restrict")
    event_id = fields.Char(default=lambda self: str(uuid.uuid4()), required=True, readonly=True)
    event_type = fields.Char(required=True, readonly=True)
    state = fields.Selection([("pending", "Pending"), ("sent", "Sent")], default="pending", required=True, index=True)
    attempts = fields.Integer(default=0, readonly=True)
    next_attempt_at = fields.Datetime(default=fields.Datetime.now, index=True)
    delivered_at = fields.Datetime(readonly=True)
    last_error = fields.Char(readonly=True)

    _unique_invoice_event = models.Constraint(
        "unique(invoice_id, event_type)", "This invoice event is already queued."
    )
    _unique_event_id = models.Constraint("unique(event_id)", "This event ID is already used.")

    @api.model
    def _cron_deliver(self):
        now = fields.Datetime.now()
        pending = self.sudo().search([
            ("state", "=", "pending"), ("next_attempt_at", "<=", now),
        ], limit=20)
        for event in pending:
            connection = self.env["payclev.connection"].sudo().search([
                ("company_id", "=", event.company_id.id),
                ("state", "=", "connected"),
            ], limit=1)
            if not connection or not connection.notification_secret:
                event._retry("Connection or notification credential is not ready.")
                continue
            try:
                local = bool(connection.allow_local_testing)
                origin = connection._payclev_origin(
                    connection.payclev_api_url,
                    production_host="api.payclev.com",
                    allow_local=local,
                )
                payload = {
                    "event_id": event.event_id,
                    "event_type": event.event_type,
                    "integration_id": connection.integration_id,
                    "organization_id": connection.organization_id,
                    "company_id": event.company_id.id,
                    "invoice_id": event.invoice_id.id,
                }
                raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
                timestamp = str(int(datetime.now(timezone.utc).timestamp()))
                signature = hmac.new(
                    connection.notification_secret.encode(),
                    timestamp.encode() + b"." + raw,
                    hashlib.sha256,
                ).hexdigest()
                response = requests.post(
                    origin + "/webhooks/odoo",
                    data=raw,
                    headers={
                        "Content-Type": "application/json",
                        "X-Payclev-Odoo-Timestamp": timestamp,
                        "X-Payclev-Odoo-Signature": signature,
                    },
                    timeout=8,
                    allow_redirects=False,
                )
                if response.status_code == 202:
                    event.write({
                        "state": "sent", "delivered_at": fields.Datetime.now(),
                        "last_error": False,
                    })
                else:
                    event._retry("Payclev returned HTTP %s." % response.status_code)
            except (requests.RequestException, ValueError):
                event._retry("Payclev notification delivery failed.")

    def _retry(self, reason):
        self.ensure_one()
        attempts = self.attempts + 1
        self.write({
            "attempts": attempts,
            "next_attempt_at": fields.Datetime.now() + timedelta(minutes=min(60, 2 ** min(attempts, 6))),
            "last_error": reason,
        })
