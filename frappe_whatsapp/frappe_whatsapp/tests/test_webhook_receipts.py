"""Real SQL receipt identity, caller-transaction isolation and batch decomposition."""
import copy
import json
import unittest
from unittest.mock import patch

import frappe
from frappe_whatsapp import webhook_receipts as receipts
from frappe_whatsapp.utils import signature, webhook
from frappe_whatsapp.frappe_whatsapp.tests.test_webhook_signature import account, payload


def event(identity="fictional-event-1", **overrides):
    return {"provider": "WhatsApp", "account_id": "fictional-phone", "app_id": "fictional-app",
            "event_type": "message", "event_id": identity,
            "payload": {"fictional": True, "id": identity}, **overrides}


class TestWebhookReceipts(unittest.TestCase):
    def setUp(self):
        frappe.set_user("Administrator")
        self.assertTrue(receipts.ready(), "Guarded receipt schema migration required")
        self.point = "receipt_test_" + frappe.generate_hash(length=8)
        frappe.db.savepoint(self.point)
        self.commit_functions = list(frappe.db.after_commit._functions)

    def tearDown(self):
        frappe.db.rollback(save_point=self.point)
        frappe.db.after_commit._functions.clear()
        frappe.db.after_commit._functions.extend(self.commit_functions)

    def test_duplicate_preserves_earlier_caller_work(self):
        todo = frappe.get_doc({"doctype": "ToDo", "description": "Fictional Meta transaction anchor"}).insert()
        first = receipts.record_events([event()])
        repeated = receipts.record_events([event()])
        self.assertEqual(first, repeated)
        self.assertTrue(frappe.db.exists("ToDo", todo.name))
        self.assertEqual(frappe.db.count(receipts.DOCTYPE, {"event_key": first[0]}), 1)

    def test_record_never_commits_and_does_not_enqueue_before_commit(self):
        with patch.object(frappe.db, "commit") as commit, patch.object(frappe, "enqueue") as enqueue:
            names = receipts.record_events([event()])
        commit.assert_not_called()
        enqueue.assert_not_called()
        self.assertEqual(frappe.db.get_value(receipts.DOCTYPE, names[0], "state"), "Pending")

    def test_invalid_later_event_does_not_insert_earlier(self):
        valid = event()
        key = receipts._prepare(valid)["event_key"]
        with self.assertRaises(receipts.ReceiptError):
            receipts.record_events([valid, event("bad", account_id="")])
        self.assertFalse(frappe.db.exists(receipts.DOCTYPE, key))

    def test_account_app_and_provider_namespaces_are_distinct(self):
        events = [event(), event(account_id="other"), event(app_id="other"), event(provider="Instagram")]
        names = receipts.record_events(events)
        self.assertEqual(len(set(names)), 4)

    def test_reordered_batch_converges(self):
        events = [event(str(n)) for n in range(5)]
        names = receipts.record_events(events)
        self.assertEqual(set(names), set(receipts.record_events(list(reversed(events)) + events)))
        self.assertEqual(frappe.db.count(receipts.DOCTYPE, {"name": ["in", names]}), 5)

    def test_replay_preserves_original_evidence(self):
        name = receipts.record_events([event()])[0]
        receipts.record_events([event(payload={"fictional": "changed"})])
        self.assertEqual(json.loads(frappe.db.get_value(receipts.DOCTYPE, name, "payload")), event()["payload"])

    def test_generic_update_cannot_rewrite_receipt(self):
        name = receipts.record_events([event()])[0]
        doc = frappe.get_doc(receipts.DOCTYPE, name)
        doc.payload = '{}'
        with self.assertRaises(frappe.ValidationError):
            doc.save(ignore_permissions=True)

    def test_guest_and_sales_roles_cannot_read_receipt(self):
        name = receipts.record_events([event()])[0]
        doc = frappe.get_doc(receipts.DOCTYPE, name)
        self.assertFalse(frappe.has_permission(receipts.DOCTYPE, "read", doc=doc, user="Guest"))
        permissions = frappe.get_meta(receipts.DOCTYPE).permissions
        self.assertEqual({p.role for p in permissions if p.read}, {"System Manager"})
        self.assertFalse(any(p.create or p.write or p.delete for p in permissions))

    def test_enqueue_failure_retains_pending(self):
        names = receipts.record_events([event()])
        with patch.object(frappe, "enqueue", side_effect=RuntimeError("fictional queue outage")):
            receipts.enqueue_receipts(names)
        self.assertEqual(frappe.db.get_value(receipts.DOCTYPE, names[0], "state"), "Pending")

    def test_all_message_and_status_atoms_are_retained(self):
        data = payload()
        value = data['entry'][0]['changes'][0]['value']
        value['messages'].append({**value['messages'][0], 'id': 'wamid.second'})
        value['statuses'] = [{'id': 'wamid.out', 'status': 'sent'}, {'id': 'wamid.out', 'status': 'read'}]
        scoped = signature.scope_payload(data, [account()], 'app-a')[0]
        events = list(webhook.receipt_events(scoped))
        self.assertEqual([e['event_type'] for e in events], ['message','message','status','status'])
        self.assertEqual(len(set(receipts.record_events(events))), 4)
        value['messages'].reverse()
        value['statuses'].reverse()
        self.assertEqual(set(receipts.record_events(list(webhook.receipt_events(scoped)))),
                         set(receipts.record_events(events)))

    def test_worker_accepts_frappe_unbound_request_proxy(self):
        with patch.object(receipts, '_lock', return_value=None), patch.object(frappe.db, 'rollback'):
            receipts.run_receipt('fictional')

    def test_worker_refuses_http_transaction_context(self):
        with patch.object(frappe, 'request', object()), patch.object(frappe.db, 'commit') as commit:
            with self.assertRaises(receipts.ReceiptError):
                receipts.run_receipt('fictional')
            commit.assert_not_called()

    def test_schema_unavailable_never_falls_back_to_volatile_ingest(self):
        with patch.object(receipts, 'ready', return_value=False), patch.object(frappe, 'get_doc') as insert:
            with self.assertRaises(receipts.ReceiptError):
                receipts.record_events([event()])
            insert.assert_not_called()
