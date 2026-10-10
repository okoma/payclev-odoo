"""Explicit, per-journal opt-in for Payclev confirmed-payment write-back."""

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError


class AccountJournal(models.Model):
    _inherit = "account.journal"

    payclev_bridge_enabled = fields.Boolean(
        string="Allow Payclev payment write-back",
        default=False,
        help=(
            "Allow the Payclev integration user to register confirmed external "
            "payments in this journal. Requires a manual incoming method with "
            "an outstanding receipts account."
        ),
    )
    payclev_managed = fields.Boolean(
        string="Managed by Payclev add-on", default=False, readonly=True, copy=False
    )

    @api.model_create_multi
    def create(self, vals_list):
        if any("payclev_bridge_enabled" in vals for vals in vals_list):
            self._check_payclev_configuration_access()
        return super().create(vals_list)

    def write(self, vals):
        if "payclev_bridge_enabled" in vals:
            self._check_payclev_configuration_access()
        protected = {
            "name", "code", "type", "company_id", "currency_id",
            "default_account_id", "inbound_payment_method_line_ids",
            "payclev_bridge_enabled", "payclev_managed",
        }
        if protected.intersection(vals) and any(
            self.env["payclev.payment.option"].sudo().search_count([
                ("journal_id", "=", journal.id)
            ]) for journal in self
        ):
            raise UserError(_("This journal is managed through the Payclev payment option. "
                              "Pause that option rather than editing its accounting setup."))
        return super().write(vals)

    def unlink(self):
        if any(self.env["payclev.payment.option"].sudo().search_count([
            ("journal_id", "=", journal.id)
        ]) for journal in self):
            raise UserError(_("A Payclev-managed journal cannot be deleted."))
        return super().unlink()

    def _check_payclev_configuration_access(self):
        if not self.env.user.has_group("account.group_account_manager"):
            raise AccessError(
                _("Only an accounting manager may configure Payclev journals.")
            )
