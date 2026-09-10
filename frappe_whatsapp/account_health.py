"""Saved-account health: local evidence plus one explicit bounded metadata GET.

No schema/cache, sends, subscriptions, credential mutation or payload projection.
The current manager and account locks cover the bounded provider check so a
concurrent disable/reassignment cannot change the authority being inspected.
"""
import json
import re
import time

import frappe
from frappe.utils import get_datetime, now_datetime

from frappe_whatsapp import transport

GRAPH = "https://graph.facebook.com"
MAX_RESPONSE_BYTES = 64 * 1024
TIMEOUT = (5, 10)
DEADLINE_SECONDS = 20
OBSERVATION_TYPES = (
    "phone_number_quality_update", "phone_number_name_update", "account_update",
    "account_alerts", "account_review_update", "business_capability_update",
    "message_template_status_update", "message_template_quality_update", "template_category_update",
)
RECEIPT_STATES = ("Pending", "Processing", "Processed", "Failed", "Ignored")
OUTBOX_STATES = ("Draft", "Queued", "Claimed", "Submitting", "Accepted", "Delivered", "Read", "Deferred", "Blocked", "Failed", "Unknown", "Cancelled")


def _number(value):
    return isinstance(value, str) and bool(re.fullmatch(r"[0-9]{1,80}", value))


def _manager():
    user = frappe.session.user
    if not user or user == "Guest" or frappe.db.get_value("User", user, "enabled", for_update=True) != 1:
        raise frappe.PermissionError("System Manager access is required.")
    # Bypass stale role caches. Administrator is Frappe's built-in all-role user.
    if user != "Administrator" and not frappe.db.get_values("Has Role", {
        "parent": user, "parenttype": "User", "parentfield": "roles", "role": "System Manager",
    }, "name", for_update=True):
        raise frappe.PermissionError("System Manager access is required.")


def _account(account_name, expected_modified):
    _manager()
    if not isinstance(account_name, str) or not account_name or len(account_name) > 140 or not isinstance(expected_modified, str):
        raise frappe.ValidationError("Save and reload the account before checking health.")
    account = frappe.get_doc("WhatsApp Account", account_name, for_update=True)
    account.check_permission("read")
    try:
        current = get_datetime(account.modified) == get_datetime(expected_modified)
    except (ValueError, TypeError, OverflowError):
        current = False
    if not current:
        raise frappe.ValidationError("The account changed. Reload it before checking health.")
    return account


def _configuration(account):
    # Inspect encrypted credential presence without selecting/decrypting values.
    encrypted = {row[0] for row in frappe.db.sql(
        "SELECT fieldname FROM `__Auth` WHERE doctype=%s AND name=%s AND encrypted=1 AND fieldname IN ('token','app_secret')",
        ("WhatsApp Account", account.name),
    )}
    def identity(value):
        return "configured" if _number(value) else "invalid" if value else "missing"
    return {
        "token": "configured" if "token" in encrypted else "missing",
        "app_secret": "configured" if "app_secret" in encrypted else "missing",
        "app_id": identity(account.app_id), "business_id": identity(account.business_id),
        "phone_id": identity(account.phone_id),
        "webhook_verify_token": "configured" if account.webhook_verify_token else "missing",
        "version": "configured" if isinstance(account.version, str) and re.fullmatch(r"v[1-9][0-9]{0,2}\.[0-9]{1,2}", account.version) else "invalid" if account.version else "missing",
    }


def _receipt_counts(account_id, app_id):
    if not _number(account_id) or not _number(app_id):
        return {"available": False, "reason_code": "missing_scope"}
    if not frappe.db.table_exists("Meta Webhook Receipt"):
        return {"available": False, "reason_code": "receipt_ledger_unavailable"}
    rows = frappe.db.sql("""SELECT state, COUNT(*) AS count, MAX(received_at) AS last_received_at
        FROM `tabMeta Webhook Receipt` WHERE provider='WhatsApp' AND account_id=%s AND app_id=%s GROUP BY state""",
        (account_id, app_id), as_dict=True)
    counts = {state: 0 for state in RECEIPT_STATES}
    received = []
    for row in rows:
        if row.state in counts:
            counts[row.state] = int(row.count)
        if row.last_received_at:
            received.append(str(row.last_received_at))
    return {"available": True, "states": counts, "total": sum(counts.values()),
        "backlog": counts["Pending"] + counts["Processing"], "failed": counts["Failed"],
        "last_received_at": max(received) if received else None}


def _observations(account):
    if not _number(account.business_id) or not _number(account.app_id) or not frappe.db.table_exists("Meta Webhook Receipt"):
        return []
    # WABA observations are deliberately not attributed to this phone. Neither
    # payload nor event_id is read; arrival order cannot establish current state.
    rows = frappe.db.sql("""SELECT event_type, received_at, state FROM `tabMeta Webhook Receipt`
        WHERE provider='WhatsApp' AND account_id=%s AND app_id=%s AND event_type IN %s
        ORDER BY received_at DESC, name DESC LIMIT 10""", (account.business_id, account.app_id, OBSERVATION_TYPES), as_dict=True)
    return [{"event_type": row.event_type, "received_at": str(row.received_at) if row.received_at else None,
        "receipt_state": row.state if row.state in RECEIPT_STATES else "Unknown", "source": "signed_waba_receipt",
        "interpretation": "unsupported_order_unverified", "phone_mapping": "unverified"} for row in rows]


