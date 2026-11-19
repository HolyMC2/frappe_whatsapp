"""The site's one WhatsApp channel: connected API, manual wa.me, or off.

Every app that talks to customers on WhatsApp (Taller, CRM, marketing's
composer) asks `resolve_mode()` instead of keeping its own switch, so a worker
never sees an API send in one screen and a wa.me link for the same customer in
another.

- ``api``: the default outgoing WhatsApp Account (WhatsApp Settings, see
  ``utils.outgoing_default``) is Active; the server sends.
- ``manual``: the worker opens WhatsApp on their own device with the message
  prefilled (https://wa.me/<digits>?text=...). Nothing is sent by the server
  and nothing is confirmed as delivered.
- ``off``: no WhatsApp actions.

The admin setting is WhatsApp Settings.channel_mode (Auto / Manual / Off,
unset = Auto). With Manual and a `shop_whatsapp_number`, screens tell the
worker to send from the shop's number instead of «your own WhatsApp».
"""

from __future__ import annotations

import re
from typing import Optional
from urllib.parse import quote

import frappe

MODES = ("api", "manual", "off")
_SETTINGS = "WhatsApp Settings"


def _setting(fieldname: str):
	try:
		return frappe.db.get_single_value(_SETTINGS, fieldname)
	except Exception:
		return None


def api_ready() -> bool:
	"""A real, Active default outgoing account — not merely the app installed.

	Same resolution as every send (`utils.outgoing_default`): the Settings choice,
	else one unambiguous flagged account."""
	account = _setting("default_outgoing_account")
	if not account:
		from frappe_whatsapp.utils import outgoing_default

		account = outgoing_default()
	if not account:
		return False
	return frappe.db.get_value("WhatsApp Account", account, "status") == "Active"


def resolve_mode() -> str:
	choice = (_setting("channel_mode") or "Auto").strip()
	if choice == "Off":
		return "off"
	if choice == "Manual":
		return "manual"
	return "api" if api_ready() else "manual"


def shop_number() -> str:
	return (_setting("shop_whatsapp_number") or "").strip()


_DISPLAY = re.compile(r"\+?[0-9][0-9 ()-]{5,38}")


def display_number(metadata) -> Optional[str]:
	"""The business number Meta shows customers (`metadata.display_phone_number`), or None."""
	value = (metadata or {}).get("display_phone_number") if isinstance(metadata, dict) else None
	if not isinstance(value, str):
		return None
	value = value.strip()
	return value if _DISPLAY.fullmatch(value) else None


def remember_display_number(account, metadata) -> None:
	"""Keep the account's customer-facing number as Meta's messages state it (a write only when it changes)."""
	number = display_number(metadata)
	if not number or not account or (account.get("display_phone_number") or "") == number:
		return
	if not frappe.get_meta("WhatsApp Account").has_field("display_phone_number"):
		return
	frappe.db.set_value("WhatsApp Account", account.name, "display_phone_number", number, update_modified=False)
	account.display_phone_number = number


def business_number() -> str:
	"""The number customers write to: the one the shop typed, else the connected account's."""
	typed = shop_number()
	if typed:
		return typed
	account = _setting("default_outgoing_account")
	if not account:
		from frappe_whatsapp.utils import outgoing_default

		account = outgoing_default()
	if not account or not frappe.get_meta("WhatsApp Account").has_field("display_phone_number"):
		return ""
	return (frappe.db.get_value("WhatsApp Account", account, "display_phone_number") or "").strip()


def printable(number: str, region: Optional[str] = None) -> str:
	"""A number as a ticket prints it: international format when it parses, else as written."""
	digits = wa_digits(number, region)
	if not digits:
		return number.strip()
	import phonenumbers

	return phonenumbers.format_number(phonenumbers.parse("+" + digits, None), phonenumbers.PhoneNumberFormat.INTERNATIONAL)


def ticket_number() -> str:
	"""The shop's WhatsApp for sale and repair tickets; empty when the shop turned it off or has none."""
	if _setting("print_on_tickets") in (0, "0"):
		return ""
	number = business_number()
	return printable(number) if number else ""


