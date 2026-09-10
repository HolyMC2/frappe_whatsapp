"""Two-process first-open/take versus legacy physical-send proof.

Dedicated paused lab only. Real DB transactions and core/transport; the physical
request function is a deterministic double. Fictional rows are closed/disabled.
"""

from contextlib import ExitStack
import json
import os
from pathlib import Path
import selectors
import subprocess
import sys
import traceback
from unittest.mock import patch
from uuid import uuid4

sys.path.insert(0, "/tmp/meta-wa-20260910")
os.chdir("/home/frappe/frappe-bench/sites")
import frappe
from crm.api import conversations as control
from frappe_whatsapp import transport
from frappe_whatsapp.legacy_outbox import LegacySendBlocked
from frappe_whatsapp.frappe_whatsapp.tests.test_native_outbox import Response

SITE = "meta-reliability-test-20260910.lab.xoloitzcuintles.com"


def emit(event, **values):
    print(json.dumps({"event": event, **values}), flush=True)


def initialize():
    frappe.init(site=SITE)
    frappe.connect()
    frappe.local.conf = frappe._dict(frappe.conf)
    frappe.conf.developer_mode = 1
    assert frappe.conf.maintenance_mode and frappe.conf.pause_scheduler
    assert {"crm", "frappe_whatsapp", "doco", "doco_marketing"}.issubset(frappe.get_installed_apps())
    frappe.set_user("Administrator")


def blocked():
    stack = ExitStack()
    for target in ("requests.sessions.Session.request", "smtplib.SMTP.sendmail", "frappe.enqueue", "frappe.sendmail"):
        stack.enter_context(patch(target, side_effect=AssertionError("No external attempts in legacy fence proof")))
    stack.enter_context(patch.object(frappe, "publish_realtime"))
    return stack


def child(mode, cfg):
    initialize()
    try:
        with blocked():
            name = control.conversation_key("WhatsApp", cfg["account_id"], cfg["peer"])
            emit("ready")
            assert sys.stdin.readline().strip() == "go"
            if mode == "open":
                with control.conversation_fence(name):
                    doc = control.get_or_create("WhatsApp", cfg["account_id"], cfg["peer"])
                    result = control.apply_control(doc.name, "take", doc.generation, uuid4().hex)
                    if cfg.get("hold"):
                        emit("control_uncommitted")
                        assert sys.stdin.readline().strip() == "release"
                    frappe.db.commit()
                emit("control_committed", generation=result["generation"])
            else:
                # Establish an RR snapshot before the current guard read.
                if mode == "send":
                    assert not frappe.db.get_value(control.DOCTYPE, name, "name")
                    emit("snapshot_absent")
                prior = None
                if cfg.get("hold"):
                    prior = frappe.get_doc({"doctype": "ToDo", "description": "Fictional prior request work"}).insert()
                def request(method, url, **kwargs):
                    control._assert_fence(name)
                    if prior:
                        assert frappe.db.exists("ToDo", prior.name)
                    with open(cfg["journal"], "a") as journal:
                        journal.write(cfg["peer"] + "\n")
                        journal.flush()
                        os.fsync(journal.fileno())
                    emit("physical_double")
                    if cfg.get("hold"):
                        assert sys.stdin.readline().strip() == "release"
                    return Response(payload={"messaging_product": "whatsapp", "contacts": [{"wa_id": cfg["peer"]}],
                                             "messages": [{"id": "wamid.fictional-legacy-process"}]})
                payload = {"messaging_product": "whatsapp", "to": cfg["peer"], "type": "text", "text": {"body": "Fictional process proof"}}
                with patch.object(transport.requests, "request", side_effect=request):
                    try:
                        transport.api(cfg["account"], "POST", "https://graph.facebook.com/v23.0/" + cfg["account_id"] + "/messages",
                                      data=json.dumps(payload))
                    except LegacySendBlocked as error:
                        detail, sql_code = error, None
                        while detail:
                            if type(detail).__name__ == "OperationalError" and detail.args and isinstance(detail.args[0], int):
                                sql_code = detail.args[0]
                            if error.reason_code not in {"native_outbound_intent_required", "legacy_control_conflict"}:
                                print(json.dumps({"diagnostic_class": type(detail).__name__,
                                    "frames": [(frame.filename, frame.lineno, frame.name) for frame in traceback.extract_tb(detail.__traceback__)]}), file=sys.stderr, flush=True)
                            detail = detail.__context__
                        assert error.reason_code in {"native_outbound_intent_required", "legacy_control_conflict"}
                        if mode == "send_fresh":
                            assert error.reason_code == "native_outbound_intent_required"
                        emit("legacy_denied", reason=error.reason_code, sql_error_code=sql_code)
                    else:
                        if prior:
                            assert frappe.db.exists("ToDo", prior.name)
                            frappe.db.delete("ToDo", {"name": prior.name})
                        emit("legacy_sent", prior_work_preserved=bool(prior))
                frappe.db.commit()
    finally:
        frappe.db.rollback()
        frappe.destroy()


