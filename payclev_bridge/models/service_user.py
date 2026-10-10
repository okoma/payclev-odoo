"""Keep API-only bridge accounts out of Odoo's human invitation lifecycle."""

import logging

from dateutil.relativedelta import relativedelta

from odoo import fields, models
from odoo.fields import Domain


_logger = logging.getLogger(__name__)


class ResUsers(models.Model):
    _inherit = "res.users"

    payclev_service_user = fields.Boolean(
        string="Payclev service account", copy=False, readonly=True, default=False,
        help="API-only account managed by the Payclev Odoo bridge; it is not a human invitation.",
    )
    payclev_connection_ids = fields.One2many(
        "payclev.connection", "service_user_id", string="Payclev connections", readonly=True,
    )
    state = fields.Selection(selection_add=[("service", "Service account")])

    def _compute_state(self):
        super()._compute_state()
        for user in self:
            if user.payclev_service_user or user.payclev_connection_ids:
                user.state = "service"

    def _search_state(self, operator, value):
        if operator != "in":
            return NotImplemented
        selected = set(value)
        service = Domain("payclev_service_user", "=", True) | Domain(
            "payclev_connection_ids", "!=", False
        )
        result = Domain.FALSE
        if "service" in selected:
            result |= service
        if "active" in selected:
            result |= (~service & Domain("log_ids", "!=", False))
        if "new" in selected:
            result |= (~service & Domain("log_ids", "=", False))
        return result

    def send_unregistered_user_reminder(self, *, after_days=5, batch_size=100):
        """Odoo 19 auth_signup reminder, excluding linked/API-only Payclev users."""
        template = self.env.ref(
            "auth_signup.mail_template_data_unregistered_users", raise_if_not_found=False
        )
        if not template:
            _logger.warning("Odoo unregistered-user reminder template is unavailable")
            self.env["ir.cron"]._commit_progress(deactivate=True)
            return
        datetime_min = fields.Datetime.today() - relativedelta(days=after_days)
        datetime_max = datetime_min + relativedelta(days=1)
        invited_by_users = self.search_fetch([
            ("share", "=", False),
            ("create_uid.email", "!=", False),
            ("create_date", ">=", datetime_min),
            ("create_date", "<", datetime_max),
            ("log_ids", "=", False),
            ("payclev_service_user", "=", False),
            ("payclev_connection_ids", "=", False),
        ], ["name", "login", "create_uid"]).grouped("create_uid")

        for creator, invited_users in invited_by_users.items():
            names = [f"{user.name} ({user.login})" for user in invited_users]
            template.with_context(
                dbname=self.env.cr.dbname, invited_users=names
            ).send_mail(
                creator.id, email_layout_xmlid="mail.mail_notification_light", force_send=False
            )
            if not self.env["ir.cron"]._commit_progress(len(invited_users)):
                _logger.info("send_unregistered_user_reminder: timeout reached, stopping")
                break
