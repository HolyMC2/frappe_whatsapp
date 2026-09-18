"""A delivery receipt reaches the outgoing row that stored the number we dialled.

Meta answers a Mexican mobile's receipt with `recipient_id` 521 + the 10 national
digits, while a legacy send stores the 52 + 10 spelling it dialled. The signed
receipt path bound the row to the reported spelling alone, so every sent,
delivered, read and failed receipt for those sends was dropped as
`message_not_found` and the message stayed on «Enviando…» (docomexico
2026-09-17, 15 receipts that day). No Meta request leaves the process.
"""

import unittest
from unittest.mock import patch

import frappe

from frappe_whatsapp.utils import webhook
from frappe_whatsapp.webhook_receipts import ReceiptError

DIALLED = "525550001111"  # what a legacy producer sends and stores
REPORTED = "5215550001111"  # what Meta reports for that same mobile
OTHER = "5215550009999"


class Row:
    def __init__(self):
        self.status = "sent"
        self.failure_reason = "stale"
        self.saved = False

    def save(self, ignore_permissions=False):
        self.saved = True


class TestRecipientSpellings(unittest.TestCase):
    def test_a_mexican_mobile_is_known_by_both_spellings(self):
        self.assertEqual(webhook._recipient_spellings(REPORTED), [REPORTED, DIALLED])
        self.assertEqual(webhook._recipient_spellings(DIALLED), [DIALLED, REPORTED])

    def test_every_other_recipient_keeps_exactly_one_spelling(self):
        # Neither a dialled 52 + 10 nor a reported 521 + 10: another country, a
        # short or long number, and anything that is not plain digits.
        for value in (REPORTED + "1", "15550001111", "5255500011", "", "52x5550001111", "5215550001111 "):
            with self.subTest(value=value):
                self.assertEqual(webhook._recipient_spellings(value), [value])


class TestStatusLookup(unittest.TestCase):
    """The row lookup `_apply_message_status` runs on the signed-receipt path."""

    def apply(self, recipient_id, stored_to):
        entry = {"id": "wamid.receipt-fixture", "status": "delivered", "recipient_id": recipient_id}
        self.row, self.looked_up = Row(), []

        def get_value(doctype, filters=None, *args, **kwargs):
            self.looked_up.append(filters["to"])
            return "wm-fixture" if filters["to"] == stored_to else None

        with patch.dict(frappe.flags, {"meta_webhook_receipt": "receipt-fixture"}, clear=False), \
                patch("frappe_whatsapp.delivery.fold_native_delivery", return_value={"matched": False}), \
                patch.object(frappe.db, "get_value", side_effect=get_value), \
                patch.object(frappe, "get_doc", return_value=self.row):
            webhook._apply_message_status(entry, ["Doco Ventas"])

    def test_the_row_that_stored_the_dialled_number_is_found_and_updated(self):
        self.apply(REPORTED, DIALLED)
        self.assertEqual(self.looked_up, [REPORTED, DIALLED])
        self.assertEqual((self.row.status, self.row.failure_reason, self.row.saved), ("delivered", None, True))

    def test_the_reported_spelling_wins_when_a_row_carries_it(self):
        self.apply(REPORTED, REPORTED)
        self.assertEqual(self.looked_up, [REPORTED])
        self.assertEqual((self.row.status, self.row.saved), ("delivered", True))

    def test_another_number_is_still_not_this_recipient(self):
        with self.assertRaises(ReceiptError) as caught:
            self.apply(OTHER, DIALLED)
        self.assertEqual(caught.exception.reason, "message_not_found")
        self.assertEqual((self.row.status, self.row.saved), ("sent", False))
