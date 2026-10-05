"""Document/template composition with mocked Meta and no database or real sends."""

import copy
import json
import sys
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, mock_open, patch

import frappe

from frappe_whatsapp import native_outbox, template_vars, transport
from frappe_whatsapp.frappe_whatsapp.doctype.whatsapp_message.whatsapp_message import WhatsAppMessage, send_template


class TestTemplateTransport(unittest.TestCase):
    def setUp(self):
        self.template = frappe._dict(
            name="unit-document", actual_name="unit_document", template_name="unit_document",
            language_code="es_MX", template="Documento para {{1}}", sample_values="Sample",
            header_type="DOCUMENT", sample="/files/sample.pdf", buttons=[],
        )
        self.account = frappe._dict(name="unit-account", url="https://graph.facebook.com", version="v23.0", phone_id="991122")
        self.account.get_password = Mock(return_value="fake-token")
        self.reference = Mock()
        self.load = self.enterContext(patch.object(frappe, "get_doc", side_effect=self.get_doc))
        self.enterContext(patch.object(frappe.utils, "get_url", return_value="https://shop.example.test"))
        self.files = self.enterContext(patch.object(frappe.db, "get_value", return_value=None))
        self.enterContext(patch.object(frappe, "get_installed_apps", return_value=[]))
        self.enterContext(patch.object(frappe, "get_hooks", return_value=[]))
        self.enterContext(patch.object(frappe, "flags", frappe._dict()))
        self.api = self.enterContext(patch.object(transport, "api", return_value={"messages": [{"id": "wamid.document-send"}]}))
        self.raw = self.enterContext(patch.object(transport, "raw"))
        self.resolve = self.enterContext(patch.object(template_vars, "resolve", return_value=SimpleNamespace(ok=True, values={"1": "Ana"})))
        self.check = self.enterContext(patch.object(template_vars, "check_values", return_value=SimpleNamespace(ok=True, values={"1": "Explicit"})))

    def get_doc(self, doctype, name=None, **kwargs):
        return {"WhatsApp Templates": self.template, "WhatsApp Account": self.account, "Quotation": self.reference}[doctype]

    def message(self, **extra):
        doc = WhatsAppMessage.__new__(WhatsAppMessage)
        doc.__dict__.update(dict(
            doctype="WhatsApp Message", type="Outgoing", to="+15550194001", template=self.template.name,
            whatsapp_account=self.account.name, content_type="text", message_type="Template",
            message_id=None, message="", attach="https://cdn.example.test/COT-1.pdf?signature=opaque",
            body_param=None, reference_doctype="Quotation", reference_name="COT-1", product_catalog_json=None,
            buttons="{}", is_reply=False, reply_to_message_id=None, flags=frappe._dict(),
        ))
        doc.__dict__.update(extra)
        return doc

    def sent(self):
        return json.loads(self.api.call_args.kwargs["data"])

    def header(self):
        return next(c for c in self.sent()["template"]["components"] if c["type"] == "header")["parameters"][0]["document"]

    def local_files(self, *, private=False, size=128, signature=b"%PDF-", filename="COT-1.pdf"):
        prefix = "/private/files/" if private else "/files/"
        self.enterContext(patch.object(frappe, "get_site_path", side_effect=lambda *parts: str(Path("/fictional/site").joinpath(*parts))))
        self.enterContext(patch.object(Path, "is_file", return_value=True))
        self.enterContext(patch.object(Path, "stat", return_value=SimpleNamespace(st_size=size)))
        self.enterContext(patch.object(Path, "open", mock_open(read_data=signature)))
        self.files.return_value = filename
        return prefix + "random-token.pdf"

    def test_external_link_filename_body_and_no_upload(self):
        doc = self.message()
        doc.send_template()
        self.assertEqual(self.header(), {"link": doc.attach, "filename": "COT-1.pdf"})
        self.assertEqual(self.sent()["template"]["language"], {"code": self.template.language_code})
        self.assertEqual(self.sent()["template"]["components"][0]["parameters"], [{"type": "text", "text": "Ana"}])
        self.resolve.assert_called_once_with(self.template.name, self.reference)
        self.raw.assert_not_called()

    def test_real_upload_helper_sends_document_even_when_content_type_is_text(self):
        attach = self.local_files()
        self.enterContext(patch("os.path.exists", return_value=True))
        self.enterContext(patch("os.path.getsize", return_value=128))
        self.enterContext(patch("builtins.open", mock_open(read_data=b"%PDF-1.4")))
        self.raw.return_value.json.return_value = {"id": "uploaded-pdf"}
        self.message(attach=attach.replace(".pdf", ".bin")).send_template()
        self.assertEqual(self.header(), {"id": "uploaded-pdf", "filename": "COT-1.pdf"})
        self.assertEqual(self.raw.call_args.args[:2], (self.account, "POST"))
        self.assertEqual(self.raw.call_args.kwargs["data"]["type"], "application/pdf")
        self.assertTrue(self.raw.call_args.args[2].endswith("/991122/media"))

    def test_private_upload_failure_never_becomes_a_public_link(self):
        doc = self.message(attach=self.local_files(private=True))
        with patch.object(WhatsAppMessage, "_upload_local_media", return_value=(None, "document")):
            with self.assertRaisesRegex(frappe.ValidationError, "PDF privado.*Vuelve a intentar"):
                doc.send_template()
        self.api.assert_not_called()

    def test_public_upload_failure_keeps_legacy_link_fallback(self):
        doc = self.message(attach=self.local_files())
        with patch.object(WhatsAppMessage, "_upload_local_media", return_value=(None, "document")):
            doc.send_template()
        self.assertEqual(self.header(), {"link": "https://shop.example.test" + doc.attach, "filename": "COT-1.pdf"})

    def test_signed_print_url_has_explicit_filename(self):
        doc = self.message(attach="/api/method/frappe.utils.print_format.download_pdf?name=COT-1&key=opaque", attach_filename="COT-1.pdf")
        doc.send_template()
        self.assertEqual(self.header(), {"link": "https://shop.example.test" + doc.attach, "filename": "COT-1.pdf"})

    def test_missing_and_invalid_documents_fail_before_any_meta_request(self):
        for attach in (None, "", "https://cdn.example.test/file.docx", "file:///secret.pdf", "//cdn.example.test/a.pdf", "https://user:secret@cdn.example.test/a.pdf", "https://cdn .example.test/a.pdf"):
            with self.subTest(attach=attach), self.assertRaises(frappe.ValidationError):
                self.message(attach=attach).send_template()
        self.raw.assert_not_called()
        self.api.assert_not_called()

    def test_public_file_with_spaces_has_encoded_fallback_link(self):
        attach = self.local_files().replace("random-token", "Mi documento")
        with patch.object(WhatsAppMessage, "_upload_local_media", return_value=(None, "document")):
            self.message(attach=attach).send_template()
        self.assertEqual(self.header()["link"], "https://shop.example.test/files/Mi%20documento.pdf")

    def test_missing_local_file_non_pdf_oversize_and_traversal_fail_before_upload(self):
        attach = self.local_files()
        with patch.object(Path, "is_file", return_value=False), self.assertRaisesRegex(frappe.ValidationError, "ya no está disponible"):
            self.message(attach=attach).send_template()
        with patch.object(Path, "open", mock_open(read_data=b"wrong")), self.assertRaisesRegex(frappe.ValidationError, "PDF válido"):
            self.message(attach=attach).send_template()
        with patch.object(Path, "stat", return_value=SimpleNamespace(st_size=WhatsAppMessage.MAX_DOCUMENT_BYTES + 1)), self.assertRaisesRegex(frappe.ValidationError, "100 MB"):
            self.message(attach=attach).send_template()
        with self.assertRaisesRegex(frappe.ValidationError, "ya no está disponible"):
            self.message(attach="/files/../../secret.pdf").send_template()
        self.raw.assert_not_called()
        self.api.assert_not_called()

    def test_same_site_absolute_attachment_uploads_and_preserves_display_filename(self):
        attach = self.local_files(private=True)
        with patch.object(WhatsAppMessage, "_upload_local_media", return_value=("private-pdf", "document")) as upload:
            self.message(attach="https://shop.example.test" + attach, attach_filename="Folio.pdf").send_template()
        upload.assert_called_once_with(send_as="document", attach=attach, mime_type="application/pdf")
        self.assertEqual(self.header(), {"id": "private-pdf", "filename": "Folio.pdf"})

    def test_explicit_body_values_use_existing_contract(self):
        self.message(body_param='{"1":"Explicit"}').send_template()
        self.check.assert_called_once_with(self.template.name, {"1": "Explicit"})
        self.resolve.assert_not_called()
        self.assertEqual(json.loads(self.api.call_args.kwargs["data"])["template"]["components"][0]["parameters"][0]["text"], "Explicit")

    def test_missing_body_value_stops_before_upload_or_send(self):
        self.resolve.return_value = SimpleNamespace(ok=False, reason=lambda: "Falta el folio. Completa el documento.")
        with self.assertRaisesRegex(frappe.ValidationError, "Falta el folio"):
            self.message().send_template()
        self.api.assert_not_called()
        self.raw.assert_not_called()

    def test_partial_quick_reply_overrides_static_and_dynamic_buttons_keep_indices(self):
        self.template.buttons = [frappe._dict(button_type="Visit Website", url_type="Static"),
                                 frappe._dict(button_type="Quick Reply", button_label="Acepto"),
                                 frappe._dict(button_type="Quick Reply", button_label="Tengo dudas"),
                                 frappe._dict(button_type="Visit Website", url_type="Dynamic", website_url="url")]
        self.reference.get_formatted.return_value = "opaque-link"
        self.message(buttons=json.dumps([{"index": 1, "payload": "doc:opaque-signed-token"}])).send_template()
        buttons = [c for c in self.sent()["template"]["components"] if c["type"] == "button"]
        self.assertEqual([(b["index"], b["sub_type"]) for b in buttons], [("1", "quick_reply"), ("2", "quick_reply"), ("3", "url")])
        self.assertEqual(buttons[0]["parameters"], [{"type": "payload", "payload": "doc:opaque-signed-token"}])
        self.assertEqual(buttons[1]["parameters"][0]["payload"], "Tengo dudas")

    def test_mpm_offset_is_final_meta_index(self):
        self.template.buttons = [frappe._dict(button_type="Quick Reply", button_label="Confirmo")]
        self.message(product_catalog_json='{"thumbnail_product_retailer_id":"SKU"}', buttons=[{"index": "1", "payload": "sn:opaque"}]).send_template()
        buttons = [c for c in self.sent()["template"]["components"] if c["type"] == "button"]
        self.assertEqual([b["index"] for b in buttons], ["0", "1"])
        self.assertEqual(buttons[1]["parameters"][0]["payload"], "sn:opaque")

    def test_bad_button_payloads_fail_before_upload_or_meta_send(self):
        self.template.buttons = [frappe._dict(button_type="Quick Reply", button_label="Acepto")]
        invalid = ['{', {"0": "doc:opaque"}, [{"index": True, "payload": "x"}], [{"index": 1, "payload": "x"}],
                   [{"index": 0, "payload": ""}], [{"index": 0, "payload": "x" * 129}],
                   [{"index": 0, "payload": "x\n"}], [{"index": 0, "payload": "x"}, {"index": 0, "payload": "y"}]]
        for buttons in invalid:
            with self.subTest(buttons=buttons), self.assertRaises(frappe.ValidationError):
                self.message(buttons=buttons).send_template()
        self.api.assert_not_called()
        self.raw.assert_not_called()

    def test_existing_text_and_image_template_sends_need_no_document(self):
        for header_type in (None, "IMAGE"):
            self.template.header_type = header_type
            self.message(attach=None).send_template()
        self.assertEqual(next(c for c in self.sent()["template"]["components"] if c["type"] == "header")["parameters"][0],
                         {"type": "image", "image": {"link": "https://shop.example.test/files/sample.pdf"}})
        self.raw.assert_not_called()

    def test_native_handoff_freezes_document_and_payload_and_avoids_direct_message_http(self):
        self.template.buttons = [frappe._dict(button_type="Quick Reply", button_label="Acepto")]
        bridge = types.ModuleType("crm.api.outbox_bridge")
        bridge.governing_conversation = Mock(return_value="conversation")
        bridge.queue_transcript = Mock()
        bridge.requeue_transcript = Mock(return_value=False)
        modules = {"crm": types.ModuleType("crm"), "crm.api": types.ModuleType("crm.api"), "crm.api.outbox_bridge": bridge}
        with patch.dict(sys.modules, modules), patch.object(frappe, "get_installed_apps", return_value=["crm"]), \
                patch.object(frappe.db, "savepoint"), patch.object(frappe.db, "release_savepoint"):
            doc = self.message(buttons=[{"index": 0, "payload": "doc:opaque"}])
            # A first send (before insert) freezes the transcript; a re-send would requeue instead.
            doc.is_new = Mock(return_value=True)
            doc.send_template()
            frozen = copy.deepcopy(doc.flags.native_transcript[1])
            doc.name = "WA-UNIT"
            doc.after_insert()
        self.assertEqual(doc.status, "Queued")
        self.assertIsNone(doc.message_id)
        self.api.assert_not_called()
        self.assertEqual(bridge.queue_transcript.call_args.args[2], frozen)
        self.assertEqual(json.loads(native_outbox.validate_payload(frozen, account_id=self.account.phone_id, peer_id="15550194001")), frozen)

    def test_send_template_entry_point_checks_reference_read_before_saving(self):
        outgoing = Mock()
        with patch.object(frappe, "get_doc", side_effect=[self.reference, outgoing]):
            send_template("15550194001", "Quotation", "COT-1", self.template.name, attach="https://cdn.example.test/a.pdf", buttons=[{"index": 0, "payload": "opaque"}])
        self.reference.check_permission.assert_called_once_with("read")
        outgoing.save.assert_called_once_with()
        self.reference.check_permission.side_effect = frappe.PermissionError("Sin permiso")
        with patch.object(frappe, "get_doc", return_value=self.reference) as load, self.assertRaises(frappe.PermissionError):
            send_template("15550194001", "Quotation", "COT-1", self.template.name)
        self.assertEqual(load.call_count, 1)

    def test_template_button_payload_roundtrip_reaches_domain_hook_unchanged(self):
        from frappe_whatsapp.frappe_whatsapp.tests.test_button_payloads import fixture, process_inbound

        self.template.buttons = [frappe._dict(button_type="Quick Reply", button_label="Acepto")]
        for prefix in ("sn:", "doc:"):
            with self.subTest(prefix=prefix):
                token = prefix + "opaque-record-token.with-signature"
                doc = self.message(buttons=[{"index": 0, "payload": token}])
                doc.send_template()
                outbound = next(c for c in self.sent()["template"]["components"] if c["type"] == "button")
                change = fixture("template_quick_reply")
                inbound = change["value"]["messages"][0]
                inbound["button"]["payload"] = outbound["parameters"][0]["payload"]
                inbound["context"]["id"] = doc.message_id
                inbound["from"] = self.sent()["to"]
                rows, snapshots = process_inbound(change)
                self.assertEqual(rows[0]["button_payload"], token)
                self.assertEqual(snapshots[0]["button_payload"], token)
                self.assertEqual(snapshots[0]["reply_to_message_id"], doc.message_id)
                self.assertEqual(snapshots[0]["from"], self.sent()["to"])
                self.assertEqual(rows[0]["message"], "Acepto")

    def test_legacy_freeform_document_upload_keeps_no_argument_helper_contract(self):
        doc = self.message(message_type="Manual", content_type="document", message="Tu documento", attach="https://cdn.example.test/COT-1.pdf")
        with patch("frappe_whatsapp.coexistence.assert_sendable"), \
                patch.object(WhatsAppMessage, "_upload_local_media", return_value=("freeform-media", "document")) as upload:
            doc.send_outgoing()
        upload.assert_called_once_with()
        self.assertEqual(self.sent()["document"], {"id": "freeform-media", "caption": "Tu documento", "filename": "COT-1.pdf"})
