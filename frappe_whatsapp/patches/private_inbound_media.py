"""Move existing public WhatsApp media to private files (ARC-S2.1, decision D5).

Inbound customer media used to be saved as public Files (webhook.py), reachable
by anyone holding the /files/ URL. New media is private since ARC-S2.1. This
module moves the files saved before that.

It is NOT registered in patches.txt: Marco decides D5 first. Until then only
the read-only report runs:

    bench --site <site> execute frappe_whatsapp.patches.private_inbound_media.report

`apply(confirm="D5")` performs the move; `execute()` is the patch entry once
the patch is registered after D5.

Per public File attached to a WhatsApp Message:

- "move": the bytes are used only by this File row; they move from
  public/files to private/files and every referrer of the old URL is rewritten.
- "copy": Frappe dedupes public uploads by content hash, so another File row
  (a product photo, a comment image) uses the same bytes. A private copy is
  written for this row and only this message's references are rewritten; the
  other rows and their referrers keep the public file.
- "missing": the bytes are not on disk; nothing is changed and the row is
  reported.

Referrers are found by scanning attach and long-text columns of every table
for the file name. Log tables (Version, Error Log, …) are reported, never
rewritten.
"""

import os
import re

import frappe

ATTACHED_TO = "WhatsApp Message"
CONFIRM = "D5"
SCAN_TYPES = (
	"Attach",
	"Attach Image",
	"Small Text",
	"Text",
	"Long Text",
	"Text Editor",
	"HTML Editor",
	"Markdown Editor",
	"Code",
	"JSON",
)
LOG_DOCTYPES = frozenset(
	{
		"Version",
		"Error Log",
		"Activity Log",
		"Access Log",
		"Deleted Document",
		"Scheduled Job Log",
		"Webhook Request Log",
		"Integration Request",
		"Email Queue",
		"Route History",
	}
)
_BATCH = 100


def _public_url(fname: str) -> re.Pattern:
	# "/files/x" but not "/private/files/x"; absolute URLs keep their host.
	return re.compile(r"(?<!/private)/files/" + re.escape(fname) + r"(?![\w.-])")


def _fname(url: str) -> str:
	return url.rsplit("/", 1)[-1]


def _candidates() -> list[dict]:
	return [
		row
		for row in frappe.get_all(
			"File",
			filters={"attached_to_doctype": ATTACHED_TO, "is_private": 0, "is_folder": 0},
			fields=[
				"name",
				"file_url",
				"file_name",
				"file_size",
				"attached_to_name",
				"attached_to_field",
				"creation",
			],
			order_by="creation asc",
		)
		if (row.file_url or "").startswith("/files/")
	]


def _scan_columns() -> list[tuple[str, str]]:
	fields = frappe.db.sql(
		"""
		SELECT df.parent, df.fieldname FROM `tabDocField` df
		JOIN `tabDocType` dt ON dt.name = df.parent
		WHERE df.fieldtype IN %(types)s AND dt.issingle = 0 AND IFNULL(dt.is_virtual, 0) = 0
		UNION
		SELECT cf.dt, cf.fieldname FROM `tabCustom Field` cf
		JOIN `tabDocType` dt ON dt.name = cf.dt
		WHERE cf.fieldtype IN %(types)s AND dt.issingle = 0 AND IFNULL(dt.is_virtual, 0) = 0
		""",
		{"types": SCAN_TYPES},
	)
	columns = []
	for doctype, fieldname in fields:
		if frappe.db.table_exists(doctype) and frappe.db.has_column(doctype, fieldname):
			columns.append((doctype, fieldname))
	# Comment content is a core column without a DocField of a scanned type on some versions.
	for extra in (("Comment", "content"), ("Communication", "content")):
		if extra not in columns and frappe.db.has_column(*extra):
			columns.append(extra)
	return sorted(set(columns))


def _referrers(files: list[dict]) -> dict[str, list[dict]]:
	"""Map file name → rows that mention its public URL (outside File itself)."""
	found: dict[str, list[dict]] = {_fname(f["file_url"]): [] for f in files}
	names = list(found)
	patterns = {n: _public_url(n) for n in names}
	for doctype, column in _scan_columns():
		if doctype == "File":
			continue
		for start in range(0, len(names), _BATCH):
			batch = names[start : start + _BATCH]
			regexp = "files/(" + "|".join(re.escape(n) for n in batch) + ")"
			rows = frappe.db.sql(
				f"SELECT name, `{column}` FROM `tab{doctype}` "
				f"WHERE `{column}` LIKE %s AND `{column}` REGEXP %s",
				("%files/%", regexp),
			)
			for name, value in rows:
				for fname in batch:
					if patterns[fname].search(value or ""):
						found[fname].append(
							{
								"doctype": doctype,
								"name": name,
								"field": column,
								"log": doctype in LOG_DOCTYPES,
							}
						)
	return found


