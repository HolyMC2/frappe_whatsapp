"""Webhook."""
import frappe

from frappe_whatsapp import transport
import json
import re
import requests
import time
from frappe import _
from werkzeug.wrappers import Response
import frappe.utils

from frappe_whatsapp.utils import get_whatsapp_account, signature


@frappe.whitelist(allow_guest=True)
def webhook():
	"""Meta webhook."""
	if frappe.request.method == "GET":
		return get()
	return post()


def get():
	"""Get."""
	hub_challenge = frappe.form_dict.get("hub.challenge")
	verify_token = frappe.form_dict.get("hub.verify_token")
	webhook_verify_token = frappe.db.get_value(
		'WhatsApp Account',
		{"webhook_verify_token": verify_token},
		'webhook_verify_token'
	)
	if not webhook_verify_token:
		frappe.throw("No matching WhatsApp account")

	if frappe.form_dict.get("hub.verify_token") != webhook_verify_token:
		frappe.throw("Verify token does not match")

	return Response(hub_challenge, status=200)

def post():
	"""Authenticate and account-scope the entire batch before any write."""
	from frappe_whatsapp.webhook_receipts import record_events
	scopes = signature.verify_request()
	return record_events([event for scoped in scopes for event in receipt_events(scoped)])


def receipt_events(scoped):
	"""Split every message/status into a stable, account-scoped receipt identity."""
	from frappe_whatsapp.webhook_receipts import canonical, digest, ReceiptError
	value = scoped.change["value"]
	field = scoped.change["field"]
	if field == "smb_message_echoes":
		from frappe_whatsapp.coexistence import echo_events
		yield from echo_events(scoped)
		return
	if "message_echoes" in value:
		raise ReceiptError("echo_field_mismatch")
	phone = (value.get("metadata") or {}).get("phone_number_id")
	base = {"provider": "WhatsApp", "account_id": phone or scoped.business_id,
	        "app_id": scoped.app_id}
	def event(kind, identity, change):
		return {**base, "event_type": kind, "event_id": identity, "payload": {
			"business_id": scoped.business_id, "change": change}}
	if field == "messages":
		for message in value.get("messages", []):
			if not isinstance(message.get("id"), str) or not message["id"]:
				raise ReceiptError("message_id_missing")
			atom = {k: v for k, v in value.items() if k not in {"messages", "statuses", "contacts"}}
			atom["contacts"] = [c for c in value.get("contacts", []) if c.get("wa_id") == message.get("from")]
			atom["messages"] = [message]
			yield event("message", message["id"], {"field": field, "value": atom})
		for status in value.get("statuses", []):
			if not status.get("id") or not status.get("status"):
				raise ReceiptError("status_identity_missing")
			atom = {k: v for k, v in value.items() if k not in {"messages", "statuses", "contacts"}}
			atom["statuses"] = [status]
			yield event("status", digest([status["id"], status["status"], status.get("timestamp")]),
			            {"field": field, "value": atom})
		if value.get("messages") or value.get("statuses"):
			return
	yield event(field, digest(value), scoped.change)


def consume_receipt(receipt):
	"""Recheck current account scope; consume only this durable atom."""
	from frappe_whatsapp.webhook_receipts import ReceiptError
	payload = json.loads(receipt.payload)
	accounts = [a for a in signature._accounts() if a.get("status") == "Active"
	            and (a.get("mode") or "Live") == "Live" and a.get("app_id") == receipt.app_id]
	if not any(signature._secret(a) for a in accounts):
		raise ReceiptError("account_unavailable")
	try:
		scopes = signature.scope_payload({"object": "whatsapp_business_account", "entry": [{
			"id": payload["business_id"], "changes": [payload["change"]]}]}, accounts, receipt.app_id)
	except frappe.PermissionError:
		raise ReceiptError("account_scope_revoked") from None
	if payload["change"]["field"] == "smb_message_echoes":
		from frappe_whatsapp.coexistence import consume_echo
		if len(scopes) != 1:
			raise ReceiptError("echo_account_ambiguous")
		return consume_echo(receipt, scopes[0])
	if payload["change"]["field"] in {"history", "smb_app_state_sync"}:
		return {"state": "Ignored", "reason_code": "coexistence_sync_unsupported"}
	if payload["change"]["field"] not in {"messages", "message_template_status_update"}:
		return {"state": "Ignored", "reason_code": "unsupported_event"}
	activity = None
	if receipt.event_type == "message":
		from frappe_whatsapp.customer_activity import consume_customer_activity
		if len(scopes) != 1:
			raise ReceiptError("customer_activity_account_ambiguous")
		activity = consume_customer_activity(receipt, scopes[0])
	for scoped in scopes:
		process_change(scoped)
	return {"state": "Processed", "reason_code": (activity or {}).get("reason_code", "")}


