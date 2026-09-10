"""Actual signed admission and receipt worker; SQL commit checkpoints stay isolated.

Only transaction boundaries are mapped to savepoints. Authentication, consumer,
core controls, native delivery and ordinary message projection are real code.
"""

import json
import time
import unittest
from datetime import datetime, timezone, timedelta
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

import frappe
from frappe.utils import convert_utc_to_system_timezone, now_datetime

from crm.api import conversations as control
from crm.api import outbox
from frappe_whatsapp import webhook_receipts as receipts
from frappe_whatsapp.utils import signature, webhook


def system_time(seconds):
    return convert_utc_to_system_timezone(datetime.fromtimestamp(seconds, timezone.utc).replace(tzinfo=None)).replace(tzinfo=None)


class TestReceiptPipelineSql(unittest.TestCase):
    def setUp(self):
        self.point = "receipt_pipeline_" + uuid4().hex
        frappe.db.savepoint(self.point)
        self.addCleanup(frappe.db.rollback, save_point=self.point)
        self.addCleanup(frappe.set_user, frappe.session.user)
        self.callbacks = list(frappe.db.after_commit._functions)
        self.addCleanup(self.restore_callbacks)
        frappe.set_user("Administrator")
        self.now = int(time.time()) - 5
        self.peer = "52" + str(int(uuid4().hex[:12], 16))
        self.account = frappe.get_doc({"doctype": "WhatsApp Account", "account_name": "pipeline-" + uuid4().hex,
            "phone_id": "95" + str(int(uuid4().hex[:12], 16)), "app_id": "997701", "business_id": "997702",
            "status": "Active", "mode": "Live", "version": "v23.0", "url": "https://graph.facebook.com",
            "token": "fictional-unused", "app_secret": "fictional-pipeline-secret", "allow_auto_read_receipt": 0}).insert(ignore_permissions=True)
        self.conversation = control.get_or_create("WhatsApp", self.account.phone_id, self.peer)
        control.apply_control(self.conversation.name, "take", 1, uuid4().hex)
        self.enterContext(patch.object(frappe, "request", None))
        self.enterContext(patch.object(signature, "_accounts", return_value=[self.account]))
        self.transport = self.enterContext(patch("frappe_whatsapp.transport.raw", side_effect=AssertionError("No provider requests")))
        self.enterContext(patch("frappe_whatsapp.transport.api", side_effect=AssertionError("No provider sends")))
        self.enterContext(patch.object(frappe, "enqueue", side_effect=AssertionError("No volatile worker dispatch")))
        self.enterContext(patch.object(frappe, "sendmail", side_effect=AssertionError("No mail")))

    def restore_callbacks(self):
        frappe.db.after_commit._functions.clear()
        frappe.db.after_commit._functions.extend(self.callbacks)

    def seed_bot(self):
        frappe.db.set_value(control.DOCTYPE, self.conversation.name,
            {"control_state": "Bot", "bot_enabled": 1, "human_owner": None, "generation": 7,
             "modified": system_time(self.now - 60)}, update_modified=False)

    def message(self, mid=None, *, kind="text"):
        return {"id": mid or "wamid.inbound-" + uuid4().hex, "from": self.peer, "timestamp": str(self.now),
                "type": kind, kind: {"body": "Fictional customer reply"} if kind == "text" else {"id": "fictional-media"}}

    def status(self, mid, state="delivered"):
        return {"id": mid, "recipient_id": self.peer, "timestamp": str(self.now), "status": state}

    def admit(self, messages=(), statuses=()):
        value = {"messaging_product": "whatsapp", "metadata": {"phone_number_id": self.account.phone_id}}
        if messages:
            value["messages"] = list(messages)
        if statuses:
            value["statuses"] = list(statuses)
        body = json.dumps({"object": "whatsapp_business_account", "entry": [{"id": self.account.business_id,
            "changes": [{"field": "messages", "value": value}]}]}).encode()
        request = SimpleNamespace(method="POST", get_data=lambda: body,
            headers={"X-Hub-Signature-256": signature.expected_signature("fictional-pipeline-secret", body)})
        with patch.object(frappe, "request", request):
            return webhook.post()

    def run_worker(self, name):
        start = "pipeline_start_" + uuid4().hex
        claim = "pipeline_claim_" + uuid4().hex
        frappe.db.savepoint(start)
        phases, rolled_back = [], []
        real_rollback = frappe.db.rollback
        def checkpoint():
            if not phases:
                frappe.db.savepoint(claim)
            phases.append(frappe.db.get_value(receipts.DOCTYPE, name, "state"))
        def rollback(*args, **kwargs):
            if args or kwargs:
                return real_rollback(*args, **kwargs)
            rolled_back.append(True)
            return real_rollback(save_point=claim if phases else start)
        with patch.object(frappe.db, "commit", side_effect=checkpoint), patch.object(frappe.db, "rollback", side_effect=rollback):
            receipts.run_receipt(name)
        return frappe.get_doc(receipts.DOCTYPE, name), phases, rolled_back

    def native_intent(self, mid):
        name = outbox.queue_message(self.conversation.name, 2, uuid4().hex, {"type": "text", "text": "Frozen native reply"})["name"]
        with control.conversation_fence(self.conversation.name):
            doc = outbox._load(name)
            outbox._transition(doc, "Claimed", attempts=1, claim_token=uuid4().hex)
            outbox._transition(doc, "Submitting", submitted_at=system_time(self.now - 20))
            outbox._transition(doc, "Accepted", accepted_at=system_time(self.now - 19), provider_message_id=mid)
        return doc

    def legacy(self, mid, peer=None):
        doc = frappe.get_doc({"doctype": "WhatsApp Message", "name": "pipeline-legacy-" + uuid4().hex,
            "whatsapp_account": self.account.name, "type": "Outgoing", "to": peer or self.peer,
            "message_id": mid, "message": "Already sent legacy fixture", "message_type": "Manual", "content_type": "text",
            "status": "sent", "owner": "Administrator", "modified_by": "Administrator", "creation": now_datetime(),
            "modified": now_datetime(), "docstatus": 0, "idx": 0})
        doc.db_insert()  # Already-sent fixture only; never invoke outgoing transport.
        return doc

    def test_actual_worker_projects_inbound_and_holds_bot_in_one_completion(self):
        self.seed_bot()
        message = self.message()
        name = self.admit([message])[0]
        frappe.set_user("Guest")
        row, phases, rollback = self.run_worker(name)
        self.assertEqual((phases, rollback), (["Processing", "Processed"], []))
        self.assertEqual((row.state, row.reason_code, row.attempts), ("Processed", "customer_reply_held_bot", 1))
        self.assertIsNone(row.lease_until)
        self.conversation.reload()
        self.assertEqual((self.conversation.control_state, self.conversation.generation, self.conversation.bot_enabled), ("Human", 8, 0))
        doc = frappe.get_doc("WhatsApp Message", {"message_id": message["id"], "whatsapp_account": self.account.name})
        self.assertEqual((doc.type, doc.get("from"), doc.message), ("Incoming", self.peer, message["text"]["body"]))
        self.assertEqual(frappe.db.count(control.EVENT, {"source_receipt": name, "action": "customer_reply"}), 1)

    def test_domain_failure_rolls_back_hold_and_message_but_keeps_receipt_and_prior_work(self):
        self.seed_bot()
        message = self.message()
        name = self.admit([message])[0]
        raw = frappe.db.get_value(receipts.DOCTYPE, name, "payload")
        anchor = frappe.get_doc({"doctype": "ToDo", "description": "Earlier authorized request work"}).insert()
        original = webhook.process_change
        def fail_after_projection(scoped):
            original(scoped)
            raise receipts.ReceiptError("fixture_domain_failed")
        with patch.object(webhook, "process_change", side_effect=fail_after_projection):
            row, phases, rolled_back = self.run_worker(name)
        self.assertEqual(phases, ["Processing", "Failed"])
        self.assertEqual(rolled_back, [True])
        self.assertEqual((row.reason_code, row.payload), ("fixture_domain_failed", raw))
        self.assertTrue(row.next_attempt_at)
        self.assertTrue(frappe.db.exists("ToDo", anchor.name))
        self.assertFalse(frappe.db.exists("WhatsApp Message", {"message_id": message["id"]}))
        self.assertFalse(frappe.db.exists(control.EVENT, {"source_receipt": name}))
        self.conversation.reload()
        self.assertEqual((self.conversation.control_state, self.conversation.generation), ("Bot", 7))

    def test_native_only_delivery_satisfies_missing_legacy_row(self):
        mid = "wamid.native-" + uuid4().hex
        intent = self.native_intent(mid)
        name = self.admit(statuses=[self.status(mid)])[0]
        row, phases, rollback = self.run_worker(name)
        self.assertEqual((row.state, phases, rollback), ("Processed", ["Processing", "Processed"], []))
        intent.reload()
        self.assertEqual(intent.state, "Delivered")
        self.assertFalse(frappe.db.exists("WhatsApp Message", {"message_id": mid}))

    def test_missing_target_stays_failed_then_same_receipt_retries_after_native_target_exists(self):
        mid = "wamid.late-target-" + uuid4().hex
        name = self.admit(statuses=[self.status(mid)])[0]
        first, phases, rolled_back = self.run_worker(name)
        self.assertEqual((first.state, first.reason_code, first.attempts), ("Failed", "message_not_found", 1))
        self.assertEqual((phases, rolled_back), (["Processing", "Failed"], [True]))
        self.assertTrue(first.next_attempt_at)
        intent = self.native_intent(mid)
        frappe.db.set_value(receipts.DOCTYPE, name, "next_attempt_at", now_datetime() - timedelta(seconds=1), update_modified=False)
        second, phases, rolled_back = self.run_worker(name)
        self.assertEqual((second.state, second.attempts, phases, rolled_back), ("Processed", 2, ["Processing", "Processed"], []))
        self.assertEqual((first.event_key, first.payload_hash, first.payload), (second.event_key, second.payload_hash, second.payload))
        intent.reload()
        self.assertEqual(intent.state, "Delivered")

    def test_verified_recipient_updates_only_exact_legacy_row_and_native_intent(self):
        mid = "wamid.legacy-" + uuid4().hex
        intent = self.native_intent(mid)
        wrong = self.legacy(mid, self.peer + "1")
        correct = self.legacy(mid)
        name = self.admit(statuses=[self.status(mid)])[0]
        row, _, _ = self.run_worker(name)
        self.assertEqual(row.state, "Processed")
        wrong.reload()
        correct.reload()
        intent.reload()
        self.assertEqual((wrong.status, correct.status, intent.state), ("sent", "delivered", "Delivered"))

    def test_wrong_peer_legacy_row_cannot_satisfy_missing_native_target(self):
        mid = "wamid.wrong-peer-" + uuid4().hex
        wrong = self.legacy(mid, self.peer + "1")
        name = self.admit(statuses=[self.status(mid)])[0]
        row, _, _ = self.run_worker(name)
        self.assertEqual((row.state, row.reason_code), ("Failed", "message_not_found"))
        wrong.reload()
        self.assertEqual(wrong.status, "sent")

    def test_legacy_failure_rolls_back_native_delivery_in_the_same_worker(self):
        mid = "wamid.atomic-status-" + uuid4().hex
        intent = self.native_intent(mid)
        legacy = self.legacy(mid)
        name = self.admit(statuses=[self.status(mid)])[0]
        original = webhook._apply_message_status
        def fail_after_both(entry, accounts):
            original(entry, accounts)
            raise receipts.ReceiptError("fixture_legacy_failed")
        with patch.object(webhook, "_apply_message_status", side_effect=fail_after_both):
            row, _, rolled_back = self.run_worker(name)
        self.assertEqual((row.state, row.reason_code, rolled_back), ("Failed", "fixture_legacy_failed", [True]))
        legacy.reload()
        intent.reload()
        self.assertEqual((legacy.status, intent.state), ("sent", "Accepted"))
        self.assertIsNone(intent.delivered_at)

    def test_full_batch_retains_every_atom_across_independent_domain_failures(self):
        mid = "wamid.batch-native-" + uuid4().hex
        intent = self.native_intent(mid)
        self.seed_bot()
        text, media = self.message(), self.message(kind="image")
        statuses = [self.status(mid), self.status("wamid.batch-missing-" + uuid4().hex)]
        names = self.admit([text, media], statuses)
        self.assertEqual(len(names), 4)
        rows = {frappe.db.get_value(receipts.DOCTYPE, name, "event_id"): name for name in names}
        raw = {name: frappe.db.get_value(receipts.DOCTYPE, name, "payload") for name in names}
        text_row, _, _ = self.run_worker(rows[text["id"]])
        media_row, _, _ = self.run_worker(rows[media["id"]])
        status_rows = [self.run_worker(rows[receipts.digest([entry["id"], entry["status"], entry["timestamp"]])])[0] for entry in statuses]
        self.assertEqual((text_row.state, media_row.state, media_row.reason_code), ("Processed", "Failed", "media_fetch_failed"))
        self.assertEqual([row.state for row in status_rows], ["Processed", "Failed"])
        self.assertEqual(status_rows[1].reason_code, "message_not_found")
        self.assertTrue(frappe.db.exists("WhatsApp Message", {"message_id": text["id"]}))
        self.assertFalse(frappe.db.exists("WhatsApp Message", {"message_id": media["id"]}))
        for name, body in raw.items():
            self.assertEqual(frappe.db.get_value(receipts.DOCTYPE, name, "payload"), body)
        self.assertEqual(frappe.db.count(receipts.DOCTYPE, {"name": ["in", names]}), 4)
        intent.reload()
        self.assertEqual(intent.state, "Delivered")

    def test_processed_redelivery_preserves_one_projection_and_does_not_consume_again(self):
        self.seed_bot()
        message = self.message()
        name = self.admit([message])[0]
        self.run_worker(name)
        self.assertEqual(self.admit([message]), [name])
        with patch.object(receipts, "_consumer", wraps=receipts._consumer) as consume:
            row, phases, _ = self.run_worker(name)
            consume.assert_not_called()
        self.assertEqual((row.state, row.attempts, phases), ("Processed", 1, []))
        self.assertEqual(frappe.db.count("WhatsApp Message", {"message_id": message["id"], "whatsapp_account": self.account.name}), 1)
