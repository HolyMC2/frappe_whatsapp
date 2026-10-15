"""ARC-S2.1 / D5: the public→private migration of existing WhatsApp media."""

import os
from unittest.mock import patch
from uuid import uuid4

import frappe
from frappe.tests import IntegrationTestCase

from frappe_whatsapp.patches import private_inbound_media as migration


def _message():
	name = "wa-privacy-" + uuid4().hex[:12]
	frappe.get_doc(
		{
			"doctype": "WhatsApp Message",
			"name": name,
			"type": "Incoming",
			"from": "5215550000000",
			"message_id": "wamid." + name,
			"content_type": "document",
			"message": "",
			"owner": "Administrator",
			"modified_by": "Administrator",
			"creation": frappe.utils.now_datetime(),
			"modified": frappe.utils.now_datetime(),
			"docstatus": 0,
			"idx": 0,
		}
	).db_insert()
	return name


def _public_file(content: bytes, **attached) -> "frappe.Document":
	return frappe.get_doc(
		{"doctype": "File", "file_name": "cliente-" + uuid4().hex[:6] + ".txt", "content": content,
		 "is_private": 0, **attached}
	).insert(ignore_permissions=True)


def _whatsapp_file(content: bytes):
	msg = _message()
	file = _public_file(
		content, attached_to_doctype="WhatsApp Message", attached_to_name=msg, attached_to_field="attach"
	)
	frappe.db.set_value("WhatsApp Message", msg, "attach", file.file_url, update_modified=False)
	return msg, file


def _disk(url: str) -> str:
	if url.startswith("/private/files/"):
		return frappe.get_site_path("private", "files", url.rsplit("/", 1)[-1])
	return frappe.get_site_path("public", "files", url.rsplit("/", 1)[-1])


class TestPrivateInboundMediaMigration(IntegrationTestCase):
	def setUp(self):
		frappe.set_user("Administrator")
		self.mine: set[str] = set()
		real = migration._candidates
		self.enterContext(
			patch.object(migration, "_candidates", lambda: [r for r in real() if r.name in self.mine])
		)
		# apply() commits per file; keep the test transaction intact.
		self.enterContext(patch.object(frappe.db, "commit"))
		self.addCleanup(frappe.db.rollback)

	def track(self, file):
		self.mine.add(file.name)
		return file

	def test_report_is_read_only_and_plans_a_move(self):
		msg, file = _whatsapp_file(b"fictional INE " + uuid4().hex.encode())
		self.track(file)
		report = migration.report()
		self.assertTrue(report["dry_run"])
		self.assertEqual(report["public_whatsapp_files"], 1)
		self.assertEqual(report["actions"], {"move": 1})
		self.assertEqual(frappe.db.get_value("File", file.name, "is_private"), 0)
		self.assertTrue(os.path.exists(_disk(file.file_url)))

	def test_apply_waits_for_d5(self):
		with self.assertRaises(frappe.ValidationError):
			migration.apply()
		with self.assertRaises(frappe.ValidationError):
			migration.apply(confirm="yes")

	def test_apply_moves_bytes_and_rewrites_every_live_referrer(self):
		msg, file = _whatsapp_file(b"fictional factura " + uuid4().hex.encode())
		self.track(file)
		old_url = file.file_url
		comment = frappe.get_doc(
			{"doctype": "Comment", "comment_type": "Comment", "reference_doctype": "WhatsApp Message",
			 "reference_name": msg, "content": f'<a href="{old_url}">factura</a>'}
		).insert(ignore_permissions=True)

		self.assertEqual(migration.report()["referrers_by_field"].get("Comment.content"), 1)
		result = migration.apply(confirm="D5")

		self.assertEqual(result["move"], 1)
		new_url = frappe.db.get_value("File", file.name, "file_url")
		self.assertTrue(new_url.startswith("/private/files/"), new_url)
		self.assertEqual(frappe.db.get_value("File", file.name, "is_private"), 1)
		self.assertFalse(os.path.exists(_disk(old_url)))
		self.assertTrue(os.path.exists(_disk(new_url)))
		self.assertEqual(frappe.db.get_value("WhatsApp Message", msg, "attach"), new_url)
		self.assertIn(new_url, frappe.db.get_value("Comment", comment.name, "content"))
		self.assertNotIn(f'"{old_url}"', frappe.db.get_value("Comment", comment.name, "content"))
		self.assertEqual(frappe.get_doc("File", file.name).get_content(), file.get_content())

	def test_shared_bytes_get_a_private_copy_and_the_other_row_stays_public(self):
		content = b"fictional shared photo " + uuid4().hex.encode()
		product = _public_file(content)  # e.g. a product photo with the same bytes
		msg, file = _whatsapp_file(content)
		self.track(file)
		self.assertEqual(product.file_url, file.file_url)  # Frappe deduped the bytes

		self.assertEqual(migration.report()["actions"], {"copy": 1})
		migration.apply(confirm="D5")

		new_url = frappe.db.get_value("File", file.name, "file_url")
		self.assertTrue(new_url.startswith("/private/files/"))
		self.assertEqual(frappe.db.get_value("WhatsApp Message", msg, "attach"), new_url)
		self.assertEqual(frappe.db.get_value("File", product.name, "file_url"), product.file_url)
		self.assertEqual(frappe.db.get_value("File", product.name, "is_private"), 0)
		self.assertTrue(os.path.exists(_disk(product.file_url)))
		self.assertTrue(os.path.exists(_disk(new_url)))

	def test_missing_bytes_are_reported_and_left_alone(self):
		msg, file = _whatsapp_file(b"fictional lost " + uuid4().hex.encode())
		self.track(file)
		os.remove(_disk(file.file_url))
		report = migration.report()
		self.assertEqual(report["missing_on_disk"], [file.name])
		self.assertEqual(migration.apply(confirm="D5")["missing"], 1)
		self.assertEqual(frappe.db.get_value("File", file.name, "is_private"), 0)
