"""The customer-service window of ONE business number, from authenticated evidence.

Meta opens a 24-hour window per business phone number when the customer writes
to it. Only processed, signature-verified webhook receipts count: a WhatsApp
Message row, a backlogged receipt that was never processed, or a message the
customer wrote to another of our numbers never opens this one. CRM's native
outbox decides dispatch with `evidence(..., lock=True)`; previews ask
`is_open()` with plain reads, so both read the same predicate.

Peers are exact provider digits. The only alias accepted is Mexico's legacy
mobile spelling (52 + 10 digits and 521 + 10 digits name the same WhatsApp
user); any other number is matched exactly, never by its last ten digits.
Callers turn national input into international digits first (for example
`channel.wa_digits`, which uses the site's configured country).
"""

from __future__ import annotations

import re
import time

import frappe

WINDOW_SECONDS = 24 * 60 * 60
RECEIPT = "Meta Webhook Receipt"

# The receipt's own authenticated provider timestamp, never its insertion time.
_PREDICATE = """provider='WhatsApp' AND account_id=%s AND app_id=%s
	AND event_type='message' AND state='Processed'
	AND JSON_UNQUOTE(JSON_EXTRACT(payload,'$.change.value.messages[0].from'))=%s
	AND JSON_UNQUOTE(JSON_EXTRACT(payload,'$.change.value.messages[0].timestamp')) REGEXP '^[0-9]{1,12}$'
	AND CAST(JSON_UNQUOTE(JSON_EXTRACT(payload,'$.change.value.messages[0].timestamp')) AS UNSIGNED)>%s
	AND CAST(JSON_UNQUOTE(JSON_EXTRACT(payload,'$.change.value.messages[0].timestamp')) AS UNSIGNED)<=%s
"""
_STAMP = "CAST(JSON_UNQUOTE(JSON_EXTRACT(payload,'$.change.value.messages[0].timestamp')) AS UNSIGNED)"


def peer_candidates(number) -> list[str]:
	"""Exact provider spellings for international digits; [] when not a number."""
	digits = re.sub(r"\D", "", str(number or ""))
	if not 8 <= len(digits) <= 15:
		return []
	if len(digits) == 12 and digits.startswith("52"):
		return [digits, "521" + digits[2:]]
	if len(digits) == 13 and digits.startswith("521"):
		return ["52" + digits[3:], digits]
	return [digits]


def evidence(phone_id, app_id, peer, *, now=None, lock=False):
	"""The latest processed inbound from `peer` to this number inside the window.

	Returns {"receipt", "timestamp"} or None. Future, malformed, unprocessed and
	exactly-24-hour-old evidence never counts. With `lock`, discovery is a plain
	read and the one chosen primary-key row is revalidated with a current read,
	so the dispatcher never locks unrelated inbound receipts.
	"""
	if not (phone_id and app_id and isinstance(peer, str) and peer.isdigit()):
		return None
	if not frappe.db.exists("DocType", RECEIPT):
		return None
	now = int(time.time() if now is None else now)
	params = (str(phone_id), str(app_id), peer, now - WINDOW_SECONDS, now)
	rows = frappe.db.sql(
		f"SELECT name, {_STAMP} AS ts FROM `tabMeta Webhook Receipt` WHERE {_PREDICATE} ORDER BY ts DESC LIMIT 1",
		params,
		as_dict=True,
	)
	if not rows:
		return None
	if lock and not frappe.db.sql(
		f"SELECT name FROM `tabMeta Webhook Receipt` WHERE name=%s AND {_PREDICATE} LIMIT 1 FOR UPDATE",
		(rows[0].name, *params),
	):
		return None
	return frappe._dict(receipt=rows[0].name, timestamp=int(rows[0].ts))


def is_open(account, number, *, now=None, exact=False):
	"""Preview truth for one account and recipient. Never locks, never sends.

	`account` is a WhatsApp Account name or a dict with phone_id/app_id/status.
	`exact` checks only the given spelling (a governing conversation's peer).
	Returns {"open", "peer", "last_inbound", "closes_at", "reason"} where peer is
	the exact spelling that wrote (or the first candidate), and reason is one of
	None, "account_unavailable", "invalid_number" or "closed".
	"""
	result = frappe._dict(open=False, peer=None, last_inbound=None, closes_at=None, reason=None)
	if isinstance(account, str):
		account = frappe.db.get_value(
			"WhatsApp Account", account, ["name", "phone_id", "app_id", "status"], as_dict=True
		)
	if not account or account.get("status") != "Active" or not account.get("phone_id") or not account.get("app_id"):
		result.reason = "account_unavailable"
		return result
	candidates = peer_candidates(number)
	if exact:
		# The native dispatcher checks the conversation's own peer only; a preview
		# for that conversation promises exactly what dispatch will admit.
		digits = re.sub(r"\D", "", str(number or ""))
		candidates = [digits] if digits in candidates else []
	if not candidates:
		result.reason = "invalid_number"
		return result
	result.peer = candidates[0]
	best = None
	for peer in candidates:
		found = evidence(account.get("phone_id"), account.get("app_id"), peer, now=now)
		if found and (not best or found.timestamp > best[1].timestamp):
			best = (peer, found)
	if not best:
		result.reason = "closed"
		return result
	result.update(
		open=True,
		peer=best[0],
		last_inbound=best[1].timestamp,
		closes_at=best[1].timestamp + WINDOW_SECONDS,
	)
	return result