# Assistant/domain seam: a snapshot with media identity, never account credentials.
def _button_payload(message):
	"""Carry provider button tokens verbatim; token validation belongs to domain apps."""
	if message.get("type") == "button":
		return (message.get("button") or {}).get("payload")
	if message.get("type") == "interactive":
		interactive = message.get("interactive") or {}
		if interactive.get("type") in {"button_reply", "list_reply"}:
			return (interactive.get(interactive["type"]) or {}).get("id")
	return None


def _reply_context(message):
	context = message.get("context") or {}
	reply_to_message_id = (context.get("id") or None) if "forwarded" not in context else None
	return bool(reply_to_message_id), reply_to_message_id


def _incoming_extension(message, account, phone_id):
	"""Pass an account-scoped snapshot to ``whatsapp_incoming_committed`` handlers.

	``message`` retains the existing display text/interactive ID contract;
	``button_payload`` carries the template payload or interactive reply ID verbatim.
	``reply_to_message_id`` and ``is_reply`` identify the replied-to provider message.
	Domain apps own prefix routing, signature/expiry/sender validation and replay
	protection. Handlers persist local work in the receipt transaction: they must
	not commit or send. A true result claims media retrieval, as before.
	"""
	from frappe_whatsapp.webhook_receipts import incoming_committed, incoming_is_live

	media = message.get(message.get("type")) or {}
	interactive = message.get("interactive") or {}
	button = interactive.get("button_reply") or interactive.get("list_reply") or {}
	error = next((e for e in message.get("errors", []) if str(e.get("code")) == "131052"), None)
	name = frappe.db.get_value("WhatsApp Message", {"message_id": message["id"], "whatsapp_account": account.name})
	stored = frappe.db.get_value("WhatsApp Message", name,
		["attach", "content_type", "message", "button_payload", "reply_to_message_id", "is_reply"],
		as_dict=True) if name else {}
	stored = stored or {}
	is_reply, reply_to_message_id = _reply_context(message)
	return incoming_committed({
		"type": "Incoming", "name": name, "from": message.get("from"),
		"message_id": message["id"], "whatsapp_account": account.name, "phone_id": phone_id,
		"content_type": stored.get("content_type") or ("button" if button else message.get("type")),
		"message": stored.get("message") or button.get("id") or media.get("body") or media.get("caption") or media.get("text") or "",
		"button_payload": stored.get("button_payload") if stored.get("button_payload") is not None else _button_payload(message),
		"reply_to_message_id": stored.get("reply_to_message_id") or reply_to_message_id,
		"is_reply": bool(stored.get("is_reply")) if stored.get("is_reply") is not None else is_reply,
		"media_id": media.get("id"), "filename": media.get("filename"),
		"attach": stored.get("attach"),
		"error_code": error.get("code") if error else None,
		"live": incoming_is_live(),
	})


