"""The shop's WhatsApp on its tickets: learned from Meta's messages, typed number first, switchable.

Bench-free: every site read is patched, so no database and no Meta request.
"""

import unittest
from unittest.mock import patch

import frappe

from frappe_whatsapp import channel


class _Meta:
	def has_field(self, fieldname):
		return fieldname in ("display_phone_number", "print_on_tickets")


class TestDisplayNumber(unittest.TestCase):
	def test_meta_metadata_gives_the_number(self):
		self.assertEqual(channel.display_number({"display_phone_number": "+52 669 197 7032"}), "+52 669 197 7032")
		self.assertEqual(channel.display_number({"display_phone_number": "15550001111"}), "15550001111")

	def test_anything_else_is_ignored(self):
		for metadata in (None, {}, {"display_phone_number": ""}, {"display_phone_number": 52669},
				{"display_phone_number": "llámanos"}, {"display_phone_number": "+52 669 <b>1</b>"}):
			self.assertIsNone(channel.display_number(metadata), metadata)


class TestRememberDisplayNumber(unittest.TestCase):
	def setUp(self):
		self.account = frappe._dict(name="Tienda", display_phone_number="")

	def test_a_new_number_is_stored_once(self):
		with patch.object(frappe, "get_meta", return_value=_Meta()), patch.object(frappe.db, "set_value") as write:
			channel.remember_display_number(self.account, {"display_phone_number": "+52 669 197 7032"})
			channel.remember_display_number(self.account, {"display_phone_number": "+52 669 197 7032"})
		write.assert_called_once_with("WhatsApp Account", "Tienda", "display_phone_number", "+52 669 197 7032",
			update_modified=False)

	def test_no_number_no_write(self):
		with patch.object(frappe.db, "set_value") as write:
			channel.remember_display_number(self.account, {"phone_number_id": "123"})
		write.assert_not_called()


class TestTicketNumber(unittest.TestCase):
	def run_with(self, settings, account_number: str | None = "+52 1 669 197 7032"):
		def setting(fieldname):
			return settings.get(fieldname)

		with patch.object(channel, "_setting", side_effect=setting), \
				patch.object(channel, "site_region", return_value="MX"), \
				patch.object(frappe, "get_meta", return_value=_Meta()), \
				patch.object(frappe.db, "get_value", return_value=account_number):
			return channel.ticket_number()

	def test_the_connected_account_number_prints_in_international_format(self):
		self.assertEqual(self.run_with({"default_outgoing_account": "Tienda"}), "+52 669 197 7032")

	def test_the_number_the_shop_typed_wins(self):
		self.assertEqual(self.run_with({"default_outgoing_account": "Tienda", "shop_whatsapp_number": "669 111 2233"}),
			"+52 669 111 2233")

	def test_switched_off_prints_nothing(self):
		self.assertEqual(self.run_with({"default_outgoing_account": "Tienda", "print_on_tickets": 0}), "")

	def test_no_number_yet_prints_nothing(self):
		self.assertEqual(self.run_with({"default_outgoing_account": "Tienda"}, account_number=None), "")


if __name__ == "__main__":
	unittest.main()
