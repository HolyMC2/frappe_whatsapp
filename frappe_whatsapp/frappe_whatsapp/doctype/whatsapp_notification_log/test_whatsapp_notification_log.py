# Copyright (c) 2022, Shridhar Patil and Contributors
# See license.txt

from unittest.mock import patch

import frappe
from frappe_whatsapp.testing import IntegrationTestCase


class TestWhatsAppNotificationLog(IntegrationTestCase):
    """Tests for WhatsApp Notification Log doctype."""

    def tearDown(self):
        for name in frappe.get_all("WhatsApp Notification Log", filters={"template": ["like", "Test Log%"]}, pluck="name"):
            frappe.delete_doc("WhatsApp Notification Log", name, force=True)
        frappe.db.commit()  # nosemgrep: frappe-manual-commit -- test fixture must be visible to later queries

    def test_log_creation(self):
        """Test basic notification log creation."""
        doc = frappe.get_doc({
            "doctype": "WhatsApp Notification Log",
            "template": "Test Log Template",
            "meta_data": '{"status": "success"}'
        })
        doc.insert(ignore_permissions=True)
        self.assertTrue(frappe.db.exists("WhatsApp Notification Log", doc.name))

    def test_log_with_json_metadata(self):
        """Test log stores JSON metadata correctly."""
        import json
        meta = {"messages": [{"id": "wamid.123"}], "contacts": [{"wa_id": "919900112233"}]}
        doc = frappe.get_doc({
            "doctype": "WhatsApp Notification Log",
            "template": "Test Log JSON",
            "meta_data": json.dumps(meta)
        })
        doc.insert(ignore_permissions=True)
        doc.reload()

        stored_meta = json.loads(doc.meta_data)
        self.assertEqual(stored_meta["messages"][0]["id"], "wamid.123")

    def test_log_with_error_metadata(self):
        """Test log stores error metadata."""
        import json
        meta = {"error": "Failed to send message: Invalid phone number"}
        doc = frappe.get_doc({
            "doctype": "WhatsApp Notification Log",
            "template": "Test Log Error",
            "meta_data": json.dumps(meta)
        })
        doc.insert(ignore_permissions=True)
        doc.reload()

        stored_meta = json.loads(doc.meta_data)
        self.assertIn("error", stored_meta)


