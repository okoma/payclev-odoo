"""Odoo 19 app visibility and administrator-initiated pairing safeguards."""

from urllib.parse import parse_qs, urlsplit
from unittest.mock import Mock, patch
from uuid import uuid4
import datetime
import hashlib

from odoo import fields
from odoo.tests import TransactionCase, tagged
from odoo.exceptions import ValidationError


@tagged("-at_install", "post_install")
class TestPayclevConnection(TransactionCase):
    def setUp(self):
        super().setUp()
        connection_model = self.env["payclev.connection"]
        self.connection = connection_model.search([("company_id", "=", self.env.company.id)], limit=1)
        values = {
            "payclev_app_url": "http://app.localhost:3000",
            "payclev_api_url": "http://localhost:8000",
            "allow_local_testing": True,
            "state": "not_connected",
        }
        if self.connection:
            self.connection.write(values)
        else:
            self.connection = connection_model.create({"company_id": self.env.company.id, **values})
        self.env["ir.config_parameter"].sudo().set_param("web.base.url", "http://localhost:8069")

    def test_visible_app_and_pairing_request(self):
        module = self.env["ir.module.module"].search([("name", "=", "payclev_bridge")], limit=1)
        self.assertTrue(module.application)
        menu = self.env.ref("payclev_bridge.menu_payclev_root")
        self.assertTrue(menu.web_icon)
        self.assertEqual(self.env["payclev.connection"].action_open_app()["res_id"], self.connection.id)
        self.assertEqual(self.connection.read(["state"])[0]["state"], "not_connected")

        action = self.connection.action_connect()
        self.assertEqual(action["type"], "ir.actions.act_url")
        parts = urlsplit(action["url"])
        self.assertEqual(parts.netloc, "app.localhost:3000")
        self.assertEqual(parts.path, "/integrations/odoo/authorize")
        params = parse_qs(parts.query)
        self.assertEqual(params["company_id"], [str(self.env.company.id)])
        self.assertEqual(params["base_url"], ["http://localhost:8069"])
        self.assertNotIn("api_key", params)
        self.assertEqual(self.connection.state, "pending")

    def test_http_not_allowed_without_explicit_local_setting(self):
        self.connection.allow_local_testing = False
        with self.assertRaisesRegex(ValidationError, "Payclev app URL"):
            self.connection.action_connect()

    def test_api_origin_cannot_use_app_host(self):
        self.connection.payclev_api_url = "http://app.localhost:8000"
        with self.assertRaises(ValidationError):
            self.connection.action_connect()

    def test_retry_recovers_completed_pairing_without_creating_another_key(self):
        state = "a" * 43
        expiry = fields.Datetime.now() + datetime.timedelta(days=60)
        self.connection.write({
            "state": "pending",
            "pairing_state_hash": hashlib.sha256(state.encode()).hexdigest(),
            "pairing_verifier": "b" * 43,
            "pairing_expires_at": fields.Datetime.now() + datetime.timedelta(minutes=5),
            "key_expires_at": expiry,
        })
        result = {
            "integration_id": "integration-test",
            "organization_id": "organization-test",
            "organization_name": "Test business",
        }
        with patch("odoo.addons.payclev_bridge.models.connection.requests.post", return_value=Mock(
            status_code=200, json=Mock(return_value={"data": result})
        )), patch.object(type(self.connection), "_service_user", side_effect=AssertionError("A new key must not be created")):
            self.connection.complete_pairing(state=state, code="c" * 43)
        self.assertEqual(self.connection.state, "connected")
        self.assertEqual(self.connection.integration_id, "integration-test")

    def test_reset_for_reconnection_revokes_only_bridge_keys(self):
        bot = self.connection._service_user()
        expiry = fields.Datetime.now() + datetime.timedelta(days=60)
        self.env["res.users.apikeys"].with_user(bot).sudo()._generate(
            None, "Payclev Odoo bridge", expiry
        )
        bridge_keys = self.env["res.users.apikeys"].sudo().search([
            ("user_id", "=", bot.id),
            ("name", "=", "Payclev Odoo bridge"),
        ])
        self.assertTrue(bridge_keys)
        self.connection.write({
            "state": "connected",
            "key_expires_at": expiry,
            "service_key_id": bridge_keys[0].id,
            "integration_id": "integration-test",
            "organization_id": "organization-test",
            "payclev_business": "Test business",
        })

        self.connection.action_reset_connection()

        self.assertEqual(self.connection.state, "not_connected")
        self.assertFalse(self.connection.key_expires_at)
        self.assertFalse(self.connection.integration_id)
        self.assertFalse(bridge_keys.exists())
        self.assertEqual(self.connection.service_user_id, bot)

    def test_key_rotation_is_opt_in_and_revokes_only_the_recorded_key(self):
        bot = self.connection._service_user()
        expiry = fields.Datetime.now() + datetime.timedelta(days=5)
        old_key = self.env["res.users.apikeys"].with_user(bot).sudo()._generate(
            None, "Payclev Odoo bridge", expiry
        )
        old_record = self.env["res.users.apikeys"].sudo().search([
            ("user_id", "=", bot.id), ("index", "=", old_key[:8]),
        ], order="id desc", limit=1)
        unrelated = self.env["res.users.apikeys"].with_user(bot).sudo()._generate(
            None, "Unrelated integration", expiry
        )
        unrelated_record = self.env["res.users.apikeys"].sudo().search([
            ("user_id", "=", bot.id), ("index", "=", unrelated[:8]),
        ], order="id desc", limit=1)
        self.connection.write({
            "state": "connected", "service_key_id": old_record.id,
            "key_expires_at": expiry, "integration_id": str(uuid4()),
            "organization_id": str(uuid4()),
        })
        bridge = self.env["payclev.payment.bridge"].with_user(bot)
        identity = {
            "company_id": self.env.company.id,
            "integration_id": self.connection.integration_id,
            "organization_id": self.connection.organization_id,
        }
        from odoo.exceptions import AccessError
        with self.assertRaises(AccessError):
            bridge.payclev_begin_key_rotation(**identity, request_id=str(uuid4()))
        self.connection.auto_rotate_key = True
        request_id = str(uuid4())
        result = bridge.payclev_begin_key_rotation(**identity, request_id=request_id)
        self.assertTrue(old_record.exists())
        self.assertTrue(unrelated_record.exists())
        import hashlib
        proof = hashlib.sha256((request_id + result["api_key"]).encode()).hexdigest()
        bridge.payclev_finish_key_rotation(**identity, request_id=request_id, proof=proof)
        self.assertFalse(old_record.exists())
        self.assertTrue(unrelated_record.exists())
        self.assertTrue(self.connection.service_key_id)

    def test_service_user_is_restricted_to_selected_company(self):
        bot = self.connection._service_user()
        self.assertEqual(bot.company_ids, self.env.company)
        self.assertFalse(bot.has_group("base.group_system"))
        self.assertTrue(bot.has_group("payclev_bridge.group_payclev_bridge"))
        self.assertTrue(bot.has_group("account.group_account_invoice"))
        self.assertTrue(bot.payclev_service_user)
        self.assertEqual(bot.state, "service")
        self.assertNotIn(bot, self.env["res.users"].search([("state", "in", ["new"])]))
        self.assertIn(bot, self.env["res.users"].search([("state", "in", ["service"])]))
        self.env.cr.execute("SELECT password FROM res_users WHERE id = %s", (bot.id,))
        self.assertIsNone(self.env.cr.fetchone()[0])
        self.assertFalse(bot.partner_id.signup_type)

    def test_unlinked_service_user_is_not_a_pending_invitation(self):
        bot = self.connection._service_user()
        self.connection.service_user_id = False
        bot.invalidate_recordset(["state", "payclev_connection_ids"])
        self.assertEqual(bot.state, "service")
        self.assertNotIn(bot, self.env["res.users"].search([("state", "in", ["new"])]))
        self.assertNotIn(bot, self.env["res.users"].search([
            ("log_ids", "=", False),
            ("payclev_service_user", "=", False),
            ("payclev_connection_ids", "=", False),
        ]))

    def test_inactive_subscription_error_is_specific(self):
        response = Mock(status_code=403)
        response.json.return_value = {"error": {"code": "entitlement_required"}}
        self.assertIn("active plan", self.connection._completion_error(response))
