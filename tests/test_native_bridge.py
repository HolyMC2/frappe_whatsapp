"""43b1b33: sends to a natively governed customer go to CRM's durable outbox, not to Meta.

WhatsAppMessage.send_outgoing -> notify -> _defer_to_native and after_insert,
and the bulk retry, run for real against a double of `crm.api.outbox_bridge`.
The double's call shapes are checked against CRM's pinned source in
test_crm_contract.py. No Meta request and no database access happen here.
"""

import sys
import types
import unittest
from unittest.mock import Mock, patch

import frappe

from frappe_whatsapp import coexistence, transport
from frappe_whatsapp.frappe_whatsapp.doctype.bulk_whatsapp_message.bulk_whatsapp_message import BulkWhatsAppMessage
from frappe_whatsapp.frappe_whatsapp.doctype.whatsapp_message.whatsapp_message import WhatsAppMessage

PEER = "5215550001111"


class NativeRefusal(frappe.ValidationError):
    native_refusal = True  # what crm.api.outbox_bridge.NativeSendRefused declares


class BridgeCase(unittest.TestCase):
    def setUp(self):
        self.bridge = types.ModuleType("crm.api.outbox_bridge")
        self.bridge.governing_conversation = Mock(return_value="CRM-CONVERSATION-1")
        self.bridge.queue_transcript = Mock(return_value="CRM-INTENT-1")
        self.bridge.requeue_transcript = Mock(return_value="CRM-INTENT-1")
        self.enterContext(patch.dict(sys.modules, {"crm.api.outbox_bridge": self.bridge}))
        self.apps = self.enterContext(patch.object(frappe, "get_installed_apps",
                                                   return_value=["frappe", "crm", "frappe_whatsapp"]))
        self.account = frappe._dict(name="Fictional Live", url="https://graph.facebook.com", version="v23.0",
                                    phone_id="111001")
        self.account.get_password = Mock(side_effect=AssertionError("a deferred send never reads the token"))
        self.get_doc = self.enterContext(patch.object(frappe, "get_doc", return_value=self.account))
        self.savepoint = self.enterContext(patch.object(frappe.db, "savepoint"))
        self.release = self.enterContext(patch.object(frappe.db, "release_savepoint"))
        self.rollback = self.enterContext(patch.object(frappe.db, "rollback"))
        self.api = self.enterContext(patch.object(transport, "api", side_effect=AssertionError("no Meta request")))
        self.enterContext(patch.object(coexistence, "assert_sendable"))
        self.payload = {"messaging_product": "whatsapp", "to": PEER, "type": "text",
                        "text": {"preview_url": True, "body": "Tu equipo está listo"}}

    def message(self, **values):
        fields = {"doctype": "WhatsApp Message", "type": "Outgoing", "message_type": "Manual", "content_type": "text",
                  "to": "+" + PEER, "message": "Tu equipo está listo", "whatsapp_account": self.account.name,
                  "attach": None, "is_reply": 0, "reply_to_message_id": None, "status": "Failed",
                  "message_id": "wamid.previous", "failure_reason": "previous failure"}
        fields.update(values)
        return WhatsAppMessage(fields)


