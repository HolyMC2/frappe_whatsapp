# Copyright (c) 2022, Shridhar Patil and contributors
# For license information, please see license.txt

# DOCO FORK HUNK: retention. Upstream (frappe_whatsapp master @ 8441c38) ships
# this controller as a bare `pass` and no clear_old_logs anywhere in the app, so
# the table grows forever — one row per template send AND one per inbound
# webhook event. Kept to the smallest possible addition (imports + one
# staticmethod, nothing else touched) so a rebase onto upstream stays trivial.
# Registered in boat's hygiene.LOG_RETENTION at 90 days; these rows are
# delivery diagnostics (status + meta_data), while the conversation itself
# lives in WhatsApp Message.
import frappe
from frappe.model.document import Document
from frappe.utils import add_days, cint, today

DELETE_BATCH = 5000
DEFAULT_RETENTION_DAYS = 90

class WhatsAppNotificationLog(Document):
	@staticmethod
	def clear_old_logs(days: int | None = None) -> int:
		"""Frappe Log Settings interface (the LogType protocol in
		frappe/core/doctype/log_settings). Log Settings silently drops a
		logs_to_clear row whose controller lacks this method. Batched and
		idempotent."""
		# Unset -> the app default. 0 or negative DISABLES the purge, matching
		# the "0 = no purgar" convention the rest of the estate uses.
		days = DEFAULT_RETENTION_DAYS if days is None else cint(days)
		if days <= 0:
			return 0
		cutoff = add_days(today(), -days)
		deleted = 0
		while True:
			frappe.db.sql(
				"DELETE FROM `tabWhatsApp Notification Log` WHERE creation < %s LIMIT %s",
				(cutoff, DELETE_BATCH),
			)
			removed = frappe.db._cursor.rowcount or 0
			frappe.db.commit()
			deleted += removed
			if removed < DELETE_BATCH:
				break
		return deleted
