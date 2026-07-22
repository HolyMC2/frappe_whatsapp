# Copyright (c) 2026, Grupo Doco and contributors
# For license information, please see license.txt

"""FCRM conversation contract — webhook + multi-account routing.

Complements the existing utils/test_webhook.py (which already covers GET verify,
POST text/reaction/reply/button, and sent/delivered/read status). This file adds
the MULTI-ACCOUNT + failure-mode contract that regressions actually hide in:

  * inbound routed to the MATCHING WhatsApp Account by metadata.phone_number_id
    when several accounts exist,
  * an unknown phone_number_id drops the message (never mis-attributes it),
  * a `failed` status callback records failure_reason from errors[] (doco
    2026-07-11: video sends failed with no trace),
  * a status callback for a message this DB never stored is dropped quietly,
  * WhatsApp Account default in/out flags are mutually exclusive.

All outbound HTTP is mocked (make_post_request is patched) so a downstream
auto-reply can never reach live Meta with the broken lab token.
"""

from unittest.mock import MagicMock, patch

import frappe
import requests
from frappe.tests import IntegrationTestCase

# make_post_request is bound into the message doctype module — patch it there so
# any downstream Outgoing send (chatflow auto-reply) is a no-op, never live Meta.
_MPR = "frappe_whatsapp.frappe_whatsapp.doctype.whatsapp_message.whatsapp_message.make_post_request"

_A = "Conv WH Account A"
_B = "Conv WH Account B"
_PID_A = "conv_wh_phone_A"
_PID_B = "conv_wh_phone_B"
_VT_A = "conv_wh_vt_A"
_VT_B = "conv_wh_vt_B"


def _account(name, phone_id, verify_token, incoming=0, outgoing=0):
	if frappe.db.exists("WhatsApp Account", name):
		frappe.delete_doc("WhatsApp Account", name, force=True, ignore_permissions=True)
	acct = frappe.get_doc(
		{
			"doctype": "WhatsApp Account",
			"account_name": name,
			"status": "Active",
			"url": "https://graph.facebook.com",
			"version": "v19.0",
			"phone_id": phone_id,
			"business_id": f"{phone_id}_biz",
			"app_id": f"{phone_id}_app",
			"webhook_verify_token": verify_token,
			"is_default_incoming": incoming,
			"is_default_outgoing": outgoing,
		}
	)
	acct.insert(ignore_permissions=True)
	from frappe.utils.password import set_encrypted_password

	set_encrypted_password("WhatsApp Account", acct.name, "lab-fake-token", "token")
	return acct.name


def _text_payload(phone_id, sender, msg_id, body="hola"):
	return {
		"entry": [
			{
				"changes": [
					{
						"value": {
							"metadata": {"phone_number_id": phone_id},
							"contacts": [{"profile": {"name": "Conv WH Sender"}}],
							"messages": [
								{"from": sender, "id": msg_id, "type": "text", "text": {"body": body}}
							],
						}
					}
				]
			}
		]
	}


def _media_payload(phone_id, sender, msg_id, media_type="image", caption=""):
	return {
		"entry": [
			{
				"changes": [
					{
						"value": {
							"metadata": {"phone_number_id": phone_id},
							"contacts": [{"profile": {"name": "Conv WH Media"}}],
							"messages": [
								{
									"from": sender,
									"id": msg_id,
									"type": media_type,
									media_type: {"id": "MEDIA_" + msg_id, "caption": caption},
								}
							],
						}
					}
				]
			}
		]
	}


def _multi_payload(phone_id, *messages):
	return {
		"entry": [
			{
				"changes": [
					{
						"value": {
							"metadata": {"phone_number_id": phone_id},
							"contacts": [{"profile": {"name": "Conv WH Batch"}}],
							"messages": list(messages),
						}
					}
				]
			}
		]
	}


def _status_payload(msg_id, status, errors=None, conversation=None):
	value = {"statuses": [{"id": msg_id, "status": status}]}
	if conversation:
		value["statuses"][0]["conversation"] = {"id": conversation}
	if errors:
		value["statuses"][0]["errors"] = errors
	return {"entry": [{"changes": [{"field": "messages", "value": value}]}]}


