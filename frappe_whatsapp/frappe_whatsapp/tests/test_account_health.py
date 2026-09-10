"""Read-only health boundary: provider doubles and rollback-only SQL evidence."""
import json
import unittest
import uuid
from unittest.mock import Mock, patch

import frappe

from frappe_whatsapp import account_health as health
from frappe_whatsapp.frappe_whatsapp.tests.test_native_outbox import Response


class TestAccountHealth(unittest.TestCase):
    def setUp(self):
        self.account = frappe._dict(name="fictional-health", modified="2026-09-09 12:00:00", phone_id="111001",
            business_id="222002", app_id="333003", mode="Live", status="Active", version="v25.0",
            url="https://evil.invalid/do-not-use", webhook_verify_token="private-verify-token")
        self.account.check_permission = Mock()
        self.account.get_password = Mock(return_value="fictional-secret-token")
        self.enterContext(patch.object(frappe, "session", frappe._dict(user="health@example.invalid")))
        self.enabled = self.enterContext(patch.object(frappe.db, "get_value", return_value=1))
        self.roles = self.enterContext(patch.object(frappe.db, "get_values", return_value=[("manager-row",)]))
        self.load = self.enterContext(patch.object(frappe, "get_doc", return_value=self.account))
        self.sql = self.enterContext(patch.object(frappe.db, "sql", side_effect=self.query))
        self.tables = self.enterContext(patch.object(frappe.db, "table_exists", return_value=True))
        self.enterContext(patch.object(frappe, "get_installed_apps", return_value=["crm", "frappe_whatsapp"]))
        self.http = self.enterContext(patch.object(health.transport, "raw", return_value=self.success()))
        self.log = self.enterContext(patch.object(frappe, "log_error"))
        self.commit = self.enterContext(patch.object(frappe.db, "commit"))
        self.write = self.enterContext(patch.object(frappe.db, "set_value"))
        self.encrypted = [("token",), ("app_secret",)]

    def query(self, sql, values=None, **kwargs):
        if "`__Auth`" in sql:
            return self.encrypted
        return []

    def success(self, quality="GREEN", phone="111001"):
        return Response(payload={"id": phone, "quality_rating": quality})

    def view(self, remote=False):
        result = (health.check_phone if remote else health.get_health)(self.account.name, self.account.modified)
        self.log.assert_not_called(); self.commit.assert_not_called(); self.write.assert_not_called()
        serialized = json.dumps(result)
        self.assertNotIn("private-verify-token", serialized)
        self.assertNotIn("fictional-secret-token", serialized)
        self.assertNotIn("evil.invalid", serialized)
        return result

    def test_current_manager_and_saved_account_are_read_before_any_evidence(self):
        self.view()
        self.enabled.assert_called_once_with("User", "health@example.invalid", "enabled", for_update=True)
        self.roles.assert_called_once_with("Has Role", {"parent": "health@example.invalid", "parenttype": "User",
            "parentfield": "roles", "role": "System Manager"}, "name", for_update=True)
        self.load.assert_called_once_with("WhatsApp Account", "fictional-health", for_update=True)
        self.account.check_permission.assert_called_once_with("read")
        self.http.assert_not_called(); self.account.get_password.assert_not_called()

    def test_guest_disabled_and_revoked_manager_never_read_account_or_remote(self):
        frappe.session.user = "Guest"
        with self.assertRaises(frappe.PermissionError): self.view(True)
        self.enabled.assert_not_called()
        frappe.session.user = "health@example.invalid"
        self.enabled.return_value = 0
        with self.assertRaises(frappe.PermissionError): self.view(True)
        self.enabled.return_value = 1; self.roles.return_value = []
        with patch.object(frappe, "get_roles", return_value=["System Manager"]):
            with self.assertRaises(frappe.PermissionError): self.view(True)
        self.load.assert_not_called(); self.sql.assert_not_called(); self.http.assert_not_called()

    def test_stale_saved_revision_missing_account_and_permission_denial_do_not_check(self):
        for revision in (None, "nonsense", "2026-09-09 11:59:59"):
            with self.assertRaises(frappe.ValidationError): health.check_phone(self.account.name, revision)
        self.account.check_permission.side_effect = frappe.PermissionError("no access")
        with self.assertRaises(frappe.PermissionError): self.view(True)
        self.load.side_effect = frappe.DoesNotExistError("missing")
        with self.assertRaises(frappe.DoesNotExistError): self.view(True)
        self.http.assert_not_called(); self.sql.assert_not_called()

    def test_no_client_url_token_fields_or_unsaved_document_contract(self):
        for extra in (dict(url="https://evil.invalid"), dict(token="forged"), dict(fields="anything"), dict(doc={})):
            with self.assertRaises(TypeError): health.check_phone(self.account.name, self.account.modified, **extra)
        self.http.assert_not_called()

    def test_local_health_has_no_remote_or_decryption_and_no_false_connected(self):
        result = self.view()
        self.assertEqual(result["configuration"]["token"], "configured")
        self.assertEqual(result["check"]["state"], "not_checked")
        self.assertIsNone(result["check"]["checked_at"])
        self.assertEqual(result["subscriptions"], "unknown")
        self.assertEqual(result["webhook_delivery"], "unknown")
        self.http.assert_not_called(); self.account.get_password.assert_not_called()
        for call in self.sql.call_args_list:
            selected = call.args[0].split("FROM")[0].lower()
            self.assertNotIn("payload", selected); self.assertNotIn("event_id", selected)
            self.assertNotIn("password", selected)

    def test_configuration_missing_invalid_and_unavailable_ledgers_remain_unknown(self):
        self.encrypted = []
        self.account.phone_id = None; self.account.app_id = "wrong"; self.account.version = "../v25.0"
        result = self.view(True)
        self.assertEqual(result["configuration"]["phone_id"], "missing")
        self.assertEqual(result["configuration"]["app_id"], "invalid")
        self.assertFalse(result["phone_receipts"]["available"])
        self.assertEqual(result["check"]["state"], "missing_configuration")
        self.http.assert_not_called()
        self.account.phone_id = "111001"; self.account.app_id = "333003"
        self.tables.return_value = False
        self.assertEqual(self.view()["outbox"]["reason_code"], "outbox_unavailable")

    def test_demo_inactive_missing_and_unsafe_config_block_before_transport(self):
        for field, value, expected in (("mode", "Demo", "demo_no_remote"), ("mode", None, "inactive"),
            ("status", "Inactive", "inactive"), ("phone_id", "../../escape", "missing_configuration"),
            ("version", "v25.0/escape", "missing_configuration")):
            old = self.account[field]; self.account[field] = value
            self.assertEqual(self.view(True)["check"]["state"], expected)
            self.account[field] = old
        self.http.assert_not_called(); self.account.get_password.assert_not_called()

    def test_named_token_missing_unreadable_or_header_injection_is_sanitized(self):
        for token in (None, "", " token", "secret\r\nBad: yes", "x" * 4097):
            self.account.get_password.return_value = token
            self.assertEqual(self.view(True)["check"]["state"], "missing_configuration")
        self.account.get_password.side_effect = RuntimeError("secret password failure")
        self.assertEqual(self.view(True)["check"]["state"], "missing_configuration")
        self.http.assert_not_called()

    def test_exact_fixed_phone_get_uses_bearer_and_bounded_stream_only(self):
        result = self.view(True)
        self.assertEqual(result["check"]["state"], "verified")
        self.assertEqual(result["check"]["quality_rating"], "GREEN")
        self.assertEqual(result["check"]["source"], "meta_graph_phone_get")
        self.assertFalse(result["check"]["persisted"])
        self.http.assert_called_once_with(self.account, "GET", "https://graph.facebook.com/v25.0/111001",
            params={"fields": "id,quality_rating"}, headers={"Authorization": "Bearer fictional-secret-token", "Accept": "application/json"},
            timeout=(5, 10), allow_redirects=False, stream=True)
        self.account.get_password.assert_called_once_with("token", raise_exception=False)
        self.assertTrue(self.http.return_value.closed)
        self.assertEqual(result["subscriptions"], "unknown"); self.assertEqual(result["webhook_delivery"], "unknown")

    def test_provider_check_does_not_mask_missing_webhook_configuration(self):
        self.encrypted = [("token",)]
        self.account.app_id = None
        result = self.view(True)
        self.assertEqual(result["check"]["state"], "verified")
        self.assertEqual(result["configuration"]["app_secret"], "missing")
        self.assertEqual(result["configuration"]["app_id"], "missing")
        self.assertEqual(result["webhook_delivery"], "unknown")

    def test_auth_permission_rate_limit_and_unavailable_are_distinct(self):
        for status, code, expected in ((401, None, "authentication_failed"), (403, None, "permission_denied"),
            (429, None, "rate_limited"), (400, 190, "authentication_failed"), (400, 200, "permission_denied"),
            (400, 4, "rate_limited"), (400, 130429, "rate_limited"), (400, 999, "unavailable"), (500, 190, "unavailable")):
            self.http.return_value = Response(status, {"error": {"code": code, "message": "sensitive provider body"}})
            result = self.view(True)
            self.assertEqual(result["check"]["state"], expected)
            self.assertNotIn("sensitive provider body", json.dumps(result))
            self.assertTrue(self.http.return_value.closed)

    def test_redirect_timeout_oversize_and_stream_errors_never_fake_health(self):
        responses = [Response(302), Response(headers={"Content-Type": "application/json", "Content-Length": "999999"}),
            Response(headers={"Content-Type": "text/html"}), Response(chunks=[b"x" * 8192] * 9),
            Response(chunks=[b"{", TimeoutError("sensitive stream")])]
        for response in responses:
            self.http.return_value = response
            self.assertEqual(self.view(True)["check"]["state"], "unavailable")
            self.assertTrue(response.closed)
        self.http.side_effect = TimeoutError("secret URL")
        self.assertEqual(self.view(True)["check"]["state"], "unavailable")

    def test_wrong_id_duplicate_keys_unknown_quality_and_extra_metadata_rejected(self):
        responses = [self.success(phone="999999"), self.success(quality="NEW_UNKNOWN_ENUM"),
            Response(payload={"id": "111001", "quality_rating": "GREEN", "display_phone_number": "private address"}),
            Response(chunks=[b'{"id":"bad","id":"111001","quality_rating":"GREEN"}']),
            Response(chunks=[b'{"id":"111001","quality_rating":NaN}'])]
        for response in responses:
            self.http.return_value = response
            self.assertEqual(self.view(True)["check"]["state"], "unavailable")

    def test_stream_deadline_and_close_failure_are_safe(self):
        self.http.return_value = Response(chunks=[b'{', b'"id":"111001","quality_rating":"GREEN"}'])
        with patch.object(health.time, "monotonic", side_effect=[0, 0, 10, 21]):
            self.assertEqual(self.view(True)["check"]["state"], "unavailable")
        self.assertTrue(self.http.return_value.closed)
        self.http.return_value = self.success()
        self.http.return_value.close = Mock(side_effect=RuntimeError("close"))
        self.assertEqual(self.view(True)["check"]["state"], "verified")


