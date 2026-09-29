# Copyright (c) 2026, Marco / Grupo Doco and contributors
# See license.txt
"""The template-variable contract (frappe_whatsapp.template_vars): one answer to
«what fills {{n}}» for the Cloud API send and the manual wa.me prefill. No test
here reaches Meta: sends go through TestWhatsAppMessage's Graph double."""

import json
from contextlib import contextmanager
from datetime import date
from unittest.mock import patch

import frappe
from frappe_whatsapp import template_vars as tv
from frappe_whatsapp.frappe_whatsapp.doctype.whatsapp_message import test_whatsapp_message as twm
from frappe_whatsapp.testing import IntegrationTestCase

PREFIX = "zz_tv_"


def _template(name, body, field_names="", sample_values="", actual_name=None):
	"""A local template row, inserted without hooks (no Meta round-trip)."""
	if frappe.db.exists("WhatsApp Templates", name):
		frappe.delete_doc("WhatsApp Templates", name, force=True, ignore_permissions=True)
	doc = frappe.get_doc({
		"doctype": "WhatsApp Templates",
		"name": name,
		"template_name": name,
		"actual_name": actual_name or name,
		"template": body,
		"field_names": field_names,
		"sample_values": sample_values,
		"category": "UTILITY",
		"language_code": "es_MX",
		"status": "APPROVED",
	})
	doc.db_insert()
	return name


def _provider(values, keys=None):
	keys = keys or {k: k.replace("_", " ").capitalize() for k in values}
	return {"keys": keys, "resolve": lambda doc, wanted: {k: values.get(k) for k in wanted}}


@contextmanager
def _registry(providers=None, defaults=None):
	with patch.object(tv, "providers", side_effect=lambda dt: (providers or {}).get(dt, [])), \
		patch.object(tv, "default_mappings", return_value=defaults or {}):
		yield