def line(proc, timeout=15):
    with selectors.DefaultSelector() as select:
        select.register(proc.stdout, selectors.EVENT_READ)
        result = bytearray()
        while True:
            assert select.select(timeout), "Child protocol timed out"
            byte = os.read(proc.stdout.fileno(), 1)
            if not byte:
                raise AssertionError("Child exited before proof event: " + proc.stderr.read().decode())
            if byte == b"\n":
                return json.loads(result)
            result.extend(byte)


def spawn(mode, cfg):
    proc = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), mode, json.dumps(cfg)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0)
    assert line(proc)["event"] == "ready"
    proc.stdin.write(b"go\n"); proc.stdin.flush()
    return proc


def waiting(proc):
    with selectors.DefaultSelector() as select:
        select.register(proc.stdout, selectors.EVENT_READ)
        assert not select.select(0.25), "Operation escaped active conversation fence"


def release(proc):
    proc.stdin.write(b"release\n"); proc.stdin.flush()


def finish(proc):
    out, err = proc.communicate(timeout=15)
    assert proc.returncode == 0, (out, err)
    emit("process_ok", remaining_stdout=out.decode(), stderr=err.decode())


def proof():
    initialize()
    account = None
    peers = []
    children = []
    journal = "/tmp/legacy-guard-double-" + uuid4().hex
    with blocked():
        try:
            account_id = "94" + str(int(uuid4().hex[:12], 16))
            account = frappe.get_doc({"doctype": "WhatsApp Account", "account_name": "legacy-proof-" + uuid4().hex,
                "phone_id": account_id, "status": "Active", "mode": "Live", "url": "https://graph.facebook.com",
                "version": "v23.0", "app_id": "994201", "business_id": "994202", "token": "fictional-proof-token"}).insert()
            frappe.db.commit()
            cfg = {"account": account.name, "account_id": account_id, "journal": journal}
            # A legacy attempt already at HTTP owns the absent-row fence; first
            # open/take must wait and can become authoritative only afterward.
            peers.append("52" + str(int(uuid4().hex[:12], 16)))
            first = {**cfg, "peer": peers[-1]}
            sender = spawn("send", {**first, "hold": True}); children.append(sender)
            assert line(sender)["event"] == "snapshot_absent"
            assert line(sender)["event"] == "physical_double"
            opener = spawn("open", first); children.append(opener)
            waiting(opener)
            release(sender)
            sent = line(sender)
            assert sent["event"] == "legacy_sent" and sent["prior_work_preserved"]
            assert line(opener)["event"] == "control_committed"
            finish(sender); finish(opener)
            emit("send_before_first_open_pass", physical_attempts=1)
            # An uncommitted open/take wins the fence. The sender primes an old
            # absent snapshot, waits, then must see the newly committed row.
            peers.append("52" + str(int(uuid4().hex[:12], 16)))
            second = {**cfg, "peer": peers[-1]}
            opener = spawn("open", {**second, "hold": True}); children.append(opener)
            assert line(opener)["event"] == "control_uncommitted"
            sender = spawn("send", second); children.append(sender)
            assert line(sender)["event"] == "snapshot_absent"
            waiting(sender)
            release(opener)
            assert line(opener)["event"] == "control_committed"
            held = line(sender)
            assert held["event"] == "legacy_denied"
            if held["reason"] == "legacy_control_conflict":
                assert isinstance(held["sql_error_code"], int)
            emit("old_snapshot_result", **{key: value for key, value in held.items() if key != "event"})
            finish(opener); finish(sender)
            fresh = spawn("send_fresh", second); children.append(fresh)
            denied = line(fresh)
            assert denied["event"] == "legacy_denied" and denied["reason"] == "native_outbound_intent_required"
            finish(fresh)
            assert Path(journal).read_text().splitlines() == [peers[0]]
            emit("first_open_before_send_pass", stale_snapshot_bypassed=False, physical_attempts=0)
        finally:
            for proc in children:
                if proc.poll() is None:
                    proc.kill(); proc.communicate(timeout=5)
            frappe.db.rollback()
            if account:
                for peer in peers:
                    name = control.conversation_key("WhatsApp", account.phone_id, peer)
                    if frappe.db.exists(control.DOCTYPE, name):
                        doc = control._load(name)
                        control.apply_control(name, "close", doc.generation, uuid4().hex, reason="Fictional legacy fence proof complete")
                frappe.db.set_value("WhatsApp Account", account.name, "status", "Inactive")
                frappe.db.commit()
                emit("cleanup", conversations_closed=True, fictional_account_disabled=True)
            frappe.destroy()
    emit("PASS", child_processes=5, physical_doubles=1, live_provider_attempts=0)


if __name__ == "__main__":
    if len(sys.argv) > 1:
        child(sys.argv[1], json.loads(sys.argv[2]))
    else:
        proof()
