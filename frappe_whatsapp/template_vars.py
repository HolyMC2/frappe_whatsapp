"""What goes into a template's {{1}}, {{2}}… for one record.

Every path that fills a WhatsApp template asks `resolve()`: the Cloud API send
(`WhatsApp Message.send_template`), the manual wa.me prefill (CRM's manual box,
Taller's post-status prompt), the CRM composer's preview and the review queue.
The API and manual modes therefore cannot disagree about a value.

The mapping is `WhatsApp Templates.field_names`: comma-separated tokens in
placeholder order, token i fills {{i}} wherever {{i}} appears in the body (so a
body that shows {{3}} before {{2}} still gets the right values). A token is

- a context key (``customer_first_name``, ``repair_order``, ``order_total``…)
  that an app resolves for its own doctypes through the
  ``whatsapp_template_context`` hook, or
- a fieldname of the record, or a one-level link path (``client.first_name``),
  which is how tenants' own mappings were written before the keys existed.

``key:number`` asks for an amount without its currency symbol, for bodies that
already print it («${{4}} MXN»).

Hook shapes (any app):

    whatsapp_template_context = {"Repair Order": ["myapp.wa_vars.REPAIR_ORDER"]}
    whatsapp_template_defaults = ["myapp.wa_vars.DEFAULT_MAPPINGS"]

A context entry is a dict ``{"keys": {key: label}, "resolve": fn(doc, keys) ->
{key: value}}``; values may be str, int, date/datetime or `Money`. Providers for
one doctype run in hook order and the first non-empty value wins. A defaults
entry maps a template name (Meta ``actual_name``) to its token string; it fills
`field_names` only where the tenant left it empty, and stands in for an empty
`field_names` at resolve time.

A value that resolves empty is never sent, and neither is a Meta sample value:
the result names what is missing and every sender blocks on it.
"""

from __future__ import annotations

import json
import re
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Optional

import frappe
from frappe import _

_VAR_RE = re.compile(r"\{\{\s*(\d+)\s*\}\}")
_TEMPLATES = "WhatsApp Templates"
# Field types a fieldname token may read. Passwords, tables and code never leave.
_READABLE_FIELDTYPES = {
	"Data", "Select", "Link", "Dynamic Link", "Small Text", "Text", "Long Text", "Read Only",
	"Phone", "Int", "Float", "Currency", "Percent", "Date", "Datetime", "Time", "Autocomplete",
}


@dataclass(frozen=True)
class Money:
	amount: float
	currency: Optional[str] = None


@dataclass
class Resolution:
	template: str
	body: str
	footer: str
	tokens: list[str]
	values: dict[str, str] = field(default_factory=dict)
	missing: list[dict] = field(default_factory=list)
	# {"1": "Customer first name", …}: what each slot holds, for editors.
	labels: dict[str, str] = field(default_factory=dict)

	@property
	def ok(self) -> bool:
		return not self.missing

	@property
	def count(self) -> int:
		return len(placeholders(self.body))

	@property
	def text(self) -> str:
		"""The message as the customer reads it; unresolved slots stay as {{n}}."""
		body = render(self.body, self.values)
		return "\n\n".join(p for p in (body, self.footer) if p)

	def body_param(self) -> Optional[str]:
		"""{"1": v1, "2": v2, …} in numeric order, or None for a template without
		variables. Only call when `ok`."""
		if not self.values:
			return None
		return json.dumps({k: self.values[k] for k in numeric_keys(self.values)}, ensure_ascii=False)

	def reason(self) -> str:
		labels = ", ".join(m["label"] for m in self.missing)
		return _("WhatsApp template {0} cannot be filled: missing {1}.").format(self.template, labels)

	def as_dict(self) -> dict:
		return {
			"template": self.template, "body": self.body, "footer": self.footer,
			"tokens": self.tokens, "values": self.values, "missing": self.missing,
			"labels": self.labels, "ok": self.ok, "text": self.text,
		}


# --- body helpers -----------------------------------------------------------

def placeholders(body: Optional[str]) -> list[int]:
	return sorted({int(n) for n in _VAR_RE.findall(body or "")})


