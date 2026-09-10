"""Durable Meta inbound work. No provider submissions belong in this consumer.

Only run_receipt owns worker commit/rollback. record_events participates in the
caller's transaction. The scheduler recovers lost enqueue and expired claims.
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import timedelta

import frappe
from frappe.utils import now_datetime, get_datetime

DOCTYPE = "Meta Webhook Receipt"
PROVIDERS = {"WhatsApp", "Messenger", "Instagram"}
MAX_ATTEMPTS = 8
LEASE_SECONDS = 300
MAX_EVENTS = 1000
MAX_PAYLOAD = 128 * 1024


class ReceiptError(RuntimeError):
    def __init__(self, reason):
        self.reason = reason
        super().__init__(reason)


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def ready():
    return bool(frappe.db.exists("DocType", DOCTYPE) and frappe.db.has_column(DOCTYPE, "event_key"))


def _prepare(event):
    if not isinstance(event, dict) or event.get("provider") not in PROVIDERS:
        raise ReceiptError("invalid_provider")
    for key in ("account_id", "app_id", "event_type", "event_id"):
        value = event.get(key)
        if not isinstance(value, str) or not value or len(value) > (512 if key == "event_id" else 140):
            raise ReceiptError("invalid_event_identity")
    if not isinstance(event.get("payload"), dict):
        raise ReceiptError("invalid_payload")
    payload = canonical(event["payload"])
    if len(payload.encode()) > MAX_PAYLOAD:
        raise ReceiptError("payload_too_large")
    key = digest([event[k] for k in ("provider", "app_id", "account_id", "event_type", "event_id")])
    return {"doctype": DOCTYPE, "event_key": key, "provider": event["provider"],
            "account_id": event["account_id"], "app_id": event["app_id"],
            "event_type": event["event_type"], "event_id": event["event_id"],
            "payload": payload, "payload_hash": hashlib.sha256(payload.encode()).hexdigest(),
            "state": "Pending", "attempts": 0, "received_at": now_datetime()}


def record_events(events: list[dict]) -> list[str]:
    """Insert all authenticated events before the request's normal commit.

    Duplicate event identity retains its first evidence. A provider edit must use
    its own event type/version; replay never overwrites a receipt's payload.
    """
    if not isinstance(events, list) or len(events) > MAX_EVENTS:
        raise ReceiptError("invalid_batch")
    prepared = [_prepare(event) for event in events]  # validate ALL before writes
    if not ready():
        raise ReceiptError("receipt_schema_unavailable")
    names = []
    for values in sorted(prepared, key=lambda value: value["event_key"]):
        name = values["event_key"]
        point = "meta_receipt_" + frappe.generate_hash(length=10)
        frappe.db.savepoint(point)
        try:
            frappe.get_doc(values).insert(ignore_permissions=True)
        except (frappe.DuplicateEntryError, frappe.UniqueValidationError):
            frappe.db.rollback(save_point=point)
            if not frappe.db.sql(f"SELECT name FROM `tab{DOCTYPE}` WHERE name=%s FOR UPDATE", name):
                raise
        names.append(name)
    if names:
        # No exception can turn an already committed HTTP receipt into lost work.
        frappe.db.after_commit.add(lambda: enqueue_receipts(tuple(dict.fromkeys(names))))
    return names


def enqueue_receipts(names):
    for name in names:
        try:
            frappe.enqueue("frappe_whatsapp.webhook_receipts.run_receipt", receipt_name=name,
                           queue="short", job_id="meta-receipt:" + name,
                           deduplicate=True, timeout=LEASE_SECONDS)
        except Exception:
            frappe.logger("frappe_whatsapp").warning("meta_receipt_enqueue_deferred")
            # Sweeper is authoritative recovery; never log payloads/provider errors.


def _lock(name):
    rows = frappe.db.sql(f"SELECT * FROM `tab{DOCTYPE}` WHERE name=%s FOR UPDATE", name, as_dict=True)
    return rows[0] if rows else None


def _eligible(row, now):
    if row.state in {"Processed", "Ignored"}:
        return False
    if row.attempts >= MAX_ATTEMPTS and row.state != "Processing":
        return False
    if row.state == "Processing" and row.lease_until and get_datetime(row.lease_until) > now:
        return False
    return not row.next_attempt_at or get_datetime(row.next_attempt_at) <= now


def _consumer(row):
    if row.provider == "WhatsApp":
        from frappe_whatsapp.utils.webhook import consume_receipt
    elif "doco_marketing" in frappe.get_installed_apps():
        from doco_marketing.api.messenger_webhook import consume_receipt
    else:
        raise ReceiptError("consumer_unavailable")
    return consume_receipt(row)


def run_receipt(receipt_name):
    """Background job transaction boundary; never invoke inside an HTTP request."""
    if getattr(frappe, "request", None):
        raise ReceiptError("worker_context_required")
    row = _lock(receipt_name)
    now = now_datetime()
    if not row or not _eligible(row, now):
        frappe.db.rollback()
        return
    if row.attempts >= MAX_ATTEMPTS:
        frappe.db.set_value(DOCTYPE, row.name, {"state": "Failed", "lease_until": None,
            "next_attempt_at": None, "reason_code": "retry_exhausted"}, update_modified=False)
        frappe.db.commit()
        return
    attempts = int(row.attempts) + 1
    frappe.db.set_value(DOCTYPE, row.name, {"state": "Processing", "attempts": attempts,
        "lease_until": now + timedelta(seconds=LEASE_SECONDS), "next_attempt_at": None,
        "reason_code": None}, update_modified=False)
    frappe.db.commit()  # worker boundary: durable evidence before any enrichment
    old_receipt = frappe.flags.get("meta_webhook_receipt")
    old_replay = frappe.flags.get("meta_webhook_replay")
    try:
        row = _lock(receipt_name)
        # An overlapping worker may claim only after lease expiry; detect it.
        if row.state != "Processing" or row.attempts != attempts:
            frappe.db.rollback()
            return
        frappe.flags.meta_webhook_receipt = row.name
        frappe.flags.meta_webhook_replay = attempts > 1 or row.event_type in {"history", "standby"} or row.event_type.startswith("standby:") or (
            now - get_datetime(row.received_at) > timedelta(minutes=5))
        result = _consumer(row) or {"state": "Processed"}
        state = result.get("state", "Processed")
        if state not in {"Processed", "Ignored"}:
            raise ReceiptError("invalid_consumer_outcome")
        reason = result.get("reason_code") or ""
        if reason and not re.fullmatch(r"[a-z0-9_]{1,100}", reason):
            raise ReceiptError("invalid_consumer_reason")
        frappe.db.set_value(DOCTYPE, row.name, {"state": state, "processed_at": now_datetime(),
            "lease_until": None, "next_attempt_at": None, "reason_code": reason}, update_modified=False)
        frappe.db.commit()  # local domain work and completion are atomic
    except Exception as error:
        frappe.db.rollback()  # this worker only; HTTP work was committed first
        reason = getattr(error, "reason", "consumer_failed")
        if not isinstance(reason, str) or not re.fullmatch(r"[a-z0-9_]{1,100}", reason):
            reason = "consumer_failed"
        row = _lock(receipt_name)
        if row and row.state == "Processing" and row.attempts == attempts:
            frappe.db.set_value(DOCTYPE, row.name, {"state": "Failed", "lease_until": None,
                "reason_code": reason, "next_attempt_at": now_datetime() + timedelta(
                    seconds=min(3600, 15 * 2 ** attempts)) if attempts < MAX_ATTEMPTS else None},
                update_modified=False)
            frappe.db.commit()
        else:
            frappe.db.rollback()
    finally:
        frappe.flags.meta_webhook_receipt = old_receipt
        frappe.flags.meta_webhook_replay = old_replay


def sweep():
    """Bounded recovery of committed receipts and abandoned safe-ingest claims."""
    if not ready():
        return
    now = now_datetime()
    rows = frappe.db.sql(f"""SELECT name FROM `tab{DOCTYPE}`
        WHERE (attempts < %s OR state = 'Processing') AND state IN ('Pending','Failed','Processing')
        AND (next_attempt_at IS NULL OR next_attempt_at <= %s)
        AND (state != 'Processing' OR lease_until IS NULL OR lease_until <= %s)
        ORDER BY creation LIMIT 200""", (MAX_ATTEMPTS, now, now), as_dict=True)
    enqueue_receipts([row.name for row in rows])
