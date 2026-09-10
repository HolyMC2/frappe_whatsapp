"""Legacy egress scope and actual core/gateway integration; provider doubles only."""

import copy
import json
import sys
import unittest
from unittest.mock import Mock, patch

import frappe

from frappe_whatsapp import legacy_outbox as adapter, transport
from frappe_whatsapp.frappe_whatsapp.tests.test_native_outbox import Response


class TestLegacyTransport(unittest.TestCase):
    def setUp(self):
        self.account = frappe._dict(name="fictional-legacy-unit", phone_id="9941001", version="v23.0", mode="Live", status="Active")
        self.account.get_password = Mock(return_value="fictional-token")
        self.peer = "5215550194001"
        self.url = "https://graph.facebook.com/v23.0/9941001/messages"
        self.payload = {"messaging_product": "whatsapp", "to": self.peer, "type": "text", "text": {"body": "Fictional message"}}
        self.enterContext(patch.object(frappe, "flags", frappe._dict()))
        self.enterContext(patch.object(frappe, "get_installed_apps", return_value=[]))
        self.enterContext(patch.object(transport, "resolve_account", return_value=self.account))
        self.rows = self.enterContext(patch.object(frappe.db, "get_values", return_value=[self.account]))
        self.http = self.enterContext(patch.object(transport.requests, "request", side_effect=lambda *a, **k: self.response()))
        self.old_post = self.enterContext(patch.object(transport, "make_post_request"))
        self.old_request = self.enterContext(patch.object(transport, "make_request"))
        self.logs = self.enterContext(patch.object(frappe, "log_error"))

    def response(self, **kwargs):
        return Response(payload={"messaging_product": "whatsapp", "contacts": [{"wa_id": self.peer}],
                                 "messages": [{"id": "wamid.fictional-legacy"}]}, **kwargs)

    def call(self, entry="api", data=None, url=None, **kwargs):
        data = json.dumps(self.payload) if data is None else data
        url = url or self.url
        if entry == "post":
            return transport.post(self.account, url, data=data, **kwargs)
        return getattr(transport, entry)(self.account, "POST", url, data=data, **kwargs)

    def test_api_raw_and_post_use_frozen_bounded_physical_http_not_old_helpers(self):
        for entry in ("api", "raw", "post"):
            self.call(entry)
            args, kwargs = self.http.call_args
            self.assertEqual(args, ("POST", self.url))
            self.assertEqual(json.loads(kwargs["data"]), self.payload)
            self.assertIsInstance(kwargs["data"], bytes)
            self.assertFalse(kwargs["allow_redirects"])
            self.assertEqual(kwargs["timeout"], (5, 20))
            self.assertTrue(kwargs["stream"])
            self.assertEqual(kwargs["headers"]["Content-Type"], "application/json")
        self.assertEqual(self.http.call_count, 3)
        self.old_post.assert_not_called()
        self.old_request.assert_not_called()

    def test_optional_crm_absent_still_validates_current_account_without_creating(self):
        with patch.object(frappe, "get_doc", side_effect=AssertionError("No identity creation")):
            self.call()
        self.assertTrue(self.rows.call_args.kwargs["for_update"])
        self.assertEqual(self.rows.call_args.args[1], {"phone_id": self.account.phone_id})

    def test_installed_but_broken_crm_cannot_silently_fall_through(self):
        with patch.object(frappe, "get_installed_apps", return_value=["crm"]), patch.dict(sys.modules, {"crm.api.outbox_legacy": None}):
            with self.assertRaisesRegex(adapter.LegacySendBlocked, "legacy_control_unavailable"):
                self.call()
        self.http.assert_not_called()

    def test_current_read_conflict_code_survives_adapter_without_http(self):
        from crm.api.outbox_legacy import LegacySendBlocked
        with patch.object(frappe, "get_installed_apps", return_value=["crm"]), \
                patch("crm.api.outbox_legacy.guard_legacy_send", side_effect=LegacySendBlocked("legacy_control_conflict")):
            with self.assertRaisesRegex(adapter.LegacySendBlocked, "^legacy_control_conflict$"):
                self.call()
        self.http.assert_not_called()

    def test_wrong_missing_duplicate_inactive_and_demo_current_accounts_hold(self):
        original = copy.copy(self.account)
        for rows in ([], [original, original], [frappe._dict(original, name="other")],
                     [frappe._dict(original, status="Inactive")], [frappe._dict(original, mode="Demo")],
                     [frappe._dict(original, version="v24.0")]):
            self.rows.return_value = rows
            with self.assertRaisesRegex(adapter.LegacySendBlocked, "legacy_account_scope_invalid"):
                self.call()
        self.http.assert_not_called()

    def test_unsafe_or_ambiguous_urls_are_rejected_before_account_lookup(self):
        for url in (self.url.replace("https:", "http:"), self.url.replace("graph.facebook.com", "evil.invalid"),
                    self.url.replace("graph.facebook.com", "user@graph.facebook.com"),
                    self.url.replace("graph.facebook.com", "graph.facebook.com:444"), self.url + "/", self.url + "?access_token=secret",
                    self.url + "#fragment", self.url.replace("messages", "%6dessages"), self.url.replace("messages", "messages/extra")):
            with self.subTest(url=url), self.assertRaises(adapter.LegacySendBlocked):
                self.call(url=url)
        self.rows.assert_not_called()
        self.http.assert_not_called()

    def test_duplicate_keys_multibody_form_and_batch_overrides_are_rejected(self):
        for data in ('{"to":"1","to":"2"}', '{"text":{"body":"a","body":"b"}}', '[]',
                     'to=5215550194001&type=text', '{"batch":[{"method":"POST"}]}'):
            with self.assertRaises(adapter.LegacySendBlocked):
                self.call(data=data)
        for data in ('{"batch":[]}', 'batch=%5B%5D', 'method=POST&to=1'):
            with self.assertRaises(adapter.LegacySendBlocked):
                self.call(data=data, url="https://graph.facebook.com/v23.0")
        for kwargs in ({"json": self.payload}, {"params": {"to": "different"}}, {"files": {"file": b"no"}}):
            with self.assertRaises(adapter.LegacySendBlocked):
                self.call("raw", **kwargs)
        self.http.assert_not_called()

    def test_invalid_and_multiple_recipients_cannot_choose_a_different_scope(self):
        for extra in ({"to": "+" + self.peer}, {"to": [self.peer]}, {"to": 123}, {"recipient": {"id": self.peer}},
                      {"messaging_product": "messenger"}, {"recipient_type": "group"}, {"image": {"id": "extra"}}, {"type": []}):
            with self.assertRaises(adapter.LegacySendBlocked):
                self.call(data=json.dumps({**self.payload, **extra}))
        self.http.assert_not_called()

    def test_oversize_and_opaque_batch_bodies_cannot_hide_as_administrative_calls(self):
        from io import BytesIO
        root = "https://graph.facebook.com/v23.0"
        for body in ('{"batch":[' + ' ' * 65536 + '] }', 'batch=' + 'x' * 65536,
                     [("batch", "[]")], BytesIO(b"batch=[]"), {b"batch": "[]"}, '[{"method":"POST"}]'):
            with self.subTest(kind=type(body).__name__), self.assertRaises(adapter.LegacySendBlocked):
                self.call("raw", data=body, url=root)
        with self.assertRaises(adapter.LegacySendBlocked):
            transport.raw(self.account, "POST", root, files={"batch": (None, "[]")})
        for kwargs in ({"params": {b"batch": "[]"}}, {"params": {"batch[0][method]": "POST"}},
                       {"data": {"batch[0][method]": "POST"}}):
            with self.assertRaises(adapter.LegacySendBlocked):
                transport.raw(self.account, "POST", root, **kwargs)
        self.http.assert_not_called()
        self.old_post.assert_not_called()

    def test_raw_json_is_frozen_and_redirect_timeout_overrides_cannot_relax_bounds(self):
        transport.raw(self.account, "POST", self.url, json=self.payload, allow_redirects=True, timeout=None)
        kwargs = self.http.call_args.kwargs
        self.assertNotIn("json", kwargs)
        self.payload["text"]["body"] = "Changed after attempt"
        self.assertEqual(json.loads(kwargs["data"])["text"]["body"], "Fictional message")
        self.assertFalse(kwargs["allow_redirects"])
        self.assertEqual(kwargs["timeout"], (5, 20))

    def test_read_and_presence_actions_are_honestly_held_without_recipient_guess(self):
        for body in ({"messaging_product": "whatsapp", "status": "read", "message_id": "wamid.private"},
                     {"recipient": {"id": self.peer}, "sender_action": "mark_seen"}):
            with self.assertRaisesRegex(adapter.LegacySendBlocked, "legacy_action_recipient_unverified"):
                self.call(data=json.dumps(body))
        self.http.assert_not_called()

    def test_admin_media_upload_and_get_keep_existing_transport_paths(self):
        url = "https://graph.facebook.com/v23.0/9941001/message_templates"
        self.call(data='{"name":"approved_template"}', url=url)
        self.old_post.assert_called_once()
        transport.raw(self.account, "POST", url.replace("message_templates", "media"),
            data={"messaging_product": "whatsapp"}, files={"file": b"fictional"})
        transport.raw(None, "GET", "https://lookaside.fbsbx.com/media")
        self.assertEqual(self.http.call_count, 2)
        self.rows.assert_not_called()

    def test_demo_stays_no_effect_and_does_not_invent_live_native_acceptance(self):
        self.account.mode = "Demo"
        with patch.object(transport, "_log_demo"):
            out = self.call(data='{}')
        self.assertTrue(out["messages"][0]["id"].startswith(transport.DEMO_ID_PREFIX))
        self.rows.assert_not_called()
        self.http.assert_not_called()

    def test_request_body_depth_size_cycles_and_nonfinite_numbers_hold(self):
        nested = {}
        for _ in range(14):
            nested = {"x": nested}
        cyclic = {}; cyclic["x"] = cyclic
        for body in ({**self.payload, "text": {"body": "x" * 65536}},
                     {**self.payload, "text": nested}, {**self.payload, "text": cyclic},
                     {**self.payload, "text": {"body": float("nan")}}):
            with self.assertRaises(adapter.LegacySendBlocked):
                with adapter.guard_transport_send(self.account, "POST", self.url, json_body=body):
                    self.fail("invalid bounded body")
        self.http.assert_not_called()

    def test_method_override_headers_hold_before_http(self):
        with self.assertRaises(adapter.LegacySendBlocked):
            self.call(headers={"X-HTTP-Method-Override": "DELETE"})
        self.http.assert_not_called()

    def test_pre_http_hold_replaces_stale_integration_response_with_static_error(self):
        frappe.flags.integration_request = Mock()
        frappe.flags.integration_request.json.return_value = {"error": {"message": "prior private token"}}
        with self.assertRaises(adapter.LegacySendBlocked):
            self.call(data="{}")
        self.assertEqual(frappe.flags.integration_request.json(), {"error": {"message": "legacy_message_scope_invalid"}})
        self.http.assert_not_called()

    def test_redirect_timeout_oversize_malformed_wrong_recipient_are_safe_unknown(self):
        cases = [Response(status=302), TimeoutError("token/body/private URL"),
                 Response(chunks=[b"x" * 65537]), Response(chunks=[b"not-json"]),
                 Response(payload={"messages": [{"id": "wamid.fake"}], "contacts": [{"wa_id": "other"}]}),
                 Response(chunks=[b' {"messages":[],"messages":[]}'])]
        for value in cases:
            self.http.side_effect = value if isinstance(value, Exception) else None
            self.http.return_value = value
            with self.assertRaises(adapter.LegacyMessageError) as error:
                self.call()
            self.assertEqual((error.exception.reason_code, error.exception.outcome), ("provider_response_uncertain", "Unknown"))
            self.assertEqual(frappe.flags.integration_request.json(), {"error": {"message": "provider_response_uncertain"}})
            if not isinstance(value, Exception):
                self.assertTrue(value.closed)
        self.logs.assert_not_called()

    def test_explicit_graph_4xx_exposes_static_failure_only(self):
        self.http.side_effect = None
        self.http.return_value = Response(status=400, payload={"error": {"code": 190, "message": "private token and raw body"}})
        with self.assertRaises(adapter.LegacyMessageError) as error:
            self.call()
        self.assertEqual((error.exception.reason_code, error.exception.outcome), ("provider_rejected", "Failed"))
        self.assertNotIn("private", json.dumps(frappe.flags.integration_request.json()))
        self.logs.assert_not_called()

    def test_response_deadline_is_checked_while_streaming(self):
        self.http.side_effect = None
        self.http.return_value = self.response()
        with patch("frappe_whatsapp.native_outbox.time.monotonic", side_effect=[0, 31]):
            with self.assertRaisesRegex(adapter.LegacyMessageError, "provider_response_uncertain"):
                self.call()


