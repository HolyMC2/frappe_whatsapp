"""Final WhatsApp message scope for both transport entry points.

Admin/media edges remain separate. Read/presence effects have no trustworthy
recipient in this generic seam and are held until an exact-message adapter exists.
"""

from contextlib import ExitStack, contextmanager
import json
import re
from urllib.parse import parse_qsl, unquote, urlsplit

import frappe

from frappe_whatsapp.transport import NotConfigured

GRAPH = "graph.facebook.com"
MAX_BYTES = 65536
KINDS = {"text", "image", "audio", "video", "document", "sticker", "location", "contacts", "interactive", "reaction", "template"}


class LegacySendBlocked(NotConfigured):
    def __init__(self, reason="legacy_message_scope_invalid"):
        self.reason_code = reason
        # Some legacy Notification handlers inspect this even before HTTP. Clear
        # a previous request's response instead of exposing stale provider text.
        frappe.flags.integration_request = SafeIntegrationResponse(reason)
        super().__init__(reason)


class LegacyMessageError(frappe.ValidationError):
    def __init__(self, reason, outcome="Unknown"):
        self.reason_code, self.outcome = reason, outcome
        super().__init__(reason)


class SafeIntegrationResponse:
    """Existing message controllers can inspect errors without retaining secrets."""
    def __init__(self, reason="provider_response_uncertain"):
        self.reason = reason

    def json(self):
        return {"error": {"message": self.reason}}


def message_result(response, peer, deadline):
    # Reuse the native gateway's bounded JSON reader and exact-recipient evidence.
    from frappe_whatsapp.native_outbox import _classify
    result = _classify(response, peer, deadline)
    if result["state"] != "Accepted":
        raise LegacyMessageError(result["reason_code"], result["state"])
    return {"messaging_product": "whatsapp", "contacts": [{"input": peer, "wa_id": peer}],
            "messages": [{"id": result["provider_message_id"]}]}


def message_headers(headers):
    headers = dict(headers or {})
    # Canonical JSON is what was authorized; prevent another content type or
    # HTTP method override from changing how Graph interprets those bytes.
    for key in list(headers):
        if str(key).lower() in {"x-http-method", "x-http-method-override", "x-method-override"}:
            raise LegacySendBlocked()
        if str(key).lower() == "content-type":
            headers.pop(key)
    headers["Content-Type"] = "application/json"
    return headers


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise LegacySendBlocked()
        result[key] = value
    return result


def _reject_constant(value):
    raise LegacySendBlocked()


def _effect_keys(keys):
    result = set()
    for key in keys:
        if not isinstance(key, str):
            raise LegacySendBlocked()
        # Form/query parsers may interpret bracket notation as an array. Do not
        # let batch[0][method] or byte keys evade the generic-batch exclusion.
        result.add(re.split(r"[\[.]", unquote(key).strip().lower(), maxsplit=1)[0])
    return result


def _body(data):
    try:
        if isinstance(data, (str, bytes)):
            if len(data) > MAX_BYTES:
                raise LegacySendBlocked()
            data = json.loads(data, object_pairs_hook=_pairs, parse_constant=_reject_constant)
        if not isinstance(data, dict):
            raise LegacySendBlocked()
        count = 0
        def check(value, depth=0):
            nonlocal count
            count += 1
            if count > 2000 or depth > 12:
                raise LegacySendBlocked()
            if isinstance(value, dict):
                for key, item in value.items():
                    if not isinstance(key, str):
                        raise LegacySendBlocked()
                    check(item, depth + 1)
            elif isinstance(value, list):
                for item in value:
                    check(item, depth + 1)
            elif value is not None and type(value) not in (str, int, float, bool):
                raise LegacySendBlocked()
        check(data)
        frozen = json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode()
        if len(frozen) > MAX_BYTES:
            raise LegacySendBlocked()
        return json.loads(frozen), frozen
    except LegacySendBlocked:
        raise
    except Exception:
        raise LegacySendBlocked() from None