def render(body: Optional[str], values: dict) -> str:
	"""Replace each {{n}} by values[str(n)] wherever it appears; unknown or empty
	slots keep their placeholder so a gap is visible, never silently blank."""

	def sub(match):
		value = values.get(match.group(1))
		return str(value) if value not in (None, "") else match.group(0)

	return _VAR_RE.sub(sub, body or "")


def numeric_keys(values: dict) -> list[str]:
	return sorted(values, key=lambda k: int(k) if str(k).isdigit() else 0)


def parse_tokens(raw: Optional[str]) -> list[str]:
	return [t.strip() for t in (raw or "").split(",") if t.strip()]


# --- registry (hooks) -------------------------------------------------------

def providers(doctype: str) -> list[dict]:
	out = []
	for path in frappe.get_hooks("whatsapp_template_context", {}).get(doctype, []) or []:
		try:
			out.append(frappe.get_attr(path))
		except Exception:
			frappe.log_error(title=f"whatsapp_template_context: cannot load {path}", message=frappe.get_traceback())
	return out


def context_keys(doctype: str) -> dict[str, str]:
	"""{key: translated label} every provider offers for `doctype`."""
	keys: dict[str, str] = {}
	for provider in providers(doctype):
		for key, label in (provider.get("keys") or {}).items():
			keys.setdefault(key, _(label))
	return keys


def default_mappings() -> dict[str, str]:
	out: dict[str, str] = {}
	for path in frappe.get_hooks("whatsapp_template_defaults") or []:
		try:
			out.update(frappe.get_attr(path) or {})
		except Exception:
			frappe.log_error(title=f"whatsapp_template_defaults: cannot load {path}", message=frappe.get_traceback())
	return out


def default_for(row) -> Optional[str]:
	"""The shipped mapping for this template, when its body has exactly as many
	variables as the mapping has tokens (a tenant's same-named template with a
	different body gets nothing rather than a wrong guess)."""
	defaults = default_mappings()
	for name in (row.get("actual_name"), row.get("template_name")):
		tokens = defaults.get((name or "").strip())
		if tokens and len(parse_tokens(tokens)) == len(placeholders(row.get("template"))):
			return tokens
	return None


# --- resolution -------------------------------------------------------------

def _template_row(template: str) -> dict:
	row = frappe.db.get_value(
		_TEMPLATES, template,
		["name", "template_name", "actual_name", "template", "footer", "field_names", "language_code"], as_dict=True,
	)
	if not row:
		frappe.throw(_("WhatsApp template {0} not found.").format(template), frappe.DoesNotExistError)
	return row


def tokens_for(row) -> list[str]:
	return parse_tokens(row.get("field_names") or default_for(row))


def resolve(
	template: str, doc=None, *, context: Optional[dict] = None, fallback_tokens: Optional[str] = None
) -> Resolution:
	"""Ordered values for `template` from `doc` (any document, or None) plus
	`context` (values the sender already computed for this event; they win over
	the providers). `fallback_tokens` is a sender's own convention for a template
	that has neither a mapping nor a shipped default. Never raises for a missing
	value; check `.ok`."""
	row = _template_row(template)
	body = row.get("template") or ""
	slots = placeholders(body)
	tokens = tokens_for(row) or parse_tokens(fallback_tokens)
	result = Resolution(template=row.name, body=body, footer=row.get("footer") or "", tokens=tokens)
	if not slots:
		return result
	# Values are words inside the template's text: they follow the template's
	# language, not the language of whoever (or whichever job) triggered the send.
	owned = context_keys(doc.doctype) if doc is not None else {}
	with _in_language(row.get("language_code")):
		wanted = {_split(t)[0] for t in tokens}
		resolved = _context_values(doc, wanted, context or {})
		for index in slots:
			token = tokens[index - 1] if index - 1 < len(tokens) else ""
			value = _value_for(doc, token, resolved, owned=owned) if token else ""
			if value:
				result.values[str(index)] = value
	# Labels are read by the worker, so they stay in the worker's language.
	for index in slots:
		token = tokens[index - 1] if index - 1 < len(tokens) else ""
		label = _label(doc, token, index, owned)
		result.labels[str(index)] = label
		if str(index) not in result.values:
			result.missing.append({"index": index, "token": token, "label": label})
	return result


