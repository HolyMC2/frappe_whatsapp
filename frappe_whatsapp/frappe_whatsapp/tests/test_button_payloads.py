"""Button transport contract: fictional provider fixtures, no site or HTTP writes."""
import copy
import json
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

import frappe

from frappe_whatsapp import transport, webhook_receipts
from frappe_whatsapp.utils import webhook


FIXTURES = Path(__file__).parent / "fixtures" / "button_replies.json"


def fixture(name):
    return json.loads(FIXTURES.read_text())[name]


def process_inbound(change, handler=None, live=True):
    """Run the real consumer/hook seam against captured rows; reusable by roundtrips."""
    rows, snapshots = [], []
    account = frappe._dict(name="account-a", token="never-expose-account-token")
    scoped = frappe._dict(change=copy.deepcopy(change), accounts=(account.name,))

    def get_doc(values, name=None):
        if values == "WhatsApp Account":
            assert name == account.name
            return account
        assert isinstance(values, dict)
        assert values["doctype"] in {"WhatsApp Message", "WhatsApp Notification Log"}
        doc = frappe._dict(values)

        def insert(**kwargs):
            assert kwargs == {"ignore_permissions": True}
            if doc.doctype == "WhatsApp Message":
                doc.name = "message-row-" + str(len(rows))
                rows.append(dict(doc))
            return doc

        doc.insert = insert
        return doc

    def get_value(doctype, name, fields=None, as_dict=False):
        assert doctype == "WhatsApp Message"
        if isinstance(name, dict):
            assert name["whatsapp_account"] == account.name
            return next((row["name"] for row in rows if row["message_id"] == name["message_id"]), None)
        assert as_dict
        row = next(row for row in rows if row["name"] == name)
        return frappe._dict({field: row.get(field) for field in fields})

    def committed(snapshot):
        snapshots.append(snapshot.copy())
        return handler(snapshot) if handler else False

    with ExitStack() as stack:
        stack.enter_context(patch.object(frappe, "get_doc", side_effect=get_doc))
        stack.enter_context(patch.object(frappe.db, "get_value", side_effect=get_value))
        for name in ("commit", "rollback", "set_value", "sql"):
            stack.enter_context(patch.object(frappe.db, name, side_effect=AssertionError("Unexpected DB write")))
        stack.enter_context(patch.object(frappe, "get_hooks", return_value=["test.domain.handler"], create=True))
        stack.enter_context(patch.object(frappe, "get_attr", return_value=committed, create=True))
        stack.enter_context(patch.object(webhook_receipts, "incoming_is_live", return_value=live))
        stack.enter_context(patch.object(transport, "raw", side_effect=AssertionError("Unexpected HTTP")))
        stack.enter_context(patch("requests.sessions.Session.request", side_effect=AssertionError("Unexpected HTTP")))
        webhook.process_change(scoped)
    # The fake persistence method is an implementation detail, not row data.
    for row in rows:
        row.pop("insert", None)
    return rows, snapshots


