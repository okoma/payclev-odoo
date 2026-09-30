"""Explicit, per-journal opt-in for PayClev confirmed-payment write-back."""

from odoo import _, api, fields, models
from odoo.exceptions import AccessError


class AccountJournal(models.Model):
    _inherit = "account.journal"

    payclev_bridge_enabled = fields.Boolean(
        string="Allow PayClev payment write-back",
        default=False,
        help=(
            "Allow the PayClev integration user to register confirmed external "
            "payments in this journal. Requires a manual incoming method with "
            "an outstanding receipts account."
        ),
    )

    @api.model_create_multi
    def create(self, vals_list):
        if any("payclev_bridge_enabled" in vals for vals in vals_list):
            self._check_payclev_configuration_access()
        return super().create(vals_list)

    def write(self, vals):
        if "payclev_bridge_enabled" in vals:
            self._check_payclev_configuration_access()
        return super().write(vals)

    def _check_payclev_configuration_access(self):
        if not self.env.user.has_group("account.group_account_manager"):
            raise AccessError(
                _("Only an accounting manager may configure PayClev journals.")
            )
