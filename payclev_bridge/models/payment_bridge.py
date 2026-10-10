"""Small Odoo 19 bridge: one confirmed Payclev payment, one invoice, one transaction.

The API-key user needs ordinary Odoo invoice/payment permissions. Only the
idempotency record is accessed with sudo; accounting records never are.
"""

from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from uuid import UUID
import hashlib
import hmac
import secrets

from odoo import _, api, fields, models, release
from odoo.exceptions import AccessError, UserError

BRIDGE_VERSION = "1.2.0"


class PayClevPaymentBridge(models.Model):
    _name = "payclev.payment.bridge"
    _description = "Payclev confirmed payment identity"
    _rec_name = "payment_uuid"
    _check_company_auto = True

    company_id = fields.Many2one("res.company", required=True, index=True)
    payment_uuid = fields.Char(required=True, index=True)
    invoice_id = fields.Many2one(
        "account.move", required=True, check_company=True, ondelete="restrict"
    )
    partner_id = fields.Many2one("res.partner", required=True, check_company=True)
    currency_id = fields.Many2one("res.currency", required=True)
    # Odoo 19 Integer is PostgreSQL int4. Store decimal digits so large exact
    # minor-unit amounts cannot silently overflow or pass through a float.
    amount_minor = fields.Char(required=True)
    journal_id = fields.Many2one("account.journal", required=True, check_company=True)
    payment_method_line_id = fields.Many2one(
        "account.payment.method.line", required=True
    )
    payment_date = fields.Date(required=True)
    payment_id = fields.Many2one(
        "account.payment", check_company=True, ondelete="restrict"
    )

    _unique_company_payment = models.Constraint(
        "unique(company_id, payment_uuid)",
        "This Payclev payment was already registered in this company.",
    )

    @api.model
    def payclev_health(self, company_id):
        company = self._company(company_id)
        methods = self.payclev_payment_methods(company.id)
        return {
            "bridge_version": BRIDGE_VERSION,
            "odoo_version": release.version,
            "database_uuid": self.env["ir.config_parameter"]
            .sudo()
            .get_param("database.uuid"),
            "bot_user_id": self.env.user.id,
            "bot_login": self.env.user.login,
            "company_id": company.id,
            "company_name": company.name,
            "company_currency": company.currency_id.name,
            "payment_method_count": len(methods),
            "payment_writeback_ready": bool(methods),
        }

    def _paired_connection(self, company_id, integration_id, organization_id):
        company = self._company(company_id)
        connection = self.env["payclev.connection"].sudo().search([
            ("company_id", "=", company.id),
            ("service_user_id", "=", self.env.user.id),
            ("state", "=", "connected"),
            ("integration_id", "=", str(integration_id)),
            ("organization_id", "=", str(organization_id)),
        ], limit=1)
        if not connection:
            raise AccessError(_("This service key is not linked to the requested Payclev business and Odoo company."))
        return company, connection

    @api.model
    def payclev_connection_status(self, company_id, integration_id, organization_id):
        _, connection = self._paired_connection(company_id, integration_id, organization_id)
        return {
            "auto_rotate_key": bool(connection.auto_rotate_key),
            "key_expires_at": fields.Datetime.to_string(connection.key_expires_at) if connection.key_expires_at else None,
            "rotation_request_id": connection.rotation_request_id or None,
            "payment_setup_status": connection.payment_setup_status,
            "show_invoice_panel": bool(connection.show_invoice_panel),
            "show_invoice_activity": bool(connection.show_invoice_panel and connection.show_invoice_activity),
        }

    @api.model
    def payclev_notification_secret(self, company_id, integration_id, organization_id):
        """Provision the webhook secret only to this company's paired service user."""
        _, connection = self._paired_connection(company_id, integration_id, organization_id)
        if not connection.notification_secret:
            connection.notification_secret = secrets.token_urlsafe(48)
        return {"notification_secret": connection.notification_secret}

    @api.model
    def payclev_report_payment_setup(self, company_id, integration_id, organization_id,
                                     payment_account_id=None, currency=None, ready=False,
                                     payment_accounts=None):
        company, connection = self._paired_connection(company_id, integration_id, organization_id)
        methods = self.payclev_payment_methods(company.id)
        if payment_accounts is not None:
            if not isinstance(payment_accounts, list) or len(payment_accounts) > 50:
                raise UserError(_("Payclev sent an invalid payment-option list."))
            verified = []
            review = []
            seen = set()
            for entry in payment_accounts:
                if not isinstance(entry, dict):
                    raise UserError(_("Payclev sent an invalid payment option."))
                item_currency = entry.get("currency")
                if (
                    not isinstance(item_currency, str)
                    or len(item_currency) != 3
                    or item_currency in seen
                ):
                    raise UserError(_("Payclev sent an invalid or repeated currency."))
                seen.add(item_currency)
                if entry.get("ready") is True:
                    selected = next((item for item in methods if
                        f"{item['journal_id']}:{item['method_id']}" == entry.get("id")
                        and item["journal_currency"] == item_currency), None)
                    if not selected:
                        raise UserError(_("A selected Payclev payment option is no longer eligible in Odoo."))
                    verified.append((item_currency, selected["journal_name"]))
                else:
                    review.append(item_currency)
            status = "journal_verified" if verified else (
                "needs_review" if payment_accounts else "not_configured"
            )
            connection.sudo().write({
                "payment_setup_status": status,
                "payment_setup_journal": verified[0][1] if verified else False,
                "payment_setup_currency": verified[0][0] if verified else False,
                "payment_setup_currencies": ", ".join(
                    currency for currency, _ in sorted(verified)
                ) or False,
                "payment_setup_review_currencies": ", ".join(sorted(review)) or False,
                "payment_setup_reported_at": fields.Datetime.now(),
            })
            return {"payment_setup_status": status}
        if ready:
            selected = next((item for item in methods if
                f"{item['journal_id']}:{item['method_id']}" == payment_account_id
                and item["journal_currency"] == currency), None)
            if not selected:
                raise UserError(_("The selected payment journal is no longer eligible in Odoo."))
            status = "journal_verified"
            journal = selected["journal_name"]
        else:
            status = "needs_review" if payment_account_id else "not_configured"
            journal = False
        connection.write({
            "payment_setup_status": status,
            "payment_setup_journal": journal,
            "payment_setup_currency": currency if ready else False,
            "payment_setup_currencies": currency if ready else False,
            "payment_setup_review_currencies": currency if not ready and payment_account_id else False,
            "payment_setup_reported_at": fields.Datetime.now(),
        })
        return {"payment_setup_status": status}

    @api.model
    def payclev_begin_key_rotation(self, company_id, integration_id, organization_id, request_id):
        _, connection = self._paired_connection(company_id, integration_id, organization_id)
        try:
            request_id = str(UUID(str(request_id)))
        except (TypeError, ValueError) as error:
            raise UserError(_("The key rotation request is invalid.")) from error
        self.env.cr.execute("SELECT id FROM payclev_connection WHERE id = %s FOR UPDATE", [connection.id])
        connection.invalidate_recordset()
        now = fields.Datetime.now()
        if not connection.auto_rotate_key:
            raise AccessError(_("Automatic key renewal has not been enabled by the Odoo administrator."))
        if not connection.key_expires_at or not (now < connection.key_expires_at <= now + timedelta(days=14)):
            raise UserError(_("The service key is not in its renewal window. An expired key requires fresh pairing."))
        active_key = self.env["res.users.apikeys"].sudo().browse(connection.service_key_id).exists()
        if not active_key or active_key.user_id != connection.service_user_id or active_key.name != "Payclev Odoo bridge":
            raise AccessError(_("Odoo cannot identify the exact active service key. Reconnect before rotating."))
        if connection.rotation_key_id:
            if connection.rotation_key_id == connection.service_key_id:
                raise AccessError(_("The pending and active key identities overlap. No key was revoked."))
            pending_key = self.env["res.users.apikeys"].sudo().browse(connection.rotation_key_id).exists()
            if pending_key:
                if pending_key.user_id != connection.service_user_id or pending_key.name != "Payclev Odoo bridge":
                    raise AccessError(_("The pending key identity does not match this connection."))
                pending_key._remove()
        expiry = now + timedelta(days=60)
        key = self.env["res.users.apikeys"].with_user(connection.service_user_id).sudo()._generate(
            None, "Payclev Odoo bridge", expiry
        )
        self.env.cr.execute(
            "SELECT id FROM res_users_apikeys WHERE user_id = %s AND index = %s ORDER BY id DESC LIMIT 1",
            (connection.service_user_id.id, key[:8]),
        )
        key_id = self.env.cr.fetchone()[0]
        connection.write({
            "rotation_request_id": request_id,
            "rotation_key_id": key_id,
            "rotation_key_expires_at": expiry,
            "rotation_proof_hash": hashlib.sha256((request_id + key).encode()).hexdigest(),
        })
        return {"request_id": request_id, "api_key": key, "key_expires_at": fields.Datetime.to_string(expiry)}

    @api.model
    def payclev_finish_key_rotation(self, company_id, integration_id, organization_id,
                                    request_id, proof):
        _, connection = self._paired_connection(company_id, integration_id, organization_id)
        self.env.cr.execute("SELECT id FROM payclev_connection WHERE id = %s FOR UPDATE", [connection.id])
        connection.invalidate_recordset()
        if not connection.rotation_key_id or not hmac.compare_digest(
            str(connection.rotation_request_id or ""), str(request_id)
        ) or not hmac.compare_digest(str(connection.rotation_proof_hash or ""), str(proof)):
            raise AccessError(_("The replacement key could not be verified."))
        keys = self.env["res.users.apikeys"].sudo()
        replacement = keys.browse(connection.rotation_key_id).exists()
        prior = keys.browse(connection.service_key_id).exists()
        if connection.rotation_key_id == connection.service_key_id:
            raise AccessError(_("The replacement and prior keys have the same identity. No key was revoked."))
        if (not replacement or replacement.user_id != connection.service_user_id
                or replacement.name != "Payclev Odoo bridge"):
            raise UserError(_("The replacement key is missing or belongs to another connection."))
        if prior:
            if prior.user_id != connection.service_user_id or prior.name != "Payclev Odoo bridge":
                raise AccessError(_("The previous key belongs to another connection."))
            prior._remove()
        connection.write({
            "service_key_id": replacement.id,
            "key_expires_at": connection.rotation_key_expires_at,
            "last_rotated_at": fields.Datetime.now(),
            "rotation_request_id": False,
            "rotation_key_id": False,
            "rotation_key_expires_at": False,
            "rotation_proof_hash": False,
        })
        return {"key_expires_at": fields.Datetime.to_string(connection.key_expires_at)}

    @api.model
    def payclev_payment_methods(self, company_id):
        company = self._company(company_id)
        bridge = self.with_company(company)
        journals = bridge.env["account.journal"].search(
            [
                ("company_id", "=", company.id),
                ("type", "in", ["bank", "cash"]),
                ("payclev_bridge_enabled", "=", True),
            ]
        )
        result = []
        managed_options = {
            option.journal_id.id: option
            for option in bridge.env["payclev.payment.option"].sudo().search([
                ("company_id", "=", company.id),
            ])
        }
        for journal in journals:
            managed = managed_options.get(journal.id)
            if journal.payclev_managed and (
                not managed or not managed.active
            ):
                continue
            for method in journal.inbound_payment_method_line_ids:
                if (
                    method.code != "manual"
                    or not method.payment_account_id
                    or method.payment_account_id == journal.default_account_id
                    or managed and (
                        method != managed.payment_method_line_id
                        or method.payment_account_id != managed.outstanding_account_id
                    )
                ):
                    continue
                result.append(
                    {
                        "journal_id": journal.id,
                        "journal_name": journal.name,
                        "journal_currency": (
                            journal.currency_id or company.currency_id
                        ).name,
                        "method_id": method.id,
                        "method_name": method.name,
                        "outstanding_account_id": method.payment_account_id.id or None,
                        "payclev_option_id": managed.id if managed else None,
                    }
                )
        return result

    @api.model
    def payclev_invoice_payment_links(self, company_id, invoice_ids):
        """Return only Odoo-issued portal links for currently payable invoices.

        These links are not Payclev write-back options. Odoo owns checkout,
        transaction creation, and the resulting accounting payment.
        """
        company = self._company(company_id)
        if (
            not isinstance(invoice_ids, list)
            or len(invoice_ids) > 200
            or any(type(value) is not int or value <= 0 for value in invoice_ids)
        ):
            raise UserError(_("Choose up to 200 valid customer invoices."))
        invoices = self.env["account.move"].with_company(company).search([
            ("id", "in", invoice_ids),
            ("company_id", "=", company.id),
            ("move_type", "=", "out_invoice"),
        ])
        result = []
        for invoice in invoices:
            if (
                not invoice._has_to_be_paid()
                or invoice.partner_id.company_id
                and invoice.partner_id.company_id != company
            ):
                continue
            providers = self.env["payment.provider"].sudo().with_company(
                company
            )._get_compatible_providers(
                company.id,
                invoice.partner_id.id,
                invoice.amount_total,
                currency_id=invoice.currency_id.id,
            ).filtered("is_published")
            if not providers:
                continue
            methods = self.env["payment.method"].sudo().with_company(
                company
            )._get_compatible_payment_methods(
                providers.ids,
                invoice.partner_id.id,
                currency_id=invoice.currency_id.id,
            )
            if not methods:
                continue
            result.append({
                "invoice_id": invoice.id,
                "path": invoice.get_portal_url(
                    query_string="&payment=True", anchor="portal_pay"
                ),
            })
        return result

    @api.model
    def payclev_find_payment(
        self,
        company_id,
        payment_uuid,
        invoice_id,
        customer_id,
        amount_minor,
        currency,
        journal_id,
        payment_method_line_id,
        payment_date,
    ):
        company = self._company(company_id)
        bridge = self.with_company(company)
        key = self._uuid(payment_uuid)
        identity = bridge.sudo().search(
            [("company_id", "=", company.id), ("payment_uuid", "=", key)], limit=1
        )
        if not identity:
            return False
        bridge._verify_identity(
            identity,
            invoice_id,
            customer_id,
            amount_minor,
            currency,
            journal_id,
            payment_method_line_id,
            payment_date,
        )
        return bridge._snapshot(identity)

    @api.model
    def payclev_payment_snapshot(self, company_id, payment_id):
        company = self._company(company_id)
        bridge = self.with_company(company)
        payment = bridge.env["account.payment"].browse(int(payment_id)).exists()
        if not payment or payment.company_id != company:
            raise UserError(_("The payment is not in the selected company."))
        payment.check_access("read")
        return bridge._payment_data(payment)

    @api.model
    def payclev_apply_confirmed_payment(
        self,
        company_id,
        invoice_id,
        customer_id,
        payment_uuid,
        amount_minor,
        currency,
        journal_id,
        payment_method_line_id,
        payment_date,
    ):
        company = self._company(company_id)
        bridge = self.with_company(company)
        key = self._uuid(payment_uuid)
        if isinstance(amount_minor, bool) or not isinstance(amount_minor, int):
            raise UserError(_("The payment amount must be exact minor units."))
        if amount_minor <= 0:
            raise UserError(_("The payment amount must be positive."))
        if not isinstance(currency, str) or len(currency) != 3:
            raise UserError(_("The payment currency is invalid."))
        try:
            payment_day = date.fromisoformat(payment_date)
        except (TypeError, ValueError) as error:
            raise UserError(_("The payment date is invalid.")) from error
        if payment_day > fields.Date.context_today(bridge):
            raise UserError(_("The payment date cannot be in the future."))

        # JSON-2 wraps this method in one Odoo transaction. The transaction-
        # scoped UUID lock serializes retries, including a retry after a lost
        # HTTP response; the SQL constraint remains the final uniqueness guard.
        bridge.env.cr.execute(
            "SELECT pg_advisory_xact_lock(%s, %s)",
            [company.id, int.from_bytes(UUID(key).bytes[:4], "big", signed=True)],
        )
        existing = bridge.sudo().search(
            [("company_id", "=", company.id), ("payment_uuid", "=", key)], limit=1
        )
        if existing:
            bridge._verify_identity(
                existing,
                invoice_id,
                customer_id,
                amount_minor,
                currency,
                journal_id,
                payment_method_line_id,
                payment_day,
            )
            return bridge._snapshot(existing)

        invoice = bridge.env["account.move"].browse(int(invoice_id)).exists()
        if not invoice or invoice.company_id != company:
            raise UserError(_("The invoice is not in the selected company."))
        invoice.check_access("read")
        if invoice.move_type != "out_invoice" or invoice.state != "posted":
            raise UserError(_("Only posted customer invoices can receive payment."))
        if invoice.partner_id.id != int(customer_id):
            raise UserError(_("The invoice customer does not match the payment."))
        if invoice.currency_id.name != currency.upper():
            raise UserError(_("The invoice and payment currencies differ."))
        # The payment is in the invoice currency. Odoo owns its company-currency
        # conversion and any exchange-difference entry; Payclev never does FX.
        journal = bridge.env["account.journal"].browse(int(journal_id)).exists()
        method = (
            bridge.env["account.payment.method.line"]
            .browse(int(payment_method_line_id))
            .exists()
        )
        managed = bridge.env["payclev.payment.option"].sudo().search([
            ("journal_id", "=", journal.id), ("company_id", "=", company.id)
        ], limit=1)
        if (
            not journal
            or journal.company_id != company
            or journal.type not in ("bank", "cash")
            or not journal.payclev_bridge_enabled
            or (journal.currency_id or company.currency_id) != invoice.currency_id
            or journal.payclev_managed and (
                not managed
                or not managed.active
                or method != managed.payment_method_line_id
                or method.payment_account_id != managed.outstanding_account_id
            )
            or not method
            or method not in journal.inbound_payment_method_line_ids
            or method.code != "manual"
            or not method.payment_account_id
            or method.payment_account_id == journal.default_account_id
        ):
            raise UserError(
                _(
                    "Configure an eligible incoming journal, payment method, and outstanding receipts account."
                )
            )
        journal.check_access("read")
        method.check_access("read")
        # Different PayClev UUIDs racing on the same invoice must not both spend
        # an old residual. The row lock is released with this JSON-2 transaction.
        bridge.env.cr.execute(
            "SELECT id FROM account_move WHERE id = %s FOR UPDATE", [invoice.id]
        )
        # Claim before checking residual. An exception anywhere below rolls
        # back the identity and all accounting entries in the same transaction.
        identity = bridge.sudo().create(
            {
                "company_id": company.id,
                "payment_uuid": key,
                "invoice_id": invoice.id,
                "partner_id": invoice.partner_id.id,
                "currency_id": invoice.currency_id.id,
                "amount_minor": str(amount_minor),
                "journal_id": journal.id,
                "payment_method_line_id": method.id,
                "payment_date": payment_day,
            }
        )

        invoice.invalidate_recordset(["amount_residual", "payment_state"])
        before_minor = bridge._minor(invoice.amount_residual, invoice.currency_id)
        if before_minor < amount_minor:
            raise UserError(_("The payment exceeds the current Odoo invoice residual."))

        exponent = invoice.currency_id.decimal_places
        major = Decimal(amount_minor) / (Decimal(10) ** exponent)
        # Odoo's Monetary ORM boundary accepts a float. All PayClev validation
        # and comparisons remain exact Decimal/integer minor units. Fail closed
        # if this amount cannot survive that boundary at currency precision.
        if Decimal(str(float(major))) != major:
            raise UserError(_("The payment exceeds Odoo's exact-money precision."))
        wizard = (
            bridge.env["account.payment.register"]
            .with_company(company)
            .with_context(active_model="account.move", active_ids=[invoice.id])
            .create(
                {
                    "journal_id": journal.id,
                    "currency_id": invoice.currency_id.id,
                    "payment_method_line_id": method.id,
                    "payment_date": payment_day.isoformat(),
                    "amount": float(major),
                    "payment_difference_handling": "open",
                    "communication": "Payclev " + key,
                }
            )
        )
        payment = wizard._create_payments()
        if len(payment) != 1 or payment.company_id != company:
            raise UserError(_("Odoo did not create the expected company payment."))
        payment.check_access("read")
        invoice.invalidate_recordset(["amount_residual", "payment_state"])
        after_minor = bridge._minor(invoice.amount_residual, invoice.currency_id)
        if before_minor - after_minor != amount_minor:
            raise UserError(_("Odoo did not apply the exact payment to the invoice."))
        payment_data = bridge._payment_data(payment)
        if (
            payment_data["company_id"] != company.id
            or payment_data["customer_id"] != invoice.partner_id.id
            or payment_data["currency"] != currency.upper()
            or payment_data["amount_minor"] != amount_minor
            or payment_data["state"] not in ("in_process", "paid")
        ):
            raise UserError(
                _("Odoo created a payment that does not match the request.")
            )
        allocations = payment_data["allocations"]
        if (
            len(allocations) != 1
            or allocations[0]["invoice_id"] != invoice.id
            or allocations[0]["amount_minor"] != amount_minor
        ):
            raise UserError(_("Odoo payment allocation did not match the invoice."))
        identity.payment_id = payment.id
        return bridge._snapshot(identity)

    def _company(self, company_id):
        if not (
            self.env.user.has_group("payclev_bridge.group_payclev_bridge")
            and self.env.user.has_group("account.group_account_invoice")
        ):
            raise AccessError(
                _("Payclev bridge and accounting permissions are required.")
            )
        company = self.env["res.company"].browse(int(company_id)).exists()
        if not company or company not in self.env.user.company_ids:
            raise AccessError(_("The selected company is not available."))
        company.check_access("read")
        return company

    @staticmethod
    def _uuid(value):
        try:
            return str(UUID(str(value)))
        except (TypeError, ValueError) as error:
            raise UserError(_("The Payclev payment UUID is invalid.")) from error

    @staticmethod
    def _minor(value, currency):
        try:
            scaled = Decimal(str(value)) * (Decimal(10) ** currency.decimal_places)
        except (InvalidOperation, TypeError) as error:
            raise UserError(_("Odoo returned an invalid monetary value.")) from error
        rounded = scaled.to_integral_value()
        if abs(scaled - rounded) > Decimal("0.00001"):
            raise UserError(_("Odoo returned a payment outside currency precision."))
        return int(rounded)

    def _verify_identity(
        self,
        identity,
        invoice_id,
        customer_id,
        amount_minor,
        currency,
        journal_id,
        payment_method_line_id,
        payment_date,
    ):
        try:
            expected_day = (
                date.fromisoformat(payment_date)
                if isinstance(payment_date, str)
                else payment_date
            )
        except ValueError as error:
            raise UserError(_("The payment date is invalid.")) from error
        if (
            identity.invoice_id.id != int(invoice_id)
            or identity.partner_id.id != int(customer_id)
            or int(identity.amount_minor) != amount_minor
            or identity.currency_id.name != currency.upper()
            or identity.journal_id.id != int(journal_id)
            or identity.payment_method_line_id.id != int(payment_method_line_id)
            or identity.payment_date != expected_day
        ):
            raise UserError(_("This Payclev payment UUID belongs to another payment."))
        if not identity.payment_id:
            raise UserError(_("The Payclev payment identity has no committed result."))

    def _snapshot(self, identity):
        identity.invoice_id.with_user(self.env.user).check_access("read")
        payment = identity.payment_id.with_user(self.env.user)
        payment.check_access("read")
        return {
            "payment_uuid": identity.payment_uuid,
            "payment": self._payment_data(payment),
            "invoice_id": identity.invoice_id.id,
            "invoice_residual_minor": self._minor(
                identity.invoice_id.amount_residual, identity.currency_id
            ),
            "invoice_payment_state": identity.invoice_id.payment_state,
        }

    def _payment_data(self, payment):
        currency = payment.currency_id
        allocations = []
        applied_payment_minor = 0
        for line in payment.move_id.line_ids.filtered(
            lambda item: item.account_type == "asset_receivable"
        ):
            if line.currency_id != currency:
                raise UserError(
                    _("The Odoo payment receivable currency is unexpected.")
                )
            for partial in line.matched_debit_ids | line.matched_credit_ids:
                payment_side_amount = (
                    partial.debit_amount_currency
                    if line == partial.debit_move_id
                    else partial.credit_amount_currency
                )
                applied_payment_minor += self._minor(payment_side_amount, currency)
                other = (
                    partial.debit_move_id
                    if partial.credit_move_id == line
                    else partial.credit_move_id
                )
                invoice = other.move_id
                if (
                    invoice.move_type != "out_invoice"
                    or invoice.company_id != payment.company_id
                ):
                    continue
                amount = (
                    partial.debit_amount_currency
                    if other == partial.debit_move_id
                    else partial.credit_amount_currency
                )
                allocations.append(
                    {
                        "invoice_id": invoice.id,
                        "amount_minor": self._minor(amount, invoice.currency_id),
                        "partial_reconcile_id": partial.id,
                    }
                )
        amount_minor = self._minor(payment.amount, currency)
        if applied_payment_minor > amount_minor:
            raise UserError(_("Odoo payment allocations exceed the payment amount."))
        return {
            "id": payment.id,
            "company_id": payment.company_id.id,
            "customer_id": payment.partner_id.id,
            "journal_id": payment.journal_id.id,
            "payment_method_line_id": payment.payment_method_line_id.id,
            "currency": currency.name,
            "amount_minor": amount_minor,
            "unapplied_amount_minor": amount_minor - applied_payment_minor,
            "date": payment.date.isoformat(),
            "state": payment.state,
            "memo": payment.memo,
            "write_date": payment.write_date.isoformat()
            if payment.write_date
            else None,
            "allocations": allocations,
        }