class TestWebhookRouting(IntegrationTestCase):
	def setUp(self):
		self.acct_a = _account(_A, _PID_A, _VT_A)
		self.acct_b = _account(_B, _PID_B, _VT_B)
		p = patch(_MPR, return_value={"messages": [{"id": "wamid.mock"}]})
		p.start()
		self.addCleanup(p.stop)

	def _post(self, payload):
		req = MagicMock()
		req.method = "POST"
		frappe.local.form_dict = frappe._dict(payload)
		with patch("frappe_whatsapp.utils.webhook.frappe.request", req):
			from frappe_whatsapp.utils.webhook import webhook

			return webhook()

	def _get(self, verify_token, challenge="chal-123"):
		req = MagicMock()
		req.method = "GET"
		frappe.local.form_dict = frappe._dict(
			{"hub.challenge": challenge, "hub.verify_token": verify_token, "hub.mode": "subscribe"}
		)
		with patch("frappe_whatsapp.utils.webhook.frappe.request", req):
			from frappe_whatsapp.utils.webhook import webhook

			return webhook()

	# --- GET handshake ---

	def test_get_handshake_echoes_challenge_for_matching_account(self):
		resp = self._get(_VT_B, challenge="echo-me-42")
		self.assertEqual(resp.status_code, 200)
		self.assertEqual(resp.get_data(as_text=True), "echo-me-42")

	def test_get_handshake_rejects_unknown_token(self):
		with self.assertRaises(frappe.ValidationError):
			self._get("no-such-verify-token")

	# --- POST inbound routing by phone_number_id (multi-account) ---

	def test_inbound_routes_to_matching_account_by_phone_id(self):
		self._post(_text_payload(_PID_B, "5215551234501", "wamid.conv_wh_route_b"))
		self._post(_text_payload(_PID_A, "5215551234502", "wamid.conv_wh_route_a"))

		mb = frappe.get_doc("WhatsApp Message", {"message_id": "wamid.conv_wh_route_b"})
		self.assertEqual(mb.whatsapp_account, self.acct_b)
		self.assertEqual(mb.type, "Incoming")
		self.assertEqual(mb.get("from"), "5215551234501")
		self.assertEqual(mb.message, "hola")

		ma = frappe.get_doc("WhatsApp Message", {"message_id": "wamid.conv_wh_route_a"})
		self.assertEqual(ma.whatsapp_account, self.acct_a)

	def test_unknown_phone_id_creates_no_message(self):
		self._post(_text_payload("phone_id_no_account", "5215551234599", "wamid.conv_wh_unknown"))
		self.assertFalse(
			frappe.db.exists("WhatsApp Message", {"message_id": "wamid.conv_wh_unknown"})
		)

	# --- status callbacks ---

	def test_failed_status_records_failure_reason(self):
		"""A failed send must persist Meta's errors[] so the failure is diagnosable
		(doco 2026-07-11: video sends failed silently)."""
		msg = frappe.get_doc(
			{
				"doctype": "WhatsApp Message",
				"type": "Outgoing",
				"to": "5215551234510",
				"message": "outbound",
				"message_id": "wamid.conv_wh_status_fail",
				"content_type": "text",
				"whatsapp_account": self.acct_b,
			}
		)
		msg.flags.ignore_validate = True
		msg.db_insert()

		self._post(
			_status_payload(
				"wamid.conv_wh_status_fail",
				"failed",
				errors=[
					{
						"code": 131047,
						"title": "Re-engagement message",
						"error_data": {"details": "Message failed: outside 24h window"},
					}
				],
			)
		)
		msg.reload()
		self.assertEqual(msg.status, "failed")
		self.assertIn("131047", msg.failure_reason or "")
		self.assertIn("outside 24h window", msg.failure_reason or "")

	def test_status_for_unknown_message_is_dropped_quietly(self):
		# Must NOT raise (a 500 here makes Meta retry-storm); no row to update.
		self._post(_status_payload("wamid.never_stored_by_this_db", "delivered"))


def _image_payload(phone_id, sender, msg_id, caption="", extra_messages=None):
	messages = list(extra_messages or [])
	messages.append(
		{"from": sender, "id": msg_id, "type": "image", "image": {"id": "conv_media_1", "caption": caption}}
	)
	return {
		"entry": [
			{
				"changes": [
					{
						"value": {
							"metadata": {"phone_number_id": phone_id},
							"contacts": [{"profile": {"name": "Conv WH Sender"}}],
							"messages": messages,
						}
					}
				]
			}
		]
	}


