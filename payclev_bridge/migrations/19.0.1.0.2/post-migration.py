"""Record the exact bridge key for previously paired companies.

An ambiguous key is deliberately left unresolved; it must not be guessed or
revoked during an upgrade.
"""

from odoo import api, SUPERUSER_ID


def migrate(cr, version):
    env = api.Environment(cr, SUPERUSER_ID, {})
    connections = env["payclev.connection"].search([
        ("state", "=", "connected"),
        ("service_user_id", "!=", False),
        ("service_key_id", "=", False),
    ])
    keys = env["res.users.apikeys"].sudo()
    for connection in connections:
        candidates = keys.search([
            ("user_id", "=", connection.service_user_id.id),
            ("name", "=", "Payclev Odoo bridge"),
        ])
        candidates = candidates.filtered(
            lambda key: key.expiration_date == connection.key_expires_at
        )
        if len(candidates) == 1:
            connection.service_key_id = candidates.id
