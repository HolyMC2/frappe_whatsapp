"""Actual Guest receipt worker + WhatsApp controller proofs; no external sends."""
import os,sys,json,uuid,base64
from contextlib import ExitStack
from unittest.mock import patch
from werkzeug.test import EnvironBuilder
from werkzeug.wrappers import Request
sys.path.insert(0,'/tmp/meta-wa-20260910')
os.chdir('/home/frappe/frappe-bench/sites')
import frappe
from frappe_whatsapp import webhook_receipts as receipts, transport
from frappe_whatsapp.utils import webhook,signature
SITE='meta-reliability-test-20260910.lab.xoloitzcuintles.com'
frappe.init(site=SITE);frappe.connect();frappe.set_user('Administrator')
frappe.local.conf=frappe._dict(frappe.local.conf);frappe.local.conf.developer_mode=1
key='actual-meta-'+uuid.uuid4().hex
account=None
names=[]
profiles=[]
msg_ids=[key+'-text',key+'-image',key+'-out']

def blocked(*args,**kwargs):raise AssertionError('External provider submission blocked')

class Response:
    content=base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aQ1sAAAAASUVORK5CYII=')
    def raise_for_status(self):pass
    def json(self):return {'url':'https://fictional.invalid/photo','mime_type':'image/png'}

def read_media(account,method,url,**kwargs):
    assert method=='GET'
    return Response()

try:
    with ExitStack() as stack:
        stack.enter_context(patch('requests.sessions.Session.request',side_effect=blocked))
        stack.enter_context(patch('smtplib.SMTP.sendmail',side_effect=blocked))
        enqueue=stack.enter_context(patch.object(frappe,'enqueue',side_effect=RuntimeError('fictional queue outage')))
        send=stack.enter_context(patch.object(transport,'api',side_effect=blocked))
        raw=stack.enter_context(patch.object(transport,'raw',side_effect=read_media))
        account=frappe.get_doc({'doctype':'WhatsApp Account','account_name':key,'status':'Active','mode':'Live',
            'phone_id':key+'-phone','app_id':key+'-app','business_id':key+'-waba','app_secret':'fictional-secret',
            'token':'fictional-token','url':'https://graph.facebook.com','version':'v25.0'}).insert(ignore_permissions=True)
        outgoing=frappe.get_doc({'doctype':'WhatsApp Message','type':'Outgoing','whatsapp_account':account.name,
            'to':'15555550104','message':'Fictional existing outbound','content_type':'text','message_id':msg_ids[2]})
        outgoing.db_insert() # stored accepted row, never submits a provider send
        frappe.db.commit()
        data={'object':'whatsapp_business_account','entry':[{'id':account.business_id,'changes':[{'field':'messages','value':{
            'metadata':{'phone_number_id':account.phone_id},'contacts':[{'wa_id':'15555550104','profile':{'name':'Fictional Visitor'}}],
            'messages':[{'id':msg_ids[0],'from':'15555550104','type':'text','text':{'body':'Fictional receipt text'}},
                        {'id':msg_ids[1],'from':'15555550104','type':'image','image':{'id':'fictional-media'}}],
            'statuses':[{'id':msg_ids[2],'status':'read','timestamp':'1800000002'},
                        {'id':msg_ids[2],'status':'sent','timestamp':'1800000000'},
                        {'id':msg_ids[2],'status':'delivered','timestamp':'1800000001'}]}}]}]}
        body=json.dumps(data).encode()
        request=Request(EnvironBuilder(method='POST',data=body,headers={
            'X-Hub-Signature-256':signature.expected_signature('fictional-secret',body)}).get_environ())
        frappe.set_user('Guest')
        with patch.object(frappe,'request',request):names=webhook.post()
        assert not frappe.db.exists('WhatsApp Message',{'message_id':msg_ids[0]})
        frappe.db.commit()
        assert len(names)==5
        print('PASS actual signed Guest HTTP: five durable atoms, no premature message or media work')
        # Reverse evidence order deliberately: read first, then delivered, then sent.
        rows=[frappe.get_doc(receipts.DOCTYPE,n) for n in names]
        rows.sort(key=lambda r: {'read':0,'delivered':1,'sent':2}.get(
            (json.loads(r.payload)['change']['value'].get('statuses') or [{}])[0].get('status'),3))
        for row in rows:receipts.run_receipt(row.name)
        for n in names:
            state=frappe.db.get_value(receipts.DOCTYPE,n,['state','reason_code'],as_dict=True)
            assert state.state=='Processed',state
        assert frappe.db.count('WhatsApp Message',{'message_id':['in',msg_ids[:2]]})==2
        image=frappe.get_doc('WhatsApp Message',{'message_id':msg_ids[1]})
        assert image.attach
        assert frappe.db.get_value('WhatsApp Message',{'message_id':msg_ids[2]},'status')=='read'
        send.assert_not_called()
        assert raw.call_count==2
        for n in names:receipts.run_receipt(n)
        assert raw.call_count==2
        print('PASS actual Guest consumer: text+media stored, read status monotonic, no submission, no duplicate fetch/effect')
finally:
    frappe.db.rollback();frappe.set_user('Administrator')
    for msg in frappe.get_all('WhatsApp Message',filters={'message_id':['in',msg_ids]},pluck='name'):
        for f in frappe.get_all('File',filters={'attached_to_doctype':'WhatsApp Message','attached_to_name':msg},pluck='name'):
            frappe.delete_doc('File',f,force=True,ignore_permissions=True)
        frappe.db.delete('WhatsApp Message',{'name':msg})
    if account:
        frappe.db.delete('WhatsApp Profiles',{'whatsapp_account':account.name})
        for n in frappe.get_all('WhatsApp Notification Log', filters={'meta_data':['like','%'+key+'%']}, pluck='name'):
            frappe.db.delete('WhatsApp Notification Log',{'name':n})
        frappe.db.delete(receipts.DOCTYPE,{'app_id':account.app_id})
        frappe.delete_doc('WhatsApp Account',account.name,force=True,ignore_permissions=True)
    frappe.db.commit();frappe.destroy()
