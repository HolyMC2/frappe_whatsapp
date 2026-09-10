"""Account-scoped coexistence intake. No sends, profile merges, or media fetches.

Only raw Meta SMB echo envelopes are supported. BSP history/state-sync examples
are not an alternative public authentication envelope.
"""

import copy
import json
import re
from contextlib import nullcontext
from contextvars import ContextVar
from datetime import datetime, timezone
from types import MappingProxyType

import frappe
from frappe.utils import convert_utc_to_system_timezone, now_datetime

from frappe_whatsapp.webhook_receipts import ReceiptError, canonical, digest

MAX_ECHOES = 1000
SUPPORTED_TYPES = frozenset({"text", "image", "audio", "video", "document"})
_NUMBER = re.compile(r"[0-9]{1,20}")
_TYPE = re.compile(r"[a-z_]{1,32}")


def _string(value, reason, limit=140):
    if not isinstance(value, str) or not value.strip() or value != value.strip() or len(value) > limit or any(ord(c) < 32 for c in value):
        raise ReceiptError(reason)
    return value


def _number(value):
    if not isinstance(value, str) or not _NUMBER.fullmatch(value):
        raise ReceiptError("echo_invalid_number")
    return value


def _business_number(metadata):
    """Allow presentation punctuation only; no country/suffix identity merging."""
    display = metadata.get("display_phone_number")
    if not isinstance(display, str) or len(display) > 40 or not re.fullmatch(r"\+?[0-9 ()-]+", display):
        raise ReceiptError("echo_business_number_missing")
    return _number(re.sub(r"[ ()+-]", "", display))


def _timestamp(value):
    # Raw Meta timestamps are epoch seconds. No floats, bools, exponent notation,
    # millisecond guessing or local-time inference. Bound before datetime usage.
    if isinstance(value, int) and not isinstance(value, bool):
        value = str(value)
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{1,12}", value):
        raise ReceiptError("echo_invalid_timestamp")
    seconds = int(value)
    if not 0 < seconds <= 253402214400:
        raise ReceiptError("echo_invalid_timestamp")
    return seconds


def validate_echo(echo, metadata):
    if not isinstance(echo, dict):
        raise ReceiptError("echo_invalid_message")
    business, peer = _number(echo.get("from")), _number(echo.get("to"))
    if business != _business_number(metadata) or business == peer:
        raise ReceiptError("echo_account_mismatch")
    _string(echo.get("id"), "echo_invalid_message_id")
    _timestamp(echo.get("timestamp"))
    kind = echo.get("type")
    if not isinstance(kind, str) or not _TYPE.fullmatch(kind):
        raise ReceiptError("echo_invalid_type")
    content = echo.get(kind)
    if not isinstance(content, dict):
        raise ReceiptError("echo_invalid_content")
    if kind == "text":
        if not isinstance(content.get("body"), str) or not content["body"] or len(content["body"]) > 65536:
            raise ReceiptError("echo_invalid_text")
    elif kind in SUPPORTED_TYPES:
        _string(content.get("id"), "echo_invalid_media_id", 512)
        for field, limit in (("mime_type", 200), ("sha256", 128), ("filename", 255), ("caption", 65536)):
            if field in content and (not isinstance(content[field], str) or len(content[field]) > limit):
                raise ReceiptError("echo_invalid_media_metadata")
    return echo


def echo_identity(echo):
    return digest([echo["to"], echo["id"]])


def echo_events(scoped):
    """Validate the whole list before yielding its first deterministic atom."""
    value = scoped.change["value"]
    if scoped.change["field"] != "smb_message_echoes" or value.get("messaging_product") != "whatsapp":
        raise ReceiptError("echo_field_mismatch")
    metadata = value.get("metadata")
    if not isinstance(metadata, dict) or not isinstance(metadata.get("phone_number_id"), str):
        raise ReceiptError("echo_account_missing")
    if len(scoped.accounts) != 1:
        raise ReceiptError("echo_account_ambiguous")
    echoes = value.get("message_echoes")
    if not isinstance(echoes, list) or not echoes or len(echoes) > MAX_ECHOES:
        raise ReceiptError("echo_invalid_batch")
    if any(key in value for key in ("messages", "statuses", "contacts", "history", "state_sync")):
        raise ReceiptError("echo_ambiguous_batch")
    validated = [validate_echo(echo, metadata) for echo in echoes]
    for echo in validated:
        atom = {key: copy.deepcopy(item) for key, item in value.items() if key != "message_echoes"}
        atom["message_echoes"] = [copy.deepcopy(echo)]
        yield {
            "provider": "WhatsApp", "account_id": metadata["phone_number_id"], "app_id": scoped.app_id,
            "event_type": "smb_message_echoes", "event_id": echo_identity(echo),
            "payload": {"business_id": scoped.business_id,
                        "change": {"field": "smb_message_echoes", "value": atom}},
        }


