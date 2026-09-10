"""Private inbound evidence. Only the receipt service mutates lifecycle state."""
import frappe
from frappe.model.document import Document


class MetaWebhookReceipt(Document):
    def validate(self):
        if not self.is_new():
            frappe.throw("Webhook receipts are immutable outside their worker lifecycle.")