def process_change(scoped):
	"""Internal consumer. Only called after full-envelope authentication."""
	value = scoped.change["value"]
	messages = value.get("messages", [])
	whatsapp_account = frappe.get_doc("WhatsApp Account", scoped.accounts[0])
	profiles = {c.get("wa_id"): (c.get("profile") or {}).get("name")
	            for c in value.get("contacts", [])}
	frappe.get_doc({
		"doctype": "WhatsApp Notification Log", "template": "Webhook",
		"meta_data": json.dumps(scoped.change),
	}).insert(ignore_permissions=True)

	if messages:
		for message in messages:
			sender_profile_name = profiles.get(message.get('from'))
			message_type = message['type']
			is_reply, reply_to_message_id = _reply_context(message)
			if any(str(e.get("code")) == "131052" for e in message.get("errors", [])):
				# Inbound oversized media has no downloadable media object.
				frappe.get_doc({
					"doctype": "WhatsApp Message", "type": "Incoming", "from": message['from'],
					"message_id": message['id'], "message": "[131052] Media file size too big",
					"content_type": "text", "whatsapp_account": whatsapp_account.name,
					"failure_reason": "131052: Media file size too big",
				}).insert(ignore_permissions=True)
			elif message_type == 'text':
				frappe.get_doc({
					"doctype": "WhatsApp Message",
					"type": "Incoming",
					"from": message['from'],
					"message": message['text']['body'],
					"message_id": message['id'],
					"reply_to_message_id": reply_to_message_id,
					"is_reply": is_reply,
					"content_type":message_type,
					"profile_name":sender_profile_name,
					"whatsapp_account":whatsapp_account.name
				}).insert(ignore_permissions=True)
			elif message_type == 'reaction':
				frappe.get_doc({
					"doctype": "WhatsApp Message",
					"type": "Incoming",
					"from": message['from'],
					"message": message['reaction']['emoji'],
					"reply_to_message_id": message['reaction']['message_id'],
					"message_id": message['id'],
					"content_type": "reaction",
					"profile_name":sender_profile_name,
					"whatsapp_account":whatsapp_account.name
				}).insert(ignore_permissions=True)
			elif message_type == 'interactive':
				interactive_data = message['interactive']
				interactive_type = interactive_data.get('type')

				# Handle button reply
				if interactive_type == 'button_reply':
					frappe.get_doc({
						"doctype": "WhatsApp Message",
						"type": "Incoming",
						"from": message['from'],
						"message": interactive_data['button_reply']['id'],
						"button_payload": _button_payload(message),
						"message_id": message['id'],
						"reply_to_message_id": reply_to_message_id,
						"is_reply": is_reply,
						"content_type": "button",
						"profile_name": sender_profile_name,
						"whatsapp_account": whatsapp_account.name
					}).insert(ignore_permissions=True)
				# Handle list reply
				elif interactive_type == 'list_reply':
					frappe.get_doc({
						"doctype": "WhatsApp Message",
						"type": "Incoming",
						"from": message['from'],
						"message": interactive_data['list_reply']['id'],
						"button_payload": _button_payload(message),
						"message_id": message['id'],
						"reply_to_message_id": reply_to_message_id,
						"is_reply": is_reply,
						"content_type": "button",
						"profile_name": sender_profile_name,
						"whatsapp_account": whatsapp_account.name
					}).insert(ignore_permissions=True)
				# Handle WhatsApp Flows (nfm_reply)
				elif interactive_type == 'nfm_reply':
					nfm_reply = interactive_data['nfm_reply']
					response_json_str = nfm_reply.get('response_json', '{}')

					# Parse the response JSON
					try:
						flow_response = json.loads(response_json_str)
					except json.JSONDecodeError:
						flow_response = {}

					# Create a summary message from the flow response
					summary_parts = []
					for key, value in flow_response.items():
						if value:
							summary_parts.append(f"{key}: {value}")
					summary_message = ", ".join(summary_parts) if summary_parts else "Flow completed"

					msg_doc = frappe.get_doc({
						"doctype": "WhatsApp Message",
						"type": "Incoming",
						"from": message['from'],
						"message": summary_message,
						"message_id": message['id'],
						"reply_to_message_id": reply_to_message_id,
						"is_reply": is_reply,
						"content_type": "flow",
						"flow_response": json.dumps(flow_response),
						"profile_name": sender_profile_name,
						"whatsapp_account": whatsapp_account.name
					}).insert(ignore_permissions=True)

					# Publish realtime event for flow response
					frappe.publish_realtime(  # nosemgrep: frappe-realtime-pick-room -- intentional site-wide fan-out for chat UIs (whatsapp_chat companion app) listening for inbound flow responses
						"whatsapp_flow_response",
						{
							"phone": message['from'],
							"message_id": message['id'],
							"flow_response": flow_response,
							"whatsapp_account": whatsapp_account.name
						},
						after_commit=True,
					)
			# NEW: Handle Shopping Cart / Orders from MPM
			elif message_type == 'order':
				order_data = message['order']

				# Inject the raw data into product_catalog_json
				frappe.get_doc({
					"doctype": "WhatsApp Message",
					"type": "Incoming",
					"from": message['from'],
					"message": _("New Order Received via WhatsApp"),
					"message_id": message['id'],
					"content_type": "order",
					"profile_name": sender_profile_name,
					"whatsapp_account": whatsapp_account.name,
					"product_catalog_json": json.dumps(order_data)
				}).insert(ignore_permissions=True)
			elif message_type in ["image", "audio", "video", "document"]:
				# The message row is inserted FIRST and the media fetch is
				# best-effort: a failed fetch used to skip the insert entirely
				# (customer photo/voice-note vanished — webhook still 200'd so
				# Meta never retried), and a raised error rolled back the whole
				# batch into a Meta retry storm.
				media = message.get(message_type) or {}
				message_doc = frappe.get_doc({
					"doctype": "WhatsApp Message",
					"type": "Incoming",
					"from": message['from'],
					"message_id": message['id'],
					"reply_to_message_id": reply_to_message_id,
					"is_reply": is_reply,
					"message": media.get("caption", ""),
					"content_type" : message_type,
					"profile_name":sender_profile_name,
					"whatsapp_account":whatsapp_account.name
				}).insert(ignore_permissions=True)

				# Domain hook persists a media-id job before native buffering/File creation.
				if _incoming_extension(message, whatsapp_account, (value.get("metadata") or {}).get("phone_number_id")):
					continue

				try:
					token = whatsapp_account.get_password("token")
					url = f"{whatsapp_account.url}/{whatsapp_account.version}/"
					media_id = media["id"]
					headers = {
						'Authorization': 'Bearer ' + token
					}
					response = transport.raw(
						whatsapp_account, "GET", f'{url}{media_id}/', headers=headers, timeout=(5, 30)
					)
					response.raise_for_status()
					media_data = response.json()
					media_url = media_data["url"]
					mime_type = media_data.get("mime_type") or ""
					file_extension = mime_type.split('/')[1] if "/" in mime_type else "bin"

					media_response = transport.raw(
						whatsapp_account, "GET", media_url, headers=headers, timeout=(5, 60)
					)
					media_response.raise_for_status()

					file_data = media_response.content
					file_name = f"{frappe.generate_hash(length=10)}.{file_extension}"

					file = frappe.get_doc(
						{
							"doctype": "File",
							"file_name": file_name,
							"attached_to_doctype": "WhatsApp Message",
							"attached_to_name": message_doc.name,
							"content": file_data,
							"attached_to_field": "attach"
						}
					).save(ignore_permissions=True)

					message_doc.attach = file.file_url
					message_doc.save(ignore_permissions=True)
				except Exception:
					if frappe.flags.get("meta_webhook_receipt"):
						from frappe_whatsapp.webhook_receipts import ReceiptError
						raise ReceiptError("media_fetch_failed") from None
					frappe.log_error(
						title="WhatsApp inbound media fetch failed",
						message=frappe.get_traceback(),
					)
					if not message_doc.message:
						# db_set: `message` is a set-once field, a doc.save()
						# here throws CannotChangeConstantError.
						message_doc.db_set(
							"message", "[media no recuperable]", update_modified=False
						)
			elif message_type == "button":
				frappe.get_doc({
					"doctype": "WhatsApp Message",
					"type": "Incoming",
					"from": message['from'],
					"message": message['button']['text'],
					"button_payload": _button_payload(message),
					"message_id": message['id'],
					"reply_to_message_id": reply_to_message_id,
					"is_reply": is_reply,
					"content_type": message_type,
					"profile_name":sender_profile_name,
					"whatsapp_account":whatsapp_account.name
				}).insert(ignore_permissions=True)
			else:
				frappe.get_doc({
					"doctype": "WhatsApp Message",
					"type": "Incoming",
					"from": message['from'],
					"message_id": message['id'],
					"message": message[message_type].get(message_type),
					"content_type" : message_type,
					"profile_name":sender_profile_name,
					"whatsapp_account":whatsapp_account.name
				}).insert(ignore_permissions=True)

			# Domain extension runs after attach is set, still in the receipt transaction.
			_incoming_extension(message, whatsapp_account, (value.get("metadata") or {}).get("phone_number_id"))

			# CTWA (Click-To-WhatsApp ad) attribution. Meta hangs the ad referral on the
			# inbound message; stamp it onto the row we just created (matched by message_id)
			# so downstream marketing can join the ad to revenue. Runs for every message type,
			# fail-open — a missing field or parse error must never break message ingestion.
			# ctwa_source_id / ctwa_clid are SITE custom fields (added per-tenant by the
			# doco_meta_catalog app), so guard on their presence first: a tenant without those
			# columns would otherwise raise MariaDB 1054 here every CTWA inbound (caught below,
			# but it would spam the Error Log), so skip cleanly when the columns are absent.
			try:
				referral = message.get("referral") or {}
				if (referral.get("source_id") or referral.get("ctwa_clid")) \
						and frappe.get_meta("WhatsApp Message").has_field("ctwa_source_id"):
					frappe.db.set_value(
						"WhatsApp Message",
						{"message_id": message["id"], "whatsapp_account": whatsapp_account.name},
						{
							"ctwa_source_id": referral.get("source_id"),
							"ctwa_clid": referral.get("ctwa_clid"),
						},
						update_modified=False,
					)
			except Exception:
				frappe.log_error("CTWA referral capture failed", frappe.get_traceback())

	update_status(scoped.change, scoped.accounts)

