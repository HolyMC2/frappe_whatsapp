"""Account-scoped window evidence, private session documents and the outgoing default.

Provider and database doubles only: no site, no Meta request.
"""

import json
import unittest
from unittest.mock import Mock, patch

import frappe

from frappe_whatsapp import transport, utils, window
from frappe_whatsapp.frappe_whatsapp.doctype.whatsapp_message.whatsapp_message import (
	WhatsAppMessage,
	send_template,
)

NOW = 1_800_000_000


class TestPeerCandidates(unittest.TestCase):
	def test_mexican_legacy_mobile_pair_both_ways(self):
		self.assertEqual(window.peer_candidates("526691234567"), ["526691234567", "5216691234567"])
		self.assertEqual(window.peer_candidates("+52 1 669 123 4567"), ["526691234567", "5216691234567"])

	def test_other_numbers_are_exact_never_a_ten_digit_suffix(self):
		self.assertEqual(window.peer_candidates("+1 555 019 4001"), ["15550194001"])
		self.assertEqual(window.peer_candidates("447700900123"), ["447700900123"])

	def test_not_a_number(self):
		for value in (None, "", "abc", "1234567", "1" * 16):
			with self.subTest(value=value):
				self.assertEqual(window.peer_candidates(value), [])


class TestEvidence(unittest.TestCase):
	def setUp(self):
		self.enterContext(patch.object(frappe.db, "exists", return_value=True))
		self.sql = self.enterContext(patch.object(frappe.db, "sql"))

	def test_bounds_exclude_future_and_exactly_24h_old_evidence(self):
		self.sql.return_value = [frappe._dict(name="R1", ts=NOW - 60)]
		found = window.evidence("PID", "APP", "526691234567", now=NOW)
		self.assertEqual(found, {"receipt": "R1", "timestamp": NOW - 60})
		query, params = self.sql.call_args.args
		self.assertIn("state='Processed'", query)
		self.assertIn(">%s", query)
		self.assertIn("<=%s", query)
		self.assertEqual(params, ("PID", "APP", "526691234567", NOW - window.WINDOW_SECONDS, NOW))
		self.assertNotIn("FOR UPDATE", query)

	def test_locked_read_revalidates_one_primary_key_row(self):
		self.sql.side_effect = [[frappe._dict(name="R1", ts=NOW - 60)], [("R1",)]]
		self.assertTrue(window.evidence("PID", "APP", "526691234567", now=NOW, lock=True))
		locked_query, locked_params = self.sql.call_args.args
		self.assertIn("name=%s", locked_query)
		self.assertIn("FOR UPDATE", locked_query)
		self.assertEqual(locked_params[0], "R1")

	def test_lost_revalidation_is_no_evidence(self):
		self.sql.side_effect = [[frappe._dict(name="R1", ts=NOW - 60)], []]
		self.assertIsNone(window.evidence("PID", "APP", "526691234567", now=NOW, lock=True))

	def test_missing_identity_never_queries(self):
		for args in (("", "APP", "52669"), ("PID", None, "52669"), ("PID", "APP", "+52669"), ("PID", "APP", None)):
			with self.subTest(args=args):
				self.assertIsNone(window.evidence(*args, now=NOW))
		self.sql.assert_not_called()


class TestIsOpen(unittest.TestCase):
	A = frappe._dict(name="A", phone_id="PID-A", app_id="APP", status="Active")
	B = frappe._dict(name="B", phone_id="PID-B", app_id="APP", status="Active")

	def test_open_on_one_account_closed_on_another_for_the_same_customer(self):
		def evidence(phone_id, app_id, peer, now=None):
			if phone_id == "PID-A" and peer == "5216691234567":
				return frappe._dict(receipt="R1", timestamp=NOW - 100)
			return None

		with patch.object(window, "evidence", side_effect=evidence):
			a = window.is_open(self.A, "526691234567", now=NOW)
			b = window.is_open(self.B, "526691234567", now=NOW)
		self.assertTrue(a.open)
		self.assertEqual(a.peer, "5216691234567")
		self.assertEqual(a.closes_at, NOW - 100 + window.WINDOW_SECONDS)
		self.assertFalse(b.open)
		self.assertEqual(b.reason, "closed")
		self.assertEqual(b.peer, "526691234567")

	def test_latest_spelling_wins(self):
		stamps = {"526691234567": NOW - 500, "5216691234567": NOW - 50}
		with patch.object(window, "evidence", side_effect=lambda p, a, peer, now=None: frappe._dict(receipt=peer, timestamp=stamps[peer])):
			self.assertEqual(window.is_open(self.A, "526691234567", now=NOW).peer, "5216691234567")

	def test_inactive_or_incomplete_account_and_bad_number(self):
		with patch.object(window, "evidence") as evidence:
			self.assertEqual(window.is_open(frappe._dict(self.A, status="Inactive"), "526691234567").reason, "account_unavailable")
			self.assertEqual(window.is_open(frappe._dict(self.A, app_id=""), "526691234567").reason, "account_unavailable")
			self.assertEqual(window.is_open(self.A, "12").reason, "invalid_number")
		evidence.assert_not_called()


