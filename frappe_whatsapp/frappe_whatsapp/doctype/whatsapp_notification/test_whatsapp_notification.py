# Copyright (c) 2022, Shridhar Patil and Contributors
# See license.txt

import json
from unittest.mock import patch

import frappe
from frappe_whatsapp import transport
from frappe_whatsapp.testing import IntegrationTestCase
from frappe_whatsapp.frappe_whatsapp.tests.test_native_outbox import Response

# Fictional, but numeric like a real Graph phone-number id: the send fence only
# scopes /{digits}/messages. Every request is answered by the double in setUp.
PHONE_ID = "9941102"
MESSAGES_URL = f"https://graph.facebook.com/v17.0/{PHONE_ID}/messages"


class TestWhatsAppNotification(IntegrationTestCase):
    """Tests for WhatsApp Notification doctype."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._ensure_test_account()
        cls._ensure_test_template()

    @classmethod
    def _ensure_test_account(cls):
        if not frappe.db.exists("WhatsApp Account", "Test WA Notif Account"):
            account = frappe.get_doc({
                "doctype": "WhatsApp Account",
                "token": "test-token",
                "account_name": "Test WA Notif Account",
                "status": "Active",
                "url": "https://graph.facebook.com",
                "version": "v17.0",
                "phone_id": PHONE_ID,
                "business_id": "notif_test_business_id",
                "app_id": "notif_test_app_id",
                "webhook_verify_token": "notif_test_verify_token",
                "is_default_incoming": 1,
                "is_default_outgoing": 1,
            })
            account.insert(ignore_permissions=True)
            frappe.db.commit()  # nosemgrep: frappe-manual-commit -- test fixture must be visible to later queries

    @classmethod
    def _ensure_test_template(cls):
        template_name = "test_notif_template-en"
        if not frappe.db.exists("WhatsApp Templates", template_name):
            doc = frappe.get_doc({
                "doctype": "WhatsApp Templates",
                "template_name": "test_notif_template",
                "actual_name": "test_notif_template",
                "template": "Hello {{1}}, your order {{2}} is ready",
                "sample_values": "John,ORD-001",
                "category": "TRANSACTIONAL",
                "language": frappe.db.get_value("Language", {"language_code": "en"}) or "en",
                "language_code": "en",
                "whatsapp_account": "Test WA Notif Account",
                "status": "APPROVED",
                "id": "test_notif_template_id",
                "header_type": "",
            })
            doc.db_insert()
            frappe.db.commit()  # nosemgrep: frappe-manual-commit -- test fixture must be visible to later queries

    def setUp(self):
        # Set password within each test's transaction scope
        from frappe.utils.password import set_encrypted_password
        set_encrypted_password("WhatsApp Account", "Test WA Notif Account", "test_notif_token", "token")
        # Clear ALL defaults then set ours (db.set_value bypasses on_update hooks)
        frappe.db.sql("UPDATE `tabWhatsApp Account` SET is_default_outgoing=0, is_default_incoming=0")
        frappe.db.set_value("WhatsApp Account", "Test WA Notif Account", {
            "is_default_outgoing": 1,
            "is_default_incoming": 1,
        })
        # Message sends leave through the fence's own requests.request
        # (transport._message_api), not make_post_request. Double that physical
        # boundary for every test so nothing can reach Meta.
        self.wamid = "wamid.notif_unused"
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
        for name in frappe.get_all("WhatsApp Notification", filters={"notification_name": ["like", "Test Notif%"]}, pluck="name"):
            frappe.delete_doc("WhatsApp Notification", name, force=True)
        frappe.db.commit()  # nosemgrep: frappe-manual-commit -- test fixture must be visible to later queries

    def _make_notification(self, **kwargs):
        doc = frappe.get_doc({
            "doctype": "WhatsApp Notification",
            "notification_name": kwargs.get("notification_name", "Test Notif 1"),
            "notification_type": kwargs.get("notification_type", "DocType Event"),
            "reference_doctype": kwargs.get("reference_doctype", "User"),
            "field_name": kwargs.get("field_name", "mobile_no"),
            "doctype_event": kwargs.get("doctype_event", "After Save"),
            "template": kwargs.get("template", "test_notif_template-en"),
            "disabled": kwargs.get("disabled", 0),
            "condition": kwargs.get("condition", ""),
        })
        if kwargs.get("fields"):
            for f in kwargs["fields"]:
                doc.append("fields", {"field_name": f})
        doc.insert(ignore_permissions=True)
        return doc

    def test_notification_creation(self):
        """Test basic notification creation."""
        doc = self._make_notification()
        self.assertTrue(frappe.db.exists("WhatsApp Notification", doc.name))

    def test_notification_autoname(self):
        """Test notification is named from notification_name field."""
        doc = self._make_notification(notification_name="Test Notif Autoname")
        self.assertEqual(doc.name, "Test Notif Autoname")

    def test_validate_invalid_field_name(self):
        """Test validation fails for non-existent field name."""
        with self.assertRaises(frappe.ValidationError):
            self._make_notification(
                notification_name="Test Notif BadField",
                field_name="nonexistent_field_xyz"
            )

    def test_validate_valid_field_name(self):
        """Test validation passes for existing field name."""
        doc = self._make_notification(
            notification_name="Test Notif GoodField",
            field_name="email"
        )
        self.assertIsNotNone(doc.name)

    def test_validate_custom_attachment_requires_attach(self):
        """Test that custom_attachment requires either attach or attach_from_field."""
        with self.assertRaises(frappe.ValidationError):
            doc = frappe.get_doc({
                "doctype": "WhatsApp Notification",
                "notification_name": "Test Notif NoAttach",
                "notification_type": "DocType Event",
                "reference_doctype": "User",
                "field_name": "mobile_no",
                "doctype_event": "After Save",
                "template": "test_notif_template-en",
                "custom_attachment": 1,
                "attach": "",
                "attach_from_field": "",
            })
            doc.insert(ignore_permissions=True)

    def test_validate_set_property_after_alert_field_exists(self):
        """Test set_property_after_alert references existing field."""
        with self.assertRaises(frappe.ValidationError):
            doc = frappe.get_doc({
                "doctype": "WhatsApp Notification",
                "notification_name": "Test Notif BadProp",
                "notification_type": "DocType Event",
                "reference_doctype": "User",
                "field_name": "mobile_no",
                "doctype_event": "After Save",
                "template": "test_notif_template-en",
                "set_property_after_alert": "nonexistent_field_abc",
            })
            doc.insert(ignore_permissions=True)

    def test_format_number(self):
        """Test format_number strips leading +."""
        doc = self._make_notification(notification_name="Test Notif Format")
        self.assertEqual(doc.format_number("+919900112233"), "919900112233")
        self.assertEqual(doc.format_number("919900112233"), "919900112233")

    def test_on_trash_clears_cache(self):
        """Test on_trash clears the notification map cache."""
        doc = self._make_notification(notification_name="Test Notif Cache")
        frappe.cache().set_value("whatsapp_notification_map", {"test": True})

        # Call on_trash directly to avoid side effects from doc.delete()
        # (delete triggers run_server_script_for_doc_event which rebuilds cache)
        doc.on_trash()

        cached = frappe.cache().get_value("whatsapp_notification_map")
        self.assertFalse(cached)

    def test_send_template_message(self):
        """Test send_template_message sends correct data."""
        # The fence sets integration_request itself, which notify() reads.
        self.wamid = "wamid.notif_test_1"

        doc = self._make_notification(
            notification_name="Test Notif Send",
            field_name="mobile_no",
            fields=["first_name", "name"],
        )

        # Create a mock source document
        user = frappe.get_doc("User", "Administrator")
        user.mobile_no = "919900112233"

        doc.send_template_message(user)

        sent_data = self._sent()
        self.assertEqual(sent_data["messaging_product"], "whatsapp")
        self.assertEqual(sent_data["to"], "919900112233")
        self.assertEqual(sent_data["type"], "template")
        self.assertEqual(sent_data["template"]["name"], "test_notif_template")

    def test_send_template_message_with_condition(self):
        """Test that condition evaluation works."""
        self.wamid = "wamid.notif_cond_1"

        doc = self._make_notification(
            notification_name="Test Notif Condition",
            field_name="mobile_no",
            condition="doc.enabled == 1",
        )

        # User with enabled=1 should trigger
        user = frappe.get_doc("User", "Administrator")
        user.mobile_no = "919900112299"
        user.enabled = 1
        doc.send_template_message(user)
        self.assertEqual(self._sent()["to"], "919900112299")

    def test_send_template_message_condition_not_met(self):
        """Test that message is not sent when condition is not met."""
        doc = self._make_notification(
            notification_name="Test Notif NoSend",
            field_name="mobile_no",
            condition="doc.enabled == 0",
        )

        user = frappe.get_doc("User", "Administrator")
        user.mobile_no = "919900112299"
        user.enabled = 1
        doc.send_template_message(user)

        self.assertFalse(self.http.called)
        self.assertFalse(self.old_post.called)

    def test_disabled_notification_does_not_send(self):
        """Test that disabled notification does not trigger."""
        doc = self._make_notification(
            notification_name="Test Notif Disabled",
            disabled=1,
        )
        user = frappe.get_doc("User", "Administrator")
        user.mobile_no = "919900112299"

        # Should return early without sending
        result = doc.send_template_message(user)
        self.assertIsNone(result)

    def test_scheduler_event_notification(self):
        """Test creating a scheduler event notification."""
        doc = frappe.get_doc({
            "doctype": "WhatsApp Notification",
            "notification_name": "Test Notif Scheduler",
            "notification_type": "Scheduler Event",
            "reference_doctype": "User",
            "event_frequency": "Daily",
            "template": "test_notif_template-en",
            "condition": "",
        })
        doc.insert(ignore_permissions=True)
        self.assertEqual(doc.notification_type, "Scheduler Event")
        self.assertEqual(doc.event_frequency, "Daily")
