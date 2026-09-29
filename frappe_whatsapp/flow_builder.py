"""Build a local WhatsApp Flow from a caller's field spec, without calling Meta.

Other apps (e.g. a CRM form) describe screens and input fields; this module
turns them into the existing WhatsApp Flow / Flow Screen / Flow Field records,
whose controller renders the Flow JSON. Creating, uploading and publishing at
Meta stay explicit actions on the WhatsApp Flow document.

A flow that Meta already knows (has a flow_id) is never rewritten in place: a
published flow's JSON is frozen at Meta, so a changed spec becomes a new local
draft that must be published on its own.
"""

import hashlib
import json
import re

import frappe
from frappe import _

INPUT_TYPES = ("TextInput", "TextArea", "DatePicker", "Dropdown", "RadioButtonsGroup", "CheckboxGroup", "OptIn")
# Meta Flow JSON component limits (characters).
LABEL_LIMITS = {"TextInput": 20, "TextArea": 20, "Dropdown": 20, "DatePicker": 40, "OptIn": 120}
HELPER_LIMIT = 80
OPTION_TITLE_LIMIT = 30
OPTION_LIMIT = 200
SCREEN_TITLE_LIMIT = 30
MAX_FIELDS_PER_SCREEN = 40
_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,79}$")


def spec_hash(spec: dict) -> str:
	encoded = json.dumps(spec, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
	return hashlib.sha256(encoded.encode()).hexdigest()


def _screen_id(index: int) -> str:
	# Screen ids accept letters and underscores only.
	letters = ""
	index += 1
	while index:
		index, rem = divmod(index - 1, 26)
		letters = chr(65 + rem) + letters
	return "SCREEN_" + letters


def _clip(text: str, limit: int) -> str:
	text = (text or "").strip()
	return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _field_rows(screen_id: str, field: dict) -> dict:
	kind = field["type"]
	if kind not in INPUT_TYPES:
		frappe.throw(_("Unsupported WhatsApp Flow field type: {0}").format(kind))
	if not _NAME.match(field.get("name") or ""):
		frappe.throw(_("Invalid WhatsApp Flow field name: {0}").format(field.get("name")))
	label = field.get("label") or field["name"]
	limit = LABEL_LIMITS.get(kind, 20)
	helper = field.get("helper") or ""
	if len(label) > limit and not helper:
		# keep the full question readable when Meta's label limit cuts it
		helper = label
	row = {
		"screen": screen_id,
		"field_name": field["name"],
		"field_type": kind,
		"label": _clip(label, limit),
		"required": 1 if field.get("required") else 0,
		"enabled": 1,
		"helper_text": _clip(helper, HELPER_LIMIT) if helper and kind in ("TextInput", "TextArea") else "",
	}
	if kind in ("Dropdown", "RadioButtonsGroup", "CheckboxGroup"):
		options = [
			{"id": str(o["id"])[:100], "title": _clip(str(o.get("title") or o["id"]), OPTION_TITLE_LIMIT)}
			for o in (field.get("options") or [])[:OPTION_LIMIT]
			if str(o.get("id") or "").strip()
		]
		if not options:
			frappe.throw(_("Field {0} has no options to choose from").format(label))
		row["options"] = json.dumps(options, ensure_ascii=False)
	return row


def _rows(spec: dict):
	screens, fields = [], []
	usable = [s for s in spec.get("screens") or [] if s.get("fields")]
	if not usable:
		frappe.throw(_("The form has no fields a WhatsApp Flow can collect"))
	for index, screen in enumerate(usable):
		screen_id = _screen_id(index)
		if len(screen["fields"]) > MAX_FIELDS_PER_SCREEN:
			frappe.throw(_("A WhatsApp Flow screen can hold at most {0} fields").format(MAX_FIELDS_PER_SCREEN))
		screens.append(
			{
				"screen_id": screen_id,
				"screen_title": _clip(screen.get("title") or spec.get("title") or "", SCREEN_TITLE_LIMIT)
				or screen_id,
				"terminal": 1 if index == len(usable) - 1 else 0,
				"layout_type": "SingleColumnLayout",
			}
		)
		fields.extend(_field_rows(screen_id, f) for f in screen["fields"])
	return screens, fields


def sync_flow(spec: dict, *, whatsapp_account: str, current: str | None = None, current_hash: str | None = None) -> dict:
	"""Create or update the local WhatsApp Flow for `spec`.

	spec = {"flow_name", "category", "cta", "screens": [{"title", "fields": [
	    {"name", "type", "label", "required", "helper", "options": [{"id", "title"}]}]}]}

	`current`/`current_hash` are the flow previously built for the same source and
	the spec_hash it was built from (the caller stores both). The flow is updated in
	place only while Meta does not know it yet.

	Returns {"flow", "spec_hash", "changed", "replaced"} (replaced: the previous
	flow a changed spec superseded, else None).
	"""
	screens, fields = _rows(spec)
	digest = spec_hash(spec)
	previous = frappe.get_doc("WhatsApp Flow", current) if current and frappe.db.exists("WhatsApp Flow", current) else None
	unchanged = previous and current_hash == digest and previous.whatsapp_account == whatsapp_account
	if unchanged:
		return {"flow": previous.name, "spec_hash": digest, "changed": False, "replaced": None}
	if previous and not previous.get("flow_id"):
		doc, replaced = previous, None
	else:
		doc = frappe.new_doc("WhatsApp Flow")
		doc.flow_name = _next_name(spec["flow_name"])
		replaced = previous.name if previous else None
	doc.whatsapp_account = whatsapp_account
	doc.category = spec.get("category") or "LEAD_GENERATION"
	if spec.get("cta"):
		doc.flow_cta = _clip(spec["cta"], 30)
	doc.set("screens", screens)
	doc.set("fields", fields)
	doc.save(ignore_permissions=True)
	return {"flow": doc.name, "spec_hash": digest, "changed": True, "replaced": replaced}


def _next_name(base: str) -> str:
	base = _clip(base, 120)
	if not frappe.db.exists("WhatsApp Flow", base):
		return base
	n = 2
	while frappe.db.exists("WhatsApp Flow", f"{base} v{n}"):
		n += 1
	return f"{base} v{n}"


def flow_state(name: str | None) -> dict | None:
	"""Local view of a flow for status screens: no Meta call."""
	if not name or not frappe.db.exists("WhatsApp Flow", name):
		return None
	row = frappe.db.get_value(
		"WhatsApp Flow", name, ["name", "status", "flow_id", "whatsapp_account", "modified", "preview_url"], as_dict=True
	)
	return {
		"name": row.name,
		"status": row.status or "Draft",
		"flow_id": row.flow_id or None,
		"on_meta": bool(row.flow_id),
		"whatsapp_account": row.whatsapp_account,
		"synced_at": str(row.modified),
		"preview_url": row.preview_url or None,
	}
