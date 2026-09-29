"""The site's one WhatsApp channel: connected API, manual wa.me, or off.

Every app that talks to customers on WhatsApp (Taller, CRM, marketing's
composer) asks `resolve_mode()` instead of keeping its own switch, so a worker
never sees an API send in one screen and a wa.me link for the same customer in
another.

- ``api``: the default outgoing WhatsApp Account is Active; the server sends.
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
	"""A real, Active default outgoing account — not merely the app installed."""
	account = _setting("default_outgoing_account")
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
		"resolved": resolve_mode(), "api_ready": api_ready()}


def save_channel_settings(channel_mode: str, shop_whatsapp_number: str = "", base_modified: Optional[str] = None) -> dict:
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
	doc.save(ignore_permissions=True)
	return settings_payload()