@contextmanager
def _in_language(language_code: Optional[str]):
	"""es_MX → es-MX for `_()`, babel dates and number words; no-op when unset."""
	lang = (language_code or "").replace("_", "-").strip()
	previous = getattr(frappe.local, "lang", None)
	if lang:
		frappe.local.lang = lang
	try:
		yield
	finally:
		frappe.local.lang = previous


def value_of(doc, token: str, *, context: Optional[dict] = None) -> str:
	"""One token's value for `doc` (an editor re-mapping a single slot)."""
	key, _fmt = _split(token)
	owned = context_keys(doc.doctype) if doc is not None else {}
	return _value_for(doc, token, _context_values(doc, {key}, context or {}), owned=owned)


def label_of(doc, token: str) -> str:
	return _label(doc, token, 0, context_keys(doc.doctype) if doc is not None else {})


def resolve_for(template: str, doctype: Optional[str], name: Optional[str], *, context: Optional[dict] = None) -> Resolution:
	doc = frappe.get_doc(doctype, name) if doctype and name else None
	return resolve(template, doc, context=context)


def check_values(template: str, values: dict) -> Resolution:
	"""Validate explicit values (a sender's or a reviewer's body_param) against the
	template: every {{n}} in the body needs a non-empty value."""
	row = _template_row(template)
	body = row.get("template") or ""
	result = Resolution(template=row.name, body=body, footer=row.get("footer") or "", tokens=tokens_for(row))
	clean = {str(k): ("" if v is None else str(v).strip()) for k, v in (values or {}).items()}
	for index in placeholders(body):
		value = clean.get(str(index), "")
		if value:
			result.values[str(index)] = value
		else:
			token = result.tokens[index - 1] if index - 1 < len(result.tokens) else ""
			result.missing.append({"index": index, "token": token, "label": _label(None, token, index, {})})
	return result


def _split(token: str) -> tuple[str, str]:
	key, _sep, fmt = token.partition(":")
	return key.strip(), fmt.strip()


def _context_values(doc, wanted: set, context: dict) -> dict:
	values = {k: v for k, v in context.items() if not _empty(v)}
	if doc is None:
		return values
	for provider in providers(doc.doctype):
		keys = {k for k in wanted if k in (provider.get("keys") or {}) and k not in values}
		if not keys:
			continue
		try:
			got = provider["resolve"](doc, keys) or {}
		except Exception:
			frappe.log_error(title=f"whatsapp template context failed for {doc.doctype}", message=frappe.get_traceback())
			continue
		for k in keys:
			if not _empty(got.get(k)):
				values[k] = got[k]
	return values


def _value_for(doc, token: str, resolved: dict, owned=()) -> str:
	"""`owned`: keys a provider answers for this doctype. When the provider has no
	value, a same-named record field must not stand in (a zero quote would print
	as «$0.00»)."""
	key, fmt = _split(token)
	if key in resolved:
		return format_value(resolved[key], fmt)
	if doc is None or key in owned:
		return ""
	return _field_value(doc, key)


def _field_value(doc, path: str) -> str:
	"""A fieldname or one-level link path on `doc`, formatted as the form shows it."""
	link_field, _sep, sub = path.partition(".")
	df = doc.meta.get_field(link_field)
	if not df or df.get("hidden") or df.fieldtype not in _READABLE_FIELDTYPES:
		return ""
	if not sub:
		try:
			return str(doc.get_formatted(link_field) or "").strip()
		except Exception:
			return str(doc.get(link_field) or "").strip()
	if df.fieldtype != "Link" or not df.options or not doc.get(link_field):
		return ""
	sdf = frappe.get_meta(df.options).get_field(sub)
	if not sdf or sdf.get("hidden") or sdf.fieldtype not in _READABLE_FIELDTYPES:
		return ""
	return str(frappe.db.get_value(df.options, doc.get(link_field), sub) or "").strip()


def _empty(value) -> bool:
	return value is None or (isinstance(value, str) and not value.strip())


