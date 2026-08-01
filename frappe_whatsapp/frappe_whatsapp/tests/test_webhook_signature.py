# Copyright (c) 2026, Marco and contributors
# For license information, please see license.txt

"""Tests for X-Hub-Signature-256 verification on inbound webhooks.

The endpoint is allow_guest=True and, before this, did no authentication on
POST at all — anyone reaching the URL could fabricate customer conversations
and rewrite message/template statuses.
"""

import hashlib
import hmac
import json
import unittest
from unittest.mock import patch

import frappe

from frappe_whatsapp.utils import signature

_ACC = "SIGTEST-ACC"
_SECRET = "top-secret-app-secret"


def _account(app_secret=None) -> str:
	if frappe.db.exists("WhatsApp Account", _ACC):
		frappe.delete_doc("WhatsApp Account", _ACC, force=1, ignore_permissions=True)
	doc = frappe.get_doc({
		"doctype": "WhatsApp Account",
		"account_name": _ACC,
		"status": "Active",
		"url": "https://graph.facebook.com",
		"version": "v19.0",
		"phone_id": "sig-phone-1",
		"business_id": "sig-biz-1",
		"token": "tok",
	})
	if app_secret:
		doc.app_secret = app_secret
	doc.flags.ignore_mandatory = True
	doc.insert(ignore_permissions=True)
	frappe.db.commit()
	return doc.name


class _FakeRequest:
	def __init__(self, body: bytes, header=None):
		self._body = body
		self.headers = {"X-Hub-Signature-256": header} if header else {}

	def get_data(self):
		return self._body


class TestSignature(unittest.TestCase):
	def setUp(self):
		frappe.set_user("Administrator")

	def tearDown(self):
		if frappe.db.exists("WhatsApp Account", _ACC):
			frappe.delete_doc("WhatsApp Account", _ACC, force=1, ignore_permissions=True)
		frappe.db.commit()

	# --- staged rollout ------------------------------------------------------

	def test_no_secret_configured_skips_verification(self):
		"""Deploying this must not break a tenant that has not opted in yet —
		otherwise every production site loses inbound WhatsApp on upgrade."""
		_account(app_secret=None)
		req = _FakeRequest(b'{"entry":[]}')
		with patch.object(frappe, "request", req):
			signature.verify_request()  # must not raise

	# --- enforcement, once opted in ------------------------------------------

	def test_valid_signature_passes(self):
		_account(app_secret=_SECRET)
		body = json.dumps({"entry": [{"changes": []}]}).encode()
		sig = signature.expected_signature(_SECRET, body)
		with patch.object(frappe, "request", _FakeRequest(body, sig)):
			signature.verify_request()

	def test_missing_signature_rejected_once_configured(self):
		_account(app_secret=_SECRET)
		with patch.object(frappe, "request", _FakeRequest(b'{"entry":[]}')):
			with self.assertRaises(frappe.PermissionError):
				signature.verify_request()

	def test_wrong_secret_rejected(self):
		_account(app_secret=_SECRET)
		body = b'{"entry":[]}'
		forged = signature.expected_signature("attacker-guess", body)
		with patch.object(frappe, "request", _FakeRequest(body, forged)):
			with self.assertRaises(frappe.PermissionError):
				signature.verify_request()

	def test_tampered_body_rejected(self):
		"""Signature is over the bytes; changing them after signing must fail."""
		_account(app_secret=_SECRET)
		original = b'{"entry":[{"amount":1}]}'
		sig = signature.expected_signature(_SECRET, original)
		tampered = b'{"entry":[{"amount":9999}]}'
		with patch.object(frappe, "request", _FakeRequest(tampered, sig)):
			with self.assertRaises(frappe.PermissionError):
				signature.verify_request()

	def test_signature_is_over_raw_bytes_not_reserialized_json(self):
		"""Re-serialising form_dict produces different bytes (key order,
		separators) and would never match — this pins the contract."""
		body = b'{"b":1,"a":2}'
		reserialized = json.dumps(json.loads(body)).encode()
		self.assertNotEqual(body, reserialized)
		self.assertNotEqual(
			signature.expected_signature(_SECRET, body),
			signature.expected_signature(_SECRET, reserialized),
		)

	def test_matches_meta_reference_hmac(self):
		"""Independent of our helper: plain HMAC-SHA256 hex, `sha256=` prefixed."""
		body = b'{"hello":"world"}'
		expected = "sha256=" + hmac.new(
			_SECRET.encode(), body, hashlib.sha256
		).hexdigest()
		self.assertEqual(signature.expected_signature(_SECRET, body), expected)

	def test_no_http_request_is_a_noop(self):
		"""The in-process simulator pushes envelopes through the same handler on
		purpose; it is gated on System Manager separately."""
		_account(app_secret=_SECRET)
		with patch.object(frappe, "request", None):
			signature.verify_request()
