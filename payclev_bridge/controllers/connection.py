"""One-use Odoo-side callback for Payclev administrator consent."""

import hashlib

from odoo import http
from odoo.exceptions import AccessError, UserError
from odoo.http import request


class PayclevConnectionController(http.Controller):
    @http.route("/payclev/connect/callback", type="http", auth="user", methods=["GET"])
    def callback(self, state=None, code=None, **_kwargs):
        if not request.env.user.has_group("base.group_system"):
            raise AccessError("Only an Odoo administrator can finish Payclev connection.")
        if not state or not code:
            raise UserError("The Payclev authorization response is incomplete.")
        connection = request.env["payclev.connection"].search([
            ("pairing_state_hash", "=", hashlib.sha256(state.encode()).hexdigest()),
            ("state", "=", "pending"),
            ("company_id", "in", request.env.user.company_ids.ids),
        ], limit=1)
        if not connection:
            raise UserError("This Payclev connection request is no longer available.")
        try:
            connection.complete_pairing(state=state, code=code)
        except UserError as error:
            connection.last_connection_error = str(error)
        return request.redirect("/web#id=%s&model=payclev.connection&view_type=form" % connection.id)
