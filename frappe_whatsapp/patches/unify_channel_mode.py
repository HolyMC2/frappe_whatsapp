"""Carry each site's existing WhatsApp channel choice into WhatsApp Settings.

Before 2026-09-29 two apps kept their own switch: doco_marketing's
Marketing Settings.channel_tier (auto / deeplink / shop_session / waba) and
Taller App Settings.whatsapp_mode (Auto / Manual / Off). Both now read
frappe_whatsapp.channel, so the stricter stored choice wins:
Off > Manual > Auto. A pinned "waba" maps to Auto, which already falls back to
manual when the account is not Active (the ladder did the same).
"""

import frappe

_RANK = {"Auto": 0, "Manual": 1, "Off": 2}
_TIER_TO_MODE = {"deeplink": "Manual", "shop_session": "Manual", "waba": "Auto", "auto": "Auto"}


def _single(doctype: str, fieldname: str):
	if not frappe.db.exists("DocType", doctype):
		return None
	# Raw read: the field may be gone from the DocType (hidden or removed)
	# while its stored value is still in tabSingles.
	rows = frappe.db.sql(
		"SELECT value FROM `tabSingles` WHERE doctype=%s AND field=%s", (doctype, fieldname)
	)
	return rows[0][0] if rows else None


def execute():
	# Runs once per site, before anyone can have chosen the new field; a sync
	# that wrote the "Auto" default must not hide the older pinned choice.
	candidates = [
		_TIER_TO_MODE.get((_single("Marketing Settings", "channel_tier") or "").strip()),
		(_single("Taller App Settings", "whatsapp_mode") or "").strip() or None,
	]
	candidates = [c for c in candidates if c in _RANK]
	mode = max(candidates, key=lambda c: _RANK[c]) if candidates else "Auto"

	number = (
		_single("Taller App Settings", "whatsapp_business_number")
		or _single("Marketing Settings", "channel_shop_phone")
		or ""
	).strip()

	frappe.db.set_single_value("WhatsApp Settings", "channel_mode", mode)
	if number:
		frappe.db.set_single_value("WhatsApp Settings", "shop_whatsapp_number", number[:30])