def update_status(data, accounts):
	"""Update status hook."""
	if data.get("field") == "message_template_status_update":
		update_template_status(data['value'], accounts)

	elif data.get("field") == "messages":
		update_message_status(data['value'], accounts)

def update_template_status(data, accounts):
	"""Template IDs are scoped to this authenticated WABA's accounts."""
	for name in frappe.get_all("WhatsApp Templates", filters={
		"id": data.get("message_template_id"), "whatsapp_account": ["in", accounts],
	}, pluck="name"):
		frappe.db.set_value("WhatsApp Templates", name, "status", data.get("event"))


def update_message_status(data, accounts):
	"""Process every status, scoped to the authenticated phone account."""
	for entry in data.get("statuses", []):
		_apply_message_status(entry, accounts)


def _recipient_spellings(recipient_id):
	"""The one recipient, spelled as Meta reports it and as an outgoing row stores it.

	A legacy row keeps the number the sender dialled; Meta answers with the recipient's
	WhatsApp id. For Mexican mobiles those differ by one digit — 52 + 10 national digits
	dialled, 521 + the same 10 returned — so an exact match dropped every delivery,
	read and failure receipt for a legacy send and the message hung on «Enviando…»
	(docomexico 2026-09-17). Only that documented pair is folded; any other number,
	and any other length, remains a different recipient.
	"""
	if re.fullmatch(r"521[0-9]{10}", recipient_id):
		return [recipient_id, "52" + recipient_id[3:]]
	if re.fullmatch(r"52[0-9]{10}", recipient_id):
		return [recipient_id, "521" + recipient_id[2:]]
	return [recipient_id]


