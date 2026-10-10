"""Normalize paired accounts without rotating or revoking their API keys."""

from odoo import SUPERUSER_ID, api


def migrate(cr, version):
    env = api.Environment(cr, SUPERUSER_ID, {})
    connections = env["payclev.connection"].sudo().search([
        ("service_user_id", "!=", False),
    ])
    users = connections.mapped("service_user_id")
    if not users:
        return
    users.write({"payclev_service_user": True})
    users.mapped("partner_id").signup_cancel()
    # Older bridge accounts had a random browser password. The API key used by
    # JSON-2 is stored separately and remains valid after this change.
    cr.execute("UPDATE res_users SET password = NULL WHERE id = ANY(%s)", (users.ids,))
    users.invalidate_recordset(["password"])
