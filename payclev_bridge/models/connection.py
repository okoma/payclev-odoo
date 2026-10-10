"""Administrator-facing Payclev connection managed inside Odoo."""

import base64
import datetime
import hashlib
import secrets
from urllib.parse import urlencode, urlsplit

import requests

from odoo import api, fields, models, _
from odoo.exceptions import AccessError, UserError, ValidationError


class PayclevConnection(models.Model):
    _name = "payclev.connection"
    _description = "Payclev Connection"
    _rec_name = "company_id"
    _company_unique = models.Constraint(
        "unique (company_id)", "Only one Payclev connection is allowed per company."
    )

    company_id = fields.Many2one("res.company", required=True, index=True, ondelete="cascade")
    state = fields.Selection(
        [("not_connected", "Not connected"), ("pending", "Waiting for approval"), ("connected", "Connected")],
        default="not_connected", required=True, readonly=True,
    )
    payclev_business = fields.Char(readonly=True)
    integration_id = fields.Char(readonly=True)
    organization_id = fields.Char(readonly=True)
    notification_secret = fields.Char(copy=False, readonly=True, groups="base.group_system")
    key_expires_at = fields.Datetime(readonly=True)
    last_connected_at = fields.Datetime(readonly=True)
    last_connection_error = fields.Text(readonly=True)
    auto_rotate_key = fields.Boolean(
        string="Renew service key automatically",
        default=False,
        help="Recommended: Payclev verifies a replacement service key before the current one expires, then revokes the old key after the new connection is active. This helps keep invoice sync and checkout available.",
    )
    show_invoice_panel = fields.Boolean(
        string="Show Payclev on invoices", default=False,
        help="Optional read-only collection status on customer invoices. Payclev does not change Odoo balances through this panel.",
    )
    show_invoice_activity = fields.Boolean(
        string="Show Payclev activity timeline", default=False,
        help="Optional recent collection activity on customer invoices. Enable the invoice panel first.",
    )
    last_rotated_at = fields.Datetime(readonly=True)
    rotation_request_id = fields.Char(copy=False, readonly=True)
    rotation_key_id = fields.Integer(copy=False, readonly=True)
    rotation_key_expires_at = fields.Datetime(copy=False, readonly=True)
    rotation_proof_hash = fields.Char(copy=False, readonly=True)
    payment_setup_status = fields.Selection(
        [("not_configured", "Not configured"), ("journal_verified", "Journal verified"),
         ("needs_review", "Needs review")],
        default="not_configured", readonly=True,
    )
    payment_setup_journal = fields.Char(readonly=True)
    payment_setup_currency = fields.Char(readonly=True)
    payment_setup_currencies = fields.Char(
        string="Verified currencies", readonly=True
    )
    payment_setup_review_currencies = fields.Char(
        string="Currencies needing review", readonly=True
    )
    payment_setup_reported_at = fields.Datetime(readonly=True)
    payclev_app_url = fields.Char(default="https://app.payclev.com", required=True)
    payclev_api_url = fields.Char(default="https://api.payclev.com", required=True)
    deployment_mode = fields.Selection(
        [("self_hosted", "Self-hosted"), ("odoo_sh", "Odoo.sh")],
        default="self_hosted", required=True,
    )
    allow_local_testing = fields.Boolean(default=False, help="Only for an Odoo instance running on localhost during development.")
    pairing_state_hash = fields.Char(copy=False, readonly=True)
    pairing_verifier = fields.Char(copy=False, readonly=True)
    pairing_expires_at = fields.Datetime(copy=False, readonly=True)
    service_user_id = fields.Many2one("res.users", readonly=True, copy=False)
    service_key_id = fields.Integer(readonly=True, copy=False)
    opted_in_journal_count = fields.Integer(compute="_compute_opted_in_journal_count", string="Opted-in payment journals")

    def _compute_opted_in_journal_count(self):
        for connection in self:
            connection.opted_in_journal_count = self.env["account.journal"].search_count([
                ("company_id", "=", connection.company_id.id),
                ("type", "in", ["bank", "cash"]),
                ("payclev_bridge_enabled", "=", True),
            ])

    def action_payment_journals(self):
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "name": _("Payclev payment journals"),
            "res_model": "account.journal",
            "view_mode": "list,form",
            "domain": [("company_id", "=", self.company_id.id), ("type", "in", ["bank", "cash"])],
            "target": "current",
        }

    def action_payment_options(self):
        self.ensure_one()
        if not self.env.user.has_group("account.group_account_manager"):
            raise AccessError(_("Only an accounting manager may configure Payclev payments."))
        return {
            "type": "ir.actions.act_window",
            "name": _("Payclev payment options"),
            "res_model": "payclev.payment.option",
            "view_mode": "list,form",
            "domain": [("company_id", "=", self.company_id.id)],
            "context": {
                "default_company_id": self.company_id.id,
                "active_test": False,
            },
            "target": "current",
        }

    def action_manage_in_payclev(self):
        self.ensure_one()
        if self.state != "connected" or not self.organization_id:
            raise UserError(_("Connect this Odoo company first."))
        app_url = self._payclev_origin(self.payclev_app_url, production_host="app.payclev.com", allow_local=self.allow_local_testing)
        return {
            "type": "ir.actions.act_url",
            "url": "%s/integrations?organization_id=%s&setup=odoo" % (app_url, self.organization_id),
            "target": "new",
        }

    @api.model
    def action_open_app(self):
        if not self.env.user.has_group("base.group_system"):
            raise AccessError(_("Only an Odoo administrator can manage Payclev."))
        company = self.env.company
        connection = self.search([("company_id", "=", company.id)], limit=1)
        if not connection:
            connection = self.create({"company_id": company.id})
        return {
            "type": "ir.actions.act_window",
            "name": _("Payclev"),
            "res_model": self._name,
            "res_id": connection.id,
            "view_mode": "form",
            "target": "current",
        }

    @staticmethod
    def _validated_origin(value, *, allow_local=False, label="Odoo base URL"):
        parsed = urlsplit((value or "").strip())
        if (
            not parsed.hostname or parsed.username or parsed.password
            or parsed.path not in ("", "/") or parsed.query or parsed.fragment
            or parsed.scheme not in ({"https", "http"} if allow_local else {"https"})
            or (parsed.scheme == "http" and parsed.hostname not in {"localhost", "127.0.0.1"}
                and not parsed.hostname.endswith(".localhost"))
        ):
            raise ValidationError(_("%s must be a bare HTTPS origin. HTTP is allowed only when local testing is enabled on a localhost Odoo server.") % label)
        return f"{parsed.scheme}://{parsed.netloc.lower()}"

    @classmethod
    def _payclev_origin(cls, value, *, production_host, allow_local=False):
        label = "Payclev app URL" if production_host == "app.payclev.com" else "Payclev API URL"
        origin = cls._validated_origin(value, allow_local=allow_local, label=label)
        host = urlsplit(origin).hostname
        local_hosts = {"localhost", "127.0.0.1"}
        if production_host == "app.payclev.com":
            local_hosts.add("app.localhost")
        if host != production_host and not (allow_local and host in local_hosts):
            raise ValidationError(_("%s must use the official Payclev host, or localhost for explicit local testing.") % label)
        return origin

    def _pairing_status(self, *, api_url, state):
        """Resolve an uncertain previous exchange before minting another API key."""
        try:
            response = requests.post(
                f"{api_url}/public/v1/odoo/pairing/status",
                json={"state": state, "code_verifier": self.pairing_verifier},
                timeout=10,
                allow_redirects=False,
            )
            if response.status_code != 200:
                raise UserError(_("Payclev could not check this connection request. Try again later."))
            return response.json().get("data")
        except (requests.RequestException, ValueError) as error:
            raise UserError(_("Payclev could not check this connection request. Try again later.")) from error

    @staticmethod
    def _completion_error(response):
        try:
            body = response.json()
        except ValueError:
            body = None
        error = body.get("error") if isinstance(body, dict) else None
        code = error.get("code") if isinstance(error, dict) else None
        if code == "entitlement_required":
            return _("This Payclev workspace has no active plan with accounting integrations. Activate a plan in Payclev, then start a new connection here.")
        if code == "integration_limit_reached":
            return _("This Payclev workspace has reached its accounting integration limit. Review its plan in Payclev, then start a new connection here.")
        if code in {"odoo_pairing_expired", "odoo_pairing_replayed"}:
            return _("This connection request expired or was already used. Start a new connection from this Odoo app.")
        return _("Payclev could not verify this Odoo connection (HTTP %s). Start a new connection here or contact support.") % response.status_code

    def _finish_pairing(self, result, *, expiry):
        self.write({
            "state": "connected",
            "integration_id": result["integration_id"],
            "organization_id": result["organization_id"],
            "payclev_business": result["organization_name"],
            "key_expires_at": expiry,
            "last_connected_at": fields.Datetime.now(),
            "last_connection_error": False,
            "pairing_state_hash": False,
            "pairing_verifier": False,
            "pairing_expires_at": False,
        })
        return result

    def action_connect(self):
        self.ensure_one()
        if not self.env.user.has_group("base.group_system"):
            raise AccessError(_("Only an Odoo administrator can connect Payclev."))
        if self.state == "connected":
            raise UserError(_("Disconnect this integration in Payclev, then reset the connection here before reconnecting."))
        web_base_url = self.env["ir.config_parameter"].sudo().get_param("web.base.url")
        local = self.allow_local_testing and urlsplit(web_base_url).hostname in {"localhost", "127.0.0.1"}
        app_url = self._payclev_origin(self.payclev_app_url, production_host="app.payclev.com", allow_local=local)
        self._payclev_origin(self.payclev_api_url, production_host="api.payclev.com", allow_local=local)
        base_url = self._validated_origin(web_base_url, allow_local=local)
        if self.company_id not in self.env.user.company_ids:
            raise AccessError(_("You do not have access to this Odoo company."))
        state = secrets.token_urlsafe(32)
        verifier = secrets.token_urlsafe(48)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        self.write({
            "state": "pending",
            "last_connection_error": False,
            "pairing_state_hash": hashlib.sha256(state.encode()).hexdigest(),
            "pairing_verifier": verifier,
            "pairing_expires_at": fields.Datetime.now() + datetime.timedelta(minutes=10),
        })
        database_uuid = self.env["ir.config_parameter"].sudo().get_param("database.uuid")
        if not database_uuid:
            raise UserError(_("Odoo database identity is unavailable."))
        params = urlencode({
            "base_url": base_url,
            "database_name": self.env.cr.dbname,
            "deployment_mode": self.deployment_mode,
            "database_uuid": database_uuid,
            "company_id": self.company_id.id,
            "company_name": self.company_id.name,
            "state": state,
            "code_challenge": challenge,
        })
        return {"type": "ir.actions.act_url", "url": f"{app_url}/integrations/odoo/authorize?{params}", "target": "self"}

    def action_reset_connection(self):
        self.ensure_one()
        if not self.env.user.has_group("base.group_system"):
            raise AccessError(_("Only an Odoo administrator can reset Payclev."))
        if self.state != "connected":
            raise UserError(_("Only a connected Payclev integration can be reset."))
        # The Odoo form confirms that the Payclev-side connection was disconnected
        # first. Revoke only this company's dedicated bridge credentials.
        if self.service_user_id:
            key_ids = [key_id for key_id in (self.service_key_id, self.rotation_key_id) if key_id]
            if not self.service_key_id:
                raise UserError(_("Odoo cannot identify the exact active Payclev service key. Contact support before resetting this connection."))
            for key_id in key_ids:
                key = self.env["res.users.apikeys"].sudo().browse(key_id).exists()
                if key:
                    if key.user_id != self.service_user_id or key.name != "Payclev Odoo bridge":
                        raise AccessError(_("The stored service key identity does not match this connection. No key was revoked."))
                    key._remove()
        self.write({
            "state": "not_connected",
            "payclev_business": False,
            "integration_id": False,
            "organization_id": False,
            "notification_secret": False,
            "key_expires_at": False,
            "last_connected_at": False,
            "service_key_id": False,
            "last_connection_error": False,
            "pairing_state_hash": False,
            "pairing_verifier": False,
            "pairing_expires_at": False,
            "auto_rotate_key": False,
            "show_invoice_panel": False,
            "show_invoice_activity": False,
            "last_rotated_at": False,
            "rotation_request_id": False,
            "rotation_key_id": False,
            "rotation_key_expires_at": False,
            "rotation_proof_hash": False,
            "payment_setup_status": "not_configured",
            "payment_setup_journal": False,
            "payment_setup_currency": False,
            "payment_setup_currencies": False,
            "payment_setup_review_currencies": False,
            "payment_setup_reported_at": False,
        })
        return True

    def _service_user(self):
        self.ensure_one()
        if self.service_user_id:
            return self.service_user_id
        database_uuid = self.env["ir.config_parameter"].sudo().get_param("database.uuid")
        user = self.env["res.users"].sudo().with_context(no_reset_password=True).create({
            "name": "Payclev service user — %s" % self.company_id.name,
            "login": "payclev-%s-%s@integration.invalid" % (database_uuid, self.company_id.id),
            "payclev_service_user": True,
            "company_id": self.company_id.id,
            "company_ids": [(6, 0, [self.company_id.id])],
            "group_ids": [(6, 0, [
                self.env.ref("base.group_user").id,
                self.env.ref("account.group_account_invoice").id,
                self.env.ref("payclev_bridge.group_payclev_bridge").id,
            ])],
        })
        self.service_user_id = user
        return user

    def complete_pairing(self, *, state, code):
        self.ensure_one()
        if not self.env.user.has_group("base.group_system"):
            raise AccessError(_("Only an Odoo administrator can finish Payclev connection."))
        if (
            self.state != "pending" or not self.pairing_state_hash
            or self.pairing_expires_at < fields.Datetime.now()
            or not secrets.compare_digest(hashlib.sha256(state.encode()).hexdigest(), self.pairing_state_hash)
        ):
            raise UserError(_("This connection request expired. Start again from Payclev in Odoo."))
        web_base_url = self.env["ir.config_parameter"].sudo().get_param("web.base.url")
        local = self.allow_local_testing and urlsplit(web_base_url).hostname in {"localhost", "127.0.0.1"}
        api_url = self._payclev_origin(self.payclev_api_url, production_host="api.payclev.com", allow_local=local)
        previous_result = self._pairing_status(api_url=api_url, state=state)
        if previous_result:
            if not self.key_expires_at:
                raise UserError(_("Payclev connected, but Odoo cannot confirm the service key expiry. Contact support before reconnecting."))
            return self._finish_pairing(previous_result, expiry=self.key_expires_at)
        if self.key_expires_at:
            raise UserError(_("Payclev is still checking this connection. Retry this return link later; Odoo will not create a second service key."))
        bot = self._service_user()
        notification_secret = self.notification_secret or secrets.token_urlsafe(48)
        self.notification_secret = notification_secret
        expiry = fields.Datetime.now() + datetime.timedelta(days=60)
        key = self.env["res.users.apikeys"].with_user(bot).sudo()._generate(
            None, "Payclev Odoo bridge", expiry
        )
        self.env.cr.execute(
            "SELECT id FROM res_users_apikeys WHERE user_id = %s AND index = %s ORDER BY id DESC LIMIT 1",
            (bot.id, key[:8]),
        )
        key_id = self.env.cr.fetchone()[0]
        # Payclev probes JSON-2 in a separate transaction. Make the new bot/key
        # visible first; retain its exact identity for safe failure cleanup.
        self.key_expires_at = expiry
        self.service_key_id = key_id
        self.env.cr.commit()
        try:
            response = requests.post(
                f"{api_url}/public/v1/odoo/pairing/complete",
                json={
                    "state": state,
                    "code": code,
                    "code_verifier": self.pairing_verifier,
                    "api_key": key,
                    "key_expires_at": expiry.isoformat() + "Z",
                    "notification_secret": notification_secret,
                },
                timeout=25,
                allow_redirects=False,
            )
            if response.status_code != 200 and response.status_code < 500:
                self.env["res.users.apikeys"].sudo().browse(key_id)._remove()
                message = self._completion_error(response)
                self.write({
                    "state": "not_connected",
                    "key_expires_at": False,
                    "pairing_state_hash": False,
                    "pairing_verifier": False,
                    "pairing_expires_at": False,
                    "last_connection_error": message,
                })
                self.env.cr.commit()
                raise UserError(message)
            if response.status_code != 200:
                raise requests.RequestException("Payclev returned a server error")
            result = response.json()["data"]
        except (requests.RequestException, ValueError, KeyError) as error:
            try:
                result = self._pairing_status(api_url=api_url, state=state)
            except UserError:
                result = None
            if not result:
                raise UserError(_("Connection status is uncertain. Check Payclev before starting a new request; the temporary service key expires automatically.")) from error
        return self._finish_pairing(result, expiry=expiry)
