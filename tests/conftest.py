"""Bench-free suite: install the Frappe stand-in before any test module imports the app.

These tests patch every Frappe call they make and run where Frappe is not
installed (the release CI lane). On a bench, run the app's own suites with
`bench run-tests --app frappe_whatsapp` instead.
"""

import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import _frappe_stub  # noqa: E402

if not _frappe_stub.install():
    raise RuntimeError("tests/ is the bench-free suite and needs an environment without Frappe installed")
