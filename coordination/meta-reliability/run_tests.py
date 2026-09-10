"""Isolated candidate imports in the lab; block all external transports."""
import os
import sys
import unittest
from unittest.mock import patch
sys.path.insert(0, '/tmp/meta-wa-20260910')
os.chdir('/home/frappe/frappe-bench/sites')
import frappe
frappe.init(site='doco-mirror.lab.xoloitzcuintles.com')
frappe.connect()
frappe.set_user('Administrator')
frappe.local.conf = frappe._dict(frappe.local.conf)
frappe.local.conf.developer_mode = 1
frappe.flags.in_test = True
frappe.local.test_objects = {}

def blocked(*args, **kwargs):
    raise AssertionError('External transport blocked in Meta acceptance')

try:
    with patch('requests.sessions.Session.request', side_effect=blocked), \
         patch('smtplib.SMTP.sendmail', side_effect=blocked), patch.object(frappe.db, 'commit'):
        suite = unittest.defaultTestLoader.loadTestsFromNames(sys.argv[1:])
        result = unittest.TextTestRunner(verbosity=2).run(suite)
    sys.exit(not result.wasSuccessful())
finally:
    frappe.db.rollback()
    frappe.destroy()
