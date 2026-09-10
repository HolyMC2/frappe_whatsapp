"""Authenticate raw Meta bytes, then bind every change to its signing app/account.

There is deliberately no unsigned setup mode. Configure app_id, app_secret,
business_id and phone_id before deploying this receiver. Demo ingestion uses a
separate operator-only entry point; absence of an HTTP request grants no trust.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import re
from dataclasses import dataclass

import frappe

_HEADER = "X-Hub-Signature-256"
_PREFIX = "sha256="
MAX_BODY_BYTES = 2 * 1024 * 1024
MAX_CHANGES = 1000


@dataclass(frozen=True)
class ScopedChange:
    business_id: str
    app_id: str
    accounts: tuple[str, ...]
    change: dict


def _accounts():
    # Read current credentials/configuration; cached documents could authorize a
    # revoked app or an account transferred to another WABA.
    return [frappe.get_doc("WhatsApp Account", name)
            for name in frappe.get_all("WhatsApp Account", pluck="name")]


def _secret(account):
    try:
        return account.get_password("app_secret", raise_exception=False) or ""
    except Exception:
        return ""


def configured_secrets() -> list[str]:
    """Compatibility diagnostic only. Never use a site-wide match for routing."""
    return [secret for account in _accounts() if (secret := _secret(account))]


def expected_signature(secret: str, body: bytes) -> str:
    return _PREFIX + hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


def is_valid(body: bytes, header: str | None, secrets: list[str]) -> bool:
    if not isinstance(header, str) or not re.fullmatch(r"sha256=[0-9a-f]{64}", header):
        return False
    return any(hmac.compare_digest(expected_signature(secret, body), header) for secret in secrets)


def _deny():
    frappe.throw(frappe._("Invalid webhook signature or account scope."), frappe.PermissionError)


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def verify_request() -> list[ScopedChange]:
    """Return only changes authenticated and authorized by the raw HTTP body.

    Validate the entire envelope before the caller can write even its first row.
    A known app's signature cannot authorize an unknown WABA, a different app's
    phone, a disabled account, or a Demo account.
    """
    request = getattr(frappe, "request", None)
    if not request or request.method != "POST":
        _deny()
    body = request.get_data() or b""
    if not isinstance(body, bytes) or not body or len(body) > MAX_BODY_BYTES:
        _deny()
    accounts = _accounts()
    active = [a for a in accounts if a.get("status") == "Active"
              and (a.get("mode") or "Live") == "Live" and a.get("app_id")]
    apps = set()
    for candidate in active:
        secret = _secret(candidate)
        if secret and is_valid(body, request.headers.get(_HEADER), [secret]):
            apps.add(str(candidate.app_id))
    # Secret reuse across different app IDs is an ambiguous trust configuration.
    if len(apps) != 1:
        _deny()
    try:
        data = json.loads(body, object_pairs_hook=_object)
    except (ValueError, UnicodeError):
        _deny()
    return scope_payload(data, active, apps.pop())


def scope_payload(data: dict, accounts: list, app_id: str) -> list[ScopedChange]:
    """Pure envelope validation/routing, also reused by the gated Demo path."""
    if not isinstance(data, dict) or data.get("object") != "whatsapp_business_account":
        _deny()
    entries = data.get("entry")
    if not isinstance(entries, list) or len(entries) > MAX_CHANGES:
        _deny()
    result = []
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("id"), str) or not entry["id"]:
            _deny()
        candidates = [a for a in accounts if str(a.get("app_id")) == app_id
                      and isinstance(a.get("business_id"), str) and a.get("business_id")
                      and a.get("business_id") == entry["id"]]
        if not candidates or not isinstance(entry.get("changes"), list):
            _deny()
        for change in entry["changes"]:
            if not isinstance(change, dict) or not isinstance(change.get("field"), str):
                _deny()
            value = change.get("value")
            if not isinstance(value, dict):
                _deny()
            metadata = value.get("metadata") or {}
            if not isinstance(metadata, dict):
                _deny()
            phone = metadata.get("phone_number_id")
            selected = candidates
            if phone is not None:
                if not isinstance(phone, str) or not phone:
                    _deny()
                selected = [a for a in candidates if a.get("phone_id") == phone]
                if len(selected) != 1:
                    _deny()
            elif change["field"] in {"messages", "smb_message_echoes", "history", "smb_app_state_sync"} or "messages" in value or "statuses" in value:
                _deny()
            for key in ("messages", "statuses", "contacts"):
                if key in value and (not isinstance(value[key], list)
                                     or any(not isinstance(v, dict) for v in value[key])):
                    _deny()
            result.append(ScopedChange(entry["id"], app_id,
                                       tuple(a.name for a in selected), change))
            if len(result) > MAX_CHANGES:
                _deny()
    return result
