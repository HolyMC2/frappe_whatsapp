# WhatsApp template variables: one contract for both channel modes

`frappe_whatsapp/template_vars.py` answers one question for every sender: *which
values go into `{{1}}`, `{{2}}`… of this template for this record?* The Cloud API
send (`WhatsApp Message.send_template`) and every manual wa.me prefill call it, so
`api` and `manual` mode cannot disagree.

## Contract

- **Mapping** = `WhatsApp Templates.field_names`: comma-separated tokens in
  placeholder order; token *i* fills every `{{i}}` in the body, wherever it
  appears (`entregado` prints `{{3}}` before `{{2}}`, `pedido_pagado` `{{2}}`
  before `{{1}}`). `body_param` is always emitted in numeric order.
- **Token** = a context key that an app resolves for its doctypes (hook
  `whatsapp_template_context`), or a fieldname / one-level link path of the
  record (tenant mappings written before the keys existed keep working;
  passwords, tables and hidden fields are never read). `key:number` prints an
  amount without its currency symbol, for bodies that carry `$…MXN` themselves.
- **Formatting**: amounts with the record's (or the site's default) currency via
  `fmt_money`; dates as the site language's long date (babel); the rest as text.
- **Defaults**: apps ship a mapping per Meta template name (hook
  `whatsapp_template_defaults`). It fills `field_names` only when it is empty
  and the body has exactly as many variables (on insert, on Meta fetch, and on
  every migrate via `after_migrate`), and stands in for an empty mapping at
  resolve time. A tenant's own mapping is never overwritten.
- **Missing value**: never an empty parameter and never a Meta sample value.
  API send → `frappe.throw` with the named value; review queue → row `Fallido`
  `No enviado [missing_values]: …`; Taller auto-notify → no send, Comment
  `tracker-wa:unfilled:<status> — <reason>` shown in the order's «Avisos al
  cliente»; manual prefill → `{{n}}` stays visible, `missing` lists it and no
  wa.me link is offered.

| Hook entry | Doctype | Keys |
|---|---|---|
| `taller.services.wa_template_context.REPAIR_ORDER` | Repair Order | customer_first_name, customer_name, repair_order, device_model, tracking_url, diagnosis, quote_amount, promised_date, parts_eta, days_in_shop, pickup_deadline, warranty_expires_on, shop_name |
| `taller…CRM_DEAL` | CRM Deal | the repair keys, from the Deal's latest Repair Order (folio = order, never the deal id) |
| `crm.api.whatsapp_template_context.CRM_DEAL / CRM_LEAD` | CRM Deal, CRM Lead | customer_first_name, customer_name |
| `doco.docoutils.whatsapp_template_context.SALES_ORDER` | Sales Order | order_id, order_total, customer_name, customer_contact, fulfillment |
| `doco_marketing.services.whatsapp_template_context.SALES_ORDER` | Sales Order | customer_first_name, order_short_id, shop_name, checkout_url, order_items_summary, first_item_name |
| `doco_marketing…STOREFRONT_LEAD` | Storefront Lead | product_name, shop_name, product_url |

Why extend `field_names` instead of a new store: it is already the column
`send_template`, the CRM composer's mapping editor (`set_template_field_map`) and
the review queue read, it is per template and per tenant, and a key is just a
token the resolver understands. The campaign steps' per-step `template_params`
(doco_marketing) stay as they are: a step legitimately maps one template
differently per campaign.

## Shipped mappings

| Template | Tokens |
|---|---|
| orden_recibida | customer_first_name, repair_order, tracking_url |
| equipo_listo | customer_first_name, repair_order |
| entregado | customer_first_name, repair_order, device_model |
| diagnostico_listo | customer_first_name, repair_order, diagnosis, quote_amount:number, promised_date |
| espera_de_pieza | customer_first_name, repair_order, parts_eta |
| recordatorio_recoleccion | customer_first_name, repair_order, days_in_shop, pickup_deadline |
| reparacion_no_viable | customer_first_name, repair_order, diagnosis |
| garantia_por_vencer | customer_first_name, device_model, warranty_expires_on |
| pedido_recibido / pagado / listo / enviado / cancelado | order_id, order_total |
| pedido_apartado | order_id, order_total, customer_name, customer_contact, fulfillment |
| recupera_pedido | customer_first_name, order_short_id, shop_name, order_total, checkout_url |
| recupera_producto | customer_first_name, first_item_name, order_total, checkout_url |
| recupera_carrito | customer_first_name, shop_name, order_items_summary, order_total, checkout_url |
| recupera_corto | customer_first_name, order_total, checkout_url |
| volvio_stock | product_name, shop_name, product_url |
| cupon_registro | customer_first_name, coupon_code, coupon_benefits, coupon_valid_until |
| bienvenida_credito | none: no sender and no credit-sale record to read from (see below) |