def format_value(value: Any, fmt: str = "") -> str:
	"""Site-formatted text for a context value: Money with the record's currency
	(or bare with ``:number``), dates as the site's long date, the rest as text."""
	from frappe.utils import flt, fmt_money

	if isinstance(value, Money):
		if fmt == "number":
			return fmt_money(flt(value.amount))
		return fmt_money(flt(value.amount), currency=value.currency or _default_currency())
	if isinstance(value, datetime):
		value = value.date()
	if isinstance(value, date):
		return long_date(value)
	return str(value).strip()


def _default_currency() -> Optional[str]:
	company = frappe.defaults.get_global_default("company")
	return (company and frappe.get_cached_value("Company", company, "default_currency")) or frappe.defaults.get_global_default("currency")


def long_date(value) -> str:
	"""«15 de julio de 2026» on a Spanish site, «July 15, 2026» on an English one."""
	from frappe.utils import formatdate, getdate

	lang = (frappe.local.lang if getattr(frappe.local, "lang", None) else None) or frappe.db.get_single_value("System Settings", "language") or "en"
	try:
		from babel.dates import format_date

		return format_date(getdate(value), format="long", locale=lang.replace("-", "_"))
	except Exception:
		return formatdate(value)


def _label(doc, token: str, index: int, labels: dict) -> str:
	if not token:
		return _("{0} (the template has no variable mapping)").format("{{%d}}" % index)
	key, _fmt = _split(token)
	if key in labels:
		return labels[key]
	if doc is not None:
		df = doc.meta.get_field(key.partition(".")[0])
		if df:
			return _(df.label or key)
	return key


# --- seeding ----------------------------------------------------------------

def apply_default_mapping(doc) -> bool:
	"""Fill an empty `field_names` from the shipped defaults. Never overwrites."""
	if (doc.get("field_names") or "").strip():
		return False
	tokens = default_for(doc)
	if not tokens:
		return False
	doc.field_names = tokens
	return True


def seed_default_mappings(apply: int = 0, verbose: int = 1) -> list[dict]:
	"""Fill `field_names` on existing templates whose mapping is empty and whose
	name has a shipped default. Dry run unless `apply`; prints before/after per
	template. Rows with a tenant mapping are reported and left untouched.

	    bench --site <site> execute frappe_whatsapp.template_vars.seed_default_mappings --kwargs "{'apply': 0}"
	"""
	if not frappe.db.exists("DocType", _TEMPLATES):
		return []
	defaults = default_mappings()
	report = []
	for row in frappe.get_all(
		_TEMPLATES,
		fields=["name", "template_name", "actual_name", "template", "field_names", "status"],
		order_by="name asc",
	):
		name = (row.actual_name or row.template_name or "").strip()
		shipped = defaults.get(name)
		current = (row.field_names or "").strip()
		entry = {"template": row.name, "status": row.status, "before": current, "after": current, "action": "none"}
		if not shipped:
			entry["action"] = "no default"
		elif current:
			entry["action"] = "kept (tenant mapping)" if current != shipped else "already set"
		elif len(parse_tokens(shipped)) != len(placeholders(row.template)):
			entry["action"] = "skipped: body has {0} variables, default has {1}".format(
				len(placeholders(row.template)), len(parse_tokens(shipped)))
		else:
			entry["after"] = shipped
			entry["action"] = "fill" if not apply else "filled"
			if apply:
				frappe.db.set_value(_TEMPLATES, row.name, "field_names", shipped, update_modified=False)
		report.append(entry)
	if verbose:
		mode = "APPLY" if apply else "DRY RUN"
		print(f"[{mode}] WhatsApp template mappings on {frappe.local.site}")
		for e in report:
			print(f"  {e['template']:<32} {e['status'] or '':<17} {e['action']:<24} before={e['before']!r} after={e['after']!r}")
	if apply:
		frappe.db.commit()
	return report


def after_migrate():
	"""Converge on every migrate: templates synced before an app shipped its
	defaults get them once the code arrives. Only empty mappings are filled."""
	try:
		seed_default_mappings(apply=1, verbose=0)
	except Exception:
		frappe.log_error(title="frappe_whatsapp: seeding template mappings failed", message=frappe.get_traceback())
