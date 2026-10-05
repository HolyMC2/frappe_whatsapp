# Copyright (c) 2022, Shridhar Patil and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document


class WhatsAppSettings(Document):
	def on_update(self):
		"""Settings is the default-outgoing authority; the account flag follows it.

		db.set_value, not save(): an account's own on_update would write back here.
		"""
		chosen = self.default_outgoing_account
		if not chosen or not frappe.db.exists("WhatsApp Account", chosen):
			return
		for name in frappe.get_all("WhatsApp Account", filters={"is_default_outgoing": 1}, pluck="name"):
			if name != chosen:
				frappe.db.set_value("WhatsApp Account", name, "is_default_outgoing", 0)
		if not frappe.db.get_value("WhatsApp Account", chosen, "is_default_outgoing"):
			frappe.db.set_value("WhatsApp Account", chosen, "is_default_outgoing", 1)
