"""Frozen WA gateway: fake provider only, no live sends or database writes."""

import copy
import json
import sys
import types
import unittest
from unittest.mock import Mock, patch

import frappe

from frappe_whatsapp import native_outbox as gateway


class Response:
    def __init__(self, status=200, payload=None, *, chunks=None, headers=None):
        self.status_code = status
        self.headers = {"Content-Type": "application/json"} if headers is None else headers
        self.chunks = chunks if chunks is not None else [json.dumps(payload).encode()]
        self.closed = False
        self.consumed = 0

    def iter_content(self, chunk_size):
        assert chunk_size == 8192
        for chunk in self.chunks:
            self.consumed += 1
            if isinstance(chunk, Exception):
                raise chunk
            yield chunk

    def close(self):
        self.closed = True

    @property
    def content(self):
        raise AssertionError("Unbounded response buffering is forbidden")


class TestNativeOutbox(unittest.TestCase):
    def setUp(self):
        self.intent = frappe._dict(name="frozen-intent", provider="WhatsApp", account_id="111001", peer_id="5215550001111")
        self.payload = {"messaging_product": "whatsapp", "to": self.intent.peer_id, "type": "text", "text": {"body": "Frozen message"}}
        self.row = frappe._dict(name="exact-account", phone_id=self.intent.account_id, status="Active", mode="Live", version="v23.0")
        self.account = frappe._dict(self.row)
        self.account.get_password = Mock(return_value="fictional-exact-token")
        self.authority = Mock()
        core = types.ModuleType("crm.api.outbox")
        core.require_dispatch = self.authority
        self._start(patch.dict(sys.modules, {"crm.api.outbox": core}))
        self.lookup = self._start(patch.object(frappe.db, "get_values", return_value=[self.row]))
        self.load = self._start(patch.object(frappe, "get_doc", return_value=self.account))
        self.post = self._start(patch.object(gateway.transport, "raw", return_value=self.success()))
        self.log = self._start(patch.object(frappe, "log_error"))

    def _start(self, patcher):
        value = patcher.start()
        self.addCleanup(patcher.stop)
        return value

    def success(self, peer=None, message_id="wamid.real-fixture"):
        return Response(payload={"messaging_product": "whatsapp", "contacts": [{"input": peer or self.intent.peer_id, "wa_id": peer or self.intent.peer_id}], "messages": [{"id": message_id}]})

    def send(self):
        result = gateway.send_frozen(self.intent, self.payload)
        self.log.assert_not_called()
        return result

    def assert_blocked(self, reason="frozen_payload_invalid"):
        self.assertEqual(self.send(), {"state": "Blocked", "reason_code": reason, "retryable": False})
        self.post.assert_not_called()

    def test_authority_is_first_and_denied_context_cannot_resolve_or_send(self):
        self.authority.side_effect = PermissionError("secret request body")
        self.payload = object()
        self.assert_blocked("dispatch_authority_required")
        self.lookup.assert_not_called()
        self.load.assert_not_called()
        self.authority.assert_called_once_with(self.intent.name, "WhatsApp", self.intent.account_id, self.intent.peer_id, payload=self.payload)

    def test_authority_receives_exact_payload_and_can_deny_changed_body(self):
        frozen = copy.deepcopy(self.payload)
        def authorize(name, provider, account, peer, *, payload):
            if payload != frozen:
                raise PermissionError()
        self.authority.side_effect = authorize
        self.payload["text"]["body"] = "caller replacement"
        self.assert_blocked("dispatch_authority_required")
        self.lookup.assert_not_called()

    def test_creation_validator_is_pure_and_returns_canonical_bytes(self):
        original = copy.deepcopy(self.payload)
        encoded = gateway.validate_payload(self.payload, account_id=self.intent.account_id, peer_id=self.intent.peer_id)
        reordered = dict(reversed(list(self.payload.items())))
        self.assertEqual(encoded, gateway.validate_payload(reordered, account_id=self.intent.account_id, peer_id=self.intent.peer_id))
        self.assertEqual(json.loads(encoded), original)
        self.assertEqual(self.payload, original)
        self.lookup.assert_not_called()
        self.load.assert_not_called()
        self.post.assert_not_called()
        self.authority.assert_not_called()

    def test_creation_validator_raises_only_static_value_error_for_invalid_shapes(self):
        for payload in (None, {"type": []}, {"type": "text", "to": "secret invalid recipient"}):
            with self.assertRaisesRegex(ValueError, "^frozen_payload_invalid$"):
                gateway.validate_payload(payload, account_id=self.intent.account_id, peer_id=self.intent.peer_id)
        self.lookup.assert_not_called()
        self.post.assert_not_called()

    def test_absent_core_api_denies_without_fallback(self):
        with patch.dict(sys.modules, {"crm.api.outbox": None}):
            self.assert_blocked("dispatch_authority_required")
        self.lookup.assert_not_called()

    def test_forged_document_and_receipt_flags_do_not_grant_authority(self):
        self.authority.side_effect = PermissionError()
        self.intent.flags = frappe._dict(ignore_permissions=True, native_outbox=True)
        with patch.object(frappe, "flags", frappe._dict(meta_webhook_receipt="external-receipt", native_outbox=True)):
            self.assert_blocked("dispatch_authority_required")

    def test_exact_account_header_frozen_bytes_and_one_bounded_post(self):
        result = self.send()
        self.assertEqual(result, {"state": "Accepted", "provider_message_id": "wamid.real-fixture"})
        self.lookup.assert_called_once_with("WhatsApp Account", {"phone_id": self.intent.account_id},
            ["name", "phone_id", "status", "mode", "version"], as_dict=True, for_update=True)
        self.load.assert_called_once_with({"doctype": "WhatsApp Account", **self.row})
        self.account.get_password.assert_called_once_with("token", raise_exception=False)
        self.post.assert_called_once()
        args, kwargs = self.post.call_args
        self.assertIs(args[0], self.account)
        self.assertEqual(args[1:], ("POST", "https://graph.facebook.com/v23.0/111001/messages"))
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer fictional-exact-token")
        self.assertEqual(kwargs["timeout"], (5, 20))
        self.assertIs(kwargs["allow_redirects"], False)
        self.assertIs(kwargs["stream"], True)
        self.assertEqual(json.loads(kwargs["data"]), self.payload)
        self.assertIsInstance(kwargs["data"], bytes)
        self.assertNotIn(b"fictional-exact-token", kwargs["data"])
        self.assertTrue(self.post.return_value.closed)

    def test_mutation_after_serialization_cannot_change_submitted_body(self):
        original = copy.deepcopy(self.payload)
        def attempt(*args, **kwargs):
            self.payload["text"]["body"] = "later edit"
            self.assertEqual(json.loads(kwargs["data"]), original)
            return self.success()
        self.post.side_effect = attempt
        self.assertEqual(self.send()["state"], "Accepted")

    def test_recipient_product_account_and_provider_mismatch_block_before_lookup(self):
        cases = [({"to": "+5215550001111"}, {}), ({"to": "5215550001112"}, {}),
            ({"messaging_product": "messenger"}, {}), ({}, {"account_id": "111001/../other"}),
            ({}, {"peer_id": 5215550001111}), ({}, {"provider": "Instagram"})]
        for payload, intent in cases:
            with self.subTest(payload=payload, intent=intent):
                original_payload, original_intent = copy.deepcopy(self.payload), copy.copy(self.intent)
                self.payload.update(payload)
                self.intent.update(intent)
                self.assert_blocked()
                self.payload, self.intent = original_payload, original_intent
        self.lookup.assert_not_called()

    def test_arbitrary_callback_status_and_extra_content_are_rejected(self):
        for addition in ({"biz_opaque_callback_data": "unverified"}, {"status": "read"},
                         {"access_token": "secret"}, {"image": {"id": "other"}}):
            with self.subTest(addition=addition):
                self.payload.update(addition)
                self.assert_blocked()
                for key in addition:
                    self.payload.pop(key)

    def test_malformed_shapes_unknown_types_and_large_body_are_blocked(self):
        original = copy.deepcopy(self.payload)
        for value in (None, [], "text", {"body": ""}, {"body": "x" * 4097}, {"body": "safe", "preview_url": 1}):
            self.payload["text"] = value
            self.assert_blocked()
        self.payload = original
        self.payload["type"] = "contacts"
        self.assert_blocked()

    def test_cycle_depth_and_total_json_budget_fail_before_http(self):
        cycle = {}
        cycle["cycle"] = cycle
        for data in (cycle, {"x": "x" * 65537}, {"x": [0] * 101}):
            self.payload = data
            self.assert_blocked()
        self.payload = {"messaging_product": "whatsapp", "to": self.intent.peer_id, "type": "template",
            "template": {"name": "large", "language": {"code": "es_MX"}, "components": [
                {"type": "body", "parameters": [{"type": "text", "text": "a" * 30000} for _ in range(10)]}]}}
        self.assert_blocked()

    def test_missing_ambiguous_inactive_demo_and_changed_account_block(self):
        for rows in ([], [self.row, self.row]):
            self.lookup.return_value = rows
            self.assert_blocked("account_configuration_invalid")
        for change in ({"status": "Inactive"}, {"mode": "Demo"}, {"mode": None}, {"phone_id": "other"},
                       {"version": "v23.0/other"}, {"version": None}):
            self.lookup.return_value = [frappe._dict({**self.row, **change})]
            self.assert_blocked("account_configuration_invalid")

    def test_missing_or_unsafe_token_never_attempts_http(self):
        for token in (None, "", "x\r\nInjected: value", "x y", "x\x00y", "tökén", "x" * 4097):
            self.account.get_password.return_value = token
            self.assert_blocked("account_configuration_invalid")
        self.account.get_password.side_effect = RuntimeError("secret failed decryption")
        self.assert_blocked("account_configuration_invalid")

    def test_account_storage_error_is_static_before_http(self):
        self.lookup.side_effect = RuntimeError("sensitive database detail")
        self.assert_blocked("account_configuration_invalid")

    def test_media_ids_links_reactions_and_frozen_templates_do_not_resolve_live_sources(self):
        contents = [
            ("image", {"id": "provider-media", "caption": "Frozen caption"}),
            ("audio", {"link": "https://media.example.test/a.mp3"}),
            ("video", {"id": "video-1"}),
            ("document", {"id": "document-1", "filename": "receipt.pdf"}),
            ("reaction", {"message_id": "wamid.target", "emoji": ""}),
            ("template", {"name": "frozen_template", "language": {"code": "es_MX"},
                "components": [{"type": "body", "parameters": [{"type": "text", "text": "Frozen parameter"}]}]}),
        ]
        for kind, content in contents:
            with self.subTest(kind=kind):
                self.payload = {"messaging_product": "whatsapp", "to": self.intent.peer_id, "type": kind, kind: content}
                self.post.return_value = self.success()
                self.assertEqual(self.send()["state"], "Accepted")
                self.assertEqual(json.loads(self.post.call_args.kwargs["data"]), self.payload)
        self.assertEqual(self.post.call_count, len(contents))
        self.assertTrue(all(call.args[0].get("doctype") == "WhatsApp Account" for call in self.load.call_args_list))

    def test_media_ambiguous_reference_credentials_and_local_file_are_blocked(self):
        for media in ({"id": "x", "link": "https://example.test/a"}, {"link": "file:///tmp/a"},
                      {"link": "https://user:secret@example.test/a"}, {"link": "https://example.test:8443/a"}, {}):
            self.payload = {"messaging_product": "whatsapp", "to": self.intent.peer_id, "type": "image", "image": media}
            self.assert_blocked()

    def test_interactive_button_list_and_flow_are_already_complete(self):
        actions = [
            ("button", {"buttons": [{"type": "reply", "reply": {"id": "choice", "title": "Yes"}}]}),
            ("list", {"button": "Choose", "sections": [{"rows": [{"id": "one", "title": "One"}]}]}),
            ("flow", {"name": "flow", "parameters": {"flow_message_version": "3", "flow_id": "123456", "flow_cta": "Open",
                "flow_action": "navigate", "flow_action_payload": {"screen": "FIRST"}, "flow_token": "already-frozen-token"}}),
        ]
        for kind, action in actions:
            self.payload = {"messaging_product": "whatsapp", "to": self.intent.peer_id, "type": "interactive",
                "interactive": {"type": kind, "body": {"text": "Choose"}, "action": action}}
            self.post.return_value = self.success()
            self.assertEqual(self.send()["state"], "Accepted")
            self.assertEqual(json.loads(self.post.call_args.kwargs["data"]), self.payload)

    def catalog_payload(self, kind="catalog_message"):
        actions = {
            "catalog_message": {"name": "catalog_message"},
            "product": {"catalog_id": "123456", "product_retailer_id": "SKU uno"},
            "product_list": {"catalog_id": "123456", "sections": [
                {"product_items": [{"product_retailer_id": "SKU uno"}]}]},
        }
        interactive = {"type": kind, "body": {"text": "Catálogo disponible"}, "action": actions[kind]}
        if kind == "product_list":
            interactive["header"] = {"type": "text", "text": "Productos"}
        return {"messaging_product": "whatsapp", "to": self.intent.peer_id,
                "type": "interactive", "interactive": interactive}

    def validate_catalog(self, payload):
        return gateway.validate_payload(payload, account_id=self.intent.account_id, peer_id=self.intent.peer_id)

    def assert_catalog_invalid(self, payload):
        with self.assertRaisesRegex(ValueError, "^frozen_payload_invalid$"):
            self.validate_catalog(payload)
        self.lookup.assert_not_called()
        self.load.assert_not_called()
        self.post.assert_not_called()
        self.authority.assert_not_called()

    def test_catalog_messages_validate_canonical_bytes_without_resolving_or_mutating(self):
        for kind in ("catalog_message", "product", "product_list"):
            payload = self.catalog_payload(kind)
            original = copy.deepcopy(payload)
            self.assertEqual(json.loads(self.validate_catalog(payload)), original)
            self.assertEqual(payload, original)
        self.lookup.assert_not_called()
        self.load.assert_not_called()
        self.post.assert_not_called()
        self.authority.assert_not_called()

    def test_catalog_thumbnail_is_optional_but_empty_or_ambiguous_parameters_are_rejected(self):
        payload = self.catalog_payload()
        self.validate_catalog(payload)
        payload["interactive"]["action"]["parameters"] = {"thumbnail_product_retailer_id": "SKU con espacio"}
        self.validate_catalog(payload)
        for parameters in ({}, None, [], {"thumbnail_product_retailer_id": ""},
                           {"thumbnail_product_retailer_id": "   "},
                           {"thumbnail_product_retailer_id": "SKU", "catalog_id": "123456"}):
            with self.subTest(parameters=parameters):
                payload["interactive"]["action"]["parameters"] = parameters
                self.assert_catalog_invalid(payload)

    def test_single_product_body_and_footer_are_optional_and_headers_are_forbidden(self):
        payload = self.catalog_payload("product")
        del payload["interactive"]["body"]
        self.validate_catalog(payload)
        payload["interactive"]["footer"] = {"text": "Consulta disponibilidad"}
        self.validate_catalog(payload)
        for kind in ("product", "catalog_message"):
            payload = self.catalog_payload(kind)
            for header in ({"type": "text", "text": "Forbidden"}, {"type": "image", "image": {"id": "x"}}, None):
                payload["interactive"]["header"] = header
                self.assert_catalog_invalid(payload)

    def test_product_list_requires_a_text_header_and_nonempty_body(self):
        for header in (None, {}, {"type": "image", "image": {"id": "x"}},
                       {"type": "text"}, {"type": "text", "text": ""},
                       {"type": "text", "text": "x" * 61}):
            payload = self.catalog_payload("product_list")
            if header is None:
                del payload["interactive"]["header"]
            else:
                payload["interactive"]["header"] = header
            self.assert_catalog_invalid(payload)
        for kind in ("catalog_message", "product_list"):
            payload = self.catalog_payload(kind)
            del payload["interactive"]["body"]
            self.assert_catalog_invalid(payload)

    def test_product_list_allows_ten_sections_and_thirty_distinct_products_total(self):
        payload = self.catalog_payload("product_list")
        sections = [{"title": f"Section {i}", "product_items": [
            {"product_retailer_id": f"SKU-{i}-{j}"} for j in range(3)]} for i in range(10)]
        payload["interactive"]["action"]["sections"] = sections
        self.assertEqual(json.loads(self.validate_catalog(payload)), payload)
        sections[-1]["product_items"].append({"product_retailer_id": "thirty-first"})
        self.assert_catalog_invalid(payload)
        sections[-1]["product_items"].pop()
        sections.append({"title": "Eleventh", "product_items": [{"product_retailer_id": "one-more"}]})
        self.assert_catalog_invalid(payload)

    def test_product_list_titles_are_optional_for_one_section_and_required_for_multiple(self):
        payload = self.catalog_payload("product_list")
        self.validate_catalog(payload)
        sections = payload["interactive"]["action"]["sections"]
        sections.append({"title": "Second", "product_items": [{"product_retailer_id": "second"}]})
        self.assert_catalog_invalid(payload)
        sections[0]["title"] = "T" * 24
        self.validate_catalog(payload)
        for title in ("", "   ", "T" * 25, None, []):
            sections[0]["title"] = title
            self.assert_catalog_invalid(payload)

    def test_product_list_rejects_duplicate_skus_within_and_across_sections(self):
        payload = self.catalog_payload("product_list")
        sections = payload["interactive"]["action"]["sections"]
        sections[0]["product_items"].append({"product_retailer_id": "SKU uno"})
        self.assert_catalog_invalid(payload)
        sections[0]["product_items"].pop()
        sections[0]["title"] = "One"
        sections.append({"title": "Two", "product_items": [{"product_retailer_id": "SKU uno"}]})
        self.assert_catalog_invalid(payload)

    def test_product_ids_remain_exact_and_blank_control_or_overlong_ids_are_rejected(self):
        for sku in ("SKU con espacio", "áéñ-零", "x" * 140):
            payload = self.catalog_payload("product")
            payload["interactive"]["action"]["product_retailer_id"] = sku
            self.assertEqual(json.loads(self.validate_catalog(payload))["interactive"]["action"]["product_retailer_id"], sku)
        for sku in ("", " ", " SKU", "SKU ", "SKU\tuno", "SKU\nuno", "SKU\x7f", "x" * 141, None, 123, True, []):
            for kind in ("product", "product_list", "catalog_message"):
                payload = self.catalog_payload(kind)
                action = payload["interactive"]["action"]
                if kind == "product":
                    action["product_retailer_id"] = sku
                elif kind == "product_list":
                    action["sections"][0]["product_items"][0]["product_retailer_id"] = sku
                else:
                    action["parameters"] = {"thumbnail_product_retailer_id": sku}
                self.assert_catalog_invalid(payload)
        for catalog in ("", " ", "123 ", "abc", 123, True, [], "1" * 41):
            payload = self.catalog_payload("product")
            payload["interactive"]["action"]["catalog_id"] = catalog
            self.assert_catalog_invalid(payload)

    def test_catalog_subobjects_reject_unknown_keys_without_frozen_payload_rewriting(self):
        for kind in ("catalog_message", "product", "product_list"):
            for path in ((), ("action",), ("body",)):
                payload = self.catalog_payload(kind)
                node = payload["interactive"]
                for key in path:
                    node = node[key]
                node["unrecognized"] = "never sent"
                original = copy.deepcopy(payload)
                self.assert_catalog_invalid(payload)
                self.assertEqual(payload, original)
        for path in (("header",), ("action", "sections", 0),
                     ("action", "sections", 0, "product_items", 0)):
            payload = self.catalog_payload("product_list")
            node = payload["interactive"]
            for key in path:
                node = node[key]
            node["unrecognized"] = "never sent"
            self.assert_catalog_invalid(payload)
        payload = self.catalog_payload()
        payload["interactive"]["action"]["name"] = "other_action"
        self.assert_catalog_invalid(payload)

    def test_catalog_text_bounds_and_malformed_sections_fail_before_provider_lookup(self):
        for kind in ("catalog_message", "product", "product_list"):
            payload = self.catalog_payload(kind)
            payload["interactive"]["body"]["text"] = "B" * 1024
            payload["interactive"]["footer"] = {"text": "F" * 60}
            self.validate_catalog(payload)
            payload["interactive"]["body"]["text"] += "!"
            self.assert_catalog_invalid(payload)
            payload["interactive"]["body"]["text"] = "Valid"
            payload["interactive"]["footer"]["text"] += "!"
            self.assert_catalog_invalid(payload)
        for sections in (None, {}, [], [None], [{}], [{"product_items": []}],
                         [{"product_items": {}}], [{"product_items": [None]}],
                         [{"product_items": [{}]}]):
            payload = self.catalog_payload("product_list")
            payload["interactive"]["action"]["sections"] = sections
            self.assert_catalog_invalid(payload)

    def test_catalog_payloads_use_existing_frozen_submission_path_with_fake_provider_only(self):
        for kind in ("catalog_message", "product", "product_list"):
            self.payload = self.catalog_payload(kind)
            self.post.return_value = self.success()
            self.assertEqual(self.send()["state"], "Accepted")
            self.assertEqual(json.loads(self.post.call_args.kwargs["data"]), self.payload)
            self.assertIs(self.post.call_args.args[0], self.account)
        self.assertEqual(self.post.call_count, 3)

    def test_definitive_graph_rejection_is_failed_and_safe(self):
        for status in (400, 401, 403, 404, 409, 429):
            self.post.return_value = Response(status, {"error": {"code": 190, "message": "secret token and customer body", "error_data": {"secret": "value"}}})
            result = self.send()
            self.assertEqual(result, {"state": "Failed", "reason_code": "provider_rate_limited" if status == 429 else "provider_rejected", "retryable": status == 429})
            self.assertNotIn("secret", json.dumps(result))
            self.assertTrue(self.post.return_value.closed)

    def test_server_error_and_redirect_are_unknown_without_read_or_retry(self):
        for status in (301, 307, 500, 503):
            self.post.return_value = Response(status, chunks=[AssertionError("Must not read redirect/server body")])
            before = self.post.call_count
            result = self.send()
            self.assertEqual(result["state"], "Unknown")
            self.assertIs(result["retryable"], False)
            self.assertEqual(self.post.call_count, before + 1)
            self.assertEqual(self.post.return_value.consumed, 0)
            self.assertTrue(self.post.return_value.closed)

    def test_timeout_connection_and_stream_failure_are_unknown(self):
        for failure in (TimeoutError("secret URL"), ConnectionError("request body"), RuntimeError("unexpected transport error")):
            self.post.side_effect = failure
            self.assertEqual(self.send(), {"state": "Unknown", "reason_code": "provider_response_uncertain", "retryable": False})
        self.post.side_effect = None
        self.post.return_value = Response(chunks=[b'{"messages":', TimeoutError("secret")])
        self.assertEqual(self.send()["state"], "Unknown")
        self.assertTrue(self.post.return_value.closed)

    def test_malformed_success_unproven_rejection_and_wrong_peer_are_unknown(self):
        for response in (Response(payload={}), Response(204, chunks=[]), Response(400, {"error": "wrong"}),
                         Response(400, {"error": {"code": True}}), Response(429, {"error": {"message": "no code"}}),
                         self.success(peer="5215550009999"), self.success(message_id=""),
                         self.success(message_id="wamid.demo-synthetic"), self.success(message_id="wamid.bad\nid"),
                         Response(chunks=[b"not-json"]), Response(payload={}, headers={"Content-Type": "text/html"})):
            self.post.return_value = response
            self.assertEqual(self.send()["state"], "Unknown")
            self.assertTrue(response.closed)

    def test_echoed_input_accepts_metas_canonical_id_but_nothing_else(self):
        sent = self.intent.peer_id
        canonical = sent[:2] + "1" + sent[2:]  # Meta's id for the same mobile (e.g. 52… sent, 521… returned)
        cases = {
            "echoed input, canonical id": ({"input": sent, "wa_id": canonical}, "Accepted"),
            "no echo, id differs": ({"wa_id": canonical}, "Unknown"),
            "echo of another number": ({"input": canonical, "wa_id": canonical}, "Unknown"),
            "echoed input, id not a number": ({"input": sent, "wa_id": "other"}, "Unknown"),
            "echoed input, id missing": ({"input": sent}, "Unknown"),
        }
        for label, (contact, state) in cases.items():
            with self.subTest(label):
                self.post.return_value = Response(payload={"messaging_product": "whatsapp", "contacts": [contact],
                                                           "messages": [{"id": "wamid.canonical-fixture"}]})
                self.assertEqual(self.send()["state"], state)

    def test_response_cap_prevents_buffering_and_closes_stream(self):
        responses = [Response(headers={"Content-Type": "application/json", "Content-Length": str(gateway.MAX_RESPONSE_BYTES + 1)}, chunks=[b"unused"]),
                     Response(chunks=[b"x" * 8192] * 9 + [AssertionError("past cap")])]
        for response in responses:
            self.post.return_value = response
            self.assertEqual(self.send()["state"], "Unknown")
            self.assertTrue(response.closed)
        self.assertEqual(responses[0].consumed, 0)
        self.assertEqual(responses[1].consumed, 9)

    def test_slow_drip_deadline_is_unknown_and_stops_consuming(self):
        response = Response(chunks=[b"{", b"unused"])
        self.post.return_value = response
        with patch.object(gateway.time, "monotonic", side_effect=[0, 1, 31]):
            self.assertEqual(self.send()["state"], "Unknown")
        self.assertEqual(response.consumed, 1)
        self.assertTrue(response.closed)

    def test_duplicate_json_keys_cannot_hide_ambiguous_acceptance(self):
        self.post.return_value = Response(chunks=[b'{"error":{"code":190},"error":null,"messages":[{"id":"wamid.one"}]}'])
        self.assertEqual(self.send()["state"], "Unknown")
        self.assertTrue(self.post.return_value.closed)

    def test_synthetic_and_multiple_recipient_acceptance_are_not_live_success(self):
        for message_id in ("wamid.demo-abc", "not-a-provider-id", "wamid."):
            self.post.return_value = self.success(message_id=message_id)
            self.assertEqual(self.send()["state"], "Unknown")
        self.post.return_value = Response(payload={"messaging_product": "whatsapp", "contacts": [
            {"wa_id": self.intent.peer_id}, {"wa_id": self.intent.peer_id}], "messages": [{"id": "wamid.one"}]})
        self.assertEqual(self.send()["state"], "Unknown")

    def test_closed_response_error_does_not_erase_known_acceptance(self):
        self.post.return_value.close = Mock(side_effect=RuntimeError("close failed"))
        self.assertEqual(self.send(), {"state": "Accepted", "provider_message_id": "wamid.real-fixture"})
