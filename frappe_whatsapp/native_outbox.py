"""One bounded WhatsApp submission under the core outbox's private authority.

This module never composes from live business documents, chooses a default
account, uploads media, commits, retries, or logs provider content.
"""

import json
import re
import time
from urllib.parse import urlsplit

import frappe

from frappe_whatsapp import transport

GRAPH = "https://graph.facebook.com"
MAX_PAYLOAD_BYTES = 256 * 1024
MAX_RESPONSE_BYTES = 64 * 1024
TIMEOUT = (5, 20)
RESPONSE_DEADLINE_SECONDS = 30
_MEDIA = {"image", "audio", "video", "document"}
_TYPES = _MEDIA | {"text", "reaction", "template", "interactive"}


class _Invalid(ValueError):
    pass


def _result(state, reason, retryable=False):
    return {"state": state, "reason_code": reason, "retryable": retryable}


def _require(condition):
    if not condition:
        raise _Invalid()


def _string(value, maximum, *, empty=False):
    _require(isinstance(value, str) and (empty or bool(value)) and len(value) <= maximum)
    _require(not any(ord(char) < 32 and char not in "\n\r\t" for char in value))
    return value


def _identifier(value, maximum=255):
    _string(value, maximum)
    _require(value == value.strip() and not any(char.isspace() for char in value))
    return value


def _number(value, maximum=40):
    _require(isinstance(value, str) and re.fullmatch(r"[0-9]{1," + str(maximum) + "}", value))
    return value


def _keys(value, allowed, required=()):
    _require(isinstance(value, dict) and set(required) <= value.keys() <= set(allowed))


def _bounded(value, depth=0, budget=None):
    """Bound the tree before serializing, including hostile cycles/deep objects."""
    budget = [0] if budget is None else budget
    budget[0] += 1
    _require(depth <= 12 and budget[0] <= 4000)
    if isinstance(value, dict):
        _require(len(value) <= 64)
        for key, child in value.items():
            _string(key, 100)
            _bounded(child, depth + 1, budget)
    elif isinstance(value, list):
        _require(len(value) <= 100)
        for child in value:
            _bounded(child, depth + 1, budget)
    elif isinstance(value, str):
        _string(value, 65536, empty=True)
    else:
        _require(value is None or isinstance(value, (bool, int)))
        if isinstance(value, int):
            _require(abs(value) <= 2**53)


def _media(value, kind):
    allowed = {"id", "link"}
    if kind != "audio":
        allowed.add("caption")
    if kind == "document":
        allowed.add("filename")
    _keys(value, allowed)
    _require(("id" in value) != ("link" in value))
    if "id" in value:
        _identifier(value["id"], 512)
    else:
        link = _string(value["link"], 4096)
        parsed = urlsplit(link)
        _require(parsed.scheme == "https" and parsed.hostname and not parsed.username and not parsed.password)
        _require(parsed.port in (None, 443) and not parsed.fragment and not any(char.isspace() for char in link))
    if "caption" in value:
        _string(value["caption"], 1024, empty=True)
    if "filename" in value:
        _string(value["filename"], 255)


def _template(value):
    _keys(value, {"name", "language", "components"}, {"name", "language"})
    _require(re.fullmatch(r"[a-z0-9_]{1,512}", _string(value["name"], 512)))
    _keys(value["language"], {"code"}, {"code"})
    _require(re.fullmatch(r"[a-zA-Z_]{2,20}", _string(value["language"]["code"], 20)))
    components = value.get("components", [])
    _require(isinstance(components, list) and len(components) <= 20)
    for component in components:
        _keys(component, {"type", "sub_type", "index", "parameters"}, {"type", "parameters"})
        _require(component["type"] in {"header", "body", "button"})
        if component["type"] == "button":
            _require(component.get("sub_type") in {"quick_reply", "url", "mpm"})
            _require(str(component.get("index", "")) in {str(i) for i in range(10)})
        else:
            _require("sub_type" not in component and "index" not in component)
        parameters = component["parameters"]
        _require(isinstance(parameters, list) and len(parameters) <= 100)
        for parameter in parameters:
            _require(isinstance(parameter, dict))
            kind = parameter.get("type")
            _require(kind in {"text", "payload", "action"} | _MEDIA)
            _keys(parameter, {"type", kind}, {"type", kind})
            if kind in _MEDIA:
                _media(parameter[kind], kind)
            elif kind == "action":
                _require(isinstance(parameter[kind], dict) and bool(parameter[kind]))
            else:
                _string(parameter[kind], 32768, empty=True)