class TestDeferToNative(BridgeCase):
    def test_new_governed_send_is_a_queued_transcript_and_never_reaches_meta(self):
        doc = self.message()
        doc.send_outgoing()
        self.bridge.governing_conversation.assert_called_once_with(self.account, PEER)
        self.assertEqual((doc.status, doc.message_id, doc.failure_reason, doc.to), ("Queued", None, None, PEER))
        self.assertTrue(doc.flags.native_deferred)
        self.savepoint.assert_called_once()
        point = self.savepoint.call_args.args[0]
        self.assertTrue(point.startswith("native_transcript_"))
        self.assertEqual(doc.flags.native_transcript, (self.account.name, self.payload, point))
        self.bridge.queue_transcript.assert_not_called()  # the row has no name yet
        self.bridge.requeue_transcript.assert_not_called()
        self.api.assert_not_called()
        self.account.get_password.assert_not_called()

    def test_after_insert_freezes_the_intent_and_releases_the_savepoint(self):
        doc = self.message()
        doc.send_outgoing()
        point = self.savepoint.call_args.args[0]
        doc.name = "WA-MSG-0001"
        doc.after_insert()
        self.get_doc.assert_called_with("WhatsApp Account", self.account.name)
        self.bridge.queue_transcript.assert_called_once_with(doc, self.account, self.payload)
        self.release.assert_called_once_with(point)
        self.rollback.assert_not_called()
        self.assertNotIn("native_transcript", doc.flags)
        doc.after_insert()  # a second save of the same row queues nothing more
        self.assertEqual(self.bridge.queue_transcript.call_count, 1)

    def test_after_insert_without_a_deferred_send_does_nothing(self):
        doc = self.message(name="WA-MSG-0002")
        doc.after_insert()
        self.bridge.queue_transcript.assert_not_called()
        self.release.assert_not_called()
        self.rollback.assert_not_called()

    def test_refused_intent_rolls_back_the_insert_and_raises_the_same_error(self):
        refusal = NativeRefusal("La conversación está en pausa. Toma el control para responder.")
        self.bridge.queue_transcript.side_effect = refusal
        doc = self.message()
        doc.send_outgoing()
        point = self.savepoint.call_args.args[0]
        doc.name = "WA-MSG-0003"
        with self.assertRaises(NativeRefusal) as raised:
            doc.after_insert()
        self.assertIs(raised.exception, refusal)
        self.rollback.assert_called_once_with(save_point=point)
        self.release.assert_not_called()

    def test_a_transaction_already_ended_never_masks_the_refusal(self):
        refusal = NativeRefusal("conversation_owned")
        self.bridge.queue_transcript.side_effect = refusal
        self.rollback.side_effect = RuntimeError("savepoint does not exist: deadlock ended the transaction")
        doc = self.message()
        doc.send_outgoing()
        doc.name = "WA-MSG-0004"
        with self.assertRaises(NativeRefusal) as raised:
            doc.after_insert()
        self.assertIs(raised.exception, refusal)

    def test_resend_of_an_existing_row_requeues_its_own_intent(self):
        doc = self.message(name="WA-MSG-0005")
        doc.send_outgoing()
        self.bridge.requeue_transcript.assert_called_once_with(doc)
        self.bridge.queue_transcript.assert_not_called()
        self.savepoint.assert_not_called()
        self.assertEqual(doc.status, "Queued")
        self.assertNotIn("native_transcript", doc.flags)
        self.api.assert_not_called()

    def test_resend_without_an_intent_queues_exactly_one(self):
        self.bridge.requeue_transcript.return_value = None
        doc = self.message(name="WA-MSG-0006")
        doc.send_outgoing()
        self.bridge.requeue_transcript.assert_called_once_with(doc)
        self.bridge.queue_transcript.assert_called_once_with(doc, self.account, self.payload)
        self.savepoint.assert_not_called()

    def test_crm_refusal_reaches_the_caller_unchanged(self):
        refusal = NativeRefusal("El asistente atiende esta conversación. Toma el control para responder.")
        self.bridge.governing_conversation.side_effect = refusal
        doc = self.message(status=None)
        with self.assertRaises(NativeRefusal) as raised:
            doc.send_outgoing()
        self.assertIs(raised.exception, refusal)
        self.assertIsNone(doc.status)  # not a transport failure
        self.api.assert_not_called()


class TestLegacyPathUnchanged(BridgeCase):
    def setUp(self):
        super().setUp()
        self.account.get_password = Mock(return_value="fictional-token")
        self.api.side_effect = None
        self.api.return_value = {"messages": [{"id": "wamid.legacy-fixture"}]}

    def assert_legacy_send(self, doc):
        self.assertEqual((doc.status, doc.message_id), ("Success", "wamid.legacy-fixture"))
        self.assertFalse(doc.flags.get("native_deferred"))
        self.api.assert_called_once()
        self.assertEqual(self.api.call_args.args[:3],
                         (self.account, "POST", "https://graph.facebook.com/v23.0/111001/messages"))
        self.savepoint.assert_not_called()
        self.bridge.queue_transcript.assert_not_called()
        self.bridge.requeue_transcript.assert_not_called()

    def test_ungoverned_recipient_keeps_the_legacy_send(self):
        self.bridge.governing_conversation.return_value = None
        doc = self.message()
        doc.send_outgoing()
        self.bridge.governing_conversation.assert_called_once_with(self.account, PEER)
        self.assert_legacy_send(doc)

    def test_without_crm_installed_the_bridge_is_never_consulted(self):
        self.apps.return_value = ["frappe", "frappe_whatsapp"]
        doc = self.message()
        doc.send_outgoing()
        self.bridge.governing_conversation.assert_not_called()
        self.assert_legacy_send(doc)

    def test_crm_without_the_bridge_module_keeps_the_legacy_send(self):
        with patch.dict(sys.modules, {"crm.api.outbox_bridge": None}):
            doc = self.message()
            doc.send_outgoing()
        self.assert_legacy_send(doc)


