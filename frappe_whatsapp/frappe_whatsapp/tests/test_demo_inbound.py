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

		with patch.object(webhook, "process_change", side_effect=RuntimeError("boom")):
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

	# --- authorization + cross-account containment ---------------------------

	def test_requires_system_manager(self):
		"""Every entry point was a bare @frappe.whitelist(), so any authenticated
		user — including a portal Website User — could drive the simulator."""
		victim = "demo-sim-lowpriv@example.com"
		if not frappe.db.exists("User", victim):
			u = frappe.get_doc({
				"doctype": "User", "email": victim, "first_name": "LowPriv",
				"send_welcome_email": 0, "user_type": "Website User",
			})
			u.flags.ignore_permissions = True
			u.insert(ignore_permissions=True)
			frappe.db.commit()
		self.addCleanup(frappe.set_user, "Administrator")
		frappe.set_user(victim)
		with self.assertRaises(frappe.PermissionError):
			demo.simulate_inbound_text(self.demo_acc, _FROM, "deberia rebotar")

	def test_delivery_status_refuses_another_account(self):
		"""The Demo guard alone was NOT enough here: a status envelope carries no
		`messages`, so webhook.post() routes to update_message_status, which
		resolves the target by message_id ALONE with no account filter. Passing a
		LIVE account's wamid rewrote that real message's status."""
		frappe.set_user("Administrator")
		live_msg = frappe.get_doc({
			"doctype": "WhatsApp Message",
			"type": "Incoming",
			"from": "5215551234567",
			"message": "mensaje real de cliente",
			"message_id": "wamid.REAL-CUSTOMER-MSG",
			"content_type": "text",
			"whatsapp_account": self.live_acc,
			"status": "delivered",
		})
		live_msg.flags.ignore_permissions = True
		live_msg.insert(ignore_permissions=True)
		frappe.db.commit()
		self.addCleanup(
			frappe.delete_doc, "WhatsApp Message", live_msg.name,
			force=1, ignore_permissions=True,
		)

		with self.assertRaises(frappe.ValidationError):
			demo.simulate_delivery_status(
				self.demo_acc, "wamid.REAL-CUSTOMER-MSG", "failed"
			)
		self.assertEqual(
			frappe.db.get_value("WhatsApp Message", live_msg.name, "status"),
			"delivered",
			"a real customer message must not be rewritten by the simulator",
		)

	def test_rejects_account_without_phone_id(self):
		"""Without a phone_id the webhook drops the message but the caller still
		got {"simulated": True} — success reported, empty thread shown."""
		frappe.set_user("Administrator")
		name = _account("DEMO-TEST-NOPHONE", transport.MODE_DEMO, "")
		self.addCleanup(
			frappe.delete_doc, "WhatsApp Account", name, force=1, ignore_permissions=True
		)
		frappe.db.set_value("WhatsApp Account", name, "phone_id", "")
		frappe.clear_document_cache("WhatsApp Account", name)
		with self.assertRaises(frappe.ValidationError):
			demo.simulate_inbound_text(name, _FROM, "sin phone id")

	def test_conversation_script_is_capped(self):
		frappe.set_user("Administrator")
		huge = json.dumps([{"direction": "in", "body": f"m{i}"} for i in range(500)])
		with self.assertRaises(frappe.ValidationError):
			demo.simulate_conversation(self.demo_acc, _FROM, huge)

	def test_conversation_rejects_bad_script(self):
		with self.assertRaises(frappe.ValidationError):
			demo.simulate_conversation(self.demo_acc, _FROM, "not json at all")