class TestTemplateVars(IntegrationTestCase):
	def tearDown(self):
		for name in frappe.get_all("WhatsApp Templates", filters={"name": ["like", f"{PREFIX}%"]}, pluck="name"):
			frappe.db.delete("WhatsApp Templates", name)
		# seed_default_mappings(apply=1) commits; so must the cleanup.
		frappe.db.commit()  # nosemgrep: frappe-manual-commit -- undo fixtures an applied seed committed

	def test_render_fills_by_index_whatever_the_order_in_the_body(self):
		body = "Gracias {{1}}. Entregamos tu {{3}} (folio {{2}})."
		self.assertEqual(
			tv.render(body, {"1": "Ana", "2": "RO-1", "3": "iPhone 11"}),
			"Gracias Ana. Entregamos tu iPhone 11 (folio RO-1).",
		)
		self.assertEqual(tv.render(body, {"1": "Ana"}), "Gracias Ana. Entregamos tu {{3}} (folio {{2}}).")

	def test_mapping_resolves_from_providers_in_placeholder_order(self):
		tpl = _template(f"{PREFIX}entregado", "Gracias {{1}}. Tu {{3}} (folio {{2}}).",
			field_names="customer_first_name,repair_order,device_model")
		values = {"customer_first_name": "Ana", "repair_order": "RO-9", "device_model": "Moto G"}
		with _registry({"User": [_provider(values)]}):
			result = tv.resolve(tpl, frappe.get_doc("User", "Administrator"))
		self.assertTrue(result.ok)
		self.assertEqual(result.values, {"1": "Ana", "2": "RO-9", "3": "Moto G"})
		self.assertEqual(result.text, "Gracias Ana. Tu Moto G (folio RO-9).")
		self.assertEqual(list(json.loads(result.body_param())), ["1", "2", "3"])

	def test_context_wins_over_providers(self):
		tpl = _template(f"{PREFIX}ctx", "Hola {{1}}", field_names="customer_first_name")
		with _registry({"User": [_provider({"customer_first_name": "Ana"})]}):
			result = tv.resolve(tpl, frappe.get_doc("User", "Administrator"), context={"customer_first_name": "Beto"})
		self.assertEqual(result.values, {"1": "Beto"})

	def test_missing_value_is_named_and_left_visible(self):
		tpl = _template(f"{PREFIX}miss", "Hola {{1}}, tu orden {{2}}.", field_names="customer_first_name,repair_order")
		keys = {"customer_first_name": "Customer first name", "repair_order": "Repair order folio"}
		with _registry({"User": [_provider({"customer_first_name": "Ana", "repair_order": ""}, keys)]}):
			result = tv.resolve(tpl, frappe.get_doc("User", "Administrator"))
		self.assertFalse(result.ok)
		self.assertEqual([m["index"] for m in result.missing], [2])
		self.assertIn("Repair order folio", result.reason())
		self.assertEqual(result.text, "Hola Ana, tu orden {{2}}.")

	def test_a_provider_key_without_value_never_falls_back_to_a_field(self):
		tpl = _template(f"{PREFIX}owned", "Hola {{1}}", field_names="first_name")
		with _registry({"User": [_provider({"first_name": ""})]}):
			result = tv.resolve(tpl, frappe.get_doc("User", "Administrator"))
		self.assertFalse(result.ok, "User.first_name must not stand in for the provider's key")

	def test_meta_sample_values_are_never_used(self):
		tpl = _template(f"{PREFIX}sample", "Hola {{1}}, folio {{2}}.", sample_values="Juan,REP-2026-0001")
		with _registry():
			result = tv.resolve(tpl, frappe.get_doc("User", "Administrator"))
		self.assertEqual(len(result.missing), 2)
		self.assertNotIn("Juan", result.text)
		self.assertNotIn("REP-2026-0001", result.text)

	def test_shipped_default_stands_in_for_an_empty_mapping(self):
		tpl = _template(f"{PREFIX}dflt", "Hola {{1}}", actual_name=f"{PREFIX}shipped")
		with _registry({"User": [_provider({"customer_first_name": "Ana"})]}, {f"{PREFIX}shipped": "customer_first_name"}):
			self.assertEqual(tv.resolve(tpl, frappe.get_doc("User", "Administrator")).values, {"1": "Ana"})

	def test_default_with_other_variable_count_is_not_guessed(self):
		tpl = _template(f"{PREFIX}count", "Hola {{1}} {{2}}", actual_name=f"{PREFIX}shipped")
		with _registry({}, {f"{PREFIX}shipped": "customer_first_name"}):
			self.assertEqual(tv.tokens_for(frappe.db.get_value("WhatsApp Templates", tpl, "*", as_dict=True)), [])

	def test_fieldname_tokens_read_the_record_but_never_secrets(self):
		tpl = _template(f"{PREFIX}field", "Hola {{1}} {{2}}", field_names="first_name,new_password")
		with _registry():
			result = tv.resolve(tpl, frappe.get_doc("User", "Administrator"))
		self.assertEqual(result.values.get("1"), frappe.db.get_value("User", "Administrator", "first_name"))
		self.assertEqual([m["index"] for m in result.missing], [2])

	def test_amounts_and_dates_use_site_formats(self):
		self.assertEqual(tv.format_value(tv.Money(850, "MXN"), "number"), frappe.utils.fmt_money(850))
		self.assertEqual(tv.format_value(tv.Money(850, "MXN")), frappe.utils.fmt_money(850, currency="MXN"))
		self.assertTrue(tv.format_value(date(2026, 7, 15)))
		self.assertIn("2026", tv.format_value(date(2026, 7, 15)))

	def test_check_values_blocks_empty_slots(self):
		tpl = _template(f"{PREFIX}chk", "Hola {{1}}, {{2}}")
		with _registry():
			self.assertFalse(tv.check_values(tpl, {"1": "Ana", "2": " "}).ok)
			self.assertFalse(tv.check_values(tpl, {"1": "Ana"}).ok)
			self.assertTrue(tv.check_values(tpl, {"2": "b", "1": "a"}).ok)

	def test_seed_fills_only_empty_mappings_and_only_on_apply(self):
		empty = _template(f"{PREFIX}seed_a", "Hola {{1}}", actual_name=f"{PREFIX}seed_a")
		mapped = _template(f"{PREFIX}seed_b", "Hola {{1}}", field_names="first_name", actual_name=f"{PREFIX}seed_b")
		wrong = _template(f"{PREFIX}seed_c", "Hola {{1}} {{2}}", actual_name=f"{PREFIX}seed_c")
		defaults = {f"{PREFIX}seed_a": "customer_first_name", f"{PREFIX}seed_b": "customer_first_name",
			f"{PREFIX}seed_c": "customer_first_name"}
		with _registry({}, defaults):
			tv.seed_default_mappings(apply=0, verbose=0)
			self.assertFalse(frappe.db.get_value("WhatsApp Templates", empty, "field_names"))
			report = {r["template"]: r for r in tv.seed_default_mappings(apply=1, verbose=0)}
		self.assertEqual(frappe.db.get_value("WhatsApp Templates", empty, "field_names"), "customer_first_name")
		self.assertEqual(frappe.db.get_value("WhatsApp Templates", mapped, "field_names"), "first_name")
		self.assertFalse(frappe.db.get_value("WhatsApp Templates", wrong, "field_names"))
		self.assertEqual(report[mapped]["action"], "kept (tenant mapping)")
		self.assertTrue(report[wrong]["action"].startswith("skipped"))


