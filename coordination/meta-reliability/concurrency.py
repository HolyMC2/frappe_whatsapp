"""Committed, multi-process fault proofs on the fictional Meta-only lab site."""
import os, sys, json, subprocess, uuid
from datetime import timedelta
from unittest.mock import patch
sys.path.insert(0, '/tmp/meta-wa-20260910')
os.chdir('/home/frappe/frappe-bench/sites')
import frappe
from frappe.utils import now_datetime
from frappe_whatsapp import webhook_receipts as r

SITE='meta-reliability-test-20260910.lab.xoloitzcuintles.com'
PREFIX='meta-proof-20260910-'

def block(*args, **kwargs):
    raise AssertionError('external transport blocked')

def initialize():
    frappe.init(site=SITE);frappe.connect();frappe.set_user('Administrator')
    frappe.local.conf=frappe._dict(frappe.local.conf);frappe.local.conf.developer_mode=1

def event(key):
    return {'provider':'WhatsApp','account_id':PREFIX+'phone','app_id':PREFIX+'app',
            'event_type':'message','event_id':key,'payload':{'fictional':True,'key':key}}

def todo(key):
    return frappe.get_doc({'doctype':'ToDo','description':key}).insert(ignore_permissions=True).name

def consumer(row):
    mode=os.environ.get('META_PROOF_MODE','normal')
    if mode=='enrich-fail':raise r.ReceiptError('enrichment_unavailable')
    if mode=='crash-claim':os._exit(77)
    todo(row.event_id+'-effect')
    if mode=='crash-local':os._exit(78)
    if mode=='expect-replay':assert frappe.flags.meta_webhook_replay
    return {'state':'Processed'}

def child(operation,key):
    initialize()
    try:
        with patch('requests.sessions.Session.request',side_effect=block), \
             patch('smtplib.SMTP.sendmail',side_effect=block), \
             patch.object(frappe,'enqueue',side_effect=RuntimeError('fake queue down')), \
             patch.object(r,'_consumer',side_effect=consumer):
            if operation.startswith('receive'):
                todo(key+'-anchor')
                batch = [event(key)] if operation == 'receive' else [
                    event(key + '-first'), event(key + '-second')]
                if operation == 'receive-reversed':
                    batch.reverse()
                names=r.record_events(batch)
                frappe.db.commit()
                print(json.dumps({'receipts':sorted(names)}))
            elif operation=='worker':r.run_receipt(r._prepare(event(key))['event_key'])
    except frappe.QueryDeadlockError:
        # A database deadlock rejects the HTTP transaction. The parent models a
        # NEW provider delivery, never a helper swallowing/rolling back prior work.
        raise SystemExit(75)
    finally:frappe.db.rollback();frappe.destroy()

if len(sys.argv)>1:
    child(sys.argv[1],sys.argv[2]);raise SystemExit()

initialize()
created=[]
def spawn(operation,key,mode='normal'):
    proc = subprocess.Popen([sys.executable,__file__,operation,key],stdout=subprocess.PIPE,stderr=subprocess.PIPE,
                            text=True,env={**os.environ,'META_PROOF_MODE':mode})
    proc.proof_args = operation,key,mode
    return proc
def finish(proc,expected=0,retries=2):
    out,err=proc.communicate(timeout=40)
    if proc.returncode==75 and retries:
        print('RETRY identified DB concurrency conflict: fresh transaction/process')
        return finish(spawn(*proc.proof_args), expected, retries-1)
    assert proc.returncode==expected,(proc.returncode,err[-1500:])
    return json.loads(out) if out.strip() else None

def fresh():
    key=PREFIX+uuid.uuid4().hex;created.append(key);return key

def row(key):
    frappe.db.rollback()
    return frappe.get_doc(r.DOCTYPE,r._prepare(event(key))['event_key'])

def make_due(key,**values):
    frappe.db.set_value(r.DOCTYPE,row(key).name,{'next_attempt_at':None,
        'lease_until':now_datetime()-timedelta(minutes=10),**values},update_modified=False)
    frappe.db.commit()

try:
    batch_key=fresh()
    created.extend([batch_key+'-first', batch_key+'-second'])
    a,b=spawn('receive-batch',batch_key),spawn('receive-reversed',batch_key)
    assert finish(a)==finish(b)
    assert frappe.db.count('ToDo',{'description':batch_key+'-anchor'})==2
    assert row(batch_key+'-first').state == row(batch_key+'-second').state == 'Pending'
    print('PASS reversed overlapping batches: both atoms retained once, both earlier anchors preserved')

    key=fresh()
    a,b=spawn('receive',key),spawn('receive',key)
    assert finish(a)==finish(b)
    frappe.db.rollback()  # discard the earlier batch's repeatable-read snapshot
    assert frappe.db.count('ToDo',{'description':key+'-anchor'})==2
    assert row(key).state=='Pending'
    print('PASS duplicate HTTP transactions: one receipt, both earlier anchors preserved')
    a,b=spawn('worker',key),spawn('worker',key)
    finish(a);finish(b)
    assert row(key).state=='Processed' and row(key).attempts==1
    assert frappe.db.count('ToDo',{'description':key+'-effect'})==1
    finish(spawn('worker',key))
    assert frappe.db.count('ToDo',{'description':key+'-effect'})==1
    print('PASS two workers + replay: exactly one local effect')

    key=fresh();finish(spawn('receive',key));finish(spawn('worker',key,'enrich-fail'))
    assert row(key).state=='Failed' and row(key).reason_code=='enrichment_unavailable'
    assert not frappe.db.exists('ToDo',{'description':key+'-effect'})
    make_due(key);finish(spawn('worker',key,'expect-replay'))
    assert row(key).state=='Processed' and row(key).attempts==2
    print('PASS failed enrichment: retained receipt, recoverable across processes')

    for mode,exit_code in [('crash-claim',77),('crash-local',78)]:
        key=fresh();finish(spawn('receive',key));finish(spawn('worker',key,mode),exit_code)
        assert row(key).state=='Processing'
        assert not frappe.db.exists('ToDo',{'description':key+'-effect'})
        make_due(key);finish(spawn('worker',key,'expect-replay'))
        assert row(key).state=='Processed' and row(key).attempts==2
        assert frappe.db.count('ToDo',{'description':key+'-effect'})==1
        print('PASS '+mode+': durable claim, rollback of unfinished effect, restart recovery')

    key=fresh();finish(spawn('receive',key));make_due(key,state='Processing',attempts=r.MAX_ATTEMPTS)
    finish(spawn('worker',key))
    assert row(key).state=='Failed' and row(key).reason_code=='retry_exhausted'
    assert not frappe.db.exists('ToDo',{'description':key+'-effect'})
    print('PASS final abandoned lease: explicit exhausted outcome, no repeated effect')
finally:
    frappe.db.rollback()
    for key in created:
        frappe.db.delete(r.DOCTYPE,{'event_key':r._prepare(event(key))['event_key']})
        for suffix in ['-anchor','-effect']:
            frappe.db.delete('ToDo',{'description':key+suffix})
    frappe.db.commit();frappe.destroy()
