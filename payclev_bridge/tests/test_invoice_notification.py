"""Posting is independent of Payclev; outbox delivery is signed and retryable."""

import hashlib
import hmac
from unittest.mock import Mock, patch

from odoo.tests import TransactionCase, tagged


@tagged("-at_install", "post_install")
class TestInvoiceNotification(TransactionCase):
    def setUp(self):
        super().setUp()
        connection = self.env["payclev.connection"].search([
            ("company_id", "=", self.env.company.id)
        ], limit=1)
        values = {
            "state": "connected",
            "integration_id": "11111111-1111-4111-8111-111111111111",
            "organization_id": "22222222-2222-4222-8222-222222222222",
            "notification_secret": "s" * 48,
            "allow_local_testing": True,
            "payclev_api_url": "http://localhost:8000",
        }
        if connection:
            connection.write(values)
        else:
            connection = self.env["payclev.connection"].create({
                "company_id": self.env.company.id, **values
            })
        self.partner = self.env["res.partner"].create({
            "name": "Payclev notification test customer"
        })

    def _post_invoice(self):
        journal = self.env["account.journal"].search([
            ("company_id", "=", self.env.company.id), ("type", "=", "sale")
        ], limit=1)
        account = self.env["account.account"].search([
            ("account_type", "=", "income")
        ], limit=1)
        invoice = self.env["account.move"].create({
            "move_type": "out_invoice",
            "partner_id": self.partner.id,
            "journal_id": journal.id,
            "invoice_line_ids": [(0, 0, {
                "name": "Notification test", "quantity": 1,
                "price_unit": 10, "account_id": account.id,
            })],
        })
        with patch("odoo.addons.payclev_bridge.models.invoice_notification.requests.post") as send:
            invoice.action_post()
            send.assert_not_called()
        self.assertEqual(invoice.state, "posted")
        event = self.env["payclev.invoice.notification"].search([
            ("invoice_id", "=", invoice.id)
        ])
        self.assertEqual(len(event), 1)
        return event

    def test_post_creates_outbox_and_signed_delivery(self):
        event = self._post_invoice()
        with patch(
            "odoo.addons.payclev_bridge.models.invoice_notification.requests.post",
            return_value=Mock(status_code=202),
        ) as send:
            event._cron_deliver()
        self.assertEqual(event.state, "sent")
        kwargs = send.call_args.kwargs
        raw = kwargs["data"]
        stamp = kwargs["headers"]["X-Payclev-Odoo-Timestamp"]
        expected = hmac.new(
            ("s" * 48).encode(), stamp.encode() + b"." + raw, hashlib.sha256
        ).hexdigest()
        self.assertEqual(kwargs["headers"]["X-Payclev-Odoo-Signature"], expected)
        self.assertFalse(kwargs["allow_redirects"])

    def test_payclev_failure_retries_without_changing_invoice(self):
        event = self._post_invoice()
        with patch(
            "odoo.addons.payclev_bridge.models.invoice_notification.requests.post",
            return_value=Mock(status_code=503),
        ):
            event._cron_deliver()
        self.assertEqual(event.state, "pending")
        self.assertEqual(event.attempts, 1)
        self.assertEqual(event.invoice_id.state, "posted")
