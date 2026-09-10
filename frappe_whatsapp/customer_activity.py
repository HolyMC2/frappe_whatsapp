"""Bound the durable WA message atom before entering customer control.

Proposed receipt hookup only: the existing webhook is intentionally unchanged.
"""

import hashlib
import json
import re

import frappe
from frappe.utils import get_datetime, now_datetime

from frappe_whatsapp.webhook_receipts import ReceiptError, canonical, digest


def _require(condition, reason="customer_activity_receipt_invalid"):
    if not condition:
        raise ReceiptError(reason)


def _ready():
    return "crm" in frappe.get_installed_apps() and all(
        frappe.db.exists("DocType", name) for name in ("CRM Conversation", "CRM Conversation Control Event"))


def _live_claim(row):
    try:
        return type(row.attempts) is int and row.attempts >= 1 and bool(row.lease_until) and get_datetime(row.lease_until) > now_datetime()
    except (ValueError, TypeError, AttributeError):
        return False


def _atom(receipt, scoped):
    _require(not getattr(frappe, "request", None) and receipt.name
             and frappe.flags.get("meta_webhook_receipt") == receipt.name, "customer_activity_worker_required")
    fields = ("provider", "account_id", "app_id", "event_type", "event_id", "event_key", "payload", "payload_hash", "state", "attempts", "lease_until")
    actual = frappe.db.get_value("Meta Webhook Receipt", receipt.name, fields, as_dict=True, for_update=True)
    _require(actual and actual.state == "Processing" and all(actual.get(field) == receipt.get(field) for field in fields))
    _require(_live_claim(actual), "customer_activity_claim_required")
    _require(actual.provider == "WhatsApp" and actual.event_type == "message")
    _require(isinstance(actual.payload, str) and len(actual.payload.encode()) <= 128 * 1024)
    _require(hashlib.sha256(actual.payload.encode()).hexdigest() == actual.payload_hash)
    _require(digest([actual.provider, actual.app_id, actual.account_id, actual.event_type, actual.event_id])
             == actual.event_key == receipt.name)
    try:
        payload = json.loads(actual.payload)
        _require(len(scoped.accounts) == 1 and scoped.app_id == actual.app_id
                 and scoped.business_id == payload["business_id"]
                 and canonical(scoped.change) == canonical(payload["change"]))
        change = payload["change"]
        value = change["value"]
        _require(change["field"] == "messages" and value["messaging_product"] == "whatsapp")
        _require(value["metadata"]["phone_number_id"] == actual.account_id)
        _require(not any(key in value for key in ("statuses", "message_echoes", "history", "state_sync", "smb_app_state_sync")))
        messages = value["messages"]
        _require(isinstance(messages, list) and len(messages) == 1 and isinstance(messages[0], dict))
        message = messages[0]
        _require(message["id"] == actual.event_id)
        _require(isinstance(message["from"], str) and re.fullmatch(r"[0-9]{1,20}", message["from"]))
        _require(isinstance(actual.account_id, str) and re.fullmatch(r"[0-9]{1,40}", actual.account_id))
        timestamp = message.get("timestamp")
        if type(timestamp) is int:
            timestamp = str(timestamp)
        _require(isinstance(timestamp, str) and re.fullmatch(r"[0-9]{1,12}", timestamp), "customer_activity_timestamp_invalid")
        timestamp = int(timestamp)
        _require(0 < timestamp <= 253402214400, "customer_activity_timestamp_invalid")
    except (KeyError, TypeError, ValueError, UnicodeError, AttributeError):
        raise ReceiptError("customer_activity_receipt_invalid") from None
    return actual.account_id, message["from"], timestamp


def consume_customer_activity(receipt, scoped):
    """Call before ordinary message projection, in the same receipt transaction."""
    account_id, peer_id, timestamp = _atom(receipt, scoped)
    if not _ready():
        return {"state": "Ignored", "reason_code": "customer_activity_control_unavailable"}
    try:
        from crm.api.conversation_activity import internal_apply_customer_activity
    except ImportError:
        raise ReceiptError("customer_activity_control_unavailable") from None
    return internal_apply_customer_activity("WhatsApp", account_id, peer_id,
        receipt_name=receipt.name, provider_timestamp=timestamp)
