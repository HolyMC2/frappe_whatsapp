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


_MAX_SCRIPT_STEPS = 50


def _require_operator() -> None:
	"""Only a System Manager may drive the simulator.

	Every entry point here was a bare @frappe.whitelist(), which means any
	AUTHENTICATED user — including a portal/Website User with no desk role —
	could call it. Both WhatsApp Account and WhatsApp Message grant permissions
	to System Manager alone, and every insert on these paths runs with
	ignore_permissions=True, so the whitelist was effectively handing the public
	a write primitive over WhatsApp data. Account names are guessable too
	(autoname is field:account_name, i.e. human-readable strings).

	System Manager is the right bar: it is exactly who may already read and
	write the doctypes these calls fabricate rows in.
	"""
	frappe.only_for("System Manager")


def _demo_account_or_throw(account: Any):
	"""Resolve the account and refuse if it is not in Demo mode."""
	_require_operator()
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


def _require_phone_id(account_doc) -> None:
	"""Refuse rather than report a success that silently dropped the message.

	webhook.post() resolves the account from metadata.phone_number_id and bails
	via `if messages and not whatsapp_account: return` when it cannot match one.
	With no phone_id on the account the envelope carries None, the message is
	dropped, and the caller still got {"simulated": True} plus a message_id that
	was never stored — the operator sees success and an empty thread.
	"""
	if not account_doc.get("phone_id"):
		frappe.throw(
			_(
				"WhatsApp Account {0} has no phone_id, so inbound cannot be routed to "
				"it and the message would be silently dropped. Set a phone_id first."
			).format(account_doc.name),
			title=_("Account cannot receive"),
		)


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
		# Unconditional: restoring only when `previous` was not None left the
		# synthetic Meta envelope on frappe.local.form_dict for the rest of the
		# request whenever the attribute had been absent. Hard to reach (frappe
		# .init seeds an empty _dict) and the tests seed a sentinel so they never
		# covered the None branch — costs nothing to just always restore.
		frappe.local.form_dict = previous if previous is not None else frappe._dict()


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
	_require_phone_id(doc)
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
	_require_phone_id(doc)
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

	# The Demo guard above is NOT sufficient on this path, and that was a real
	# hole. A status envelope carries no `messages`, so webhook.post() routes to
	# update_message_status, which resolves the target by message_id ALONE — no
	# account filter (utils/webhook.py) — and then saves it. So validating the
	# caller's Demo account and stopping there let anyone pass the wamid of a
	# LIVE account's real customer message and rewrite its status,
	# failure_reason and conversation_id. The account was checked, then never
	# consulted again.
	#
	# Bind the target to the guarded account explicitly, and refuse anything
	# that is not itself demo traffic.
	target = frappe.db.get_value(
		"WhatsApp Message",
		{"message_id": message_id},
		["name", "whatsapp_account", "is_demo"],
		as_dict=True,
	)
	if not target:
		frappe.throw(
			_("No WhatsApp Message with message_id {0} on this site.").format(message_id)
		)
	if target.whatsapp_account != doc.name:
		frappe.throw(
			_(
				"Message {0} belongs to WhatsApp Account {1}, not {2}. Refusing to "
				"rewrite the status of a message owned by another account."
			).format(message_id, target.whatsapp_account, doc.name),
			title=_("Cross-account status write refused"),
		)
	if not target.is_demo:
		frappe.throw(
			_(
				"Message {0} is real traffic, not demo traffic. Refusing to fabricate "
				"a delivery status for it."
			).format(message_id),
			title=_("Refusing to touch real traffic"),
		)

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
	# Cap the fan-out. Each `in` step runs a full webhook.post(), including a
	# WhatsApp Notification Log insert, so an unbounded script is a cheap
	# write-amplification lever.
	if len(steps) > _MAX_SCRIPT_STEPS:
		frappe.throw(
			_("script is limited to {0} steps.").format(_MAX_SCRIPT_STEPS)
		)

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