from crm.tests.test_outbox_legacy import LegacyGuardFixture


class TestLegacyTransportSql(LegacyGuardFixture):
    def test_both_entry_points_and_post_deny_actual_control_before_http(self):
        self.open()
        with patch.object(transport.requests, "request") as http, patch.object(transport, "make_post_request") as old:
            for method in ("api", "raw", "post"):
                args = (self.account, "https://graph.facebook.com/v23.0/" + self.account_id + "/messages")
                if method != "post":
                    args = (args[0], "POST", args[1])
                with self.assertRaisesRegex(adapter.LegacySendBlocked, "native_outbound_intent_required"):
                    getattr(transport, method)(*args, data=json.dumps(self.payload))
            http.assert_not_called()
            old.assert_not_called()

    def test_native_worker_real_gateway_and_guard_reach_one_double_under_same_fence(self):
        from crm.api import outbox, conversations as control
        name = self.native_intent()
        def provider(method, url, **kwargs):
            control._assert_fence(self.name)
            intent = outbox._load(name)
            self.assertEqual(intent.state, "Submitting")
            self.assertEqual(kwargs["data"], intent.payload.encode())
            return Response(payload={"messaging_product": "whatsapp", "contacts": [{"wa_id": self.peer}],
                                     "messages": [{"id": "wamid.native-through-legacy-guard"}]})
        with patch.object(frappe.db, "rollback"), patch.object(transport.requests, "request", side_effect=provider) as http:
            outbox.dispatch_intent(name)
        self.assertEqual(outbox._load(name).state, "Accepted")
        self.assertEqual(http.call_count, 1)

    def test_private_staff_and_uncontrolled_customer_use_real_guard_without_rows(self):
        from crm.api import conversations as control
        self.private_identity()
        def provider(*args, **kwargs):
            peer = json.loads(kwargs["data"])["to"]
            key = control.conversation_key("WhatsApp", self.account_id, peer)
            control._assert_fence(key)
            self.assertFalse(frappe.db.get_value(control.DOCTYPE, key, "name", for_update=True))
            return Response(payload={"messaging_product": "whatsapp", "contacts": [{"wa_id": peer}], "messages": [{"id": "wamid.legacy-staff-route"}]})
        with patch.object(transport.requests, "request", side_effect=provider) as http:
            for peer in (self.peer, self.peer + "1"):
                transport.api(self.account, "POST", "https://graph.facebook.com/v23.0/" + self.account_id + "/messages",
                              data=json.dumps({**self.payload, "to": peer}))
        self.assertEqual(http.call_count, 2)
