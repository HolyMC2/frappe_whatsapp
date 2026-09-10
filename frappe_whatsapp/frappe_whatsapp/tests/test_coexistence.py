"""Raw signed coexistence envelopes and actual projections; provider sends forbidden."""

import copy
import hashlib
import hmac
import json
import unittest
from unittest.mock import patch

import frappe
from werkzeug.test import EnvironBuilder
from werkzeug.wrappers import Request

from frappe_whatsapp import coexistence
from frappe_whatsapp import webhook_receipts as receipts
from frappe_whatsapp.utils import signature, webhook
from frappe_whatsapp.frappe_whatsapp.tests.test_webhook_signature import account


def echo(mid="wamid.fictional-echo", **values):
    return {"from": "15555550100", "to": "15555550200", "id": mid,
            "timestamp": "1788990000", "type": "text", "text": {"body": "External app response"}, **values}


def echo_payload(*echoes, phone="phone-a", business="waba-a"):
    return {"object": "whatsapp_business_account", "entry": [{"id": business, "changes": [{
        "field": "smb_message_echoes", "value": {"messaging_product": "whatsapp",
        "metadata": {"phone_number_id": phone, "display_phone_number": "+1 (555) 555-0100"},
        "message_echoes": list(echoes) if echoes else [echo()]},
    }]}]}