def _outbox_counts(account):
    if not _number(account.phone_id):
        return {"available": False, "reason_code": "missing_scope"}
    if "crm" not in frappe.get_installed_apps() or not frappe.db.table_exists("CRM Outbound Intent"):
        return {"available": False, "reason_code": "outbox_unavailable"}
    rows = frappe.db.sql("""SELECT state, COUNT(*) AS count FROM `tabCRM Outbound Intent`
        WHERE provider='WhatsApp' AND account_id=%s GROUP BY state""", (account.phone_id,), as_dict=True)
    counts = {state: 0 for state in OUTBOX_STATES}
    for row in rows:
        if row.state in counts:
            counts[row.state] = int(row.count)
    return {"available": True, "states": counts, "unknown": counts["Unknown"], "failed": counts["Failed"],
        "backlog": sum(counts[state] for state in ("Queued", "Claimed", "Submitting", "Deferred"))}


def _projection(account):
    return {"account_name": account.name, "account_modified": str(account.modified),
        "scope": {key: account.get(key) if _number(account.get(key)) else None for key in ("phone_id", "business_id", "app_id")},
        "source": "local_configuration_and_ledgers", "observed_at": str(now_datetime()),
        "mode": account.mode if account.mode in {"Demo", "Live"} else "Unknown",
        "status": account.status if account.status in {"Active", "Inactive"} else "Unknown",
        "configuration": _configuration(account), "phone_receipts": _receipt_counts(account.phone_id, account.app_id),
        "waba_receipts": _receipt_counts(account.business_id, account.app_id), "waba_observations": _observations(account),
        "outbox": _outbox_counts(account), "check": _check_result("not_checked", source="local_configuration"),
        "subscriptions": "unknown", "webhook_delivery": "unknown"}


def _check_result(state, *, source="meta_graph_phone_get", quality=None):
    return {"state": state, "source": source, "checked_at": None if state == "not_checked" else str(now_datetime()), "quality_rating": quality,
        "scope": "phone_metadata_only", "persisted": False}


def _read_json(response, deadline):
    size = response.headers.get("Content-Length")
    if size is not None and (not str(size).isdigit() or int(size) > MAX_RESPONSE_BYTES):
        raise ValueError()
    if response.headers.get("Content-Type", "").lower().split(";", 1)[0].strip() != "application/json":
        raise ValueError()
    body = bytearray()
    if time.monotonic() > deadline:
        raise ValueError()
    for chunk in response.iter_content(chunk_size=8192):
        if time.monotonic() > deadline or not isinstance(chunk, bytes) or len(body) + len(chunk) > MAX_RESPONSE_BYTES:
            raise ValueError()
        body.extend(chunk)
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError()
            result[key] = value
        return result
    def nonfinite(_):
        raise ValueError()
    value = json.loads(body, object_pairs_hook=unique, parse_constant=nonfinite)
    if not isinstance(value, dict):
        raise ValueError()
    return value


def _remote_check(account, configuration):
    if account.mode == "Demo":
        return _check_result("demo_no_remote", source="local_configuration")
    if account.mode != "Live" or account.status != "Active":
        return _check_result("inactive", source="local_configuration")
    if any(configuration[key] != "configured" for key in ("token", "phone_id", "version")):
        return _check_result("missing_configuration", source="local_configuration")
    try:
        token = account.get_password("token", raise_exception=False)
        if not isinstance(token, str) or not re.fullmatch(r"[\x21-\x7e]{1,4096}", token):
            raise ValueError()
    except Exception:
        return _check_result("missing_configuration", source="local_configuration")
    response = None
    try:
        deadline = time.monotonic() + DEADLINE_SECONDS
        response = transport.raw(account, "GET", f"{GRAPH}/{account.version}/{account.phone_id}",
            params={"fields": "id,quality_rating"}, headers={"Authorization": "Bearer " + token, "Accept": "application/json"},
            timeout=TIMEOUT, allow_redirects=False, stream=True)
        status = response.status_code
        if type(status) is not int or status < 200 or status >= 500 or 300 <= status < 400:
            return _check_result("unavailable")
        if status in (401, 403, 429):
            return _check_result({401: "authentication_failed", 403: "permission_denied", 429: "rate_limited"}[status])
        data = _read_json(response, deadline)
        if status >= 400:
            error = data.get("error")
            code = error.get("code") if isinstance(error, dict) else None
            if type(code) is int:
                if code in {102, 190}:
                    return _check_result("authentication_failed")
                if code in {10, 200}:
                    return _check_result("permission_denied")
                if code in {4, 17, 32, 613, 80007, 130429}:
                    return _check_result("rate_limited")
            return _check_result("unavailable")
        if set(data) != {"id", "quality_rating"} or data["id"] != account.phone_id or data["quality_rating"] not in {"GREEN", "YELLOW", "RED", "NA"}:
            return _check_result("unavailable")
        return _check_result("verified", quality=data["quality_rating"])
    except Exception:
        return _check_result("unavailable")
    finally:
        if response is not None:
            try:
                response.close()
            except Exception:
                pass


@frappe.whitelist()
def get_health(account_name, expected_modified):
    """Local metadata only. Opening the Desk view never contacts Meta."""
    return _projection(_account(account_name, expected_modified))


@frappe.whitelist(methods=["POST"])
def check_phone(account_name, expected_modified):
    """Explicit ephemeral GET; caller cannot supply credentials, URL or fields."""
    account = _account(account_name, expected_modified)
    result = _projection(account)
    result["check"] = _remote_check(account, result["configuration"])
    return result