class TestButtonPayloads(unittest.TestCase):
    def test_template_payload_is_lossless_and_visible_label_is_preserved(self):
        change = fixture("template_quick_reply")
        message = change["value"]["messages"][0]
        payload = message["button"]["payload"]
        self.assertGreater(len(payload), 140)
        rows, snapshots = process_inbound(change)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["message"], "Acepto")
        self.assertEqual(rows[0]["button_payload"], payload)
        self.assertEqual(snapshots[0]["message"], "Acepto")
        self.assertEqual(snapshots[0]["button_payload"], payload)
        self.assertEqual(snapshots[0]["reply_to_message_id"], "wamid.outbound-template")
        self.assertTrue(snapshots[0]["is_reply"])
        self.assertEqual(rows[0]["profile_name"], "Fictional Customer")

    def test_interactive_ids_keep_existing_message_contract(self):
        for name, payload in (("interactive_button_reply", "sn:opaque-staff-notice-token"),
                              ("interactive_list_reply", "existing-list-id")):
            with self.subTest(name=name):
                rows, snapshots = process_inbound(fixture(name))
                self.assertEqual(rows[0]["message"], payload)
                self.assertEqual(rows[0]["button_payload"], payload)
                self.assertEqual(rows[0]["content_type"], "button")
                self.assertEqual(snapshots[0]["message"], payload)
                self.assertEqual(snapshots[0]["button_payload"], payload)
                self.assertEqual(snapshots[0]["reply_to_message_id"], rows[0]["reply_to_message_id"])
                self.assertTrue(snapshots[0]["is_reply"])

    def test_old_label_only_template_reply_still_ingests(self):
        change = fixture("template_quick_reply")
        del change["value"]["messages"][0]["button"]["payload"]
        rows, snapshots = process_inbound(change)
        self.assertEqual(rows[0]["message"], "Acepto")
        self.assertIsNone(rows[0]["button_payload"])
        self.assertEqual(snapshots[0]["message"], "Acepto")
        self.assertIsNone(snapshots[0]["button_payload"])

    def test_plain_text_backward_compatibility(self):
        rows, snapshots = process_inbound(fixture("plain_text"))
        self.assertEqual(rows[0]["message"], "Existing text")
        self.assertNotIn("button_payload", rows[0])
        self.assertEqual(snapshots[0]["message"], "Existing text")
        self.assertIsNone(snapshots[0]["button_payload"])
        self.assertIsNone(snapshots[0]["reply_to_message_id"])
        self.assertFalse(snapshots[0]["is_reply"])

    def test_missing_context_id_does_not_interrupt_ingestion(self):
        for context in (None, {}, {"from": "15555550200"}, {"id": None}, {"id": ""},
                        {"forwarded": True}, {"forwarded": False, "id": "wamid.forwarded"}):
            with self.subTest(context=context):
                change = fixture("template_quick_reply")
                change["value"]["messages"][0]["context"] = context
                rows, snapshots = process_inbound(change)
                self.assertEqual(rows[0]["message"], "Acepto")
                self.assertFalse(rows[0]["is_reply"])
                self.assertFalse(snapshots[0]["is_reply"])
                self.assertFalse(snapshots[0]["reply_to_message_id"])

    def test_hook_exposes_account_context_and_preserves_return_contract(self):
        for live in (True, False):
            seen = []

            def handler(snapshot):
                seen.append(snapshot.copy())
                return True

            rows, snapshots = process_inbound(fixture("template_quick_reply"), handler=handler, live=live)
            self.assertEqual(seen, snapshots)
            snapshot = seen[0]
            self.assertEqual(snapshot["name"], rows[0]["name"])
            self.assertEqual(snapshot["whatsapp_account"], "account-a")
            self.assertEqual(snapshot["phone_id"], "phone-a")
            self.assertEqual(snapshot["from"], "15555550100")
            self.assertEqual(snapshot["live"], live)
            self.assertNotIn("token", snapshot)
            self.assertNotIn("never-expose-account-token", repr(snapshot))

    def test_hook_claim_return_and_provider_fallback(self):
        message = fixture("template_quick_reply")["value"]["messages"][0]
        with patch.object(frappe.db, "get_value", return_value=None), \
                patch.object(webhook_receipts, "incoming_is_live", return_value=True), \
                patch.object(webhook_receipts, "incoming_committed", return_value=True) as committed:
            claimed = webhook._incoming_extension(message, frappe._dict(name="account-a"), "phone-a")
        self.assertTrue(claimed)
        snapshot = committed.call_args.args[0]
        self.assertEqual(snapshot["message"], "Acepto")
        self.assertEqual(snapshot["button_payload"], message["button"]["payload"])
        self.assertEqual(snapshot["reply_to_message_id"], "wamid.outbound-template")
        self.assertTrue(snapshot["is_reply"])

    def test_schema_has_untruncated_read_only_payload(self):
        path = Path(__file__).parents[1] / "doctype" / "whatsapp_message" / "whatsapp_message.json"
        schema = json.loads(path.read_text())
        field = next(field for field in schema["fields"] if field["fieldname"] == "button_payload")
        self.assertEqual(field["fieldtype"], "Long Text")
        self.assertEqual(field["read_only"], 1)
        self.assertIn("button_payload", schema["field_order"])