class TestEchoAtomization(unittest.TestCase):
    def setUp(self):
        self.accounts = [account(), account("b", phone="phone-b")]

    def events(self, data):
        scopes = signature.scope_payload(data, self.accounts, "app-a")
        return [event for scope in scopes for event in webhook.receipt_events(scope)]

    def post(self, data):
        raw = json.dumps(data).encode()
        header = "sha256=" + hmac.new(b"secret-a", raw, hashlib.sha256).hexdigest()
        request = Request(EnvironBuilder(method="POST", data=raw, content_type="application/json",
                          headers={"X-Hub-Signature-256": header}).get_environ())
        with patch.object(frappe, "request", request), patch.object(signature, "_accounts", return_value=self.accounts):
            return webhook.post()

    def test_every_echo_atom_has_exact_account_app_peer_and_original_body(self):
        data = echo_payload(echo(), echo("wamid.second", to="15555550300"))
        events = self.events(data)
        self.assertEqual(len(events), 2)
        for event, original in zip(events, data["entry"][0]["changes"][0]["value"]["message_echoes"]):
            self.assertEqual(event["account_id"], "phone-a")
            self.assertEqual(event["app_id"], "app-a")
            self.assertEqual(event["event_type"], "smb_message_echoes")
            self.assertEqual(event["event_id"], coexistence.echo_identity(original))
            self.assertEqual(event["payload"]["change"]["value"]["message_echoes"], [original])

    def test_reorder_and_split_preserve_atom_identity(self):
        first, second = echo(), echo("wamid.second")
        whole = self.events(echo_payload(first, second))
        split = self.events(echo_payload(second)) + self.events(echo_payload(first))
        self.assertEqual({receipts._prepare(event)["event_key"] for event in whole},
                         {receipts._prepare(event)["event_key"] for event in split})

    def test_same_message_id_other_account_or_peer_has_distinct_identity(self):
        a = self.events(echo_payload())[0]
        b = self.events(echo_payload(phone="phone-b"))[0]
        c = self.events(echo_payload(echo(to="15555550300")))[0]
        self.assertEqual(len({receipts._prepare(event)["event_key"] for event in (a, b, c)}), 3)

    def test_reordered_text_does_not_change_source_identity(self):
        a = self.events(echo_payload())[0]
        b = self.events(echo_payload(echo(text={"body": "different delivery body"}, timestamp="1788990001")))[0]
        self.assertEqual(receipts._prepare(a)["event_key"], receipts._prepare(b)["event_key"])

    def test_invalid_sibling_prevents_any_signed_batch_receipt_write(self):
        data = echo_payload(echo(), echo("wamid.invalid", to="+15555550200"))
        with patch.object(receipts, "record_events") as record:
            with self.assertRaisesRegex(receipts.ReceiptError, "echo_invalid_number"):
                self.post(data)
            record.assert_not_called()

    def test_invalid_later_change_prevents_earlier_normal_message_write(self):
        data = echo_payload(echo(to="unscoped-peer"))
        data["entry"][0]["changes"].insert(0, {"field": "messages", "value": {
            "metadata": {"phone_number_id": "phone-a"},
            "messages": [{"id": "wamid.incoming", "from": "15555550200", "type": "text", "text": {"body": "Hi"}}],
        }})
        with patch.object(receipts, "record_events") as record:
            with self.assertRaises(receipts.ReceiptError):
                self.post(data)
            record.assert_not_called()

    def test_mixed_valid_changes_are_all_passed_to_durable_store(self):
        data = echo_payload(echo(), echo("wamid.second"))
        data["entry"][0]["changes"].append({"field": "messages", "value": {
            "metadata": {"phone_number_id": "phone-a"}, "statuses": [{"id": "wamid.local", "status": "read"}],
        }})
        with patch.object(receipts, "record_events", return_value=["a", "b", "c"]) as record:
            self.assertEqual(self.post(data), ["a", "b", "c"])
        self.assertEqual([event["event_type"] for event in record.call_args.args[0]],
                         ["smb_message_echoes", "smb_message_echoes", "status"])

    def test_business_from_must_match_exact_scoped_metadata_without_phone_merging(self):
        for sender in ("15555550101", "5215555550100", "5555550100"):
            with self.subTest(sender=sender), self.assertRaisesRegex(receipts.ReceiptError, "echo_account_mismatch"):
                self.events(echo_payload(echo(**{"from": sender})))

    def test_foreign_phone_or_waba_rejected_before_echo_ingest(self):
        for data in (echo_payload(phone="foreign-phone"), echo_payload(business="foreign-waba")):
            with self.assertRaises(frappe.PermissionError):
                self.events(data)

    def test_bounded_required_fields_reject_invalid_types(self):
        invalid = (
            {"id": ""}, {"id": "x" * 141}, {"to": True}, {"from": 15555550100},
            {"timestamp": True}, {"timestamp": 1.5}, {"timestamp": "1e9"}, {"timestamp": "999999999999"},
            {"timestamp": "0"}, {"type": "../image"}, {"text": "body"},
            {"text": {"body": "x" * 65537}}, {"text": {"body": 42}},
        )
        for values in invalid:
            with self.subTest(values=list(values)), self.assertRaises(receipts.ReceiptError):
                self.events(echo_payload(echo(**values)))

    def test_echo_collection_must_be_nonempty_bounded_list_of_objects(self):
        for echoes in (None, {}, [], [None], [echo()] * 1001):
            data = echo_payload()
            data["entry"][0]["changes"][0]["value"]["message_echoes"] = echoes
            with self.subTest(kind=type(echoes)), self.assertRaises(receipts.ReceiptError):
                self.events(data)

    def test_media_requires_id_and_bounded_metadata_without_fetching(self):
        for kind in ("image", "audio", "video", "document"):
            valid = echo(type=kind, **{kind: {"id": "media-id", "mime_type": "application/octet-stream"}})
            self.assertEqual(len(self.events(echo_payload(valid))), 1)
            valid[kind]["caption"] = []
            with self.assertRaisesRegex(receipts.ReceiptError, "echo_invalid_media_metadata"):
                self.events(echo_payload(valid))

    def test_echo_array_on_wrong_field_or_mixed_value_is_rejected(self):
        data = echo_payload()
        data["entry"][0]["changes"][0]["field"] = "messages"
        with self.assertRaisesRegex(receipts.ReceiptError, "echo_field_mismatch"):
            self.events(data)
        for key in ("messages", "statuses", "contacts", "history", "state_sync"):
            data = echo_payload()
            data["entry"][0]["changes"][0]["value"][key] = []
            with self.assertRaisesRegex(receipts.ReceiptError, "echo_ambiguous_batch"):
                self.events(data)

    def test_bsp_history_envelope_not_accepted_as_meta(self):
        with self.assertRaises(frappe.PermissionError):
            self.events({"id": "event", "event": "history", "data": {"id": "waba-a"}})


