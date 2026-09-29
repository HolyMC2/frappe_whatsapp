"""A caller's field spec as WhatsApp Flow rows: Meta limits, screen ids, no Meta call."""

import json
import unittest

import frappe

from frappe_whatsapp import flow_builder


class TestFlowRows(unittest.TestCase):
	def spec(self, *fields, screens=None):
		return {"flow_name": "Quote", "screens": screens or [{"title": "Your details", "fields": list(fields)}]}

	def test_last_screen_is_terminal_and_ids_are_letters(self):
		spec = self.spec(
			screens=[
				{"title": "One", "fields": [{"name": "a", "type": "TextInput", "label": "A"}]},
				{"title": "Empty", "fields": []},
				{"title": "Two", "fields": [{"name": "b", "type": "OptIn", "label": "B"}]},
			]
		)
		screens, fields = flow_builder._rows(spec)
		self.assertEqual([s["screen_id"] for s in screens], ["SCREEN_A", "SCREEN_B"])
		self.assertEqual([s["terminal"] for s in screens], [0, 1])
		self.assertEqual([f["screen"] for f in fields], ["SCREEN_A", "SCREEN_B"])
		self.assertEqual(flow_builder._screen_id(26), "SCREEN_AA")

	def test_long_label_is_clipped_and_kept_as_helper(self):
		label = "What is the best phone number to reach you?"
		_screens, fields = flow_builder._rows(self.spec({"name": "phone", "type": "TextInput", "label": label, "required": True}))
		self.assertLessEqual(len(fields[0]["label"]), flow_builder.LABEL_LIMITS["TextInput"])
		self.assertEqual(fields[0]["helper_text"], label)
		self.assertEqual(fields[0]["required"], 1)

	def test_dropdown_options_are_bounded(self):
		options = [{"id": f"o{i}", "title": "x" * 50} for i in range(250)]
		_screens, fields = flow_builder._rows(self.spec({"name": "pick", "type": "Dropdown", "label": "Pick", "options": options}))
		stored = json.loads(fields[0]["options"])
		self.assertEqual(len(stored), flow_builder.OPTION_LIMIT)
		self.assertTrue(all(len(o["title"]) <= flow_builder.OPTION_TITLE_LIMIT for o in stored))

	def test_invalid_specs_are_refused(self):
		for spec in (
			self.spec(),
			self.spec({"name": "x", "type": "PhotoPicker", "label": "X"}),
			self.spec({"name": "bad name", "type": "TextInput", "label": "X"}),
			self.spec({"name": "pick", "type": "Dropdown", "label": "Pick", "options": []}),
		):
			with self.assertRaises(frappe.ValidationError):
				flow_builder._rows(spec)

	def test_spec_hash_is_order_insensitive_for_keys(self):
		self.assertEqual(flow_builder.spec_hash({"a": 1, "b": [1, 2]}), flow_builder.spec_hash({"b": [1, 2], "a": 1}))
		self.assertNotEqual(flow_builder.spec_hash({"a": 1}), flow_builder.spec_hash({"a": 2}))
