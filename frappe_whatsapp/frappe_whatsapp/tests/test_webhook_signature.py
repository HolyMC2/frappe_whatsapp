"""HTTP boundary proofs; synthetic credentials, no transport or ingestion writes."""
import copy
import hashlib
import hmac
import json
import unittest
from unittest.mock import patch

import frappe
from werkzeug.test import EnvironBuilder
from werkzeug.wrappers import Request
from frappe_whatsapp.utils import signature, webhook


class Account(frappe._dict):
    def get_password(self, field, **kwargs):
        return self.secret


def account(name="a", app="app-a", secret="secret-a", business="waba-a", phone="phone-a", **kwargs):
    return Account(name=name, app_id=app, secret=secret, business_id=business,
                   phone_id=phone, status="Active", mode="Live", **kwargs)


def payload(phone="phone-a", business="waba-a"):
    return {"object": "whatsapp_business_account", "entry": [{"id": business,
            "changes": [{"field": "messages", "value": {"metadata": {"phone_number_id": phone},
            "messages": [{"id": "wamid.fictional", "from": "15555550100", "type": "text",
                          "text": {"body": "fictional"}}]}}]}]}


class TestSignature(unittest.TestCase):
    def setUp(self):
        self.accounts = [account(), account("b", "app-b", "secret-b", "waba-b", "phone-b")]

    def verify(self, data=None, raw=None, secret="secret-a", header="auto", consume=False):
        raw = raw if raw is not None else json.dumps(data if data is not None else payload()).encode()
        if header == "auto":
            header = "sha256=" + hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
        req = Request(EnvironBuilder(method="POST", data=raw, content_type="application/json",
                      headers={"X-Hub-Signature-256": header} if header is not None else {}).get_environ())
        with patch.object(frappe, "request", req), patch.object(signature, "_accounts", return_value=self.accounts):
            return webhook.post() if consume else signature.verify_request()

    def rejected(self, **kwargs):
        # process_change is the first possible domain/log write. Authentication
        # must finish across the WHOLE batch before it can be invoked once.
        with patch("frappe_whatsapp.webhook_receipts.record_events") as process:
            with self.assertRaises(frappe.PermissionError):
                self.verify(consume=True, **kwargs)
            process.assert_not_called()

    def test_valid_raw_signature_routes_exact_account(self):
        result = self.verify()
        self.assertEqual(result[0].accounts, ("a",))
        self.assertEqual(result[0].app_id, "app-a")

    def test_missing_secret_fails_closed(self):
        self.accounts[0].secret = ""
        self.rejected()

    def test_no_accounts_fails_closed(self):
        self.accounts = []
        self.rejected()

    def test_missing_signature_rejected(self):
        self.rejected(header=None)

    def test_malformed_signatures_rejected(self):
        for header in ("", "sha1=" + "a" * 40, "sha256=no", "sha256=" + "a" * 65, "sha256=ñ"):
            with self.subTest(header=header):
                self.rejected(header=header)

    def test_wrong_secret_rejected(self):
        self.rejected(secret="attacker")

    def test_tampered_raw_body_rejected(self):
        raw = json.dumps(payload()).encode()
        self.rejected(raw=raw + b" ", header=signature.expected_signature("secret-a", raw))

    def test_raw_body_is_only_payload_consumed(self):
        data = payload()
        raw = json.dumps(data, indent=3).encode()
        with patch.object(frappe.local, "form_dict", payload("phone-b", "waba-b")), \
                patch("frappe_whatsapp.webhook_receipts.record_events") as process:
            self.verify(raw=raw, consume=True)
            self.assertEqual(process.call_args.args[0][0]["account_id"], "phone-a")
            self.assertEqual(process.call_args.args[0][0]["payload"]["change"]["value"]["messages"],
                             data["entry"][0]["changes"][0]["value"]["messages"])

    def test_foreign_phone_denied(self):
        self.rejected(data=payload("foreign"))

    def test_unconfigured_waba_denied_even_if_payload_says_none(self):
        self.accounts[0].business_id = None
        self.rejected(data=payload(business="None"))

    def test_foreign_waba_denied(self):
        self.rejected(data=payload(business="foreign"))

    def test_known_different_app_denied(self):
        self.rejected(data=payload("phone-b", "waba-b"))

    def test_valid_first_foreign_later_produces_no_writes(self):
        data = payload()
        data["entry"].extend(payload("phone-b", "waba-b")["entry"])
        self.rejected(data=data)

    def test_disabled_and_demo_accounts_denied(self):
        for key, value in (("status", "Inactive"), ("mode", "Demo")):
            with self.subTest(key=key):
                self.accounts[0][key] = value
                self.rejected()
                self.accounts[0][key] = "Active" if key == "status" else "Live"

    def test_missing_app_id_denied(self):
        self.accounts[0].app_id = None
        self.rejected()

    def test_ambiguous_app_secret_denied(self):
        self.accounts[1].secret = "secret-a"
        self.rejected()

    def test_phone_must_be_unique_in_app_waba(self):
        self.accounts.append(account("duplicate"))
        self.rejected()

    def test_multiple_phones_same_app_all_changes_retained(self):
        self.accounts.append(account("c", phone="phone-c"))
        data = payload()
        data["entry"][0]["changes"].extend(payload("phone-c")["entry"][0]["changes"])
        self.assertEqual([r.accounts for r in self.verify(data=data)], [("a",), ("c",)])

    def test_template_scope_is_waba_and_app(self):
        data = payload()
        data["entry"][0]["changes"] = [{"field": "message_template_status_update",
           "value": {"event": "APPROVED", "message_template_id": "template-a"}}]
        self.assertEqual(self.verify(data=data)[0].accounts, ("a",))

    def test_message_status_requires_phone(self):
        data = payload()
        data["entry"][0]["changes"][0]["value"] = {"statuses": [{"id": "wamid.x", "status": "read"}]}
        self.rejected(data=data)

    def test_malformed_envelopes_denied(self):
        for data in ([], {}, {"object": "page", "entry": []},
                     {"object": "whatsapp_business_account", "entry": {}},
                     {"object": "whatsapp_business_account", "entry": [None]}):
            with self.subTest(data=data):
                self.rejected(data=data)

    def test_duplicate_keys_denied(self):
        self.rejected(raw=b'{"object":"page","object":"whatsapp_business_account","entry":[]}')

    def test_invalid_json_denied(self):
        self.rejected(raw=b'{')

    def test_oversized_body_denied(self):
        self.rejected(raw=b' ' * (signature.MAX_BODY_BYTES + 1))

    def test_no_http_request_is_not_authorization(self):
        with patch.object(frappe, "request", None):
            with self.assertRaises(frappe.PermissionError):
                signature.verify_request()

    def test_scoped_status_queries(self):
        with patch.object(frappe.db, "get_value", return_value=None) as lookup:
            webhook.update_message_status({"statuses": [{"id": "one", "status": "read"},
                                                         {"id": "two", "status": "sent"}]}, ("a",))
        self.assertEqual(lookup.call_count, 2)
        for call in lookup.call_args_list:
            self.assertEqual(call.kwargs["filters"]["whatsapp_account"], ["in", ("a",)])

    def test_template_query_is_scoped(self):
        with patch.object(frappe, "get_all", return_value=[]) as lookup:
            webhook.update_template_status({"message_template_id": "x", "event": "APPROVED"}, ("a",))
        self.assertEqual(lookup.call_args.kwargs["filters"]["whatsapp_account"], ["in", ("a",)])
