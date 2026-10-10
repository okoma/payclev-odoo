"""Odoo-owned configuration for Payclev's confirmed-payment write-back."""

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError, ValidationError


class PayclevPaymentOption(models.Model):
    _name = "payclev.payment.option"
    _description = "Payclev Payment Option"
    _rec_name = "currency_id"
    _check_company_auto = True
    _company_currency_unique = models.Constraint(
        "unique(company_id, currency_id)",
        "Configure only one Payclev payment option for each company and currency.",
    )

    company_id = fields.Many2one(
        "res.company", required=True, default=lambda self: self.env.company
    )
    currency_id = fields.Many2one("res.currency", required=True)
    outstanding_account_id = fields.Many2one(
        "account.account", required=True,
        string="Outstanding Receipts account",
        help="Choose a reconcilable current-asset account approved by your accountant. "
             "Payclev never creates a chart-of-accounts entry or chooses this for you.",
    )
    journal_id = fields.Many2one("account.journal", readonly=True, copy=False)
    payment_method_line_id = fields.Many2one(
        "account.payment.method.line", readonly=True, copy=False
    )
    active = fields.Boolean(default=True)

    @api.model_create_multi
    def create(self, vals_list):
        self._require_manager()
        for vals in vals_list:
            company_id = vals.get("company_id") or self.env.company.id
            currency_id = vals.get("currency_id")
            if not currency_id:
                continue
            existing = self.with_context(active_test=False).search([
                ("company_id", "=", company_id),
                ("currency_id", "=", currency_id),
            ], limit=1)
            if existing:
                raise ValidationError(_(
                    "A Payclev payment option already exists for %(currency)s in "
                    "%(company)s. Open the existing option; if it is paused, "
                    "resume it instead of creating another."
                ) % {
                    "currency": existing.currency_id.name,
                    "company": existing.company_id.name,
                })
        options = super().create(vals_list)
        for option in options:
            option._provision()
        return options

    def write(self, vals):
        self._require_manager()
        if set(vals) - {"active"}:
            raise UserError(_("A Payclev payment option is fixed after creation. "
                              "Pause it and contact your accounting administrator "
                              "before changing the setup."))
        return super().write(vals)

    def unlink(self):
        raise UserError(_("Payclev payment options cannot be deleted. Pause them instead."))

    def _require_manager(self):
        if not self.env.user.has_group("account.group_account_manager"):
            raise AccessError(_("Only an accounting manager may configure Payclev payments."))

    def _provision(self):
        self.ensure_one()
        if self.journal_id or self.payment_method_line_id:
            raise UserError(_("This Payclev payment option is already configured."))
        if self.company_id not in self.env.user.company_ids:
            raise AccessError(_("This Odoo company is not available to you."))
        account = self.outstanding_account_id
        if (
            self.company_id not in account.company_ids
            or not account.active
            or account.account_type != "asset_current"
            or not account.reconcile
            or account.currency_id and account.currency_id != self.currency_id
        ):
            raise ValidationError(_("Choose a reconcilable Outstanding Receipts "
                                    "current-asset account usable in this company and currency."))
        if not self.currency_id.active:
            raise ValidationError(_("Activate this currency in Odoo before adding it to Payclev."))
        journal_model = self.env["account.journal"].with_company(self.company_id)
        candidates = ["PC%s" % self.currency_id.name] + [
            "P%s%s" % (self.currency_id.name, index) for index in range(1, 10)
        ]
        code = next((candidate for candidate in candidates if not journal_model.search([
            ("company_id", "=", self.company_id.id), ("code", "=", candidate)
        ], limit=1)), None)
        if not code:
            raise UserError(_("No available Payclev journal code exists for this currency."))
        journal = journal_model.create({
            "name": "Payclev payments — %s" % self.currency_id.name,
            "code": code,
            "type": "bank",
            "company_id": self.company_id.id,
            "currency_id": self.currency_id.id,
            "payclev_bridge_enabled": True,
            "payclev_managed": True,
        })
        method = journal.inbound_payment_method_line_ids.filtered(
            lambda line: line.code == "manual"
        )[:1]
        if not method:
            method = self.env["account.payment.method.line"].create({
                "journal_id": journal.id,
                "payment_method_id": self.env.ref(
                    "account.account_payment_method_manual_in"
                ).id,
            })
        if journal.default_account_id == account:
            raise ValidationError(_("The Outstanding Receipts account must differ "
                                    "from the Payclev journal's bank account."))
        method.write({"payment_account_id": account.id, "name": "Payclev"})
        super().write({"journal_id": journal.id, "payment_method_line_id": method.id})


class PayclevPaymentMethodLine(models.Model):
    _inherit = "account.payment.method.line"

    def write(self, vals):
        if {"payment_account_id", "payment_method_id", "journal_id", "name"}.intersection(vals):
            if any(self.env["payclev.payment.option"].sudo().search_count([
                ("payment_method_line_id", "=", line.id)
            ]) for line in self):
                raise UserError(_("This payment method is managed through the Payclev payment option."))
        return super().write(vals)

    def unlink(self):
        if any(self.env["payclev.payment.option"].sudo().search_count([
            ("payment_method_line_id", "=", line.id)
        ]) for line in self):
            raise UserError(_("A Payclev-managed payment method cannot be deleted."))
        return super().unlink()