# The controller's low-level insertion seam accepts only this identity token and
# exact in-memory document/values. JSON fields and frappe.flags cannot mint it.
_INSERT_TOKEN = object()
_INSERT_CONTEXT = ContextVar("coexistence_projection", default=None)
_EXTERNAL_FIELDS = ("external_receipt", "external_media", "external_sent_at")
_PROJECTION_FIELDS = frozenset({
    "doctype", "name", "owner", "creation", "modified", "modified_by", "docstatus", "idx", "__islocal",
    "type", "message_type", "status", "from", "to", "message_id", "message", "content_type",
    "whatsapp_account", *_EXTERNAL_FIELDS,
})


def _persisted_external(doc):
    # __islocal/is_new can be supplied by generic document clients. They must
    # never hide a real stored origin from the retry/send guard.
    if not doc.name:
        return False
    if not frappe.db.has_column("WhatsApp Message", "external_receipt"):
        return False
    return bool(frappe.db.get_value("WhatsApp Message", doc.name, "external_receipt", for_update=True))


def reject_external_mutation(doc):
    """Ordinary document APIs cannot create, edit, strip or rename this origin."""
    if any(doc.get(field) not in (None, "") for field in _EXTERNAL_FIELDS) or _persisted_external(doc):
        frappe.throw("External business-app messages are immutable projections.", frappe.ValidationError)


def assert_sendable(doc):
    # The stored marker is checked even when a generic client clears all fields
    # on its in-memory document before invoking an existing retry/send path.
    if any(doc.get(field) not in (None, "") for field in _EXTERNAL_FIELDS) or _persisted_external(doc):
        frappe.throw("External business-app messages cannot be sent again.", frappe.ValidationError)


def assert_projection_insert(doc, token):
    context = _INSERT_CONTEXT.get()
    if token is not _INSERT_TOKEN or not context or context[0] is not doc:
        raise ReceiptError("external_projection_forbidden")
    expected = context[1]
    if any(key not in _PROJECTION_FIELDS and value not in (None, "", 0, False)
           for key, value in doc.as_dict().items()):
        raise ReceiptError("external_projection_fields_invalid")
    if any(doc.get(key) != value for key, value in expected.items()):
        raise ReceiptError("external_projection_fields_invalid")
    if not doc.is_new() or doc.doctype != "WhatsApp Message":
        raise ReceiptError("external_projection_forbidden")


def _assert_worker_receipt(receipt):
    if getattr(frappe, "request", None) or frappe.flags.get("meta_webhook_receipt") != receipt.name:
        raise ReceiptError("coexistence_worker_required")
    fields = ("provider", "account_id", "app_id", "event_type", "event_id", "payload", "state")
    actual = frappe.db.get_value("Meta Webhook Receipt", receipt.name, fields, as_dict=True)
    if not actual or actual.state != "Processing" or any(actual.get(field) != receipt.get(field) for field in fields):
        raise ReceiptError("coexistence_receipt_mismatch")


def projection_name(receipt, echo):
    return "wa-external-" + digest([receipt.app_id, receipt.account_id, echo["to"], echo["id"]])


def _projection_values(receipt, account, echo):
    kind = echo["type"]
    media = echo[kind] if kind != "text" else None
    body = echo["text"]["body"] if kind == "text" else media.get("caption", "")
    sent_at = convert_utc_to_system_timezone(
        datetime.fromtimestamp(_timestamp(echo["timestamp"]), timezone.utc).replace(tzinfo=None)
    ).replace(tzinfo=None)
    now = now_datetime()
    return {
        "doctype": "WhatsApp Message", "name": projection_name(receipt, echo),
        "owner": frappe.session.user, "creation": now, "modified": now,
        "modified_by": frappe.session.user, "docstatus": 0, "idx": 0,
        "type": "Outgoing", "message_type": "Manual", "status": "sent",
        "from": echo["from"], "to": echo["to"], "message_id": echo["id"],
        "message": frappe.utils.escape_html(body).replace("\n", "<br>"),
        "content_type": kind, "whatsapp_account": account.name,
        "external_receipt": receipt.name, "external_sent_at": sent_at,
        "external_media": (canonical({key: media[key] for key in ("id", "mime_type", "sha256", "filename")
                                      if key in media}) if media is not None else None),
    }


