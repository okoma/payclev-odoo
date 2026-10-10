"""Run inside a real Odoo 19 test database, never against production data."""

from ast import literal_eval
from uuid import uuid4
from unittest.mock import MagicMock, patch

from odoo import Command, fields
from odoo.addons.account.tests.common import AccountTestInvoicingCommon
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.tests import tagged


@tagged("-at_install", "post_install")
class TestPayClevPaymentBridge(AccountTestInvoicingCommon):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = cls.env.company
        cls.journal = cls.company_data["default_journal_bank"] or cls.env[
            "account.journal"
        ].create(
            {
                "name": "Payclev test bank",
                "code": "PCVB",
                "type": "bank",
                "company_id": cls.company.id,
            }
        )
        cls.journal.payclev_bridge_enabled = True
        cls.method = cls.journal.inbound_payment_method_line_ids.filtered(
            lambda line: line.code == "manual"
        )[:1]
        if not cls.method:
            cls.method = cls.env["account.payment.method.line"].create(
                {
                    "journal_id": cls.journal.id,
                    "payment_method_id": cls.env.ref(
                        "account.account_payment_method_manual_in"
                    ).id,
                }
            )
        if not cls.method.payment_account_id:
            cls.method.payment_account_id = cls.env["account.account"].create(
                {
                    "name": "Payclev test outstanding receipts",
                    "code": "999998",
                    "account_type": "asset_current",
                    "reconcile": True,
                }
            )
        cls.bot = cls.env["res.users"].create(
            {
                "name": "PayClev test bot",
                "login": "payclev-test-bot@example.invalid",
                "company_id": cls.company.id,
                "company_ids": [Command.set(cls.company.ids)],
                "group_ids": [
                    Command.link(cls.env.ref("account.group_account_user").id),
                    Command.link(cls.env.ref("payclev_bridge.group_payclev_bridge").id),
                ],
            }
        )

    def setUp(self):
        super().setUp()
        self.invoice = self._create_invoice_one_line(
            price_unit=100.0, tax_ids=[], post=True
        )
        self.bridge = self.env["payclev.payment.bridge"].with_user(self.bot)

    def _request(self, **overrides):
        currency = self.invoice.currency_id
        values = {
            "company_id": self.company.id,
            "invoice_id": self.invoice.id,
            "customer_id": self.invoice.partner_id.id,
            "payment_uuid": str(uuid4()),
            "amount_minor": 40 * 10**currency.decimal_places,
            "currency": currency.name,
            "journal_id": self.journal.id,
            "payment_method_line_id": self.method.id,
            "payment_date": fields.Date.today().isoformat(),
        }
        return values | overrides

    def test_health_and_methods_are_company_scoped(self):
        health = self.bridge.payclev_health(self.company.id)
        self.assertEqual(health["bridge_version"], "1.2.0")
        self.assertTrue(health["odoo_version"].startswith("19."))
        self.assertEqual(health["company_currency"], self.company.currency_id.name)
        methods = self.bridge.payclev_payment_methods(self.company.id)
        self.assertIn(
            (self.journal.id, self.method.id),
            [(row["journal_id"], row["method_id"]) for row in methods],
        )

    def test_official_payment_link_requires_odoo_online_eligibility(self):
        self.env["ir.config_parameter"].sudo().set_param(
            "account_payment.enable_portal_payment", "False"
        )
        self.assertEqual(
            self.bridge.payclev_invoice_payment_links(
                self.company.id, [self.invoice.id]
            ),
            [],
        )
        providers = MagicMock()
        providers.ids = [31]
        providers.filtered.return_value = providers
        with (
            patch.object(type(self.invoice), "_has_to_be_paid", return_value=True),
            patch.object(
                type(self.env["payment.provider"]),
                "_get_compatible_providers",
                return_value=providers,
            ),
            patch.object(
                type(self.env["payment.method"]),
                "_get_compatible_payment_methods",
                return_value=MagicMock(),
            ),
        ):
            links = self.bridge.payclev_invoice_payment_links(
                self.company.id, [self.invoice.id]
            )
        self.assertEqual(len(links), 1)
        self.assertEqual(links[0]["invoice_id"], self.invoice.id)
        self.assertIn(f"/my/invoices/{self.invoice.id}?access_token=", links[0]["path"])
        self.assertIn("&payment=True#portal_pay", links[0]["path"])
        self.assertEqual(
            self.bridge.payclev_invoice_payment_links(self.company.id, [99999999]),
            [],
        )

    def test_database_enforces_company_payment_uuid_uniqueness(self):
        self.env.cr.execute(
            """
            SELECT pg_get_constraintdef(oid)
              FROM pg_constraint
             WHERE conrelid = 'payclev_payment_bridge'::regclass
               AND contype = 'u'
            """
        )
        definitions = [row[0] for row in self.env.cr.fetchall()]
        self.assertTrue(
            any("company_id" in item and "payment_uuid" in item for item in definitions)
        )

    def test_partial_payment_is_posted_applied_and_retry_is_duplicate_safe(self):
        request = self._request()
        first = self.bridge.payclev_apply_confirmed_payment(**request)
        self.assertEqual(first["invoice_id"], self.invoice.id)
        self.assertEqual(first["payment"]["amount_minor"], request["amount_minor"])
        self.assertEqual(first["payment"]["unapplied_amount_minor"], 0)
        self.assertEqual(len(first["payment"]["allocations"]), 1)
        allocation = first["payment"]["allocations"][0]
        self.assertEqual(allocation["invoice_id"], self.invoice.id)
        self.assertEqual(allocation["amount_minor"], request["amount_minor"])
        self.assertTrue(allocation["partial_reconcile_id"])
        self.assertEqual(
            first["invoice_residual_minor"],
            60 * 10**self.invoice.currency_id.decimal_places,
        )

        # Models the client losing the first HTTP response after Odoo commits.
        found = self.bridge.payclev_find_payment(
            **{
                key: request[key]
                for key in (
                    "company_id",
                    "payment_uuid",
                    "invoice_id",
                    "customer_id",
                    "amount_minor",
                    "currency",
                    "journal_id",
                    "payment_method_line_id",
                    "payment_date",
                )
            }
        )
        retried = self.bridge.payclev_apply_confirmed_payment(**request)
        self.assertEqual(found["payment"]["id"], first["payment"]["id"])
        self.assertEqual(retried["payment"]["id"], first["payment"]["id"])
        self.assertEqual(
            self.env["payclev.payment.bridge"]
            .sudo()
            .search_count(
                [
                    ("company_id", "=", self.company.id),
                    ("payment_uuid", "=", request["payment_uuid"]),
                ]
            ),
            1,
        )

    def test_same_uuid_with_changed_financial_or_journal_details_is_rejected(self):
        request = self._request()
        self.bridge.payclev_apply_confirmed_payment(**request)
        for changed in (
            {"amount_minor": request["amount_minor"] + 1},
            {"customer_id": self.partner_b.id},
            {"invoice_id": self.invoice.id + 999999},
            {"journal_id": self.journal.id + 999999},
            {"payment_date": "2020-01-01"},
        ):
            with self.subTest(changed=changed), self.assertRaises(UserError):
                self.bridge.payclev_apply_confirmed_payment(**(request | changed))

    def test_overpayment_rolls_back_identity_and_accounting_payment(self):
        request = self._request(
            amount_minor=101 * 10**self.invoice.currency_id.decimal_places
        )
        with self.assertRaises(UserError), self.env.cr.savepoint():
            self.bridge.payclev_apply_confirmed_payment(**request)
        self.assertFalse(
            self.env["payclev.payment.bridge"]
            .sudo()
            .search([("payment_uuid", "=", request["payment_uuid"])])
        )
        self.assertEqual(self.invoice.amount_residual, 100.0)

    def test_unconfigured_method_and_wrong_currency_are_rejected(self):
        request = self._request(currency="ZZZ")
        with self.assertRaises(UserError):
            self.bridge.payclev_apply_confirmed_payment(**request)
        method = self.method
        old_account = method.payment_account_id
        with self.env.cr.savepoint():
            method.payment_account_id = False
            with self.assertRaises(UserError):
                self.bridge.payclev_apply_confirmed_payment(**self._request())
            method.payment_account_id = old_account

    def test_journal_opt_in_is_required_and_manager_controlled(self):
        with self.assertRaises(AccessError):
            self.journal.with_user(self.bot).write({"payclev_bridge_enabled": False})
        self.journal.payclev_bridge_enabled = False
        self.assertFalse(self.bridge.payclev_payment_methods(self.company.id))
        with self.assertRaises(UserError):
            self.bridge.payclev_apply_confirmed_payment(**self._request())

    def test_managed_payment_option_owns_journal_and_can_be_paused(self):
        option = self.env["payclev.payment.option"].create({
            "company_id": self.company.id,
            "currency_id": self.company.currency_id.id,
            "outstanding_account_id": self.method.payment_account_id.id,
        })
        self.assertTrue(option.journal_id.payclev_managed)
        self.assertEqual(option.payment_method_line_id.name, "Payclev")
        methods = self.bridge.payclev_payment_methods(self.company.id)
        self.assertIn(option.journal_id.id, [row["journal_id"] for row in methods])
        managed = next(
            row for row in methods if row["journal_id"] == option.journal_id.id
        )
        self.assertEqual(managed["payclev_option_id"], option.id)
        with self.assertRaises(UserError):
            option.journal_id.write({"currency_id": False})
        with self.assertRaises(UserError):
            option.payment_method_line_id.write({"payment_account_id": False})
        request = self._request(
            journal_id=option.journal_id.id,
            payment_method_line_id=option.payment_method_line_id.id,
        )
        first = self.bridge.payclev_apply_confirmed_payment(**request)
        self.assertEqual(first["payment"]["amount_minor"], request["amount_minor"])
        option.active = False
        methods = self.bridge.payclev_payment_methods(self.company.id)
        self.assertNotIn(option.journal_id.id, [row["journal_id"] for row in methods])
        # Idempotent lookup remains available after pausing the option.
        self.assertEqual(
            self.bridge.payclev_apply_confirmed_payment(**request)["payment"]["id"],
            first["payment"]["id"],
        )

    def test_managed_option_registers_foreign_currency_invoice(self):
        currency = self.env.ref("base.USD")
        if currency == self.company.currency_id:
            currency = self.env.ref("base.EUR")
        currency.active = True
        option = self.env["payclev.payment.option"].create({
            "company_id": self.company.id,
            "currency_id": currency.id,
            "outstanding_account_id": self.method.payment_account_id.id,
        })
        self.invoice = self._create_invoice_one_line(
            price_unit=100.0, tax_ids=[], post=True, currency_id=currency.id
        )
        request = self._request(
            journal_id=option.journal_id.id,
            payment_method_line_id=option.payment_method_line_id.id,
        )
        result = self.bridge.payclev_apply_confirmed_payment(**request)
        self.assertEqual(result["payment"]["currency"], currency.name)
        self.assertEqual(result["payment"]["amount_minor"], request["amount_minor"])

    def test_paused_option_is_visible_and_cannot_be_created_again(self):
        option = self.env["payclev.payment.option"].create({
            "company_id": self.company.id,
            "currency_id": self.company.currency_id.id,
            "outstanding_account_id": self.method.payment_account_id.id,
        })
        option.active = False
        action = self.env.ref("payclev_bridge.action_payclev_payment_options")
        self.assertFalse(literal_eval(action.context)["active_test"])
        with self.assertRaisesRegex(ValidationError, self.company.currency_id.name):
            self.env["payclev.payment.option"].create({
                "company_id": self.company.id,
                "currency_id": self.company.currency_id.id,
                "outstanding_account_id": self.method.payment_account_id.id,
            })

    def test_management_screen_reports_verified_currencies_separately(self):
        connection_model = self.env["payclev.connection"]
        connection = connection_model.search([
            ("company_id", "=", self.company.id)
        ], limit=1)
        values = {
            "company_id": self.company.id,
            "state": "connected",
            "service_user_id": self.bot.id,
            "integration_id": "integration-test",
            "organization_id": "organization-test",
        }
        if connection:
            connection.write(values)
        else:
            connection = connection_model.create(values)
        self.assertFalse(connection.action_payment_options()["context"]["active_test"])
        result = self.bridge.payclev_report_payment_setup(
            company_id=self.company.id,
            integration_id="integration-test",
            organization_id="organization-test",
            payment_accounts=[
                {"id": f"{self.journal.id}:{self.method.id}",
                 "currency": self.company.currency_id.name, "ready": True},
                {"id": "unknown", "currency": "ZZZ", "ready": False},
            ],
        )
        self.assertEqual(result["payment_setup_status"], "journal_verified")
        self.assertEqual(
            connection.payment_setup_currencies, self.company.currency_id.name
        )
        self.assertEqual(connection.payment_setup_review_currencies, "ZZZ")

    def test_unauthorized_user_cannot_invoke_bridge_or_mutate_identity(self):
        user = self.simple_accountman  # Accounting rights, no PayClev group.
        with self.assertRaises(AccessError):
            self.env["payclev.payment.bridge"].with_user(user).payclev_health(
                self.company.id
            )
        with self.assertRaises(AccessError):
            self.bridge.create(
                {
                    "company_id": self.company.id,
                    "payment_uuid": str(uuid4()),
                    "invoice_id": self.invoice.id,
                    "partner_id": self.invoice.partner_id.id,
                    "currency_id": self.invoice.currency_id.id,
                    "amount_minor": "4000",
                    "journal_id": self.journal.id,
                    "payment_method_line_id": self.method.id,
                    "payment_date": fields.Date.today(),
                }
            )

    def test_other_company_is_not_accessible_to_bot(self):
        other = self.setup_other_company()["company"]
        with self.assertRaises(AccessError):
            self.bridge.payclev_health(other.id)