class TestSendTemplateUsesTheContract(IntegrationTestCase):
	"""The Cloud API send reads the same contract; a gap blocks before Graph.
	Uses TestWhatsAppMessage's account fixture and Graph double."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		twm.TestWhatsAppMessage._ensure_test_account()

	setUp = twm.TestWhatsAppMessage.setUp
	_graph = twm.TestWhatsAppMessage._graph
	_sent = twm.TestWhatsAppMessage._sent

	def _message(self, template, **extra):
		return frappe.get_doc({
			"doctype": "WhatsApp Message", "type": "Outgoing", "to": "919900112290",
			"message_type": "Template", "content_type": "text", "template": template,
			"whatsapp_account": "Test WA Msg Account", **extra,
		})

	def tearDown(self):
		twm.TestWhatsAppMessage.tearDown(self)
		for name in frappe.get_all("WhatsApp Templates", filters={"name": ["like", f"{PREFIX}%"]}, pluck="name"):
			frappe.db.delete("WhatsApp Templates", name)

	def test_explicit_values_go_out_in_numeric_order(self):
		self.wamid = "wamid.tv_order"
		tpl = _template(f"{PREFIX}send_order", "Tu {{3}} (folio {{2}}), {{1}}.")
		self._message(tpl, body_param=json.dumps({"3": "Moto G", "1": "Ana", "2": "RO-1"})).insert(ignore_permissions=True)
		params = [p["text"] for p in self._sent()["template"]["components"][0]["parameters"]]
		self.assertEqual(params, ["Ana", "RO-1", "Moto G"])

	def test_mapping_is_resolved_on_the_reference(self):
		self.wamid = "wamid.tv_ref"
		tpl = _template(f"{PREFIX}send_ref", "Hola {{1}}", field_names="first_name")
		self._message(tpl, reference_doctype="User", reference_name="Administrator").insert(ignore_permissions=True)
		params = [p["text"] for p in self._sent()["template"]["components"][0]["parameters"]]
		self.assertEqual(params, [frappe.db.get_value("User", "Administrator", "first_name")])

	def test_a_missing_value_blocks_the_send(self):
		tpl = _template(f"{PREFIX}send_gap", "Hola {{1}}, folio {{2}}", sample_values="Juan,REP-2026-0001")
		with self.assertRaisesRegex(frappe.ValidationError, "cannot be filled"):
			self._message(tpl, reference_doctype="User", reference_name="Administrator").insert(ignore_permissions=True)
		with self.assertRaisesRegex(frappe.ValidationError, "cannot be filled"):
			self._message(tpl, body_param=json.dumps({"1": "Ana", "2": ""})).insert(ignore_permissions=True)
		self.http.assert_not_called()
