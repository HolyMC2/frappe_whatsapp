# Frozen WhatsApp catalog messages

Checked 2026-09-26. `native_outbox.validate_payload` validates wire syntax only. CRM and the catalog owner must independently enforce account/catalog/item eligibility, consent and the customer-service window before creating or dispatching an intent.

| Interactive type | Required fields | Optional fields and limits |
| --- | --- | --- |
| `catalog_message` | `body.text`; `action.name=catalog_message` | `footer.text`; `action.parameters` containing exactly one nonblank `thumbnail_product_retailer_id`. Omit parameters when no thumbnail was chosen. No header. |
| `product` | `action.catalog_id`, `action.product_retailer_id` | Body and footer; no header. |
| `product_list` | Text header, body, `action.catalog_id`, `action.sections` | Footer. 1–10 sections, each with nonempty `product_items`; 30 products total. A section has `title` and `product_items` only; title optional for one section, required for multiple, at most 24 characters. |

Body text is bounded to 1,024 characters, footer to 60 and text header to 60. Product objects contain only `product_retailer_id`. Unknown keys and malformed types fail closed. Duplicate products across the entire selection are rejected under the application contract.

Catalog IDs use the existing numeric-string bound of 40 characters. Retailer IDs have a **local 140-character bound** matching the supported Frappe item-code limit; this is not a claimed Meta limit. Internal spaces and Unicode are preserved. Blank IDs, leading/trailing whitespace and control characters are rejected, without trimming or rewriting a frozen identifier.

## Primary references and verification limits

- Meta's current [single-product Postman request](https://www.postman.com/meta/whatsapp-business-platform/request/syvmul4/send-single-product-message) was accessible and confirms the action fields and optional body/footer.
- Meta's [interactive-object reference](https://whatsapp.github.io/WhatsApp-Nodejs-SDK/api-reference/types/InteractiveObject/), [action-object reference](https://whatsapp.github.io/WhatsApp-Nodejs-SDK/api-reference/types/ActionObject/), and [header reference](https://whatsapp.github.io/WhatsApp-Nodejs-SDK/api-reference/types/HeaderObject/) confirm the product-list shape, section count and text bounds. **That official SDK documentation is archived**, and some surrounding section prose is defective; it is supporting evidence, not a claim of current-version acceptance.
- Current [message reference](https://developers.facebook.com/docs/whatsapp/cloud-api/reference/messages), [catalog-message guide](https://developers.facebook.com/docs/whatsapp/cloud-api/messages/interactive-catalog-messages), and Meta's [multi-product Postman request](https://www.postman.com/meta/whatsapp-business-platform/request/j1w5o6p/send-multi-product-message) could not supply complete schema text in this session: the developer pages were inaccessible and the Postman page rendered a sign-in shell. The catalog-thumbnail omission, 30-product total and conditional section-title rules follow the approved implementation contract; independent current-provider confirmation remains pending.

The tests prove canonical immutable validation, bounded rejection, and compatibility with the existing fake-provider submission path. They do not prove live Meta acceptance or any account's commercial eligibility. No live send is part of this validator change.
