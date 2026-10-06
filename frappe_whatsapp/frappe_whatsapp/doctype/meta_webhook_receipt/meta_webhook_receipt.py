"""Private inbound evidence. Only the receipt service mutates lifecycle state."""
import frappe
from frappe.model.document import Document


class MetaWebhookReceipt(Document):
    def validate(self):
        if not self.is_new():
            frappe.throw(frappe._("This WhatsApp notice cannot be edited."))