def _plan() -> dict:
	files = _candidates()
	refs = _referrers(files) if files else {}
	items = []
	for f in files:
		fname = _fname(f.file_url)
		shared = frappe.get_all(
			"File",
			filters={"file_url": f.file_url, "name": ("!=", f.name)},
			fields=["name", "attached_to_doctype", "attached_to_name"],
		)
		on_disk = os.path.exists(frappe.get_site_path("public", "files", fname))
		if not on_disk:
			action = "missing"
		elif shared:
			action = "copy"
		else:
			action = "move"
		items.append(
			{
				"file": f.name,
				"url": f.file_url,
				"message": f.attached_to_name,
				"field": f.attached_to_field or "attach",
				"size": f.file_size or 0,
				"created": str(f.creation),
				"action": action,
				"shared_with": [dict(s) for s in shared],
				"referrers": refs.get(fname, []),
			}
		)
	# Messages that point at a public URL but own no File (outbound sends of a
	# file that belongs to something else, such as a product photo): reported only.
	orphan_refs = frappe.db.sql(
		"""
		SELECT m.name, m.type, m.attach FROM `tabWhatsApp Message` m
		WHERE m.attach LIKE '/files/%%' AND NOT EXISTS (
			SELECT 1 FROM `tabFile` f WHERE f.attached_to_doctype = 'WhatsApp Message'
			AND f.attached_to_name = m.name AND f.file_url = m.attach)
		""",
		as_dict=True,
	)
	return {"files": items, "messages_pointing_at_other_public_files": orphan_refs}


def report() -> dict:
	"""Read-only dry run: what apply() would do on this site. Changes nothing."""
	plan = _plan()
	items = plan["files"]
	counts: dict[str, int] = {}
	for item in items:
		counts[item["action"]] = counts.get(item["action"], 0) + 1
	referrer_rows = [r for item in items for r in item["referrers"]]
	by_doctype: dict[str, int] = {}
	for r in referrer_rows:
		key = r["doctype"] + "." + r["field"] + (" (log, kept)" if r["log"] else "")
		by_doctype[key] = by_doctype.get(key, 0) + 1
	others = plan["messages_pointing_at_other_public_files"]
	return {
		"site": frappe.local.site,
		"dry_run": True,
		"public_whatsapp_files": len(items),
		"bytes": sum(i["size"] for i in items),
		"actions": counts,
		"referrers_by_field": by_doctype,
		"shared_files": [
			{"file": i["file"], "shared_with": i["shared_with"]} for i in items if i["shared_with"]
		],
		"missing_on_disk": [i["file"] for i in items if i["action"] == "missing"],
		"messages_pointing_at_other_public_files": {
			"count": len(others),
			"by_type": _count(others, "type"),
		},
		"files": items,
	}


def _count(rows, key):
	out: dict[str, int] = {}
	for row in rows:
		out[row[key] or ""] = out.get(row[key] or "", 0) + 1
	return out


def _private_target(fname: str) -> str:
	path = frappe.get_site_path("private", "files", fname)
	if not os.path.exists(path):
		return fname
	stem, ext = os.path.splitext(fname)
	return f"{stem}-{frappe.generate_hash(length=6)}{ext}"


def _privatize(item: dict) -> str:
	fname = _fname(item["url"])
	source = frappe.get_site_path("public", "files", fname)
	target_name = _private_target(fname)
	target = frappe.get_site_path("private", "files", target_name)
	if item["action"] == "move":
		os.rename(source, target)
		frappe.db.after_rollback.add(lambda: os.path.exists(target) and os.rename(target, source))
	else:
		with open(source, "rb") as fh:
			content = fh.read()
		with open(target, "xb") as fh:
			fh.write(content)
		frappe.db.after_rollback.add(lambda: os.path.exists(target) and os.remove(target))
	new_url = "/private/files/" + target_name
	frappe.db.set_value(
		"File", item["file"], {"file_url": new_url, "is_private": 1}, update_modified=False
	)
	return new_url


def _rewrite(item: dict, new_url: str) -> int:
	pattern = _public_url(_fname(item["url"]))
	rewritten = 0
	targets = [{"doctype": ATTACHED_TO, "name": item["message"], "field": item["field"], "log": False}]
	if item["action"] == "move":
		# The bytes left /files/: every live referrer must follow them.
		targets += item["referrers"]
	for ref in targets:
		if ref["log"]:
			continue
		value = frappe.db.get_value(ref["doctype"], ref["name"], ref["field"])
		if not value:
			continue
		updated = pattern.sub(new_url, value)
		if updated != value:
			frappe.db.set_value(ref["doctype"], ref["name"], ref["field"], updated, update_modified=False)
			rewritten += 1
	return rewritten


def apply(confirm: str | None = None, limit: int | None = None) -> dict:
	"""Move public WhatsApp media to private. Requires confirm="D5" (Marco's decision)."""
	if confirm != CONFIRM:
		frappe.throw(
			"La migración de archivos de WhatsApp a privados espera la decisión D5. "
			"Revisa primero el informe: frappe_whatsapp.patches.private_inbound_media.report",
			title="Falta la decisión D5",
		)
	plan = _plan()
	done = {"move": 0, "copy": 0, "missing": 0, "referrers_rewritten": 0}
	for item in plan["files"][: limit or None]:
		if item["action"] == "missing":
			done["missing"] += 1
			continue
		new_url = _privatize(item)
		done["referrers_rewritten"] += _rewrite(item, new_url)
		done[item["action"]] += 1
		frappe.db.commit()  # disk and database move together, one file at a time
	return {"site": frappe.local.site, **done}


def execute():
	"""Patch entry. Register in patches.txt only after Marco approves D5."""
	apply(confirm=CONFIRM)