def _route(method, url, *, data, json_body, params):
    """Return the exact message account, or None for non-message admin/reads."""
    try:
        parsed = urlsplit(url)
        decoded = unquote(parsed.path)
        keys = _effect_keys(key for key, value in parse_qsl(parsed.query))
        if params:
            keys.update(_effect_keys(params if isinstance(params, dict) else dict(params)))
        if keys & {"method", "batch", "to", "recipient", "messaging_product"}:
            raise LegacySendBlocked()
        candidate = "messages" in decoded.lower().split("/")
        # Reject a batch/method override or a send body hidden behind another
        # endpoint. Form-encoded /messages is rejected by _body below as well.
        probe = json_body if json_body is not None else data
        if isinstance(probe, (str, bytes)):
            if len(probe) > MAX_BYTES:
                raise LegacySendBlocked()
            try:
                probe = json.loads(probe, object_pairs_hook=_pairs)
            except (ValueError, TypeError, UnicodeError):
                # Graph also accepts form-encoded batch and method overrides.
                probe = dict(parse_qsl(probe.decode() if isinstance(probe, bytes) else probe))
        if probe is not None and not isinstance(probe, dict):
            # requests accepts form pair lists, generators and file-like bodies.
            # This seam cannot prove their final recipient or exclude a batch.
            raise LegacySendBlocked()
        if isinstance(probe, dict):
            body_keys = _effect_keys(probe)
            if body_keys & {"batch", "method", "relative_url"}:
                raise LegacySendBlocked()
            candidate = candidate or bool(body_keys & {"to", "recipient"})
        if not candidate:
            return None
        match = re.fullmatch(r"/(v[0-9]{1,3}\.[0-9]{1,2})/([0-9]{1,40})/messages", parsed.path)
        if (method.upper() != "POST" or not match or not re.fullmatch(
                r"https://graph\.facebook\.com(?::443)?/v[0-9]{1,3}\.[0-9]{1,2}/[0-9]{1,40}/messages", url)
                or parsed.scheme != "https"
                or parsed.hostname != GRAPH or parsed.port not in (None, 443)
                or parsed.username is not None or parsed.password is not None
                or parsed.query or parsed.fragment or params):
            raise LegacySendBlocked()
        return match.group(2), match.group(1)
    except LegacySendBlocked:
        raise
    except Exception:
        raise LegacySendBlocked() from None


def _account(account, account_id, version):
    name = account if isinstance(account, str) else getattr(account, "name", None)
    rows = frappe.db.get_values("WhatsApp Account", {"phone_id": account_id},
        ["name", "phone_id", "status", "mode", "version"], as_dict=True, for_update=True)
    if (len(rows) != 1 or rows[0].name != name or rows[0].phone_id != account_id
            or rows[0].status != "Active" or (rows[0].mode or "Live") != "Live"
            or rows[0].version != version):
        raise LegacySendBlocked("legacy_account_scope_invalid")


@contextmanager
def guard_transport_send(account, method, url, *, data=None, json_body=None, params=None, files=None):
    """Yield frozen JSON bytes for message POSTs, None for unrelated operations."""
    # Existing media/Flow uploaders use a single file part. Other multipart
    # fields can carry a Graph batch without a JSON body or /messages endpoint.
    if files is not None and (not isinstance(files, dict) or set(files) != {"file"}):
        raise LegacySendBlocked()
    route = _route(method, url, data=data, json_body=json_body, params=params)
    if route is None:
        yield None
        return
    if files is not None or (data is not None and json_body is not None):
        raise LegacySendBlocked()
    body, frozen = _body(json_body if json_body is not None else data)
    if set(body) & {"status", "sender_action", "typing_indicator"}:
        raise LegacySendBlocked("legacy_action_recipient_unverified")
    peer, kind = body.get("to"), body.get("type")
    if (body.get("messaging_product") != "whatsapp" or not isinstance(peer, str)
            or not re.fullmatch(r"[0-9]{1,40}", peer) or not isinstance(kind, str) or kind not in KINDS
            or set(body) - {"messaging_product", "to", "type", "recipient_type", "context", kind}
            or not isinstance(body.get(kind), list if kind == "contacts" else dict)
            or body.get("recipient_type", "individual") != "individual"):
        raise LegacySendBlocked()
    account_id, version = route
    with ExitStack() as stack:
        try:
            if "crm" in frappe.get_installed_apps():
                from crm.api.outbox_legacy import guard_legacy_send
                stack.enter_context(guard_legacy_send("WhatsApp", account_id, peer, payload=body))
            # Read after the conversation fence, never from a cached/caller doc.
            _account(account, account_id, version)
        except LegacySendBlocked:
            raise
        except Exception as error:
            reason = getattr(error, "reason_code", "legacy_control_unavailable")
            if reason not in {"native_outbound_intent_required", "legacy_conversation_scope_invalid", "legacy_control_conflict"}:
                reason = "legacy_control_unavailable"
            raise LegacySendBlocked(reason) from None
        yield frozen