def site_region() -> Optional[str]:
	country = frappe.db.get_single_value("System Settings", "country")
	code = country and frappe.db.get_value("Country", country, "code")
	return code.upper() if code else None


def wa_digits(raw: Optional[str], region: Optional[str] = None) -> Optional[str]:
	"""International digits for wa.me (no +), or None when not diallable.

	The country code comes from `region` (default: the site's country). A
	legacy mobile prefix that WhatsApp no longer routes (e.g. 52 1 …) is folded
	when the number is only valid without it."""
	if not raw:
		return None
	digits = re.sub(r"[^0-9]", "", str(raw))
	if len(digits) < 7:
		return None
	import phonenumbers

	if region is None:
		region = site_region()
	text = str(raw).strip()
	try:
		number = phonenumbers.parse(text if text.startswith("+") else digits, region)
	except phonenumbers.NumberParseException:
		try:
			number = phonenumbers.parse("+" + digits, None)
		except phonenumbers.NumberParseException:
			return None
	if not phonenumbers.is_valid_number(number):
		national = str(number.national_number)
		if national.startswith("1"):
			folded = phonenumbers.parse(f"+{number.country_code}{national[1:]}", None)
			if phonenumbers.is_valid_number(folded):
				number = folded
	if not phonenumbers.is_valid_number(number):
		return None
	return phonenumbers.format_number(number, phonenumbers.PhoneNumberFormat.E164).lstrip("+")


def wa_link(digits: str, text: Optional[str] = None) -> str:
	url = f"https://wa.me/{digits}"
	return f"{url}?text={quote(text, safe='')}" if text else url


def sender_hint() -> Optional[str]:
	"""What the worker is told about the sending number in manual mode."""
	number = shop_number()
	return f"Envía desde el WhatsApp del negocio ({number})." if number else None


@frappe.whitelist(methods=["GET"])
def get_channel() -> dict:
	"""For any signed-in screen deciding which WhatsApp action to render."""
	if frappe.session.user == "Guest":
		frappe.throw(frappe._("Inicia sesión."), frappe.PermissionError)
	mode = resolve_mode()
	return {"mode": mode, "shop_number": shop_number(), "sender_hint": sender_hint() if mode == "manual" else None}


@frappe.whitelist(methods=["GET"])
def get_channel_settings() -> dict:
	"""Admin view: the stored choice and what it resolves to right now."""
	frappe.only_for("System Manager")
	return settings_payload()


def settings_payload() -> dict:
	doc = frappe.get_doc(_SETTINGS)
	return {"base_modified": str(doc.modified), "channel_mode": doc.get("channel_mode") or "Auto",
		"shop_whatsapp_number": doc.get("shop_whatsapp_number") or "",
		"print_on_tickets": 0 if doc.get("print_on_tickets") in (0, "0") else 1,
		"ticket_number": ticket_number() if doc.get("print_on_tickets") not in (0, "0") else "",
		"account_number": business_number() if not shop_number() else "",
		"resolved": resolve_mode(), "api_ready": api_ready()}


def save_channel_settings(channel_mode: str, shop_whatsapp_number: str = "", base_modified: Optional[str] = None,
		print_on_tickets=None) -> dict:
	"""Validated write; callers own the permission check (Taller lets its
	managers change the channel from the Taller SPA)."""
	if channel_mode not in ("Auto", "Manual", "Off"):
		frappe.throw(frappe._("Elige Automático, Manual o Desactivado."))
	number = (shop_whatsapp_number or "").strip()
	if len(number) > 30:
		frappe.throw(frappe._("El número de WhatsApp del negocio es demasiado largo."))
	doc = frappe.get_doc(_SETTINGS)
	if base_modified and str(doc.modified) != str(base_modified):
		frappe.throw(frappe._("La configuración cambió. Recarga antes de guardar."), frappe.TimestampMismatchError)
	doc.channel_mode = channel_mode
	doc.shop_whatsapp_number = number
	if print_on_tickets is not None and doc.meta.has_field("print_on_tickets"):
		doc.print_on_tickets = 1 if str(print_on_tickets).lower() in ("1", "true") else 0
	doc.save(ignore_permissions=True)
	return settings_payload()
