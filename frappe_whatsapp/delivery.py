"""Native delivery bridge for the existing WA status helper; no transport."""

import hashlib
import json

import frappe
from frappe.utils import get_datetime, now_datetime

from frappe_whatsapp.webhook_receipts import ReceiptError, canonical, digest


def _require(condition):
    if not condition:
        raise ReceiptError("native_delivery_receipt_invalid")


def _live_claim(row):
    try:
        return type(row.attempts) is int and row.attempts >= 1 and bool(row.lease_until) and get_datetime(row.lease_until) > now_datetime()
    except (ValueError, TypeError, AttributeError):
        return False


def fold_native_delivery(entry, accounts):
    """Proposed call at the start of _apply_message_status, before WM locks.

    A verified native match can satisfy an absent legacy WM. Missing matches do
    not suppress legacy processing or its retryable missing-target outcome.
    """
    name = frappe.flags.get("meta_webhook_receipt")
    if getattr(frappe, "request", None) or not name:
        raise ReceiptError("native_delivery_worker_required")
    row = frappe.db.get_value("Meta Webhook Receipt", name,
        ["provider", "account_id", "app_id", "event_type", "event_id", "event_key", "payload", "payload_hash", "state", "attempts", "lease_until"],
        as_dict=True, for_update=True)
    _require(row and row.state == "Processing" and row.provider == "WhatsApp" and row.event_type == "status")
    if not _live_claim(row):
        raise ReceiptError("native_delivery_claim_required")
    _require(isinstance(row.payload, str) and len(row.payload.encode()) <= 128 * 1024)
    _require(hashlib.sha256(row.payload.encode()).hexdigest() == row.payload_hash)
    _require(digest([row.provider, row.app_id, row.account_id, row.event_type, row.event_id]) == row.event_key == name)
    try:
        payload = json.loads(row.payload)
        _require(payload["change"]["field"] == "messages")
        value = payload["change"]["value"]
        _require(value["messaging_product"] == "whatsapp" and value["metadata"]["phone_number_id"] == row.account_id)
        _require(isinstance(value["statuses"], list) and len(value["statuses"]) == 1
                 and canonical(value["statuses"][0]) == canonical(entry))
        _require(isinstance(accounts, (tuple, list)) and len(accounts) == 1)
    except (KeyError, TypeError, ValueError, UnicodeError):
        raise ReceiptError("native_delivery_receipt_invalid") from None
    if "crm" not in frappe.get_installed_apps() or not frappe.db.exists("DocType", "CRM Outbound Intent"):
        return {"matched": False, "reason_code": "native_delivery_schema_unavailable"}
    try:
        from crm.api.outbox_delivery import apply_delivery_receipt
    except ImportError:
        raise ReceiptError("native_delivery_service_unavailable") from None
    return apply_delivery_receipt(name, expected_entry=entry, account_records=tuple(accounts))