class TestSessionDocument(unittest.TestCase):
	def setUp(self):
		self.account = frappe._dict(name="acct", url="https://graph.facebook.com", version="v23.0", phone_id="991122")
		self.account.get_password = Mock(return_value="fake-token")
		self.enterContext(patch.object(frappe, "get_doc", return_value=self.account))
		self.enterContext(patch.object(frappe.utils, "get_url", return_value="https://shop.example.test"))
		self.enterContext(patch.object(frappe, "get_installed_apps", return_value=[]))
		self.files = self.enterContext(patch.object(frappe.db, "get_value", return_value=None))
		self.api = self.enterContext(patch.object(transport, "api", return_value={"messages": [{"id": "wamid.doc"}]}))
		self.enterContext(patch("frappe_whatsapp.coexistence.assert_sendable"))
		self.enterContext(patch.object(frappe, "get_hooks", return_value=[]))

	def message(self, **extra):
		doc = WhatsAppMessage.__new__(WhatsAppMessage)
		doc.__dict__.update(dict(
			doctype="WhatsApp Message", type="Outgoing", to="526691234567", template=None,
			whatsapp_account="acct", content_type="document", message_type="Manual", message_id=None,
			message="Comprobante", attach="/private/files/a1b2c3.pdf", attach_filename=None,
			is_reply=False, reply_to_message_id=None, flags=frappe._dict(), status=None,
		))
		doc.__dict__.update(extra)
		return doc

	def test_private_upload_failure_refuses_without_a_link_send(self):
		doc = self.message()
		with patch.object(WhatsAppMessage, "_upload_local_media", return_value=(None, "document")):
			with self.assertRaisesRegex(frappe.ValidationError, "archivo privado"):
				doc.send_outgoing()
		self.api.assert_not_called()

	def test_uploaded_document_keeps_its_display_filename(self):
		doc = self.message(attach_filename="ACC-SINV-0001.pdf")
		with patch.object(WhatsAppMessage, "_upload_local_media", return_value=("media-1", "document")):
			doc.send_outgoing()
		sent = json.loads(self.api.call_args.kwargs["data"])
		self.assertEqual(sent["document"], {"id": "media-1", "caption": "Comprobante", "filename": "ACC-SINV-0001.pdf"})

	def test_file_row_name_then_url_basename(self):
		self.files.return_value = "Ticket 0001.pdf"
		with patch.object(WhatsAppMessage, "_upload_local_media", return_value=("media-1", "document")):
			self.message().send_outgoing()
		self.assertEqual(json.loads(self.api.call_args.kwargs["data"])["document"]["filename"], "Ticket 0001.pdf")
		self.files.return_value = None
		with patch.object(WhatsAppMessage, "_upload_local_media", return_value=("media-1", "document")):
			self.message(attach="/files/Mi%20ticket.pdf").send_outgoing()
		self.assertEqual(json.loads(self.api.call_args.kwargs["data"])["document"]["filename"], "Mi ticket.pdf")

	def test_public_file_keeps_its_legacy_link_fallback(self):
		with patch.object(WhatsAppMessage, "_upload_local_media", return_value=(None, "document")):
			self.message(attach="/files/catalogo.pdf").send_outgoing()
		sent = json.loads(self.api.call_args.kwargs["data"])
		self.assertEqual(sent["document"]["link"], "https://shop.example.test/files/catalogo.pdf")


	def test_durable_only_send_refuses_before_upload_without_a_native_route(self):
		doc = self.message(flags=frappe._dict(require_native=True))
		with patch.object(WhatsAppMessage, "_upload_local_media") as upload:
			with self.assertRaisesRegex(frappe.ValidationError, "ya no está conectada"):
				doc.send_outgoing()
		upload.assert_not_called()
		self.api.assert_not_called()

	def test_durable_only_send_never_posts_when_governance_vanished_after_upload(self):
		import sys
		import types

		bridge = types.SimpleNamespace(governing_conversation=Mock(side_effect=["conv-1", None]))
		crm_modules = {"crm": types.ModuleType("crm"), "crm.api": types.ModuleType("crm.api"), "crm.api.outbox_bridge": bridge}
		doc = self.message(flags=frappe._dict(require_native=True), attach_filename="T.pdf")
		with patch.dict(sys.modules, crm_modules), \
			patch.object(frappe, "get_installed_apps", return_value=["crm"]), \
			patch.object(WhatsAppMessage, "_upload_local_media", return_value=("media-1", "document")):
			with self.assertRaises(frappe.ValidationError):
				doc.send_outgoing()
		self.api.assert_not_called()


