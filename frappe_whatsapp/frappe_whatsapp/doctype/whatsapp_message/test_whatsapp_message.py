# Copyright (c) 2022, Shridhar Patil and Contributors
# See license.txt

import json
from unittest.mock import patch

import frappe
from frappe_whatsapp import transport
from frappe_whatsapp.legacy_outbox import LegacySendBlocked
from frappe_whatsapp.testing import IntegrationTestCase
from frappe_whatsapp.frappe_whatsapp.tests.test_native_outbox import Response

# Fictional, but numeric like a real Graph phone-number id: the send fence only
# scopes /{digits}/messages. Every request is answered by the double in setUp.
PHONE_ID = "9941101"
MESSAGES_URL = f"https://graph.facebook.com/v17.0/{PHONE_ID}/messages"


class TestWhatsAppMessage(IntegrationTestCase):
    """Tests for WhatsApp Message doctype."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._ensure_test_account()

    @classmethod
    def _ensure_test_account(cls):
        """Create a test WhatsApp Account if it doesn't exist."""
        if not frappe.db.exists("WhatsApp Account", "Test WA Msg Account"):
            account = frappe.get_doc({
                "doctype": "WhatsApp Account",
                "token": "test-token",
                "account_name": "Test WA Msg Account",
                "status": "Active",
                "url": "https://graph.facebook.com",
                "version": "v17.0",
                "phone_id": PHONE_ID,
                "business_id": "msg_test_business_id",
                "app_id": "msg_test_app_id",
                "webhook_verify_token": "msg_test_verify_token",
                "is_default_incoming": 1,
                "is_default_outgoing": 1,
            })
            account.insert(ignore_permissions=True)
            frappe.db.commit()  # nosemgrep: frappe-manual-commit -- test fixture must be visible to later queries

    def setUp(self):
        # Set password within each test's transaction scope
        from frappe.utils.password import set_encrypted_password
        set_encrypted_password("WhatsApp Account", "Test WA Msg Account", "test_token_123", "token")
        # Clear ALL defaults then set ours (db.set_value bypasses on_update hooks)
        frappe.db.sql("UPDATE `tabWhatsApp Account` SET is_default_outgoing=0, is_default_incoming=0")
        frappe.db.set_value("WhatsApp Account", "Test WA Msg Account", {
            "is_default_outgoing": 1,
            "is_default_incoming": 1,
        })
        # Message sends leave through the fence's own requests.request
        # (transport._message_api), not make_post_request. Double that physical
        # boundary for every test so nothing can reach Meta.
        self.wamid = "wamid.test_unused"
        self.http = self.enterContext(patch.object(transport.requests, "request", side_effect=self._graph))
        self.old_post = self.enterContext(patch.object(transport, "make_post_request"))

    def _graph(self, method, url, **kwargs):
        """Graph accepting the send for the exact recipient it was given."""
        to = json.loads(kwargs["data"])["to"]
        return Response(payload={"messaging_product": "whatsapp", "contacts": [{"input": to, "wa_id": to}],
                                 "messages": [{"id": self.wamid}]})

    def _sent(self):
        """Body of the single message POST that reached the physical boundary."""
        self.http.assert_called_once()
        self.old_post.assert_not_called()
        args, kwargs = self.http.call_args
        self.assertEqual(args, ("POST", MESSAGES_URL))
        return json.loads(kwargs["data"])

    def tearDown(self):
        for name in frappe.get_all("WhatsApp Message", filters={"to": ["like", "9199%"]}, pluck="name"):
            frappe.delete_doc("WhatsApp Message", name, force=True)
        for name in frappe.get_all("WhatsApp Message", filters={"from": ["like", "9199%"]}, pluck="name"):
            frappe.delete_doc("WhatsApp Message", name, force=True)
        for name in frappe.get_all("WhatsApp Profiles", filters={"number": ["like", "9199%"]}, pluck="name"):
            frappe.delete_doc("WhatsApp Profiles", name, force=True)
        frappe.db.commit()  # nosemgrep: frappe-manual-commit -- test fixture must be visible to later queries

    def test_incoming_message_creation(self):
        """Test creating an incoming WhatsApp message."""
        doc = frappe.get_doc({
            "doctype": "WhatsApp Message",
            "type": "Incoming",
            "from": "919900112233",
            "message": "Hello World",
            "message_id": "wamid.test_incoming_1",
            "content_type": "text",
            "whatsapp_account": "Test WA Msg Account",
        })
        doc.insert(ignore_permissions=True)
        self.assertTrue(frappe.db.exists("WhatsApp Message", doc.name))
        self.assertEqual(doc.type, "Incoming")

    def test_set_whatsapp_account_default(self):
        """Test that whatsapp_account is auto-set to default when not provided."""
        doc = frappe.get_doc({
            "doctype": "WhatsApp Message",
            "type": "Incoming",
            "from": "919900112244",
            "message": "Test default account",
            "message_id": "wamid.test_default_1",
            "content_type": "text",
        })
        doc.insert(ignore_permissions=True)
        self.assertEqual(doc.whatsapp_account, "Test WA Msg Account")

    def test_outgoing_text_message(self):
        """Test sending an outgoing text message."""
        self.wamid = "wamid.test_outgoing_1"
        doc = frappe.get_doc({
            "doctype": "WhatsApp Message",
            "type": "Outgoing",
            "to": "919900112255",
            "message": "Hello from test",
            "message_type": "Manual",
            "content_type": "text",
            "whatsapp_account": "Test WA Msg Account",
        })
        doc.insert(ignore_permissions=True)

        self.assertEqual(doc.message_id, "wamid.test_outgoing_1")
        self.assertEqual(doc.status, "Success")

        # Verify the request sent to Meta carried the correct data
        sent_data = self._sent()
        self.assertEqual(sent_data["messaging_product"], "whatsapp")
        self.assertEqual(sent_data["to"], "919900112255")
        self.assertEqual(sent_data["type"], "text")
        self.assertEqual(sent_data["text"]["body"], "Hello from test")

    def test_outgoing_text_message_with_plus_number(self):
        """Test that + is stripped from phone numbers."""
        self.wamid = "wamid.test_plus_1"
        doc = frappe.get_doc({
            "doctype": "WhatsApp Message",
            "type": "Outgoing",
            "to": "+919900112256",
            "message": "Test plus strip",
            "message_type": "Manual",
            "content_type": "text",
            "whatsapp_account": "Test WA Msg Account",
        })
        doc.insert(ignore_permissions=True)

        sent_data = self._sent()
        self.assertEqual(sent_data["to"], "919900112256")

    def test_outgoing_reply_message(self):
        """Test sending a reply message includes context."""
        self.wamid = "wamid.test_reply_1"
        doc = frappe.get_doc({
            "doctype": "WhatsApp Message",
            "type": "Outgoing",
            "to": "919900112257",
            "message": "Reply test",
            "message_type": "Manual",
            "content_type": "text",
            "is_reply": 1,
            "reply_to_message_id": "wamid.original_msg_123",
            "whatsapp_account": "Test WA Msg Account",
        })
        doc.insert(ignore_permissions=True)

        sent_data = self._sent()
        self.assertIn("context", sent_data)
        self.assertEqual(sent_data["context"]["message_id"], "wamid.original_msg_123")

    def test_outgoing_image_message(self):
        """Test sending an image message."""
        self.wamid = "wamid.test_image_1"
        doc = frappe.get_doc({
            "doctype": "WhatsApp Message",
            "type": "Outgoing",
            "to": "919900112258",
            "message": "Image caption",
            "message_type": "Manual",
            "content_type": "image",
            "attach": "https://example.com/image.jpg",
            "whatsapp_account": "Test WA Msg Account",
        })
        doc.insert(ignore_permissions=True)

        sent_data = self._sent()
        self.assertEqual(sent_data["type"], "image")
        self.assertEqual(sent_data["image"]["link"], "https://example.com/image.jpg")
        self.assertEqual(sent_data["image"]["caption"], "Image caption")

    def test_outgoing_reaction_message(self):
        """Test sending a reaction message."""
        self.wamid = "wamid.test_reaction_1"
        doc = frappe.get_doc({
            "doctype": "WhatsApp Message",
            "type": "Outgoing",
            "to": "919900112259",
            "message": "\U0001f44d",
            "message_type": "Manual",
            "content_type": "reaction",
            "reply_to_message_id": "wamid.react_to_msg",
            "whatsapp_account": "Test WA Msg Account",
        })
        doc.insert(ignore_permissions=True)

        sent_data = self._sent()
        self.assertEqual(sent_data["type"], "reaction")
        self.assertEqual(sent_data["reaction"]["emoji"], "\U0001f44d")
        self.assertEqual(sent_data["reaction"]["message_id"], "wamid.react_to_msg")

    def test_create_whatsapp_profile_on_insert(self):
        """Test that a WhatsApp Profile is created when a message is inserted."""
        doc = frappe.get_doc({
            "doctype": "WhatsApp Message",
            "type": "Incoming",
            "from": "919900112260",
            "message": "Profile creation test",
            "message_id": "wamid.test_profile_create",
            "content_type": "text",
            "profile_name": "Test Profile User",
            "whatsapp_account": "Test WA Msg Account",
        })
        doc.insert(ignore_permissions=True)

        self.assertTrue(
            frappe.db.exists("WhatsApp Profiles", {"number": "919900112260"})
        )

    def test_update_profile_name_on_update(self):
        """Test that profile name is updated when message profile_name changes."""
        # First create a profile
        profile = frappe.get_doc({
            "doctype": "WhatsApp Profiles",
            "profile_name": "Original Name",
            "number": "919900112261",
        })
        profile.insert(ignore_permissions=True)

        # Create incoming message with new profile name
        doc = frappe.get_doc({
            "doctype": "WhatsApp Message",
            "type": "Incoming",
            "from": "919900112261",
            "message": "Update profile test",
            "message_id": "wamid.test_profile_update",
            "content_type": "text",
            "profile_name": "Updated Name",
            "whatsapp_account": "Test WA Msg Account",
        })
        doc.insert(ignore_permissions=True)

        profile.reload()
        self.assertEqual(profile.profile_name, "Updated Name")

    def test_format_number_method(self):
        """Test the format_number instance method."""
        doc = frappe.new_doc("WhatsApp Message")
        self.assertEqual(doc.format_number("+919900112233"), "919900112233")
        self.assertEqual(doc.format_number("919900112233"), "919900112233")

    def test_send_read_receipt(self):
        """Test sending a read receipt.

        The fork's send fence holds read receipts before any HTTP: the body
        names a message, not a recipient, so the conversation it touches cannot
        be verified (legacy_outbox). The receipt is composed as before and
        refused with that reason; nothing reaches Meta and the row is unchanged.
        """
        doc = frappe.get_doc({
            "doctype": "WhatsApp Message",
            "type": "Incoming",
            "from": "919900112262",
            "message": "Read receipt test",
            "message_id": "wamid.test_read_receipt",
            "content_type": "text",
            "whatsapp_account": "Test WA Msg Account",
        })
        doc.insert(ignore_permissions=True)

        with patch.object(transport, "api", wraps=transport.api) as api:
            with self.assertRaisesRegex(LegacySendBlocked, "^legacy_action_recipient_unverified$"):
                doc.send_read_receipt()

        args, kwargs = api.call_args
        self.assertEqual(args[1:], ("POST", MESSAGES_URL))
        sent_data = json.loads(kwargs["data"])
        self.assertEqual(sent_data["status"], "read")
        self.assertEqual(sent_data["message_id"], "wamid.test_read_receipt")
        self.http.assert_not_called()
        self.old_post.assert_not_called()
        self.assertNotEqual(frappe.db.get_value("WhatsApp Message", doc.name, "status"), "marked as read")

    def test_outgoing_message_api_failure(self):
        """Test that outgoing message handles API failure."""
        self.http.side_effect = None
        self.http.return_value = Response(status=400, payload={
            "error": {"code": 100, "message": "Invalid phone number", "error_user_title": "Error"}
        })

        # The fence reports Graph's rejection by its static code, not provider text.
        with self.assertRaisesRegex(frappe.ValidationError, "provider_rejected"):
            doc = frappe.get_doc({
                "doctype": "WhatsApp Message",
                "type": "Outgoing",
                "to": "919900112270",
                "message": "Fail test",
                "message_type": "Manual",
                "content_type": "text",
                "whatsapp_account": "Test WA Msg Account",
            })
            doc.insert(ignore_permissions=True)
        # The failure is Meta's answer to a request that left, not a fence hold.
        self.assertEqual(self._sent()["to"], "919900112270")

    def test_send_template_whitelisted(self):
        """Test the send_template whitelisted function."""
        self.wamid = "wamid.test_template_wl"

        # First create a template (without hooks to avoid Meta API calls)
        if not frappe.db.exists("WhatsApp Templates", "test_msg_template-en"):
            frappe.get_doc({
                "doctype": "WhatsApp Templates",
                "template_name": "test_msg_template",
                "actual_name": "test_msg_template",
                "template": "Hello {{1}}",
                "category": "TRANSACTIONAL",
                "language": frappe.db.get_value("Language", {"language_code": "en"}) or "en",
                "language_code": "en",
                "whatsapp_account": "Test WA Msg Account",
                "status": "APPROVED",
                "id": "test_template_id_123",
            }).db_insert()
            frappe.db.commit()  # nosemgrep: frappe-manual-commit -- test fixture must be visible to later queries

        from frappe_whatsapp.frappe_whatsapp.doctype.whatsapp_message.whatsapp_message import send_template
        send_template(
            to="919900112263",
            reference_doctype="User",
            reference_name="Administrator",
            template="test_msg_template-en"
        )

        self.assertTrue(
            frappe.db.exists("WhatsApp Message", {"to": "919900112263", "message_type": "Template"})
        )
        self.assertEqual(self._sent()["template"]["name"], "test_msg_template")

    def test_send_template_omits_static_buttons(self):
        """Static Call Phone / Visit Website buttons must NOT appear in the
        outgoing components payload — Meta rejects sub_type=phone_number and
        applies static buttons from the approved template automatically.
        See issue #188.
        """
        self.wamid = "wamid.test_buttons"

        template_name = "test_msg_buttons_template-en"
        if not frappe.db.exists("WhatsApp Templates", template_name):
            tmpl = frappe.get_doc({
                "doctype": "WhatsApp Templates",
                "template_name": "test_msg_buttons_template",
                "actual_name": "test_msg_buttons_template",
                "template": "Hello",
                "category": "TRANSACTIONAL",
                "language": frappe.db.get_value("Language", {"language_code": "en"}) or "en",
                "language_code": "en",
                "whatsapp_account": "Test WA Msg Account",
                "status": "APPROVED",
                "id": "test_template_buttons_id",
                "name": template_name,
            })
            tmpl.flags.ignore_validate = True
            tmpl.db_insert()
            # Link child rows manually — append() before the parent has a
            # name leaves parent blank, so we wire them up here.
            buttons = [
                {"button_type": "Quick Reply", "button_label": "Yes"},
                {
                    "button_type": "Call Phone",
                    "button_label": "Call Us",
                    "phone_number": "+919876543210",
                },
                {
                    "button_type": "Visit Website",
                    "button_label": "Homepage",
                    "website_url": "https://example.com",
                    "url_type": "Static",
                },
            ]
            for idx, data in enumerate(buttons, start=1):
                row = frappe.get_doc({
                    "doctype": "WhatsApp Button",
                    "parent": template_name,
                    "parenttype": "WhatsApp Templates",
                    "parentfield": "buttons",
                    "idx": idx,
                    **data,
                })
                row.db_insert()
            frappe.db.commit()  # nosemgrep: frappe-manual-commit -- test fixture must be visible to later queries

        doc = frappe.get_doc({
            "doctype": "WhatsApp Message",
            "type": "Outgoing",
            "to": "919900112264",
            "message_type": "Template",
            "content_type": "text",
            "template": template_name,
            "whatsapp_account": "Test WA Msg Account",
        })
        doc.insert(ignore_permissions=True)

        sent_data = self._sent()
        components = sent_data["template"]["components"]
        sub_types = [c.get("sub_type") for c in components if c.get("type") == "button"]
        self.assertNotIn("phone_number", sub_types)
        self.assertNotIn("url", sub_types)  # static URL button also excluded
        self.assertIn("quick_reply", sub_types)