# DOCO FORK HUNK: G7 retention. Upstream ships no clear_old_logs on this
# controller, so the table grew forever — one row per template send AND one per
# inbound webhook event. Appended here rather than in a new file to keep the
# fork's divergence from upstream in as few places as possible.
class TestWhatsAppNotificationLogRetention(IntegrationTestCase):
    """The Log Settings contract + the purge window.

    `clear_old_logs` commits by design (a long backlog must not sit in one
    transaction), so every call goes through `_clear`, which patches
    frappe.db.commit to a no-op — otherwise these tests would permanently
    delete this site's real notification rows. Fixtures are inserted with raw
    SQL: the subject is the purge, not insert-time validation.
    """

    DEFAULT_DAYS = 90
    PREFIX = "g7ret-wnl-"
    TABLE = "tabWhatsApp Notification Log"

    def setUp(self):
        super().setUp()
        frappe.set_user("Administrator")
        self._wipe()

    def tearDown(self):
        self._wipe()
        # NOTE: no commit here. The sibling TestWhatsAppNotificationLog commits
        # its cleanup because its fixtures are committed; these are not.
        super().tearDown()

    def _wipe(self):
        frappe.db.sql(
            f"DELETE FROM `{self.TABLE}` WHERE name LIKE %s", (self.PREFIX + "%",)
        )

    def _row(self, suffix, age_days):
        name = self.PREFIX + suffix
        frappe.db.sql(
            f"""INSERT INTO `{self.TABLE}`
                (name, creation, modified, owner, modified_by, docstatus)
                VALUES (%s, DATE_SUB(NOW(), INTERVAL %s DAY), NOW(),
                        'Administrator', 'Administrator', 0)""",
            (name, age_days),
        )
        return name

    def _surviving(self):
        return {
            r[0]
            for r in frappe.db.sql(
                f"SELECT name FROM `{self.TABLE}` WHERE name LIKE %s",
                (self.PREFIX + "%",),
            )
        }

    def _clear(self, days=None):
        from frappe.model.base_document import get_controller

        controller = get_controller("WhatsApp Notification Log")
        with patch.object(frappe.db, "commit"):
            if days is None:
                controller.clear_old_logs()
            else:
                controller.clear_old_logs(days)

    def _clear_count(self, days=None):
        """_clear, but hand back what clear_old_logs returned."""
        from frappe.model.base_document import get_controller

        controller = get_controller("WhatsApp Notification Log")
        with patch.object(frappe.db, "commit"):
            if days is None:
                return controller.clear_old_logs()
            return controller.clear_old_logs(days)

    def test_the_purge_returns_its_count_as_data(self):
        """A purge summary must be reachable without scraping stdout:
        `frappe.logger(...).info(...)` writes NOTHING on the lab, on cell-0 or
        on the boat site (frappe's default level is WARNING only when
        _dev_server is set, ERROR otherwise, and DEV_SERVER is unset)."""
        self.assertEqual(self._clear_count(self.DEFAULT_DAYS), 0)
        self._row("old_a", self.DEFAULT_DAYS + 40)
        self._row("old_b", self.DEFAULT_DAYS + 41)
        self._row("fresh", 1)
        self.assertEqual(self._clear_count(self.DEFAULT_DAYS), 2)
        self.assertEqual(self._clear_count(self.DEFAULT_DAYS), 0, "idempotent")
        self.assertEqual(self._clear_count(0), 0, "disabled returns 0, not None")

    def test_log_settings_accepts_this_doctype(self):
        """Log Settings validates every logs_to_clear row against its LogType
        protocol and SILENTLY DELETES the ones that fail — so without
        clear_old_logs, boat's LOG_RETENTION entry would register nothing."""
        from frappe.core.doctype.log_settings.log_settings import _supports_log_clearing

        _supports_log_clearing.clear_cache()  # @site_cache memoises per process
        self.assertTrue(_supports_log_clearing("WhatsApp Notification Log"))

    def test_rows_past_the_window_go_and_recent_rows_stay(self):
        old = self._row("old", self.DEFAULT_DAYS + 40)
        edge = self._row("edge", self.DEFAULT_DAYS + 1)
        fresh = self._row("fresh", 1)
        inside = self._row("inside", self.DEFAULT_DAYS - 10)
        self.assertEqual(self._surviving(), {old, edge, fresh, inside})
        self._clear(self.DEFAULT_DAYS)
        self.assertEqual(self._surviving(), {fresh, inside})

    def test_default_window_is_used_when_days_is_unset(self):
        self._row("old", self.DEFAULT_DAYS + 40)
        fresh = self._row("fresh", 1)
        self._clear()
        self.assertEqual(self._surviving(), {fresh})

    def test_zero_or_negative_days_disables_the_purge(self):
        old = self._row("old", self.DEFAULT_DAYS + 400)
        fresh = self._row("fresh", 1)
        self._clear(0)
        self.assertEqual(self._surviving(), {old, fresh})
        self._clear(-7)
        self.assertEqual(self._surviving(), {old, fresh})

    def test_second_run_is_idempotent(self):
        self._row("old", self.DEFAULT_DAYS + 40)
        fresh = self._row("fresh", 1)
        self._clear(self.DEFAULT_DAYS)
        self.assertEqual(self._surviving(), {fresh})
        self._clear(self.DEFAULT_DAYS)
        self.assertEqual(self._surviving(), {fresh})

    def test_batching_clears_more_rows_than_one_batch(self):
        from frappe_whatsapp.frappe_whatsapp.doctype.whatsapp_notification_log import (
            whatsapp_notification_log as mod,
        )

        names = {self._row(f"bulk{i}", self.DEFAULT_DAYS + 30) for i in range(7)}
        fresh = self._row("fresh", 1)
        with patch.object(mod, "DELETE_BATCH", 3):
            self._clear(self.DEFAULT_DAYS)
        self.assertEqual(self._surviving(), {fresh})
        for name in names:
            self.assertFalse(
                frappe.db.exists("WhatsApp Notification Log", name)
            )
