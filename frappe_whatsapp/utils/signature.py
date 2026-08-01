# Copyright (c) 2026, Marco and contributors
# For license information, please see license.txt

"""X-Hub-Signature-256 verification for inbound Meta webhooks.

WHY
---
`utils.webhook.webhook()` is `allow_guest=True` and, until this module, did no
authentication on POST at all — only the GET handshake checked a token. Anyone
who could reach the URL could:

  - insert arbitrary `WhatsApp Message` rows (fabricated customer conversations
    that are indistinguishable, in the chat UI, from real ones),
  - rewrite any template's status through `update_template_status`, which is a
    raw SQL UPDATE keyed on an attacker-supplied id,
  - flip message `status` / `failure_reason` through `update_message_status`,
  - and spam `WhatsApp Notification Log`, which is written before any parsing.

Meta signs every webhook body with HMAC-SHA256 under the app secret and sends
it as `X-Hub-Signature-256: sha256=<hex>`. Verifying that is the fix.

STAGED ROLLOUT — READ BEFORE CHANGING
-------------------------------------
Turning this on unconditionally would have broken inbound WhatsApp on every
tenant that has not yet copied its app secret in, including production. So the
rule is deliberately keyed on configuration:

  - NO account on the site has an `app_secret`  -> verification is SKIPPED and
    a warning is logged. This is exactly today's behaviour, so deploying this
    module changes nothing until an operator opts in.
  - ANY account has an `app_secret`             -> a valid signature is
    REQUIRED for every POST, site-wide.

The all-or-nothing site-wide switch is intentional. Verifying per-account would
mean parsing the untrusted body to pick an account BEFORE authenticating it,
which lets an attacker choose which secret they are checked against — they
would simply address the one account that has no secret set.

THE BODY MUST BE THE RAW BYTES
------------------------------
The HMAC covers exactly what Meta transmitted. Re-serialising `form_dict` back
to JSON produces different bytes (key order, separators, unicode escaping) and
the signature will never match. Always hash `frappe.request.data`.
"""

from __future__ import annotations

import hashlib
import hmac
from typing import Optional

import frappe

_HEADER = "X-Hub-Signature-256"
_PREFIX = "sha256="


def configured_secrets() -> list[str]:
	"""Every non-empty app secret on the site.

	Read through the doc so Frappe decrypts the Password field; `db.get_value`
	returns the ciphertext placeholder instead of the secret.
	"""
	secrets = []
	for name in frappe.get_all("WhatsApp Account", pluck="name"):
		try:
			value = frappe.get_cached_doc("WhatsApp Account", name).get_password(
				"app_secret", raise_exception=False
			)
		except Exception:
			value = None
		if value:
			secrets.append(value)
	return secrets


def expected_signature(secret: str, body: bytes) -> str:
	return _PREFIX + hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


def is_valid(body: bytes, header: Optional[str], secrets: list[str]) -> bool:
	"""Constant-time compare against every configured secret.

	Multiple secrets are tried because a site can host several WABAs from
	different Meta apps; a match on any of them proves the body came from a
	party holding one of our secrets, which is the property we need. The
	account the message belongs to is still resolved from `phone_number_id`
	downstream, so this does not weaken routing.
	"""
	if not header:
		return False
	for secret in secrets:
		if hmac.compare_digest(expected_signature(secret, body), header):
			return True
	return False


def verify_request() -> None:
	"""Authenticate the current inbound webhook POST, or throw.

	No-op when there is no HTTP request in scope. That covers the in-process
	caller (`frappe_whatsapp.demo` pushes synthetic envelopes through the same
	handler on purpose, so there is one ingestion path rather than two) — and
	that caller is separately gated on System Manager, so it is not a hole a
	guest can reach.
	"""
	request = getattr(frappe, "request", None)
	if request is None:
		return

	secrets = configured_secrets()
	if not secrets:
		# Staged rollout: nothing configured yet, so behave as before. Logged so
		# the exposure is visible rather than silently permanent.
		frappe.logger("frappe_whatsapp").warning(
			"Inbound webhook accepted WITHOUT signature verification: no WhatsApp "
			"Account has an app_secret set. Set one to enforce X-Hub-Signature-256."
		)
		return

	body = request.get_data() or b""
	header = request.headers.get(_HEADER)
	if is_valid(body, header, secrets):
		return

	# Deliberately terse and identical for missing vs wrong signatures: a
	# detailed reason tells an attacker which half they got right.
	frappe.throw(
		frappe._("Invalid webhook signature."),
		frappe.PermissionError,
		title=frappe._("Rejected"),
	)
