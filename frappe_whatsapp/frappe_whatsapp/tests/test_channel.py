"""One site-wide WhatsApp channel: API when connected, wa.me otherwise, never both."""

import unittest
from unittest.mock import patch
from urllib.parse import unquote

from frappe_whatsapp import channel
from frappe_whatsapp.patches import unify_channel_mode


class TestResolveMode(unittest.TestCase):
	def resolve(self, mode, ready):
		values = {"channel_mode": mode}
		with patch.object(channel, "_setting", side_effect=values.get), \
			patch.object(channel, "api_ready", return_value=ready):
			return channel.resolve_mode()

	def test_auto_follows_the_connected_account(self):
		self.assertEqual(self.resolve("Auto", True), "api")
		self.assertEqual(self.resolve("Auto", False), "manual")

	def test_unset_mode_behaves_as_auto(self):
		self.assertEqual(self.resolve(None, True), "api")
		self.assertEqual(self.resolve(None, False), "manual")

	def test_override_wins_over_a_connected_account(self):
		self.assertEqual(self.resolve("Manual", True), "manual")
		self.assertEqual(self.resolve("Off", True), "off")

	def test_api_needs_an_active_default_account(self):
		with patch.object(channel, "_setting", return_value=None), \
			patch("frappe_whatsapp.utils.outgoing_default", return_value=None):
			self.assertFalse(channel.api_ready())
		with patch.object(channel, "_setting", return_value="Cuenta"), \
			patch.object(channel.frappe.db, "get_value", return_value="Inactive"):
			self.assertFalse(channel.api_ready())


class TestDigitsAndLinks(unittest.TestCase):
	def test_national_number_gets_the_region_code(self):
		self.assertEqual(channel.wa_digits("55 1234 5678", "MX"), "525512345678")

	def test_legacy_mobile_prefix_is_folded(self):
		self.assertEqual(channel.wa_digits("+52 1 55 1234 5678", "MX"), "525512345678")
		self.assertEqual(channel.wa_digits("5215512345678", "MX"), "525512345678")

	def test_explicit_foreign_number_is_kept(self):
		self.assertEqual(channel.wa_digits("+1 415 555 2671", "MX"), "14155552671")

	def test_unusable_numbers_return_none(self):
		for raw in (None, "", "abc", "12345"):
			self.assertIsNone(channel.wa_digits(raw, "MX"), raw)

	def test_link_encodes_the_prefill(self):
		url = channel.wa_link("525512345678", "Folio RO-1 & listo?")
		self.assertTrue(url.startswith("https://wa.me/525512345678?text="))
		self.assertEqual(unquote(url.split("text=", 1)[1]), "Folio RO-1 & listo?")
		self.assertEqual(channel.wa_link("525512345678"), "https://wa.me/525512345678")


class TestUnifyPatch(unittest.TestCase):
	def run_patch(self, stored):
		writes = {}
		with patch.object(unify_channel_mode, "_single", side_effect=lambda dt, f: stored.get((dt, f))), \
			patch.object(unify_channel_mode.frappe.db, "set_single_value", side_effect=lambda dt, f, v: writes.__setitem__(f, v)):
			unify_channel_mode.execute()
		return writes

	def test_marketing_deeplink_becomes_manual_with_the_shop_phone(self):
		writes = self.run_patch({("Marketing Settings", "channel_tier"): "shop_session",
			("Marketing Settings", "channel_shop_phone"): "+52 55 0000 0000"})
		self.assertEqual(writes, {"channel_mode": "Manual", "shop_whatsapp_number": "+52 55 0000 0000"})

	def test_stricter_choice_wins(self):
		writes = self.run_patch({("Marketing Settings", "channel_tier"): "waba",
			("Taller App Settings", "whatsapp_mode"): "Off"})
		self.assertEqual(writes["channel_mode"], "Off")

	def test_nothing_stored_means_auto(self):
		self.assertEqual(self.run_patch({}), {"channel_mode": "Auto"})