class TestAccountHealthSQL(unittest.TestCase):
    def setUp(self):
        self.point = "health_" + frappe.generate_hash(length=10)
        frappe.db.savepoint(self.point)
        self.addCleanup(lambda: frappe.db.rollback(save_point=self.point))
        self.enterContext(patch.object(frappe, "session", frappe._dict(user="Administrator")))
        self.http = self.enterContext(patch.object(health.transport, "raw"))
        self.suffix = str(uuid.uuid4().int)[:24]
        self.phone, self.waba, self.app = self.suffix + "1", self.suffix + "2", self.suffix + "3"
        self.account = frappe.get_doc({"doctype": "WhatsApp Account", "account_name": "health-fixture-" + self.suffix,
            "phone_id": self.phone, "business_id": self.waba, "app_id": self.app, "mode": "Demo", "status": "Inactive", "version": "v25.0"}).insert(ignore_permissions=True)

    def receipt(self, account_id, *, app_id=None, provider="WhatsApp", kind="message", state="Pending"):
        from frappe_whatsapp.webhook_receipts import _prepare
        values = _prepare({"provider": provider, "account_id": account_id, "app_id": app_id or self.app,
            "event_type": kind, "event_id": frappe.generate_hash(length=20), "payload": {"private": "do not return message contents"}})
        values["state"] = state
        doc = frappe.get_doc(values); doc.name = values["event_key"]; doc.db_insert()

    def test_exact_phone_app_counts_and_bounded_waba_observations_without_payload(self):
        self.receipt(self.phone)
        self.receipt(self.phone, state="Failed")
        self.receipt(self.phone, app_id=self.app + "4", state="Failed")
        self.receipt(self.phone, provider="Messenger", state="Failed")
        self.receipt(self.phone + "9", state="Failed")
        for _ in range(12): self.receipt(self.waba, kind="phone_number_quality_update", state="Ignored")
        self.receipt(self.waba, kind="message_template_status_update", app_id=self.app + "4")
        result = health.get_health(self.account.name, str(self.account.modified))
        self.assertEqual(result["phone_receipts"]["total"], 2)
        self.assertEqual(result["phone_receipts"]["backlog"], 1)
        self.assertEqual(result["phone_receipts"]["failed"], 1)
        self.assertEqual(result["waba_receipts"]["total"], 12)
        self.assertEqual(len(result["waba_observations"]), 10)
        self.assertTrue(all(row["phone_mapping"] == "unverified" and row["interpretation"] == "unsupported_order_unverified" for row in result["waba_observations"]))
        self.assertNotIn("do not return message contents", json.dumps(result))
        self.assertEqual(result["check"]["state"], "not_checked")
        self.http.assert_not_called()

    def test_real_modified_guard_rejects_stale_browser_and_demo_never_calls_provider(self):
        with self.assertRaises(frappe.ValidationError): health.check_phone(self.account.name, "2000-01-01 00:00:00")
        result = health.check_phone(self.account.name, str(self.account.modified))
        self.assertEqual(result["check"]["state"], "demo_no_remote")
        self.http.assert_not_called()

    def test_outbox_unknown_count_has_exact_phone_provider_scope(self):
        for account_id, provider, state in ((self.phone, "WhatsApp", "Unknown"), (self.phone, "WhatsApp", "Failed"),
            (self.phone, "Messenger", "Unknown"), (self.phone + "8", "WhatsApp", "Unknown")):
            frappe.get_doc({"doctype": "CRM Outbound Intent", "name": uuid.uuid4().hex * 2,
                "provider": provider, "account_id": account_id, "state": state}).db_insert()
        result = health.get_health(self.account.name, str(self.account.modified))
        self.assertEqual(result["outbox"]["unknown"], 1)
        self.assertEqual(result["outbox"]["failed"], 1)
        self.assertEqual(result["outbox"]["backlog"], 0)
        self.http.assert_not_called()

    def test_real_role_revocation_beats_cached_manager_role(self):
        user = "health-" + self.suffix + "@example.invalid"
        doc = frappe.get_doc({"doctype": "User", "name": user, "email": user, "first_name": "Health fixture", "enabled": 1})
        doc.db_insert()
        role = frappe.get_doc({"doctype": "Has Role", "parent": user, "parenttype": "User", "parentfield": "roles", "role": "System Manager"})
        role.db_insert()
        frappe.session.user = user
        health._manager()
        frappe.db.delete("Has Role", {"name": role.name})
        with patch.object(frappe, "get_roles", return_value=["System Manager"]):
            with self.assertRaises(frappe.PermissionError): health.get_health(self.account.name, str(self.account.modified))
        self.http.assert_not_called()
