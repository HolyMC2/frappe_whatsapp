"""1f3707c: a send Meta accepted is not failed because Meta answers with its canonical wa_id.

Mexican mobiles are sent as 52… and Meta answers `wa_id` 521… with our number
echoed as `input`. Before the fix both the native gateway and the legacy
transport required `wa_id` to equal the number sent, so delivered messages
raised, WhatsApp Send Review marked them Fallido and the daily retry sent them
again. Provider doubles only: no Meta request leaves the process.
"""

import json
import time
import unittest
from unittest.mock import Mock, patch

import frappe

from frappe_whatsapp import legacy_outbox, native_outbox, transport
from frappe_whatsapp.frappe_whatsapp.tests.test_native_outbox import Response

SENT = "525550001111"  # the short spelling a producer sends
CANONICAL = "5215550001111"  # the id Meta answers for the same mobile
OTHER = "525550009999"


def accepted(contact, message_id="wamid.echo-fixture"):
    return Response(payload={"messaging_product": "whatsapp", "contacts": [contact], "messages": [{"id": message_id}]})


class TestClassifyEcho(unittest.TestCase):
    def classify(self, contact, peer=SENT):
        response = accepted(contact)
        return native_outbox._classify(response, peer, time.monotonic() + 30)

    def test_exact_echo_accepts_the_canonical_id(self):
        self.assertEqual(self.classify({"input": SENT, "wa_id": CANONICAL}),
                         {"state": "Accepted", "provider_message_id": "wamid.echo-fixture"})

    def test_matching_id_without_echo_is_still_accepted(self):
        self.assertEqual(self.classify({"wa_id": SENT})["state"], "Accepted")

    def test_without_echo_a_different_id_proves_nothing(self):
        with self.assertRaises(ValueError):
            self.classify({"wa_id": CANONICAL})

    def test_echo_of_another_number_is_not_our_recipient(self):
        for contact in ({"input": OTHER, "wa_id": CANONICAL}, {"input": CANONICAL, "wa_id": CANONICAL},
                        {"input": "+" + SENT, "wa_id": CANONICAL}):
            with self.subTest(contact=contact), self.assertRaises(ValueError):
                self.classify(contact)

    def test_echo_never_launders_a_malformed_id(self):
        for wa_id in (None, "", "521-555", "wamid.x", 5215550001111, "5" * 41):
            contact = {"input": SENT} if wa_id is None else {"input": SENT, "wa_id": wa_id}
            with self.subTest(wa_id=wa_id), self.assertRaises(ValueError):
                self.classify(contact)


class TestLegacyTransportEcho(unittest.TestCase):
    """The legacy path every automatic notice (e.g. «orden recibida») takes."""

    def setUp(self):
        self.account = frappe._dict(name="fictional-echo-account", phone_id="9941001", version="v23.0",
                                    mode="Live", status="Active")
        self.account.get_password = Mock(return_value="fictional-token")
        self.url = "https://graph.facebook.com/v23.0/9941001/messages"
        self.enterContext(patch.object(frappe, "flags", frappe._dict()))
        self.enterContext(patch.object(frappe, "get_installed_apps", return_value=[]))
        self.enterContext(patch.object(transport, "resolve_account", return_value=self.account))
        self.enterContext(patch.object(frappe.db, "get_values", return_value=[self.account]))
        self.http = self.enterContext(patch.object(transport.requests, "request"))
        self.enterContext(patch.object(transport, "make_post_request", side_effect=AssertionError("old helper")))
        self.enterContext(patch.object(frappe, "log_error", side_effect=AssertionError("no error log for a delivery")))

    def send(self, to=SENT):
        body = {"messaging_product": "whatsapp", "to": to, "type": "text", "text": {"body": "Tu orden fue recibida"}}
        return transport.api(self.account, "POST", self.url, data=json.dumps(body))

    def test_canonical_answer_is_a_success_with_the_provider_id(self):
        self.http.return_value = accepted({"input": SENT, "wa_id": CANONICAL}, "wamid.orden-recibida")
        result = self.send()
        self.assertEqual(result["messages"][0]["id"], "wamid.orden-recibida")
        self.assertEqual(result["contacts"], [{"input": SENT, "wa_id": SENT}])
        self.assertEqual(self.http.call_count, 1)
        self.assertTrue(self.http.return_value.closed)

    def test_unproven_recipient_raises_unknown_not_failed(self):
        for contact in ({"wa_id": CANONICAL}, {"input": OTHER, "wa_id": CANONICAL}):
            with self.subTest(contact=contact):
                self.http.return_value = accepted(contact)
                with self.assertRaises(legacy_outbox.LegacyMessageError) as raised:
                    self.send()
                self.assertEqual((raised.exception.reason_code, raised.exception.outcome),
                                 ("provider_response_uncertain", "Unknown"))
                self.assertEqual(frappe.flags.integration_request.json(),
                                 {"error": {"message": "provider_response_uncertain"}})