class TestWebhookMedia(IntegrationTestCase):
	"""H1 regression guard: inbound media must NEVER be silently lost, and a media
	fetch failure must never abort the batch (webhook 200s to Meta → no retry)."""

	def setUp(self):
		self.acct_a = _account(_A, _PID_A, _VT_A)
		p = patch(_MPR, return_value={"messages": [{"id": "wamid.mock"}]})
		p.start()
		self.addCleanup(p.stop)

	_post = TestWebhookRouting._post

	def test_media_fetch_failure_still_creates_placeholder_row(self):
		import requests as requests_lib

		bad = MagicMock()
		bad.status_code = 404
		bad.raise_for_status.side_effect = requests_lib.exceptions.HTTPError("404")
		with patch("frappe_whatsapp.utils.webhook.requests.get", return_value=bad):
			self._post(_image_payload(_PID_A, "5215551234520", "wamid.conv_media_lost"))

		msg = frappe.get_doc("WhatsApp Message", {"message_id": "wamid.conv_media_lost"})
		self.assertEqual(msg.content_type, "image")
		self.assertEqual(msg.message, "[media no recuperable]")
		self.assertFalse(msg.attach)

	def test_media_timeout_does_not_lose_sibling_text_in_batch(self):
		import requests as requests_lib

		text_sibling = {
			"from": "5215551234521",
			"id": "wamid.conv_media_sibling_text",
			"type": "text",
			"text": {"body": "texto que no debe perderse"},
		}
		with patch(
			"frappe_whatsapp.utils.webhook.requests.get",
			side_effect=requests_lib.exceptions.Timeout("stalled CDN"),
		):
			self._post(
				_image_payload(
					_PID_A, "5215551234521", "wamid.conv_media_timeout",
					caption="foto", extra_messages=[text_sibling],
				)
			)

		sib = frappe.get_doc("WhatsApp Message", {"message_id": "wamid.conv_media_sibling_text"})
		self.assertEqual(sib.message, "texto que no debe perderse")
		med = frappe.get_doc("WhatsApp Message", {"message_id": "wamid.conv_media_timeout"})
		# caption survives as the bubble text; no placeholder needed
		self.assertEqual(med.message, "foto")
		self.assertFalse(med.attach)

	def test_media_happy_path_attaches_file(self):
		meta_resp = MagicMock()
		meta_resp.status_code = 200
		meta_resp.raise_for_status.return_value = None
		meta_resp.json.return_value = {
			"url": "https://cdn.example/media/conv_media_1",
			"mime_type": "image/png",
		}
		bin_resp = MagicMock()
		bin_resp.status_code = 200
		bin_resp.raise_for_status.return_value = None
		# Frappe's File save runs the bytes through PIL — must be a real image.
		bin_resp.content = (
			b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
			b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\rIDATx\x9cc\xf8\xff"
			b"\xff?\x00\x05\xfe\x02\xfe\xa75\x81\x84\x00\x00\x00\x00IEND\xaeB`\x82"
		)
		with patch(
			"frappe_whatsapp.utils.webhook.requests.get", side_effect=[meta_resp, bin_resp]
		):
			self._post(_image_payload(_PID_A, "5215551234522", "wamid.conv_media_ok", caption="ok"))

		msg = frappe.get_doc("WhatsApp Message", {"message_id": "wamid.conv_media_ok"})
		self.assertEqual(msg.message, "ok")
		self.assertTrue(msg.attach)
		self.assertTrue(
			frappe.db.exists(
				"File",
				{"attached_to_doctype": "WhatsApp Message", "attached_to_name": msg.name},
			)
		)


class TestAccountDefaultsExclusive(IntegrationTestCase):
	"""WhatsApp Account.there_must_be_only_one_default — inbound routing relies on a
	single default per direction when phone_id doesn't match."""

	def test_default_incoming_is_exclusive(self):
		c = _account("Conv Excl In C", "conv_excl_in_c", "vt_c_in", incoming=1)
		d = _account("Conv Excl In D", "conv_excl_in_d", "vt_d_in", incoming=1)
		self.assertEqual(frappe.db.get_value("WhatsApp Account", c, "is_default_incoming"), 0)
		self.assertEqual(frappe.db.get_value("WhatsApp Account", d, "is_default_incoming"), 1)

	def test_default_outgoing_is_exclusive(self):
		c = _account("Conv Excl Out C", "conv_excl_out_c", "vt_c_out", outgoing=1)
		d = _account("Conv Excl Out D", "conv_excl_out_d", "vt_d_out", outgoing=1)
		self.assertEqual(frappe.db.get_value("WhatsApp Account", c, "is_default_outgoing"), 0)
		self.assertEqual(frappe.db.get_value("WhatsApp Account", d, "is_default_outgoing"), 1)