def _interactive(value):
    _keys(value, {"type", "header", "body", "footer", "action"}, {"type", "body", "action"})
    kind = value["type"]
    _require(kind in {"button", "list", "flow"})
    for label in ("body", "footer"):
        if label in value:
            _keys(value[label], {"text"}, {"text"})
            _string(value[label]["text"], 1024 if label == "body" else 60)
    if "header" in value:
        header = value["header"]
        _require(isinstance(header, dict))
        header_type = header.get("type")
        _require(header_type in {"text", "image", "video", "document"})
        _keys(header, {"type", header_type}, {"type", header_type})
        if header_type == "text":
            _string(header["text"], 60)
        else:
            _media(header[header_type], header_type)
    action = value["action"]
    if kind == "button":
        _keys(action, {"buttons"}, {"buttons"})
        _require(isinstance(action["buttons"], list) and 1 <= len(action["buttons"]) <= 3)
        for button in action["buttons"]:
            _keys(button, {"type", "reply"}, {"type", "reply"})
            _require(button["type"] == "reply")
            _keys(button["reply"], {"id", "title"}, {"id", "title"})
            _string(button["reply"]["id"], 256)
            _string(button["reply"]["title"], 20)
    elif kind == "list":
        _keys(action, {"button", "sections"}, {"button", "sections"})
        _string(action["button"], 20)
        _require(isinstance(action["sections"], list) and 1 <= len(action["sections"]) <= 10)
        total = 0
        for section in action["sections"]:
            _keys(section, {"title", "rows"}, {"rows"})
            if "title" in section:
                _string(section["title"], 24)
            _require(isinstance(section["rows"], list) and bool(section["rows"]))
            total += len(section["rows"])
            for row in section["rows"]:
                _keys(row, {"id", "title", "description"}, {"id", "title"})
                _string(row["id"], 200)
                _string(row["title"], 24)
                if "description" in row:
                    _string(row["description"], 72, empty=True)
        _require(total <= 10)
    else:
        _keys(action, {"name", "parameters"}, {"name", "parameters"})
        _require(action["name"] == "flow")
        params = action["parameters"]
        _keys(params, {"flow_message_version", "flow_id", "flow_cta", "flow_action", "flow_action_payload", "flow_token", "mode"},
              {"flow_message_version", "flow_id", "flow_cta", "flow_action", "flow_action_payload", "flow_token"})
        _require(params["flow_message_version"] == "3" and params["flow_action"] == "navigate")
        _number(params["flow_id"])
        _string(params["flow_cta"], 30)
        _identifier(params["flow_token"], 512)
        _require(params.get("mode", "published") in {"published", "draft"})
        _keys(params["flow_action_payload"], {"screen", "data"}, {"screen"})
        _identifier(params["flow_action_payload"]["screen"], 100)
        if "data" in params["flow_action_payload"]:
            _require(isinstance(params["flow_action_payload"]["data"], dict))


def _payload(payload, account_id, peer_id):
    _bounded(payload)
    _require(isinstance(payload, dict))
    kind = payload.get("type")
    _require(kind in _TYPES)
    _keys(payload, {"messaging_product", "recipient_type", "to", "type", "context", kind},
          {"messaging_product", "to", "type", kind})
    _require(payload["messaging_product"] == "whatsapp")
    _require(payload.get("recipient_type", "individual") == "individual")
    _require(_number(payload["to"], 20) == _number(peer_id, 20))
    _number(account_id)
    if "context" in payload:
        _keys(payload["context"], {"message_id"}, {"message_id"})
        _identifier(payload["context"]["message_id"])
    content = payload[kind]
    if kind == "text":
        _keys(content, {"body", "preview_url"}, {"body"})
        _string(content["body"], 4096)
        _require("preview_url" not in content or isinstance(content["preview_url"], bool))
    elif kind in _MEDIA:
        _media(content, kind)
    elif kind == "reaction":
        _keys(content, {"message_id", "emoji"}, {"message_id", "emoji"})
        _identifier(content["message_id"])
        _string(content["emoji"], 32, empty=True)
    elif kind == "template":
        _template(content)
    else:
        _interactive(content)
    body = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    _require(len(body) <= MAX_PAYLOAD_BYTES)
    return body


def validate_payload(payload, *, account_id, peer_id):
    """Pure creation/dispatch validator; return bounded canonical UTF-8 JSON."""
    try:
        return _payload(payload, account_id, peer_id)
    except (ValueError, TypeError, AttributeError, RecursionError, OverflowError):
        raise ValueError("frozen_payload_invalid") from None


