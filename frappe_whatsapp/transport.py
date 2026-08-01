# Copyright (c) 2026, Marco and contributors
# For license information, please see license.txt

"""Single egress point for every Meta Graph API call.

WHY THIS EXISTS
---------------
Before this module, 27 call sites across five doctypes each built their own
`{url}/{version}/{phone_id}/...` and called `make_post_request` /
`make_request` / `requests.*` directly. There was no way to run the app
without talking to Meta, which meant:

  - a demo tenant could not show WhatsApp at all (demo-taller has neither the
    DocTypes nor a WABA, so taller's whole tracker-notify feature is invisible
    there), and
  - a sales demo on a real WABA sends real messages to real phone numbers,
    which costs money and drags the account's Meta quality rating down.

The fix is NOT a `demo_mode` check at each call site. Twenty-seven guards is
twenty-seven chances to forget one, and the failure mode of forgetting is a
live message to a prospect's phone. Instead every outbound call funnels through
here, so "am I allowed to talk to Meta?" is answered in exactly one place.

Keep it that way: nothing outside this module may import `make_post_request` /
`make_request` or call `requests.*` against a Graph host. There is a test that
enforces this (test_transport.TestEgressInvariant).

TWO CONTRACTS
-------------
The call sites this replaces had two different return shapes, and callers
depend on both:

  api()  -> dict            for make_post_request / make_request callers
  raw()  -> Response-like   for requests.* callers, which use .status_code,
                            .json(), .content, .text and .raise_for_status()

Demo mode has to satisfy both, so `raw()` returns a DemoResponse that
duck-types just enough of requests.Response.

MODE LIVES ON THE ACCOUNT
-------------------------
Not on WhatsApp Settings. A tenant demoing to a prospect while serving real
customers needs both at once, and the app already routes per account
(is_default_incoming / is_default_outgoing, and the webhook dispatches inbound
by phone_id). Per-account is the only granularity that supports that.
"""

from __future__ import annotations

import json
import uuid
from typing import Any, Optional
from urllib.parse import urlparse

import frappe
import requests
from frappe import _
from frappe.integrations.utils import make_post_request, make_request

MODE_LIVE = "Live"
MODE_DEMO = "Demo"

#: Prefix on every synthetic message id. Deliberately greppable — if this shows
#: up in a log or a report that is supposed to describe real traffic, something
#: is reading demo data as production.
DEMO_ID_PREFIX = "wamid.demo-"


# --------------------------------------------------------------------------
# account + mode resolution
# --------------------------------------------------------------------------


def resolve_account(account: Any):
	"""Accept a WhatsApp Account name or doc; return the doc."""
	if account is None:
		return None
	if isinstance(account, str):
		if not frappe.db.exists("WhatsApp Account", account):
			return None
		return frappe.get_cached_doc("WhatsApp Account", account)
	return account


def mode_for(account: Any) -> str:
	"""Live unless the account explicitly says Demo.

	Defaults to Live so existing accounts keep sending after this lands — the
	upgrade must not silently mute a tenant's production WhatsApp. Demo is
	strictly opt-in, per account.
	"""
	doc = resolve_account(account)
	if not doc:
		return MODE_LIVE
	return (doc.get("mode") or MODE_LIVE).strip() or MODE_LIVE


def is_demo(account: Any) -> bool:
	return mode_for(account) == MODE_DEMO


# --------------------------------------------------------------------------
# demo response synthesis
# --------------------------------------------------------------------------


def _demo_id(kind: str) -> str:
	return f"demo-{kind}-{uuid.uuid4().hex[:16]}"


def _demo_payload(method: str, url: str, data: Any = None) -> dict:
	"""A Meta-shaped response for a Graph endpoint, without the round trip.

	Shapes mirror what the real API returns for the fields callers actually
	read — `messages[0].id`, `id`, `data[]`, `success`. Anything unrecognised
	falls through to a generic success so a new endpoint degrades to "did
	nothing, reported ok" rather than exploding inside a demo.
	"""
	path = (urlparse(url).path or "").rstrip("/")
	method = (method or "GET").upper()

	if path.endswith("/messages"):
		# Outbound message (text / template / media / read-receipt).
		body = _as_dict(data)
		if body.get("status") == "read":
			return {"success": True}
		return {
			"messaging_product": "whatsapp",
			"contacts": [{"input": body.get("to", ""), "wa_id": body.get("to", "")}],
			"messages": [{"id": f"{DEMO_ID_PREFIX}{uuid.uuid4().hex[:16]}"}],
		}

	if path.endswith("/media") or path.endswith("/uploads") or path.startswith("/upload"):
		return {"id": _demo_id("media"), "h": _demo_id("handle")}

	if "message_templates" in path:
		if method == "GET":
			# Empty catalogue: a demo tenant authors its own templates and they
			# are approved locally (see WhatsAppTemplates in demo mode).
			return {"data": []}
		if method == "DELETE":
			return {"success": True}
		return {
			"id": _demo_id("tpl"),
			"status": "APPROVED",
			"category": _as_dict(data).get("category", "UTILITY"),
		}

	if "/flows" in path or path.endswith("/assets"):
		if method == "DELETE":
			return {"success": True}
		if method == "GET":
			return {"data": [], "preview": {"preview_url": "https://example.invalid/demo-flow-preview"}}
		return {"id": _demo_id("flow"), "success": True}

	if method == "GET":
		# Generic GET fallback. Carries the keys the media-fetch path in
		# utils.webhook reads unconditionally (`url`, `mime_type`) so simulated
		# inbound degrades to an empty attachment instead of a KeyError — a
		# media GET is `/{version}/{media_id}/`, which no path pattern can
		# distinguish from any other object read.
		return {"data": [], "url": "", "mime_type": ""}
	return {"success": True}