def _matching_projection(name, account, echo, receipt):
    row = frappe.db.get_value("WhatsApp Message", name,
        ["name", "type", "whatsapp_account", "from", "to", "message_id", "external_receipt"],
        as_dict=True, for_update=True)
    if not row:
        return None
    expected = {"type": "Outgoing", "whatsapp_account": account.name, "from": echo["from"],
                "to": echo["to"], "message_id": echo["id"], "external_receipt": receipt.name}
    if any(row.get(key) != value for key, value in expected.items()):
        raise ReceiptError("external_projection_collision")
    return row.name


def _insert_projection(receipt, account, echo):
    name = projection_name(receipt, echo)
    existing = _matching_projection(name, account, echo, receipt)
    if existing:
        return existing
    values = _projection_values(receipt, account, echo)
    doc = frappe.get_doc(values)
    doc.__islocal = 1  # Explicit deterministic names otherwise appear loaded to Frappe.
    point = "external_echo_" + frappe.generate_hash(length=12)
    frappe.db.savepoint(point)
    context_token = _INSERT_CONTEXT.set((doc, MappingProxyType(values)))
    try:
        doc._insert_external_projection(_INSERT_TOKEN)
    except (frappe.DuplicateEntryError, frappe.UniqueValidationError):
        frappe.db.rollback(save_point=point)
        if not _matching_projection(name, account, echo, receipt):
            raise
    finally:
        _INSERT_CONTEXT.reset(context_token)
    return name


def _local_echo(account, echo):
    """No app-id heuristic, phone suffix match, Contact or profile lookup."""
    rows = frappe.db.sql("""
        SELECT name FROM `tabWhatsApp Message`
        WHERE type='Outgoing' AND whatsapp_account=%s AND `to`=%s AND message_id=%s
          AND COALESCE(external_receipt, '')=''
        ORDER BY name LIMIT 2 FOR UPDATE
    """, (account.name, echo["to"], echo["id"]), as_dict=True)
    return (rows[0].name if len(rows) == 1 else None), len(rows) > 1


def _conversation_service():
    if "crm" not in frappe.get_installed_apps() or not all(
        frappe.db.exists("DocType", doctype) for doctype in ("CRM Conversation", "CRM Conversation Control Event")
    ):
        return None
    try:
        from crm.api import conversations
    except ImportError:
        raise ReceiptError("conversation_control_import_failed") from None
    return conversations


def _conversation_control(receipt, peer_id, own):
    service = _conversation_service()
    if service is None:
        return False
    service.internal_apply_provider_event("WhatsApp", receipt.account_id, peer_id,
        "own_outbound" if own else "external_outbound", receipt_name=receipt.name, historical=False)
    return True


def consume_echo(receipt, scoped):
    """Project one committed signed receipt, then conservatively hold bot control."""
    _assert_worker_receipt(receipt)
    atoms = list(echo_events(scoped))
    if len(atoms) != 1:
        raise ReceiptError("echo_receipt_not_atomic")
    atom = atoms[0]
    if any(atom[key] != receipt.get(key) for key in ("provider", "account_id", "app_id", "event_type", "event_id")):
        raise ReceiptError("coexistence_receipt_mismatch")
    if atom["payload"] != json.loads(receipt.payload):
        raise ReceiptError("coexistence_receipt_mismatch")
    if not all(frappe.db.has_column("WhatsApp Message", field) for field in _EXTERNAL_FIELDS):
        raise ReceiptError("coexistence_schema_unavailable")
    account = frappe.get_doc("WhatsApp Account", scoped.accounts[0])
    if (account.phone_id != receipt.account_id or account.app_id != receipt.app_id
            or account.business_id != scoped.business_id or account.status != "Active"
            or (account.get("mode") or "Live") != "Live"):
        raise ReceiptError("echo_account_mismatch")
    echo = atom["payload"]["change"]["value"]["message_echoes"][0]
    service = _conversation_service()
    fence = (service.conversation_fence(service.conversation_key("WhatsApp", receipt.account_id, echo["to"]))
             if service else nullcontext())
    # Match P5's order: conversation fence before message-row locks. The core
    # provider-action helper's nested acquisition is connection-reentrant.
    with fence:
        local, ambiguous = _local_echo(account, echo)
        projected = None
        if not local and echo["type"] in SUPPORTED_TYPES:
            projected = _insert_projection(receipt, account, echo)
        applied = _conversation_control(receipt, echo["to"], bool(local))
    if not applied:
        reason = "conversation_control_unavailable"
    elif ambiguous:
        reason = "echo_correlation_ambiguous"
    elif local:
        reason = "own_cloud_echo_correlated"
    elif not projected:
        reason = "echo_type_unsupported"
    else:
        reason = "external_echo_projected"
    return {"state": "Processed" if local or projected else "Ignored", "reason_code": reason}
