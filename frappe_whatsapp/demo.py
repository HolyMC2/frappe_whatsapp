# Copyright (c) 2026, Marco and contributors
# For license information, please see license.txt

"""Inbound simulation for Demo-mode WhatsApp accounts.

`transport.py` stops outbound traffic reaching Meta. That alone gives a demo
tenant a conversation that can only ever talk — no replies, no inbound media,
no delivery ticks. Most of what the chat UI shows is inbound, so a send-only
demo still looks broken.

This module supplies the other half: synthetic inbound events.

ONE CODE PATH, NOT TWO
----------------------
Everything here builds a Meta-shaped webhook envelope and pushes it through
`utils.webhook.post()` — the exact handler a real Meta callback lands in. It
does NOT insert WhatsApp Message rows directly. That is deliberate: a parallel
"demo ingestion" path would drift from the real one, and every drift is a bug
the demo hides (account routing by phone_id, reply threading, profile-name
capture, media attachment, the unmatched-phone_id drop). If ingestion breaks,
the simulator breaks with it, which is the point.

SAFETY
------
Every entry point refuses unless the target account is in Demo mode. Injecting
fabricated inbound into a live tenant would write messages that appear to come
from a real customer into a real conversation history — indistinguishable, in
the UI, from something the customer actually said. That is the failure this
guard exists to prevent, so it is checked on the account the webhook will
ACTUALLY route to (resolved by phone_id, exactly as the webhook resolves it),
not on the caller's argument.
"""

from __future__ import annotations

import json
import time
import uuid
from typing import Any, Optional

import frappe
from frappe import _

from frappe_whatsapp import transport


def _demo_account_or_throw(account: Any):
	"""Resolve the account and refuse if it is not in Demo mode."""
	doc = transport.resolve_account(account)
	if not doc:
		frappe.throw(
			_("WhatsApp Account {0} not found.").format(account),
			title=_("Unknown account"),
		)
	if not transport.is_demo(doc):
		frappe.throw(
			_(
				"WhatsApp Account {0} is in Live mode. Inbound simulation is refused: "
				"it would write fabricated customer messages into a real conversation "
				"history. Switch the account to Demo mode first."
			).format(doc.name),
			title=_("Refusing to simulate on a live account"),
		)
	return doc


def _envelope(account_doc, value: dict) -> dict:
	"""A Meta webhook envelope shaped like the real thing.

	`metadata.phone_number_id` is what utils.webhook.post() routes on, so it
	must carry THIS account's phone_id — that is what makes the simulated
	inbound land on the right account on a multi-account site.
	"""
	value = dict(value)
	value.setdefault("messaging_product", "whatsapp")
	value["metadata"] = {
		"display_phone_number": account_doc.get("phone_id"),
		"phone_number_id": account_doc.get("phone_id"),
	}
	return {
		"object": "whatsapp_business_account",
		"entry": [
			{
				"id": account_doc.get("business_id") or "demo-business",
				"changes": [{"field": "messages", "value": value}],
			}
		],
	}


def _dispatch(envelope: dict) -> None:
	"""Run the real webhook handler over a synthetic payload.

	post() reads frappe.local.form_dict, so we swap it, call, and restore —
	rather than reimplementing ingestion. The restore is in a finally block
	because this runs inside a request whose form_dict the rest of the
	stack still needs.
	"""
	from frappe_whatsapp.utils import webhook

	previous = getattr(frappe.local, "form_dict", None)
	try:
		frappe.local.form_dict = frappe._dict(envelope)
		webhook.post()
	finally:
		if previous is not None:
			frappe.local.form_dict = previous


def _message_id() -> str:
	return f"{transport.DEMO_ID_PREFIX}{uuid.uuid4().hex[:16]}"