def _account(account_id):
    rows = frappe.db.get_values("WhatsApp Account", {"phone_id": account_id},
        ["name", "phone_id", "status", "mode", "version"], as_dict=True, for_update=True)
    _require(len(rows) == 1)
    row = rows[0]
    _require(row.phone_id == account_id and row.status == "Active" and row.mode == "Live")
    _require(isinstance(row.version, str) and re.fullmatch(r"v[1-9][0-9]{0,2}\.[0-9]{1,2}", row.version))
    # Build only from current locked metadata; do not reread an older RR snapshot
    # through the document cache. Password lookup remains scoped to this name.
    account = frappe.get_doc({"doctype": "WhatsApp Account", **row})
    token = account.get_password("token", raise_exception=False)
    _require(isinstance(token, str) and re.fullmatch(r"[\x21-\x7e]{1,4096}", token))
    return account, token


def _read_json(response, deadline):
    size = response.headers.get("Content-Length")
    if size is not None:
        _require(str(size).isdigit() and int(size) <= MAX_RESPONSE_BYTES)
    _require("application/json" in response.headers.get("Content-Type", "").lower())
    body = bytearray()
    _require(time.monotonic() <= deadline)
    for chunk in response.iter_content(chunk_size=8192):
        _require(time.monotonic() <= deadline)
        _require(isinstance(chunk, bytes) and len(body) + len(chunk) <= MAX_RESPONSE_BYTES)
        body.extend(chunk)
    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            _require(key not in result)
            result[key] = value
        return result
    parsed = json.loads(body, object_pairs_hook=unique_object)
    _require(isinstance(parsed, dict))
    return parsed


def _classify(response, peer_id, deadline):
    status = response.status_code
    if not isinstance(status, int) or status >= 500 or status < 200 or 300 <= status < 400:
        return _result("Unknown", "provider_response_uncertain")
    parsed = _read_json(response, deadline)
    if 400 <= status < 500:
        error = parsed.get("error")
        if isinstance(error, dict) and type(error.get("code")) is int and error["code"] > 0:
            return _result("Failed", "provider_rate_limited" if status == 429 else "provider_rejected", status == 429)
        return _result("Unknown", "provider_response_uncertain")
    _require(parsed.get("messaging_product") == "whatsapp" and "error" not in parsed)
    contacts, messages = parsed.get("contacts"), parsed.get("messages")
    _require(isinstance(contacts, list) and len(contacts) == 1 and isinstance(contacts[0], dict))
    _require(contacts[0].get("wa_id") == peer_id)
    _require(contacts[0].get("input", peer_id) == peer_id)
    _require(isinstance(messages, list) and len(messages) == 1 and isinstance(messages[0], dict))
    message_id = _identifier(messages[0].get("id"))
    _require(message_id.startswith("wamid.") and len(message_id) > 6 and not message_id.startswith(transport.DEMO_ID_PREFIX))
    return {"state": "Accepted", "provider_message_id": message_id}


def send_frozen(intent, payload):
    """Submit once. Only the core dispatcher can authorize this internal method."""
    try:
        from crm.api.outbox import require_dispatch
        require_dispatch(intent.name, "WhatsApp", intent.account_id, intent.peer_id, payload=payload)
    except Exception:
        return _result("Blocked", "dispatch_authority_required")
    try:
        _require(intent.provider == "WhatsApp")
        body = validate_payload(payload, account_id=intent.account_id, peer_id=intent.peer_id)
    except (ValueError, TypeError, AttributeError, RecursionError, OverflowError):
        return _result("Blocked", "frozen_payload_invalid")
    try:
        account, token = _account(intent.account_id)
    except Exception:
        return _result("Blocked", "account_configuration_invalid")
    response = None
    try:
        deadline = time.monotonic() + RESPONSE_DEADLINE_SECONDS
        response = transport.raw(account, "POST", f"{GRAPH}/{account.version}/{intent.account_id}/messages",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json", "Accept": "application/json"},
            data=body, timeout=TIMEOUT, allow_redirects=False, stream=True)
        return _classify(response, intent.peer_id, deadline)
    except Exception:
        # Once HTTP is attempted, even parsing/connection errors can hide an
        # accepted message. No exception text or provider body leaves this seam.
        return _result("Unknown", "provider_response_uncertain")
    finally:
        if response is not None:
            try:
                response.close()
            except Exception:
                pass