class TestCoexistenceSql(unittest.TestCase):
    def setUp(self):
        import uuid
        self.suffix = uuid.uuid4().hex
        self.point = "coexistence_test_" + self.suffix
        frappe.db.savepoint(self.point)
        self.addCleanup(frappe.db.rollback, save_point=self.point)
        self.addCleanup(frappe.set_user, frappe.session.user)
        frappe.set_user("Administrator")
        self.account = self._account(str(int(self.suffix[:14], 16)))
        self.peer = "15555550200"
        self.scopes_patch = patch.object(signature, "_accounts", return_value=[self.account])
        self.scopes_patch.start()
        self.addCleanup(self.scopes_patch.stop)
        self.request_patch = patch.object(frappe, "request", None)
        self.request_patch.start()
        self.addCleanup(self.request_patch.stop)
        from frappe_whatsapp import transport
        from frappe_whatsapp.frappe_whatsapp.doctype.whatsapp_message.whatsapp_message import WhatsAppMessage
        self.original_send = WhatsAppMessage.send_outgoing
        self.blockers = []
        for owner, attr in ((transport, "api"), (transport, "raw"), (WhatsAppMessage, "send_outgoing"),
                            (WhatsAppMessage, "create_whatsapp_profile"), (WhatsAppMessage, "update_profile_name")):
            blocker = patch.object(owner, attr, side_effect=AssertionError("projection invoked transport/profile path"))
            self.blockers.append(blocker.start())
            self.addCleanup(blocker.stop)
        self.send_patch = self.blockers[2]

    def _account(self, phone):
        return frappe.get_doc({
            "doctype": "WhatsApp Account", "account_name": "coexistence-" + phone,
            "phone_id": phone, "business_id": "999001", "app_id": "999002",
            "status": "Active", "mode": "Live", "url": "https://graph.facebook.com",
            "version": "v25.0", "token": "fictional-unused", "app_secret": "fictional-secret",
            "webhook_verify_token": "coexistence-verify-" + phone,
        }).insert(ignore_permissions=True)

    def receipt(self, message=None, field="smb_message_echoes"):
        data = echo_payload(message or echo("wamid." + self.suffix), phone=self.account.phone_id,
                            business=self.account.business_id)
        change = data["entry"][0]["changes"][0]
        if field != "smb_message_echoes":
            change["field"] = field
            change["value"].pop("message_echoes")
            change["value"][field] = [{"fictional": "raw provider shape retained only"}]
        scoped = signature.scope_payload(data, [self.account], self.account.app_id)[0]
        event = list(webhook.receipt_events(scoped))[0]
        name = receipts.record_events([event])[0]
        frappe.db.set_value(receipts.DOCTYPE, name, "state", "Processing", update_modified=False)
        return frappe.get_doc(receipts.DOCTYPE, name), scoped

    def consume(self, receipt):
        prior = frappe.flags.get("meta_webhook_receipt")
        frappe.flags.meta_webhook_receipt = receipt.name
        try:
            return webhook.consume_receipt(receipt)
        finally:
            frappe.flags.meta_webhook_receipt = prior

    def conversation(self, bot=False):
        from crm.api.conversations import get_or_create
        doc = get_or_create("WhatsApp", self.account.phone_id, self.peer)
        if bot:
            frappe.db.set_value("CRM Conversation", doc.name,
                {"control_state": "Bot", "bot_enabled": 1, "generation": 7}, update_modified=False)
            doc.reload()
        return doc

    def local_message(self, mid, *, peer=None, account=None, name=None):
        from frappe.utils import now_datetime
        doc = frappe.get_doc({
            "doctype": "WhatsApp Message", "name": name or "local-" + frappe.generate_hash(length=20),
            "type": "Outgoing", "to": peer or self.peer, "whatsapp_account": account or self.account.name,
            "message_id": mid, "message": "Already sent by local Cloud path", "message_type": "Manual",
            "content_type": "text", "status": "sent", "docstatus": 0, "idx": 0,
            "owner": "Administrator", "modified_by": "Administrator", "creation": now_datetime(), "modified": now_datetime(),
        })
        doc.db_insert()  # Explicit test fixture, never the production projection seam.
        return doc

    def test_guest_projection_preserves_text_and_holds_bot_without_transport_or_identity_merges(self):
        conversation = self.conversation(bot=True)
        before_profiles = frappe.db.count("WhatsApp Profiles")
        receipt, scoped = self.receipt(echo("wamid." + self.suffix, text={"body": '<script>alert(1)</script>\nHuman response'}))
        frappe.set_user("Guest")
        with patch("crm.api.whatsapp_routing.resolve_reference_for_number", side_effect=AssertionError("global phone lookup")):
            result = self.consume(receipt)
        message = frappe.get_doc("WhatsApp Message", coexistence.projection_name(receipt, scoped.change["value"]["message_echoes"][0]))
        self.assertEqual(result, {"state": "Processed", "reason_code": "external_echo_projected"})
        self.assertEqual((message.type, message.get("from"), message.to, message.whatsapp_account),
                         ("Outgoing", "15555550100", self.peer, self.account.name))
        self.assertIn("&lt;script&gt;", message.message)
        self.assertNotIn("<script>", message.message)
        self.assertIn("<br>Human response", message.message)
        self.assertEqual(message.owner, "Guest")
        self.assertFalse(frappe.has_permission("WhatsApp Message", "create", user="Guest"))
        self.assertFalse(frappe.has_permission(receipts.DOCTYPE, "read", doc=receipt, user="Guest"))
        self.assertEqual(message.external_receipt, receipt.name)
        self.assertTrue(message.external_sent_at)
        self.assertFalse(message.reference_name)
        self.assertFalse(message.profile_name)
        self.assertEqual(frappe.db.count("WhatsApp Profiles"), before_profiles)
        conversation.reload()
        self.assertEqual((conversation.control_state, conversation.generation, conversation.bot_enabled), ("Human", 8, 0))
        for blocker in self.blockers:
            blocker.assert_not_called()

    def test_media_metadata_is_stored_without_fetch_or_full_receipt_duplication(self):
        message = echo("wamid." + self.suffix, type="image", image={"id": "fictional-media",
            "mime_type": "image/jpeg", "sha256": "fictional-hash", "caption": "<b>caption</b>",
            "url": "https://example.invalid/never-fetch", "extra_raw": "only-in-receipt"})
        receipt, _ = self.receipt(message)
        self.consume(receipt)
        projected = frappe.get_doc("WhatsApp Message", coexistence.projection_name(receipt, message))
        self.assertEqual(json.loads(projected.external_media), {
            "id": "fictional-media", "mime_type": "image/jpeg", "sha256": "fictional-hash"})
        self.assertEqual(projected.message, "&lt;b&gt;caption&lt;/b&gt;")
        self.assertFalse(projected.attach)
        self.assertNotIn("only-in-receipt", projected.external_media)
        for blocker in self.blockers:
            blocker.assert_not_called()

    def test_replay_preserves_one_projection_and_one_control_generation(self):
        conversation = self.conversation(bot=True)
        receipt, scoped = self.receipt()
        first = self.consume(receipt)
        conversation.reload()
        generation = conversation.generation
        with patch.dict(frappe.flags, {"meta_webhook_replay": True}):
            self.assertEqual(self.consume(receipt), first)
        conversation.reload()
        self.assertEqual(conversation.generation, generation)
        name = coexistence.projection_name(receipt, scoped.change["value"]["message_echoes"][0])
        self.assertEqual(frappe.db.count("WhatsApp Message", {"name": name}), 1)

    def test_retry_flag_does_not_skip_first_conservative_hold(self):
        conversation = self.conversation(bot=True)
        receipt, _ = self.receipt()
        with patch.dict(frappe.flags, {"meta_webhook_replay": True}):
            self.consume(receipt)
        conversation.reload()
        self.assertEqual((conversation.control_state, conversation.generation, conversation.bot_enabled), ("Human", 8, 0))

    def test_exact_local_cloud_echo_is_correlated_without_duplicate_or_human_hold(self):
        conversation = self.conversation(bot=True)
        mid = "wamid." + self.suffix
        local = self.local_message(mid)
        receipt, _ = self.receipt(echo(mid))
        result = self.consume(receipt)
        self.assertEqual(result["reason_code"], "own_cloud_echo_correlated")
        self.assertEqual(frappe.db.count("WhatsApp Message", {"whatsapp_account": self.account.name, "message_id": mid}), 1)
        local.reload()
        self.assertFalse(local.external_receipt)
        self.assertEqual(local.message, "Already sent by local Cloud path")
        conversation.reload()
        self.assertEqual((conversation.control_state, conversation.generation, conversation.bot_enabled), ("Bot", 7, 1))

    def test_app_id_same_but_wrong_account_or_peer_is_not_own_echo(self):
        conversation = self.conversation(bot=True)
        mid = "wamid." + self.suffix
        other = self._account(self.account.phone_id + "1")
        self.local_message(mid, account=other.name)
        self.local_message(mid, peer="15555550999")
        receipt, _ = self.receipt(echo(mid))
        self.assertEqual(self.consume(receipt)["reason_code"], "external_echo_projected")
        conversation.reload()
        self.assertEqual(conversation.control_state, "Human")

    def test_ambiguous_local_matches_hold_human_and_do_not_modify_either_local_row(self):
        conversation = self.conversation(bot=True)
        mid = "wamid." + self.suffix
        locals_ = [self.local_message(mid), self.local_message(mid)]
        receipt, _ = self.receipt(echo(mid))
        result = self.consume(receipt)
        self.assertEqual(result["reason_code"], "echo_correlation_ambiguous")
        self.assertEqual(frappe.db.count("WhatsApp Message", {"whatsapp_account": self.account.name, "message_id": mid}), 3)
        for doc in locals_:
            doc.reload()
            self.assertFalse(doc.external_receipt)
            self.assertEqual(doc.message, "Already sent by local Cloud path")
        conversation.reload()
        self.assertEqual((conversation.control_state, conversation.bot_enabled), ("Human", 0))

    def test_deterministic_name_collision_does_not_overwrite_unrelated_row(self):
        receipt, scoped = self.receipt()
        message = scoped.change["value"]["message_echoes"][0]
        existing = self.local_message("wamid.other", name=coexistence.projection_name(receipt, message))
        with self.assertRaisesRegex(receipts.ReceiptError, "external_projection_collision"):
            self.consume(receipt)
        existing.reload()
        self.assertEqual(existing.message_id, "wamid.other")

    def test_duplicate_projection_uses_savepoint_and_preserves_prior_caller_work(self):
        receipt, scoped = self.receipt()
        self.consume(receipt)
        message = scoped.change["value"]["message_echoes"][0]
        name = coexistence.projection_name(receipt, message)
        prior = self.local_message("wamid.prior-" + self.suffix)
        with patch.object(coexistence, "_matching_projection", side_effect=[None, name]), \
             patch.object(frappe.db, "rollback", wraps=frappe.db.rollback) as rollback:
            self.assertEqual(coexistence._insert_projection(receipt, self.account, message), name)
        self.assertEqual(rollback.call_count, 1)
        self.assertTrue(rollback.call_args.kwargs.get("save_point"))
        self.assertTrue(frappe.db.exists("WhatsApp Message", prior.name))

    def test_callback_failure_propagates_without_helper_transaction_rollback(self):
        receipt, _ = self.receipt()
        prior = self.local_message("wamid.prior-" + self.suffix)
        with patch.object(coexistence, "_conversation_control", side_effect=receipts.ReceiptError("control_failed")), \
             patch.object(frappe.db, "rollback") as rollback:
            with self.assertRaisesRegex(receipts.ReceiptError, "control_failed"):
                self.consume(receipt)
        rollback.assert_not_called()
        self.assertTrue(frappe.db.exists("WhatsApp Message", prior.name))

    def test_missing_crm_preserves_projection_and_reports_unapplied_control(self):
        receipt, scoped = self.receipt()
        with patch.object(coexistence, "_conversation_service", return_value=None):
            result = self.consume(receipt)
        self.assertEqual(result["reason_code"], "conversation_control_unavailable")
        self.assertTrue(frappe.db.exists("WhatsApp Message", coexistence.projection_name(receipt, scoped.change["value"]["message_echoes"][0])))

    def test_missing_crm_app_is_detected_before_any_control_schema_query(self):
        with patch.object(frappe, "get_installed_apps", return_value=["frappe", "frappe_whatsapp"]), \
             patch.object(frappe.db, "exists") as exists:
            self.assertIsNone(coexistence._conversation_service())
        exists.assert_not_called()

    def test_unmigrated_crm_control_schema_is_explicitly_unavailable(self):
        with patch.object(frappe, "get_installed_apps", return_value=["frappe", "frappe_whatsapp", "crm"]), \
             patch.object(frappe.db, "exists", return_value=False):
            self.assertIsNone(coexistence._conversation_service())

    def test_unknown_echo_type_is_retained_unsupported_but_cannot_leave_bot_enabled(self):
        conversation = self.conversation(bot=True)
        receipt, _ = self.receipt(echo("wamid." + self.suffix, type="sticker", sticker={"id": "media"}))
        self.assertEqual(self.consume(receipt), {"state": "Ignored", "reason_code": "echo_type_unsupported"})
        self.assertEqual(frappe.db.count("WhatsApp Message", {"external_receipt": receipt.name}), 0)
        conversation.reload()
        self.assertEqual((conversation.control_state, conversation.bot_enabled), ("Human", 0))

    def test_history_and_state_sync_are_retained_without_projection_or_control(self):
        conversation = self.conversation(bot=True)
        for field in ("history", "smb_app_state_sync"):
            receipt, _ = self.receipt(field=field)
            self.assertEqual(self.consume(receipt), {"state": "Ignored", "reason_code": "coexistence_sync_unsupported"})
            self.assertEqual(frappe.db.count("WhatsApp Message", {"external_receipt": receipt.name}), 0)
        conversation.reload()
        self.assertEqual((conversation.control_state, conversation.generation), ("Bot", 7))

    def test_forged_public_marker_and_private_seam_token_are_rejected(self):
        from frappe_whatsapp.frappe_whatsapp.doctype.whatsapp_message.whatsapp_message import WhatsAppMessage
        self.assertNotIn(WhatsAppMessage._insert_external_projection, frappe.whitelisted)
        forged = frappe.get_doc({"doctype": "WhatsApp Message", "type": "Outgoing", "to": self.peer,
            "message": "Never send", "content_type": "text", "whatsapp_account": self.account.name,
            "external_receipt": "forged"})
        forged.flags.coexistence_projection = True
        with self.assertRaises(frappe.ValidationError):
            forged.insert(ignore_permissions=True)
        with self.assertRaisesRegex(receipts.ReceiptError, "external_projection_forbidden"):
            forged._insert_external_projection(True)
        for blocker in self.blockers:
            blocker.assert_not_called()

    def test_cleared_in_memory_origin_cannot_resend_or_rewrite_persisted_external_row(self):
        receipt, scoped = self.receipt()
        self.consume(receipt)
        doc = frappe.get_doc("WhatsApp Message", coexistence.projection_name(receipt, scoped.change["value"]["message_echoes"][0]))
        for field in coexistence._EXTERNAL_FIELDS:
            doc.set(field, None)
        doc.__islocal = 1  # Generic caller flags cannot hide the persisted row.
        with self.assertRaises(frappe.ValidationError):
            self.original_send(doc)
        with self.assertRaises(frappe.ValidationError):
            doc.save(ignore_permissions=True)
        with self.assertRaises(frappe.ValidationError):
            doc.before_rename(doc.name, "renamed", False)

    def test_forged_scoped_body_or_receipt_identity_cannot_project(self):
        receipt, scoped = self.receipt()
        altered = copy.deepcopy(scoped.change)
        altered["value"]["message_echoes"][0]["text"]["body"] = "changed after receipt"
        forged = signature.ScopedChange(scoped.business_id, scoped.app_id, scoped.accounts, altered)
        with patch.dict(frappe.flags, {"meta_webhook_receipt": receipt.name}):
            with self.assertRaisesRegex(receipts.ReceiptError, "coexistence_receipt_mismatch"):
                coexistence.consume_echo(receipt, forged)
        receipt.account_id = "foreign"
        with self.assertRaisesRegex(receipts.ReceiptError, "coexistence_receipt_mismatch"):
            self.consume(receipt)
