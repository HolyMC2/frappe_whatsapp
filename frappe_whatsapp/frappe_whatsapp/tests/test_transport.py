# Copyright (c) 2026, Marco and contributors
# For license information, please see license.txt

"""Tests for frappe_whatsapp.transport — the single Meta egress point.

The property under test is a safety property, not a feature: in Demo mode no
Graph API call may leave the process, so no message can reach a phone.

Scope, stated precisely because the earlier wording overclaimed: this covers
account-scoped Meta traffic. It does NOT make the app network-silent —
whatsapp_templates._prepare_remote_file still fetches a caller-supplied media
URL directly (see _ALLOWED), which is egress to an arbitrary host even in Demo
mode. That is a known, exempted hole, not a covered case.
"""

import pathlib
import re
import unittest
from unittest.mock import patch

import frappe

from frappe_whatsapp import transport

_ACCOUNT = "TRANSPORT-TEST-ACC"
_GRAPH = "https://graph.facebook.com"


def _ensure_account(mode: str, token: str | None = "test-token") -> str:
	if frappe.db.exists("WhatsApp Account", _ACCOUNT):
		frappe.delete_doc("WhatsApp Account", _ACCOUNT, force=1, ignore_permissions=True)
	doc = frappe.get_doc({
		# NB: no "token" key here on purpose — token is applied below only when
		# the caller asks for one, so the token=None case genuinely has none and
		# test_live_without_token_fails_closed tests something real.
		"doctype": "WhatsApp Account",
		"account_name": _ACCOUNT,
		"status": "Active",
		"mode": mode,
		"url": _GRAPH,
		"version": "v19.0",
		"phone_id": "1234567890",
		"business_id": "biz-1",
		"app_id": "app-1",
	})
	if token:
		doc.token = token
	doc.flags.ignore_mandatory = True
	doc.insert(ignore_permissions=True)
	frappe.db.commit()
	return doc.name


class TestDemoModeSuppressesEgress(unittest.TestCase):
	def setUp(self):
		frappe.set_user("Administrator")
		self.name = _ensure_account(transport.MODE_DEMO)

	def tearDown(self):
		frappe.delete_doc("WhatsApp Account", self.name, force=1, ignore_permissions=True)
		frappe.db.commit()

	def test_demo_never_performs_http(self):
		"""The whole point. If either of these is called, a prospect's phone
		could receive a real message during a sales demo."""
		url = f"{_GRAPH}/v19.0/1234567890/messages"
		with patch.object(transport, "make_post_request") as mp, \
			patch.object(transport, "make_request") as mr, \
			patch.object(transport.requests, "request") as rq:
			transport.api(self.name, "POST", url, data='{"to":"5215550001111"}')
			transport.raw(self.name, "GET", url)
			mp.assert_not_called()
			mr.assert_not_called()
			rq.assert_not_called()

	def test_demo_message_response_is_meta_shaped(self):
		url = f"{_GRAPH}/v19.0/1234567890/messages"
		out = transport.api(self.name, "POST", url, data='{"to":"5215550001111"}')
		# The send path does response["messages"][0]["id"] with no guard.
		self.assertIn("messages", out)
		self.assertTrue(out["messages"][0]["id"].startswith(transport.DEMO_ID_PREFIX))
		self.assertEqual(out["contacts"][0]["wa_id"], "5215550001111")

	def test_demo_template_create_is_approved(self):
		"""A demo tenant must end up with usable templates: taller's
		_template_for() only picks APPROVED rows, so anything else leaves the
		demo unable to show the Recibido notification at all."""
		url = f"{_GRAPH}/v19.0/biz-1/message_templates"
		out = transport.api(self.name, "POST", url, data='{"category":"UTILITY"}')
		self.assertEqual(out["status"], "APPROVED")
		self.assertEqual(out["category"], "UTILITY")

	def test_demo_raw_duck_types_response(self):
		"""Call sites converted from requests.* use these four members."""
		r = transport.raw(self.name, "GET", f"{_GRAPH}/v19.0/biz-1/message_templates")
		self.assertEqual(r.status_code, 200)
		self.assertIsNone(r.raise_for_status())
		self.assertIsInstance(r.json(), dict)
		self.assertIsInstance(r.content, bytes)

	def test_demo_generic_get_has_media_keys(self):
		"""utils.webhook reads media_data["url"] unconditionally."""
		out = transport.api(self.name, "GET", f"{_GRAPH}/v19.0/some-media-id/")
		self.assertIn("url", out)
		self.assertIn("mime_type", out)


