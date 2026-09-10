"""The legacy status edge may trust only an actual native receipt fold."""

import copy
import hashlib
import unittest
from datetime import timedelta
from unittest.mock import patch

import frappe
from frappe.utils import now_datetime

from frappe_whatsapp import delivery
from frappe_whatsapp.webhook_receipts import ReceiptError, canonical, digest


class TestDeliveryBridge(unittest.TestCase):
    def setUp(self):
        self.entry = {"id": "wamid.exact", "status": "delivered", "recipient_id": "5215550100999", "timestamp": "1788980400"}
        payload = canonical({"business_id": "990002", "change": {"field": "messages", "value": {
            "messaging_product": "whatsapp", "metadata": {"phone_number_id": "990001"}, "statuses": [self.entry]}}})
        self.receipt = frappe._dict(provider="WhatsApp", account_id="990001", app_id="990003", event_type="status",
            event_id=digest([self.entry["id"], self.entry["status"], self.entry["timestamp"]]), payload=payload,
            payload_hash=hashlib.sha256(payload.encode()).hexdigest(), state="Processing",
            attempts=1, lease_until=now_datetime() + timedelta(minutes=5))
        self.receipt.name = self.receipt.event_key = digest([self.receipt[k] for k in ("provider", "app_id", "account_id", "event_type", "event_id")])
        self.enterContext(patch.object(frappe, "request", None))
        self.enterContext(patch.object(frappe, "flags", frappe._dict(meta_webhook_receipt=self.receipt.name)))
        self.read = self.enterContext(patch.object(frappe.db, "get_value", return_value=self.receipt))
        self.apps = self.enterContext(patch.object(frappe, "get_installed_apps", return_value=["frappe", "crm", "frappe_whatsapp"]))
        self.exists = self.enterContext(patch.object(frappe.db, "exists", return_value=True))
        self.fold = self.enterContext(patch("crm.api.outbox_delivery.apply_delivery_receipt",
            return_value={"matched": True, "intent_name": "exact-intent", "intent_state": "Delivered", "reason_code": "native_delivery_applied"}))

    def call(self, entry=None, accounts=None):
        return delivery.fold_native_delivery(self.entry if entry is None else entry, ["exact-account"] if accounts is None else accounts)

    def test_actual_receipt_and_scope_are_passed_to_core_before_legacy_lookup(self):
        result = self.call()
        self.assertTrue(result["matched"])
        self.fold.assert_called_once_with(self.receipt.name, expected_entry=self.entry, account_records=("exact-account",))
        self.assertTrue(self.read.call_args.kwargs["for_update"])
        self.assertEqual(self.read.call_args.args[0], "Meta Webhook Receipt")

    def test_missing_target_does_not_claim_legacy_status_success(self):
        self.fold.return_value = {"matched": False, "reason_code": "native_delivery_target_missing"}
        self.assertEqual(self.call(), self.fold.return_value)

    def test_no_http_or_flag_only_authority(self):
        with patch.object(frappe, "request", object()), self.assertRaisesRegex(ReceiptError, "native_delivery_worker_required"):
            self.call()
        frappe.flags.meta_webhook_receipt = None
        with self.assertRaisesRegex(ReceiptError, "native_delivery_worker_required"):
            self.call()
        frappe.flags.meta_webhook_receipt = "forged"
        with self.assertRaises(ReceiptError):
            self.call()
        self.fold.assert_not_called()

    def test_changed_status_id_peer_or_timestamp_cannot_grant_missing_wm_success(self):
        for changes in ({"status": "read"}, {"id": "wamid.other"}, {"recipient_id": "5215550100000"}, {"timestamp": "1788980401"}):
            with self.assertRaises(ReceiptError):
                self.call(entry={**self.entry, **changes})
        self.fold.assert_not_called()

    def test_nonprocessing_wrong_event_and_tampered_receipt_are_denied(self):
        original = copy.deepcopy(self.receipt)
        for field, value in (("state", "Processed"), ("event_type", "message"), ("payload_hash", "0" * 64), ("event_key", "0" * 64)):
            self.receipt[field] = value
            with self.assertRaises(ReceiptError):
                self.call()
            self.receipt[field] = original[field]
        self.fold.assert_not_called()

    def test_ambiguous_account_list_never_reaches_core(self):
        for accounts in ([], ["one", "two"], "exact-account"):
            with self.assertRaises(ReceiptError):
                self.call(accounts=accounts)
        self.fold.assert_not_called()

    def test_processing_flag_without_live_attempt_cannot_grant_delivery(self):
        original = copy.deepcopy(self.receipt)
        for field, value in (("attempts", 0), ("attempts", True), ("lease_until", None), ("lease_until", "invalid"),
                             ("lease_until", now_datetime() - timedelta(seconds=1))):
            self.receipt[field] = value
            with self.assertRaisesRegex(ReceiptError, "native_delivery_claim_required"):
                self.call()
            self.receipt[field] = original[field]
        self.fold.assert_not_called()

    def test_absent_crm_or_native_schema_leaves_legacy_folding_available(self):
        self.apps.return_value = ["frappe", "frappe_whatsapp"]
        self.assertEqual(self.call(), {"matched": False, "reason_code": "native_delivery_schema_unavailable"})
        self.apps.return_value.append("crm")
        self.exists.return_value = False
        self.assertFalse(self.call()["matched"])
        self.fold.assert_not_called()

    def test_core_failure_propagates_without_commit_or_rollback(self):
        self.fold.side_effect = RuntimeError("fictional fold failure")
        with patch.object(frappe.db, "commit") as commit, patch.object(frappe.db, "rollback") as rollback:
            with self.assertRaises(RuntimeError):
                self.call()
            commit.assert_not_called()
            rollback.assert_not_called()