def _apply_message_status(entry, accounts):
	native = None
	if frappe.flags.get("meta_webhook_receipt"):
		from frappe_whatsapp.delivery import fold_native_delivery
		native = fold_native_delivery(entry, accounts)
	id = entry['id']
	status = entry['status']
	conversation = entry.get('conversation', {}).get('id')
	filters = {"message_id": id, "whatsapp_account": ["in", accounts], "type": "Outgoing"}
	name = None
	# The bridge verified this recipient against the immutable status atom, so the row
	# must carry that number. One spelling at a time, reported first: a bridged send can
	# hold both a native projection row and a legacy row for one message id, and a single
	# `in` lookup would pick between them by whatever the engine returned first.
	for spelling in (_recipient_spellings(entry["recipient_id"]) if native is not None else [None]):
		lookup = filters if spelling is None else {**filters, "to": spelling}
		name = frappe.db.get_value("WhatsApp Message", filters=lookup, for_update=True)
		if name:
			break
	if not name:
		if native and native.get("matched"):
			return
		if frappe.flags.get("meta_webhook_receipt"):
			from frappe_whatsapp.webhook_receipts import ReceiptError
			raise ReceiptError("message_not_found")
		# A status callback for a message this DB never stored (console/API
		# sends, other integrations on the same number). Crashing here 500s
		# the webhook and makes Meta retry-storm — drop it quietly.
		return

	doc = frappe.get_doc("WhatsApp Message", name, for_update=True)
	# Separate receipt jobs can reach the same message out of order. Serialize
	# that aggregate and never replace delivery/read evidence with an older state.
	rank = {"sent": 1, "delivered": 2, "read": 3}
	current = (doc.status or "").lower()
	if status not in {*rank, "failed"}:
		from frappe_whatsapp.webhook_receipts import ReceiptError
		raise ReceiptError("unsupported_status")
	if rank.get(current, 0) > rank.get(status, 0) and current in {"delivered", "read"}:
		return
	if current == "failed" and status == "sent":
		return
	doc.status = status
	if status in {"delivered", "read"}:
		doc.failure_reason = None
	if conversation:
		doc.conversation_id = conversation
	# Meta explains async failures (media fetch, re-engagement window, policy)
	# ONLY here — dropping errors[] made every failed send undiagnosable
	# (doco 2026-07-11: video sends failed with no trace).
	if status == "failed" and entry.get("errors"):
		bits = []
		for err in entry["errors"][:3]:
			detail = (err.get("error_data") or {}).get("details") or err.get("message") or ""
			bits.append(f"{err.get('code', '?')} · {err.get('title', '')} · {detail}"[:220])
		doc.failure_reason = "\n".join(bits)
	doc.save(ignore_permissions=True)