def _as_dict(data: Any) -> dict:
	if isinstance(data, dict):
		return data
	if isinstance(data, (str, bytes)):
		try:
			parsed = json.loads(data)
			return parsed if isinstance(parsed, dict) else {}
		except (ValueError, TypeError):
			return {}
	return {}


class DemoResponse:
	"""Duck-types the slice of requests.Response the call sites actually use.

	Only `.status_code`, `.json()`, `.content`, `.text`, `.headers` and
	`.raise_for_status()` are touched anywhere in this app — verified by
	reading all 27 sites — so that is all this implements. It is intentionally
	not a full Response: a caller reaching for something else should fail
	loudly here rather than get a plausible-looking blank.
	"""

	def __init__(self, payload: dict, status_code: int = 200):
		self._payload = payload
		self.status_code = status_code
		self.headers = {"content-type": "application/json", "x-demo-mode": "1"}

	def json(self) -> dict:
		return self._payload

	@property
	def text(self) -> str:
		return json.dumps(self._payload)

	@property
	def content(self) -> bytes:
		return self.text.encode("utf-8")

	def raise_for_status(self) -> None:
		return None

	def __repr__(self) -> str:  # pragma: no cover - debugging aid
		return f"<DemoResponse {self.status_code} {self._payload!r}>"


# --------------------------------------------------------------------------
# egress
# --------------------------------------------------------------------------


def _guard_live(account_doc, url: str) -> None:
	"""Refuse a live call that cannot possibly succeed.

	Previously an account with no token still fired the request and came back
	with an opaque Meta 401 that surfaced to the user as an unhelpful API
	error. Fail closed instead, and say which account is misconfigured.
	"""
	if not account_doc:
		return
	token = None
	try:
		token = account_doc.get_password("token", raise_exception=False)
	except Exception:
		token = None
	if not token:
		frappe.throw(
			_(
				"WhatsApp Account {0} has no token, so it cannot send. Set the token, "
				"or switch the account to Demo mode to exercise the flow without "
				"contacting Meta."
			).format(getattr(account_doc, "name", "?")),
			title=_("WhatsApp account not configured"),
		)


def api(
	account: Any,
	method: str,
	url: str,
	*,
	headers: Optional[dict] = None,
	data: Any = None,
) -> dict:
	"""Graph call returning a parsed dict. Replaces make_post_request/make_request."""
	if is_demo(account):
		_log_demo(account, method, url, data)
		return _demo_payload(method, url, data)

	account_doc = resolve_account(account)
	_guard_live(account_doc, url)
	if (method or "POST").upper() == "POST":
		return make_post_request(url, headers=headers, data=data)
	return make_request(method, url, headers=headers, data=data)


def raw(account: Any, method: str, url: str, **kwargs):
	"""Graph call returning a Response-like. Replaces direct requests.* usage."""
	if is_demo(account):
		_log_demo(account, method, url, kwargs.get("data") or kwargs.get("json"))
		return DemoResponse(_demo_payload(method, url, kwargs.get("data") or kwargs.get("json")))

	account_doc = resolve_account(account)
	_guard_live(account_doc, url)
	return requests.request((method or "GET").upper(), url, **kwargs)


def post(account: Any, url: str, **kwargs) -> dict:
	return api(account, "POST", url, **kwargs)


def get(account: Any, url: str, **kwargs) -> dict:
	return api(account, "GET", url, **kwargs)


def delete(account: Any, url: str, **kwargs) -> dict:
	return api(account, "DELETE", url, **kwargs)


def _log_demo(account: Any, method: str, url: str, data: Any) -> None:
	"""Record the suppressed call.

	A demo that silently does nothing is indistinguishable from a demo that is
	broken. This leaves a trail an operator can point at to prove the message
	was composed correctly and simply not delivered.
	"""
	doc = resolve_account(account)
	frappe.logger("frappe_whatsapp").info(
		{
			"demo_mode": True,
			"account": getattr(doc, "name", None),
			"method": (method or "GET").upper(),
			"url": url,
			"payload": _as_dict(data) or None,
		}
	)
