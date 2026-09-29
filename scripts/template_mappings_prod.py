"""Preview or apply the shipped WhatsApp template variable mappings on one site.

Nothing here sends a message or calls Meta. From the stack directory (~/muelle on
contavm):

    # dry run — prints before/after per template, writes nothing
    docker compose exec -T backend bench --site <site> console < template_mappings_prod.py
    # apply — only with the explicit flag, and only once the contract code is live
    docker compose exec -T -e TEMPLATE_MAPPINGS_APPLY=1 backend bench --site <site> console < template_mappings_prod.py

Self-contained on purpose: the dry run also works BEFORE the code roll, to review
exactly what the roll's migrate will fill (frappe_whatsapp's after_migrate applies
the same rules: fill `field_names` only when it is empty and the body has as many
variables as the mapping). Apply refuses to run on a site without the contract
(`frappe_whatsapp.template_vars`): the old send path would read these keys as
fieldnames and send empty parameters.

The console reads this file statement by statement (and functions defined there
do not see its globals), so the script is flat top-level code with no blank lines
inside a block. Keep MAPPINGS equal to the apps' DEFAULT_MAPPINGS (taller, doco,
doco_marketing); the script reports any drift when the code is installed.
"""

import os
import re

import frappe

MAPPINGS = {
	# taller.services.wa_template_context
	"orden_recibida": "customer_first_name,repair_order,tracking_url",
	"equipo_listo": "customer_first_name,repair_order",
	"entregado": "customer_first_name,repair_order,device_model",
	"diagnostico_listo": "customer_first_name,repair_order,diagnosis,quote_amount:number,promised_date",
	"espera_de_pieza": "customer_first_name,repair_order,parts_eta",
	"recordatorio_recoleccion": "customer_first_name,repair_order,days_in_shop,pickup_deadline",
	"reparacion_no_viable": "customer_first_name,repair_order,diagnosis",
	"garantia_por_vencer": "customer_first_name,device_model,warranty_expires_on",
	# doco.docoutils.whatsapp_template_context
	"pedido_recibido": "order_id,order_total",
	"pedido_pagado": "order_id,order_total",
	"pedido_listo": "order_id,order_total",
	"pedido_enviado": "order_id,order_total",
	"pedido_cancelado": "order_id,order_total",
	"pedido_apartado": "order_id,order_total,customer_name,customer_contact,fulfillment",
	# doco_marketing.services.whatsapp_template_context
	"recupera_pedido": "customer_first_name,order_short_id,shop_name,order_total,checkout_url",
	"recupera_producto": "customer_first_name,first_item_name,order_total,checkout_url",
	"recupera_carrito": "customer_first_name,shop_name,order_items_summary,order_total,checkout_url",
	"recupera_corto": "customer_first_name,order_total,checkout_url",
	"volvio_stock": "product_name,shop_name,product_url",
	"cupon_registro": "customer_first_name,coupon_code,coupon_benefits,coupon_valid_until",
}
APPLY = os.environ.get("TEMPLATE_MAPPINGS_APPLY") == "1"
try:
	from frappe_whatsapp import template_vars as CONTRACT
except ImportError:
	CONTRACT = None
if CONTRACT:
	SHIPPED = CONTRACT.default_mappings()
	DRIFT = {k: (v, SHIPPED.get(k)) for k, v in MAPPINGS.items() if SHIPPED.get(k) != v}
	print("code defaults match this script" if not DRIFT else f"DRIFT script vs code: {DRIFT}")
if APPLY and not CONTRACT:
	print("REFUSED: frappe_whatsapp.template_vars is not installed on this site; roll the code first.")
	APPLY = None
print(f"[{'APPLY' if APPLY else 'DRY RUN'}] {frappe.local.site}")
COUNTS = {}
for row in ([] if APPLY is None else frappe.get_all(
	"WhatsApp Templates",
	fields=["name", "template_name", "actual_name", "status", "template", "field_names"],
	order_by="name asc",
)):
	mapping = MAPPINGS.get((row.actual_name or row.template_name or "").strip())
	before = (row.field_names or "").strip()
	slots = len({int(n) for n in re.findall(r"\{\{\s*(\d+)\s*\}\}", row.template or "")})
	after, action = before, "no default"
	if mapping and before:
		action = "already set" if before == mapping else "kept (tenant mapping)"
	elif mapping and slots != len(mapping.split(",")):
		action = f"SKIPPED: body has {slots} vars, mapping {len(mapping.split(','))}"
	elif mapping:
		after, action = mapping, ("filled" if APPLY else "would fill")
	if APPLY and action == "filled":
		frappe.db.set_value("WhatsApp Templates", row.name, "field_names", mapping, update_modified=False)
	COUNTS[action.split(":")[0]] = COUNTS.get(action.split(":")[0], 0) + 1
	print(f"  {row.name:<30} {row.status or '':<17} {action:<22} before={before!r} after={after!r}")
print("summary:", COUNTS)
if APPLY:
	frappe.db.commit()
