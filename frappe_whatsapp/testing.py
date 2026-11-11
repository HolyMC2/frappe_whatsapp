import frappe

try:
    from frappe.tests import IntegrationTestCase
except ImportError:
    from frappe.tests.utils import FrappeTestCase as IntegrationTestCase


def make_default_account(name):
    """Make `name` the only default incoming and outgoing account.

    Outgoing sends resolve through WhatsApp Settings first
    (utils.outgoing_default), and every account insert flagged as default
    writes itself there, so the account flags alone leave the choice to
    whichever test module inserted its account last.
    db.set_value and set_single_value skip on_update, so nothing writes back.
    """
    frappe.db.sql("UPDATE `tabWhatsApp Account` SET is_default_outgoing=0, is_default_incoming=0")
    frappe.db.set_value("WhatsApp Account", name, {"is_default_outgoing": 1, "is_default_incoming": 1})
    frappe.db.set_single_value("WhatsApp Settings", "default_outgoing_account", name)
