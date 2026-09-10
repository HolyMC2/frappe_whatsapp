"""WA atom-to-control seam with bounded trusted receipt doubles."""

import copy
import hashlib
import json
import unittest
from datetime import timedelta
from unittest.mock import patch

import frappe
from frappe.utils import now_datetime

from frappe_whatsapp import customer_activity as activity
from frappe_whatsapp.utils.signature import ScopedChange
from frappe_whatsapp.webhook_receipts import ReceiptError, canonical, digest


class TestCustomerActivity(unittest.TestCase):
    def setUp(self):
        self.account, self.peer = "910001", "5215550100999"
        self.message = {"id": "wamid.customer", "from": self.peer, "timestamp": "1788980400", "type": "text", "text": {"body": "raw receipt only"}}
        self.change = {"field": "messages", "value": {"messaging_product": "whatsapp", "metadata": {"phone_number_id": self.account}, "messages": [self.message]}}
        self.make_receipt()
        self.enterContext(patch.object(frappe, "request", None))
        self.enterContext(patch.object(frappe, "flags", frappe._dict(meta_webhook_receipt=self.receipt.name)))
        self.read = self.enterContext(patch.object(frappe.db, "get_value", side_effect=lambda *a, **k: copy.deepcopy(self.receipt)))
        self.ready = self.enterContext(patch.object(activity, "_ready", return_value=True))
        self.core = self.enterContext(patch("crm.api.conversation_activity.internal_apply_customer_activity", return_value={"state": "Processed", "reason_code": "customer_reply_held_bot"}))

    def make_receipt(self, *, event_type="message"):
        payload = canonical({"business_id": "910002", "change": self.change})
        self.receipt = frappe._dict(provider="WhatsApp", account_id=self.account, app_id="910003", event_type=event_type,
            event_id=self.message["id"], payload=payload, payload_hash=hashlib.sha256(payload.encode()).hexdigest(), state="Processing",
            attempts=1, lease_until=now_datetime() + timedelta(minutes=5))
        self.receipt.name = self.receipt.event_key = digest([self.receipt[key] for key in ("provider", "app_id", "account_id", "event_type", "event_id")])
        self.scoped = ScopedChange("910002", "910003", ("exact-account",), copy.deepcopy(self.change))
        if getattr(self, "read", None):
            frappe.flags.meta_webhook_receipt = self.receipt.name

    def consume(self):
        return activity.consume_customer_activity(self.receipt, self.scoped)

    def test_exact_provider_timestamp_and_receipt_forwarded_without_content(self):
        result = self.consume()
        self.assertEqual(result["reason_code"], "customer_reply_held_bot")
        self.core.assert_called_once_with("WhatsApp", self.account, self.peer, receipt_name=self.receipt.name, provider_timestamp=1788980400)
        self.assertIs(self.read.call_args.kwargs["for_update"], True)
        self.assertNotIn("raw receipt only", repr(self.core.call_args))

    def test_http_or_forged_worker_context_denied_before_storage_or_core(self):
        with patch.object(frappe, "request", object()), self.assertRaisesRegex(ReceiptError, "customer_activity_worker_required"):
            self.consume()
        frappe.flags.meta_webhook_receipt = "wrong"
        with self.assertRaisesRegex(ReceiptError, "customer_activity_worker_required"):
            self.consume()
        self.read.assert_not_called()
        self.core.assert_not_called()

    def test_caller_changed_receipt_cannot_replace_durable_atom(self):
        actual = copy.deepcopy(self.receipt)
        self.read.side_effect = None
        self.read.return_value = actual
        self.receipt.payload = self.receipt.payload.replace("raw receipt only", "replacement")
        with self.assertRaises(ReceiptError):
            self.consume()
        self.core.assert_not_called()

    def test_scope_app_business_change_or_multiple_accounts_is_rejected(self):
        scopes = [ScopedChange("other", "910003", ("exact-account",), self.change),
                  ScopedChange("910002", "other", ("exact-account",), self.change),
                  ScopedChange("910002", "910003", ("a", "b"), self.change),
                  ScopedChange("910002", "910003", ("exact-account",), {"field": "history", "value": {}})]
        for scoped in scopes:
            self.scoped = scoped
            with self.assertRaises(ReceiptError):
                self.consume()
        self.core.assert_not_called()

    def test_later_invalid_sibling_and_mixed_atom_cannot_reach_control(self):
        self.change["value"]["messages"].append(copy.deepcopy(self.message))
        self.make_receipt()
        with self.assertRaises(ReceiptError):
            self.consume()
        self.change["value"]["messages"].pop()
        for key in ("statuses", "message_echoes", "history", "state_sync"):
            self.change["value"][key] = []
            self.make_receipt()
            with self.assertRaises(ReceiptError):
                self.consume()
            del self.change["value"][key]
        self.core.assert_not_called()

    def test_missing_millisecond_bool_and_noncanonical_timestamp_rejected(self):
        for timestamp in (None, True, 1788980400000, " 1788980400", "1e9", "-1", 0):
            self.message["timestamp"] = timestamp
            self.make_receipt()
            with self.assertRaises(ReceiptError):
                self.consume()
        self.core.assert_not_called()

    def test_integer_provider_seconds_are_supported_without_receipt_time_substitution(self):
        self.message["timestamp"] = 1788980400
        self.make_receipt()
        self.receipt.received_at = "2099-01-01 00:00:00"
        self.consume()
        self.assertEqual(self.core.call_args.kwargs["provider_timestamp"], 1788980400)

    def test_non_numeric_peer_and_account_are_rejected_without_normalization(self):
        for peer in ("+5215550100999", " 5215550100999", "staff@example.test", 5215550100999):
            self.message["from"] = peer
            self.make_receipt()
            with self.assertRaises(ReceiptError):
                self.consume()
        self.core.assert_not_called()

    def test_history_echo_and_nonprocessing_receipt_are_rejected(self):
        for event_type in ("history", "smb_message_echoes", "status"):
            self.make_receipt(event_type=event_type)
            with self.assertRaises(ReceiptError):
                self.consume()
        self.make_receipt()
        self.receipt.state = "Processed"
        with self.assertRaises(ReceiptError):
            self.consume()
        self.core.assert_not_called()

    def test_missing_core_schema_preserves_raw_intake_without_claiming_hold(self):
        self.ready.return_value = False
        self.assertEqual(self.consume(), {"state": "Ignored", "reason_code": "customer_activity_control_unavailable"})
        self.core.assert_not_called()

    def test_processing_without_actual_attempt_or_live_lease_is_denied(self):
        for values in ({"attempts": 0}, {"attempts": True}, {"lease_until": None}, {"lease_until": "invalid"},
                       {"lease_until": now_datetime() - timedelta(seconds=1)}):
            self.make_receipt()
            self.receipt.update(values)
            with self.assertRaisesRegex(ReceiptError, "customer_activity_claim_required"):
                self.consume()
        self.core.assert_not_called()

    def test_core_failure_propagates_to_outer_receipt_without_rollback(self):
        self.core.side_effect = RuntimeError("fictional core failure")
        with patch.object(frappe.db, "rollback") as rollback, patch.object(frappe.db, "commit") as commit:
            with self.assertRaises(RuntimeError):
                self.consume()
            rollback.assert_not_called()
            commit.assert_not_called()

    def test_generic_replay_flag_does_not_suppress_trusted_customer_reply(self):
        frappe.flags.meta_webhook_replay = True
        self.consume()
        self.core.assert_called_once()