class TestOutgoingDefault(unittest.TestCase):
	def test_settings_choice_wins_even_when_inactive(self):
		with patch.object(frappe.db, "get_single_value", return_value="Dueño"), \
			patch.object(frappe.db, "exists", return_value=True), \
			patch.object(frappe, "get_all") as flags:
			self.assertEqual(utils.outgoing_default(), "Dueño")
		flags.assert_not_called()

	def test_empty_settings_adopt_only_one_unambiguous_active_flag(self):
		with patch.object(frappe.db, "get_single_value", return_value=None):
			with patch.object(frappe, "get_all", return_value=["Tienda"]) as flags:
				self.assertEqual(utils.outgoing_default(), "Tienda")
			self.assertEqual(flags.call_args.kwargs["filters"], {"is_default_outgoing": 1, "status": "Active"})
			with patch.object(frappe, "get_all", return_value=["Tienda", "Dueño"]):
				self.assertIsNone(utils.outgoing_default())
			with patch.object(frappe, "get_all", return_value=[]):
				self.assertIsNone(utils.outgoing_default())

	def test_deleted_settings_choice_is_not_trusted(self):
		with patch.object(frappe.db, "get_single_value", return_value="Gone"), \
			patch.object(frappe.db, "exists", return_value=False), \
			patch.object(frappe, "get_all", return_value=[]):
			self.assertIsNone(utils.outgoing_default())


class TestLegacySendTemplateGuards(unittest.TestCase):
	def setUp(self):
		self.reference = Mock(doctype="Sales Invoice")
		self.reference.name = "SINV-1"
		self.enterContext(patch.object(frappe.utils, "get_url", return_value="https://shop.example.test"))

	def test_private_file_of_another_document_is_refused_before_save(self):
		outgoing = Mock()
		with patch.object(frappe, "get_doc", side_effect=[self.reference, outgoing]), \
			patch.object(frappe, "get_all", return_value=[]) as files, \
			patch.object(frappe, "get_hooks", return_value=[]):
			with self.assertRaises(frappe.PermissionError):
				send_template("526691234567", "Sales Invoice", "SINV-1", "tpl", attach="/private/files/other.pdf")
		self.assertEqual(files.call_args.kwargs["filters"]["attached_to_name"], "SINV-1")
		outgoing.save.assert_not_called()

	def test_accepted_send_reports_its_real_status(self):
		outgoing = Mock(status="Queued")
		outgoing.name = "WA-1"
		with patch.object(frappe, "get_doc", side_effect=[self.reference, outgoing]), \
			patch.object(frappe, "get_hooks", return_value=[]):
			self.assertEqual(send_template("526691234567", "Sales Invoice", "SINV-1", "tpl"), {"name": "WA-1", "status": "Queued"})


if __name__ == "__main__":
	unittest.main()