class TestLiveMode(unittest.TestCase):
	def setUp(self):
		frappe.set_user("Administrator")

	def tearDown(self):
		if frappe.db.exists("WhatsApp Account", _ACCOUNT):
			frappe.delete_doc("WhatsApp Account", _ACCOUNT, force=1, ignore_permissions=True)
		frappe.db.commit()

	def test_live_still_calls_through(self):
		name = _ensure_account(transport.MODE_LIVE)
		url = f"{_GRAPH}/v19.0/1234567890/messages"
		with patch.object(transport, "make_post_request", return_value={"ok": 1}) as mp:
			out = transport.api(name, "POST", url, data="{}")
		mp.assert_called_once()
		self.assertEqual(out, {"ok": 1})

	def test_default_mode_is_live(self):
		"""Existing accounts predate the field. Defaulting to Demo would
		silently mute a tenant's production WhatsApp on upgrade."""
		name = _ensure_account(transport.MODE_LIVE)
		frappe.db.set_value("WhatsApp Account", name, "mode", None)
		frappe.clear_document_cache("WhatsApp Account", name)
		self.assertEqual(transport.mode_for(name), transport.MODE_LIVE)
		self.assertFalse(transport.is_demo(name))

	def test_live_without_token_fails_closed(self):
		"""Previously this fired the request and returned an opaque Meta 401."""
		name = _ensure_account(transport.MODE_LIVE, token=None)
		with patch.object(transport, "make_post_request") as mp:
			with self.assertRaises(frappe.ValidationError):
				transport.api(name, "POST", f"{_GRAPH}/v19.0/1234567890/messages", data="{}")
		mp.assert_not_called()


class TestEgressInvariant(unittest.TestCase):
	"""Nothing outside transport.py may talk to Meta directly.

	This is the test that keeps the design honest. A demo_mode check at each
	call site is 29 chances to forget one, and forgetting means a live message
	during a demo. If someone adds a raw call, this fails and names the file.
	"""

	#: whatsapp_templates._prepare_remote_file downloads an arbitrary,
	#: caller-supplied file URL to attach to a template. It is not an account
	#: scoped Meta Graph call and must NOT be routed through transport.
	#: Path is relative to the directory holding transport.py, which is the app
	#: root — so it carries the inner-package prefix.
	_ALLOWED = {
		("frappe_whatsapp/doctype/whatsapp_templates/whatsapp_templates.py", "requests.get(file_url"),
	}

	def test_no_direct_meta_egress_outside_transport(self):
		# transport.py sits at the app root; rglob from there covers every
		# module in the app, including utils/ which is a SIBLING of the inner
		# package (an earlier hand audit walked only the package and missed the
		# two Meta media-download calls in utils/webhook.py).
		root = pathlib.Path(transport.__file__).parent
		# Widened after review: the first version matched only
		# make_post_request/make_request and requests.(post|get|delete|put),
		# so requests.request(, requests.patch/head, requests.Session(),
		# make_get_request, urllib and httpx would all have slipped past and
		# the test would have stayed green while Demo mode was bypassed.
		pattern = re.compile(
			r"\b(make_post_request|make_request|make_get_request|make_put_request)\s*\("
			r"|\brequests\.[A-Za-z_]+\s*\("
			r"|\bhttpx\.[A-Za-z_]+\s*\("
			r"|\burllib\.request\.[A-Za-z_]+\s*\("
		)
		offenders = []
		for py in root.rglob("*.py"):
			rel = py.relative_to(root).as_posix()
			if rel == "transport.py" or "/test_" in f"/{rel}" or rel.startswith("tests/"):
				continue
			for i, line in enumerate(py.read_text().splitlines(), 1):
				if not pattern.search(line):
					continue
				if any(rel == a and frag in line for a, frag in self._ALLOWED):
					continue
				offenders.append(f"{rel}:{i}: {line.strip()}")
		self.assertEqual(
			offenders, [],
			"Direct Meta egress found outside transport.py — route it through "
			"transport.api()/transport.raw() so Demo mode cannot be bypassed:\n"
			+ "\n".join(offenders),
		)