@frappe.whitelist()
def simulate_inbound_text(
	account: str,
	from_number: str,
	body: str,
	profile_name: Optional[str] = None,
	reply_to_message_id: Optional[str] = None,
) -> dict:
	"""Deliver a synthetic inbound text message to a Demo account."""
	doc = _demo_account_or_throw(account)
	body = (body or "").strip()
	if not body:
		frappe.throw(_("body is required."))
	from_number = (from_number or "").strip()
	if not from_number:
		frappe.throw(_("from_number is required."))

	message: dict[str, Any] = {
		"from": from_number,
		"id": _message_id(),
		"timestamp": str(int(time.time())),
		"type": "text",
		"text": {"body": body},
	}
	if reply_to_message_id:
		message["context"] = {"id": reply_to_message_id}

	value: dict[str, Any] = {"messages": [message]}
	if profile_name:
		value["contacts"] = [
			{"profile": {"name": profile_name}, "wa_id": from_number}
		]

	_dispatch(_envelope(doc, value))
	return {"account": doc.name, "message_id": message["id"], "simulated": True}


@frappe.whitelist()
def simulate_inbound_reaction(
	account: str, from_number: str, emoji: str, to_message_id: str
) -> dict:
	"""Deliver a synthetic reaction to an existing message."""
	doc = _demo_account_or_throw(account)
	if not to_message_id:
		frappe.throw(_("to_message_id is required."))
	message = {
		"from": (from_number or "").strip(),
		"id": _message_id(),
		"timestamp": str(int(time.time())),
		"type": "reaction",
		"reaction": {"emoji": emoji or "👍", "message_id": to_message_id},
	}
	_dispatch(_envelope(doc, {"messages": [message]}))
	return {"account": doc.name, "message_id": message["id"], "simulated": True}


@frappe.whitelist()
def simulate_delivery_status(
	account: str, message_id: str, status: str = "read"
) -> dict:
	"""Deliver a synthetic delivery/read receipt for an outbound message.

	Without this the demo's own outbound messages never show a tick, which is
	the first thing anyone looks at in a WhatsApp UI.
	"""
	doc = _demo_account_or_throw(account)
	status = (status or "read").strip().lower()
	allowed = {"sent", "delivered", "read", "failed"}
	if status not in allowed:
		frappe.throw(
			_("status must be one of: {0}").format(", ".join(sorted(allowed)))
		)
	if not message_id:
		frappe.throw(_("message_id is required."))

	value = {
		"statuses": [
			{
				"id": message_id,
				"status": status,
				"timestamp": str(int(time.time())),
				"recipient_id": "demo-recipient",
			}
		]
	}
	_dispatch(_envelope(doc, value))
	return {"account": doc.name, "message_id": message_id, "status": status}


def _parse_script(script: Any) -> list:
	"""Decode + validate the conversation script in one place.

	Returns a list or raises. The explicit `return []` after throw() is
	unreachable in Frappe (throw raises) but keeps the contract honest for
	static analysis and for any caller that stubs throw().
	"""
	try:
		steps = json.loads(script) if isinstance(script, str) else script
	except (TypeError, ValueError):
		steps = None
	if not isinstance(steps, list):
		frappe.throw(_("script must be a JSON list of steps."))
		return []
	return steps


@frappe.whitelist()
def simulate_conversation(account: str, from_number: str, script: str) -> dict:
	"""Replay a scripted exchange so a demo opens on a populated thread.

	`script` is a JSON list of {"direction": "in"|"out", "body": "..."}.
	Outbound entries insert a WhatsApp Message, which routes through transport
	and is therefore suppressed and marked exactly like any other demo send —
	no special-casing, so what the demo shows is what the product does.
	"""
	doc = _demo_account_or_throw(account)
	steps = _parse_script(script)

	created = []
	for step in steps:
		body = (step or {}).get("body")
		if not body:
			continue
		if (step or {}).get("direction") == "out":
			msg = frappe.get_doc({
				"doctype": "WhatsApp Message",
				"type": "Outgoing",
				"to": from_number,
				"message": body,
				"content_type": "text",
				"whatsapp_account": doc.name,
			})
			msg.insert(ignore_permissions=True)
			created.append({"direction": "out", "name": msg.name})
		else:
			out = simulate_inbound_text(doc.name, from_number, body)
			created.append({"direction": "in", "message_id": out["message_id"]})

	frappe.db.commit()
	return {"account": doc.name, "steps": len(created), "messages": created}