## Inventory before this change (doco, 2026-09-29)

Evidence: doco-mirror (prod data as of 2026-08-30) and the code on mainline.
All 20 business templates had `field_names` empty.

| Template | Sender | api mode before | manual mode before |
|---|---|---|---|
| orden_recibida | Taller `tracker_notify` (Recibido, auto review row, explicit body_param) | correct | Taller prompt sent Taller's own free text, not the template |
| orden_recibida, equipo_listo | CRM composer (`get_template_preview` → `send_whatsapp_template`) | **wrong**: prefilled Meta samples; 2 + 25 customer sends carried folio `REP-2026-0001` / link `…/t/abc123` | CRM manual box: every slot `{{n}}`, «Abrir WhatsApp» blocked |
| equipo_listo | Taller supervised review row | correct | free text (see above) |
| entregado | Taller auto row, `TEMPLATE_VAR_TOKENS` | correct incl. `{{3}}`/`{{2}}` order | free text |
| diagnostico_listo | Taller template map row | **never sent**: 5 variables, no token map → silently skipped | generic greeting |
| reparacion_no_viable | Taller template map row | **wrong**: positional `{{3}}` = tracker URL instead of the reason | generic greeting |
| recordatorio_recoleccion | Taller template map row | **never sent** (4 variables) | generic greeting |
| espera_de_pieza | Taller `parts_eta` (`purchase_eta_template`) | **skipped** (3 variables, no token map) | — |
| garantia_por_vencer (not on doco) | Taller warranty sweep | empty `{{2}}` when the order has no device model | — |
| pedido_recibido…cancelado | doco `storefront_notify` (per-shop map, switch off on prod) | correct positional | automatic only; failed with an Error Log when no API account |
| pedido_apartado | doco operator alert | correct; fillers «Cliente», «—», Spanish literals | automatic only |
| recupera_* | doco_marketing abandoned sweep (review rows) | correct for the four known names; any other template → no body_param → sample values read as fieldnames → empty params | queue rows cannot be sent manually |
| volvio_stock | doco_marketing restock sweep | correct | as above |
| cupon_registro | doco_marketing registration (program's `registration_template`) | empty `{{4}}` when the coupon has no end date | as above |
| bienvenida_credito | no sender in any repo (CRM composer only) | composer prefilled samples («Juan», «Samsung A10») | blocked |
| any | Review queue row without body_param | `send_template` read sample values as fieldnames → empty params, Meta rejects | — |

## After

Every sender above calls the contract; the api values and the manual text for
the same (template, record) are identical (`taller/tests/test_wa_template_vars.py`,
`crm/tests/test_whatsapp_channel.py`, `frappe_whatsapp/…/tests/test_template_vars.py`).
Behaviour kept byte-identical where it was correct: Taller's explicit values,
storefront positional values and fillers, abandoned/restock/coupon strings.
Deliberate changes: Taller manual mode prefills the status template (when the
status has one) instead of Taller's free text; a Taller status template that
cannot be filled no longer falls back to free text; `garantia_por_vencer`'s
date prints as a long date.

Not covered (follow-ups): a manual-mode action in the review queue (rows staged
by the marketing sweeps cannot be sent by hand); a sender and data source for
`bienvenida_credito`.

## Production

`scripts/template_mappings_prod.py` previews (default) or applies
(`TEMPLATE_MAPPINGS_APPLY=1`) the shipped mappings; apply refuses to run before
the contract code is on the site. The roll's migrate applies the same rules
through `after_migrate`, so the script is the reviewed preview and the
post-roll check.
