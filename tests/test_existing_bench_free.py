"""The app's own unit suites that need no site, collected here under the Frappe stand-in.

Bench still runs them with the rest of `frappe_whatsapp/frappe_whatsapp/tests`.
Importing a TestCase class is enough for pytest to collect it. Classes that
insert documents, commit, switch users or need CRM's fixtures stay bench-only:
TestAccountHealthSQL, TestCoexistenceSql, TestCustomerActivity, TestDeliveryBridge,
TestDemoInbound, TestDemoModeSuppressesEgress, TestLiveMode, TestLegacyTransport
(its module imports CRM's LegacyGuardFixture), TestLegacyTransportSql,
TestReceiptPipelineSql, TestWebhookReceipts, the whatsapp_flow suites and every
IntegrationTestCase under doctype/, report/ and utils/.
"""

from frappe_whatsapp.frappe_whatsapp.tests.test_account_health import TestAccountHealth
from frappe_whatsapp.frappe_whatsapp.tests.test_coexistence import TestEchoAtomization
from frappe_whatsapp.frappe_whatsapp.tests.test_native_outbox import TestNativeOutbox
from frappe_whatsapp.frappe_whatsapp.tests.test_transport import TestEgressInvariant, TestReceiptEgress
from frappe_whatsapp.frappe_whatsapp.tests.test_webhook_signature import TestSignature

__all__ = [
    "TestAccountHealth",
    "TestEchoAtomization",
    "TestEgressInvariant",
    "TestNativeOutbox",
    "TestReceiptEgress",
    "TestSignature",
]
