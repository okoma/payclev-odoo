"""Invoice visibility is opt-in, company-scoped and never edits accounting fields."""

from datetime import datetime, timezone
from uuid import uuid4

from odoo.exceptions import AccessError
from odoo.tests import TransactionCase, tagged


@tagged("-at_install", "post_install")
class TestPayclevInvoiceContext(TransactionCase):
    def setUp(self):
        super().setUp()
        company = self.env.company
        self.connection = self.env["payclev.connection"].search(
            [
                ("company_id", "=", company.id),
            ],
            limit=1,
        )
        if not self.connection:
            self.connection = self.env["payclev.connection"].create(
                {
                    "company_id": company.id,
                }
            )
        self.bot = self.connection._service_user()
        self.identity = {
            "company_id": company.id,
            "integration_id": str(uuid4()),
            "organization_id": str(uuid4()),
        }
        self.connection.write(
            {
                "state": "connected",
                "service_user_id": self.bot.id,
                "integration_id": self.identity["integration_id"],
                "organization_id": self.identity["organization_id"],
            }
        )
        self.invoice = self.env["account.move"].create(
            {
                "company_id": company.id,
                "partner_id": self.env.ref("base.res_partner_2").id,
                "move_type": "out_invoice",
            }
        )
        self.bridge = self.env["payclev.payment.bridge"].with_user(self.bot)

    def _snapshot(self):
        return {
            "odoo_invoice_id": self.invoice.id,
            "invoice_uuid": str(uuid4()),
            "collection_status": "promise_to_pay",
            "payment_status": "received",
            "next_step": "Payment received. Payclev is updating Odoo.",
            "last_contact_at": datetime.now(timezone.utc).isoformat(),
            "snapshot_at": datetime.now(timezone.utc).isoformat(),
            "events": [
                {
                    "id": str(uuid4()),
                    "action": "followup.sent",
                    "occurred_at": datetime.now(timezone.utc).isoformat(),
                }
            ],
        }

    def test_off_by_default_and_read_only_snapshot(self):
        self.assertFalse(self.connection.show_invoice_panel)
        self.assertFalse(self.connection.show_invoice_activity)
        self.assertFalse(self.invoice.payclev_context_visible)
        snapshot = self._snapshot()
        with self.assertRaises(AccessError):
            self.bridge.payclev_update_invoice_contexts(
                **self.identity, snapshots=[snapshot]
            )
        self.connection.show_invoice_panel = True
        self.connection.show_invoice_activity = True
        before = self.invoice.write_date
        self.bridge.payclev_update_invoice_contexts(
            **self.identity, snapshots=[snapshot]
        )
        self.invoice.invalidate_recordset()
        self.assertEqual(self.invoice.write_date, before)
        self.assertEqual(self.invoice.payclev_collection_status, "promise_to_pay")
        self.assertEqual(self.invoice.payclev_payment_status, "received")
        self.assertEqual(self.invoice.payclev_activity_ids.label, "Follow-up sent")
        self.assertTrue(self.invoice.payclev_context_visible)
        self.assertTrue(self.invoice.payclev_activity_visible)
        self.connection.show_invoice_panel = False
        self.invoice.invalidate_recordset()
        self.assertFalse(self.invoice.payclev_context_visible)

    def test_rejects_wrong_company_invoice(self):
        self.connection.show_invoice_panel = True
        other = self.env["res.company"].create({"name": "Other Odoo company"})
        foreign_invoice = (
            self.env["account.move"]
            .sudo()
            .create(
                {
                    "company_id": other.id,
                    "partner_id": self.env.ref("base.res_partner_2").id,
                    "move_type": "out_invoice",
                }
            )
        )
        snapshot = self._snapshot()
        snapshot["odoo_invoice_id"] = foreign_invoice.id
        with self.assertRaises(AccessError):
            self.bridge.payclev_update_invoice_contexts(
                **self.identity, snapshots=[snapshot]
            )