class RetriedMessage:
    """What bulk retry touches on a WhatsApp Message row."""

    def __init__(self, name, *, defer=False):
        self.name, self.message_id, self.status = name, "wamid.kept", "Failed"
        self.flags = frappe._dict()
        self.saved = []
        self.defer = defer

    def db_update(self):
        self.saved.append((self.status, self.message_id))

    def send_outgoing(self):
        if self.defer:
            self.status, self.flags.native_deferred = "Queued", True


class TestBulkRetry(BridgeCase):
    def setUp(self):
        super().setUp()
        self.bulk = BulkWhatsAppMessage({"doctype": "Bulk WhatsApp Message", "name": "BULK-0001"})
        self.intents = {"WA-NATIVE"}
        self.exists = self.enterContext(patch.object(frappe.db, "exists", side_effect=self.row_exists))
        self.has_column = self.enterContext(patch.object(frappe.db, "has_column", return_value=True))
        self.log = self.enterContext(patch.object(frappe, "log_error"))

    def row_exists(self, doctype, filters=None):
        if doctype == "DocType":
            return filters == "CRM Outbound Intent"
        if doctype == "CRM Outbound Intent":
            return filters["transcript_message"] in self.intents
        raise AssertionError(f"unexpected exists({doctype!r}, {filters!r})")

    def retry(self, row):
        self.get_doc.return_value = row
        self.bulk.resend_single_message(row.name)
        self.get_doc.assert_called_with("WhatsApp Message", row.name)

    def test_native_row_is_left_to_its_intent_with_provider_id_intact(self):
        row = RetriedMessage("WA-NATIVE")
        self.retry(row)
        self.bridge.requeue_transcript.assert_called_once_with(row)
        self.assertEqual((row.message_id, row.status, row.saved), ("wamid.kept", "Failed", []))
        self.exists.assert_any_call("CRM Outbound Intent", {"transcript_message": "WA-NATIVE"})
        self.log.assert_not_called()

    def test_native_requeue_failure_is_logged_and_skipped_not_sent_again(self):
        self.bridge.requeue_transcript.side_effect = RuntimeError("intent is Accepted")
        row = RetriedMessage("WA-NATIVE")
        self.retry(row)
        self.assertEqual(row.saved, [])
        self.log.assert_called_once_with(title="WhatsApp bulk retry skipped a native send: WA-NATIVE")

    def test_legacy_row_deferred_on_resend_is_queued_not_success(self):
        row = RetriedMessage("WA-LEGACY", defer=True)
        self.retry(row)
        self.bridge.requeue_transcript.assert_not_called()
        self.assertEqual(row.saved, [("Queued", None), ("Queued", None)])

    def test_legacy_row_sent_again_is_success(self):
        row = RetriedMessage("WA-LEGACY")
        self.retry(row)
        self.assertEqual(row.saved, [("Queued", None), ("Success", None)])

    def test_native_detection_needs_crm_and_its_migrated_schema(self):
        self.apps.return_value = ["frappe", "frappe_whatsapp"]
        self.assertFalse(BulkWhatsAppMessage._native_transcript("WA-NATIVE"))
        self.exists.assert_not_called()
        self.apps.return_value = ["frappe", "crm", "frappe_whatsapp"]
        self.has_column.return_value = False  # CRM installed, transcript_message not migrated yet
        self.assertFalse(BulkWhatsAppMessage._native_transcript("WA-NATIVE"))
        self.has_column.assert_called_with("CRM Outbound Intent", "transcript_message")
        self.has_column.return_value = True
        self.assertTrue(BulkWhatsAppMessage._native_transcript("WA-NATIVE"))
        self.assertFalse(BulkWhatsAppMessage._native_transcript("WA-LEGACY"))