class TestControllerBoundary(unittest.TestCase):
	"""Every outgoing send (API insert, CRM composer, retry) passes the business
	guard and the site-file check before anything is uploaded or queued."""

	def setUp(self):
		self.account = frappe._dict(name="acct", url="https://graph.facebook.com", version="v23.0", phone_id="991122")
		self.account.get_password = Mock(return_value="fake-token")
		self.enterContext(patch.object(frappe, "get_doc", return_value=self.account))
		self.enterContext(patch.object(frappe.utils, "get_url", return_value="https://shop.example.test"))
		self.enterContext(patch.object(frappe, "get_installed_apps", return_value=[]))
		self.api = self.enterContext(patch.object(transport, "api", return_value={"messages": [{"id": "wamid.x"}]}))
		self.raw = self.enterContext(patch.object(transport, "raw"))
		self.enterContext(patch("frappe_whatsapp.coexistence.assert_sendable"))

	def message(self, **extra):
		return TestSessionDocument.message(self, **extra)

	def test_business_guard_sees_the_message_before_any_upload(self):
		guard = Mock(side_effect=frappe.PermissionError("Solo el dueño"))
		with patch.object(frappe, "get_hooks", return_value=["app.guard"]), \
			patch.object(frappe, "get_attr", return_value=guard), \
			patch.object(WhatsAppMessage, "_upload_local_media") as upload:
			doc = self.message()
			with self.assertRaises(frappe.PermissionError):
				doc.send_outgoing()
		self.assertIs(guard.call_args.kwargs["message"], doc)
		self.assertEqual(guard.call_args.kwargs["whatsapp_account"], "acct")
		upload.assert_not_called()
		self.api.assert_not_called()

	def _site(self, rows, readable=True):
		from pathlib import Path

		self.enterContext(patch.object(frappe, "get_hooks", return_value=[]))
		self.enterContext(patch.object(frappe, "get_site_path", side_effect=lambda *parts: str(Path("/fictional/site").joinpath(*parts))))
		self.enterContext(patch.object(Path, "is_file", return_value=True))
		self.enterContext(patch.object(frappe.db, "get_value", side_effect=lambda dt, filters, *a, **k: rows.get(filters.get("file_url")) if isinstance(filters, dict) else None))
		self.perm = self.enterContext(patch.object(frappe, "has_permission", return_value=readable))

	def test_private_file_without_its_file_row_never_reaches_meta(self):
		self._site({})
		with self.assertRaises(frappe.PermissionError):
			self.message(attach="/private/files/other-customer.pdf").send_outgoing()
		self.raw.assert_not_called()
		self.api.assert_not_called()

	def test_private_file_the_sender_cannot_read_never_reaches_meta(self):
		self._site({"/private/files/inv.pdf": "FILE-1"}, readable=False)
		with self.assertRaises(frappe.PermissionError):
			self.message(attach="/private/files/inv.pdf").send_outgoing()
		self.assertEqual(self.perm.call_args.kwargs.get("doc"), "FILE-1")
		self.raw.assert_not_called()

	def test_traversal_out_of_the_files_directory_is_refused(self):
		self._site({"/private/files/../../site_config.json": "FILE-X"})
		with self.assertRaises(frappe.ValidationError):
			self.message(attach="/private/files/../../site_config.json").send_outgoing()
		self.raw.assert_not_called()


	def test_an_alias_of_a_site_path_is_refused_not_resolved(self):
		# The business guard judges the File of the URL as written; uploading the
		# canonical file behind an alias would send a document it never judged.
		self._site({"/private/files/inv.pdf": "FILE-1", "/files/cat.pdf": "FILE-2"})
		for alias in (
			"/private/files/./inv.pdf",
			"/private/files//inv.pdf",
			"/private/files/x/../inv.pdf",
			"/private/files/%2e/inv.pdf",
			"https://shop.example.test/private/files/./inv.pdf",
			"/files/./cat.pdf",
		):
			# `//` reads as an absolute path and meets the traversal refusal first.
			with self.subTest(alias=alias), self.assertRaises((frappe.PermissionError, frappe.ValidationError)):
				self.message(attach=alias).send_outgoing()
		self.raw.assert_not_called()
		self.api.assert_not_called()

class TestExactWindow(unittest.TestCase):
	A = frappe._dict(name="A", phone_id="PID-A", app_id="APP", status="Active")

	def test_exact_checks_only_the_given_spelling(self):
		def evidence(phone_id, app_id, peer, now=None):
			return frappe._dict(receipt="R", timestamp=NOW - 5) if peer == "5216691234567" else None

		with patch.object(window, "evidence", side_effect=evidence):
			self.assertFalse(window.is_open(self.A, "526691234567", now=NOW, exact=True).open)
			self.assertTrue(window.is_open(self.A, "5216691234567", now=NOW, exact=True).open)
			self.assertTrue(window.is_open(self.A, "526691234567", now=NOW).open)
