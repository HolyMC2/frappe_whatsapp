# Copyright (c) 2026, Marco and contributors
# For license information, please see license.txt

"""Tests for frappe_whatsapp.demo — synthetic inbound for Demo accounts.

The safety property: simulation must be impossible on a Live account. Writing
fabricated inbound into a live tenant produces messages that look, in the UI,
exactly like something a real customer said.
"""

import json
import unittest
from unittest.mock import patch

import frappe

from frappe_whatsapp import demo, transport

_LIVE = "DEMO-TEST-LIVE-ACC"
_DEMO = "DEMO-TEST-DEMO-ACC"
_FROM = "5215559990001"


def _account(name: str, mode: str, phone_id: str) -> str:
	if frappe.db.exists("WhatsApp Account", name):
		frappe.delete_doc("WhatsApp Account", name, force=1, ignore_permissions=True)
	doc = frappe.get_doc({
		"doctype": "WhatsApp Account",
		"account_name": name,
		"status": "Active",
		"mode": mode,
		"token": "test-token",
		"url": "https://graph.facebook.com",
		"version": "v19.0",
		"phone_id": phone_id,
		"business_id": f"biz-{phone_id}",
		"app_id": f"app-{phone_id}",
	})
	doc.flags.ignore_mandatory = True
	doc.insert(ignore_permissions=True)
	frappe.db.commit()
	return doc.name


class TestDemoInbound(unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		frappe.set_user("Administrator")
		cls.demo_acc = _account(_DEMO, transport.MODE_DEMO, "demo-phone-9001")
		cls.live_acc = _account(_LIVE, transport.MODE_LIVE, "live-phone-9002")

	@classmethod
	def tearDownClass(cls):
		for n in (cls.demo_acc, cls.live_acc):
			if frappe.db.exists("WhatsApp Account", n):
				frappe.delete_doc("WhatsApp Account", n, force=1, ignore_permissions=True)
		frappe.db.commit()

	def _cleanup_message(self, message_id):
		name = frappe.db.get_value("WhatsApp Message", {"message_id": message_id})
		if name:
			frappe.delete_doc("WhatsApp Message", name, force=1, ignore_permissions=True)

	# --- the safety property -------------------------------------------------

	def test_refuses_on_live_account(self):
		"""The whole reason this module can be shipped at all."""
		before = frappe.db.count("WhatsApp Message")
		with self.assertRaises(frappe.ValidationError):
			demo.simulate_inbound_text(self.live_acc, _FROM, "no deberia entrar")
		self.assertEqual(
			frappe.db.count("WhatsApp Message"), before,
			"a refused simulation must not create any message",
		)

	def test_refuses_unknown_account(self):
		with self.assertRaises(frappe.ValidationError):
			demo.simulate_inbound_text("NO-SUCH-ACCOUNT", _FROM, "hola")

	def test_simulation_never_touches_meta(self):
		out = None
		with patch.object(transport, "make_post_request") as mp, \
			patch.object(transport, "make_request") as mr, \
			patch.object(transport.requests, "request") as rq:
			out = demo.simulate_inbound_text(self.demo_acc, _FROM, "sin red")
			mp.assert_not_called()
			mr.assert_not_called()
			rq.assert_not_called()
		self.addCleanup(self._cleanup_message, out["message_id"])

	# --- it actually ingests -------------------------------------------------

	def test_inbound_text_creates_incoming_message_on_right_account(self):
		out = demo.simulate_inbound_text(
			self.demo_acc, _FROM, "mi pantalla no enciende", profile_name="Juan"
		)
		self.addCleanup(self._cleanup_message, out["message_id"])
		name = frappe.db.get_value("WhatsApp Message", {"message_id": out["message_id"]})
		self.assertTrue(name, "simulated inbound produced no WhatsApp Message")
		doc = frappe.get_doc("WhatsApp Message", name)
		self.assertEqual(doc.type, "Incoming")
		self.assertEqual(doc.message, "mi pantalla no enciende")
		self.assertEqual(doc.get("from"), _FROM)
		# Routed by metadata.phone_number_id, exactly as a real callback is —
		# this is what proves the envelope reaches the right account on a
		# multi-account site rather than the default one.
		self.assertEqual(doc.whatsapp_account, self.demo_acc)

	def test_inbound_is_marked_demo(self):
		"""Inbound was the gap: the old stamp lived in notify(), which only
		runs outbound, so a simulated conversation looked real from the
		receiving side."""
		out = demo.simulate_inbound_text(self.demo_acc, _FROM, "marcado?")
		self.addCleanup(self._cleanup_message, out["message_id"])
		name = frappe.db.get_value("WhatsApp Message", {"message_id": out["message_id"]})
		self.assertEqual(frappe.db.get_value("WhatsApp Message", name, "is_demo"), 1)

	def test_form_dict_is_restored(self):
		"""_dispatch swaps frappe.local.form_dict; the rest of the request
		still needs the original back, including after a failure."""
		sentinel = frappe._dict({"sentinel": "keep-me"})
		frappe.local.form_dict = sentinel
		out = demo.simulate_inbound_text(self.demo_acc, _FROM, "restaurar")
		self.addCleanup(self._cleanup_message, out["message_id"])
		self.assertEqual(frappe.local.form_dict.get("sentinel"), "keep-me")

	def test_form_dict_restored_even_when_handler_raises(self):
		sentinel = frappe._dict({"sentinel": "keep-me-too"})
		frappe.local.form_dict = sentinel
		from frappe_whatsapp.utils import webhook

		with patch.object(webhook, "post", side_effect=RuntimeError("boom")):
			with self.assertRaises(RuntimeError):
				demo.simulate_inbound_text(self.demo_acc, _FROM, "explota")
		self.assertEqual(frappe.local.form_dict.get("sentinel"), "keep-me-too")

	def test_rejects_empty_body(self):
		with self.assertRaises(frappe.ValidationError):
			demo.simulate_inbound_text(self.demo_acc, _FROM, "   ")

	def test_delivery_status_rejects_unknown_status(self):
		with self.assertRaises(frappe.ValidationError):
			demo.simulate_delivery_status(self.demo_acc, "wamid.x", "teleported")

	def test_conversation_script_creates_both_directions(self):
		script = json.dumps([
			{"direction": "in", "body": "hola, mi bocina falla"},
			{"direction": "out", "body": "claro, traelo al taller"},
			{"direction": "in", "body": "voy en camino"},
		])
		out = demo.simulate_conversation(self.demo_acc, _FROM, script)
		self.assertEqual(out["steps"], 3)
		created = out["messages"]
		self.assertEqual([m["direction"] for m in created], ["in", "out", "in"])
		for m in created:
			if m["direction"] == "in":
				self.addCleanup(self._cleanup_message, m["message_id"])
			else:
				self.addCleanup(
					frappe.delete_doc, "WhatsApp Message", m["name"],
					force=1, ignore_permissions=True,
				)
		# The outbound leg goes through the normal send path, so it must be
		# suppressed and marked like any other demo send — no special-casing.
		out_name = next(m["name"] for m in created if m["direction"] == "out")
		self.assertEqual(frappe.db.get_value("WhatsApp Message", out_name, "is_demo"), 1)

	def test_conversation_rejects_bad_script(self):
		with self.assertRaises(frappe.ValidationError):
			demo.simulate_conversation(self.demo_acc, _FROM, "not json at all")
