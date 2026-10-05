# Document and quick-reply transport

Insert one `WhatsApp Message` as usual. Template-variable resolution remains in
`template_vars.resolve` / `check_values`, and `notify` still hands the fully built
payload to CRM's native outbox when it governs the recipient. This lane implements
the four requirements of the wa-doc-transport-20261004 brief; window policy,
document permissions/routing, signing, acceptance and replay handling belong to
the consumer apps.

## DOCUMENT header

Set `template` to the approved DOCUMENT-header template and `attach` to the actual
PDF. The template's approval sample is never used as the customer's document.
Set `attach_filename` when a signed URL has no filename or when a generated file
has an opaque storage name. Otherwise the name comes from the site's File row,
then the decoded URL basename (query parameters are excluded).

Local `/files/` and `/private/files/` attachments, including absolute same-site
URLs, upload to Meta as `document` even when the template message's `content_type`
is `text`. The header carries `{ "id": "<uploaded media>", "filename": "<folio>.pdf" }`.
Public local files retain the existing HTTPS link fallback on upload failure.
Private files require successful upload: they never become public link sends.

External HTTPS URLs and signed `/api/` print URLs remain link sends with filename.
The transport does not fetch arbitrary external links. Callers must provide links
Meta can fetch, with a PDF response, and set `attach_filename` for print endpoints.
Local existence, confinement to the site's files directory, PDF signature,
filename and the 100 MB cap are checked before upload. Missing or invalid inputs
produce Spanish recovery instructions before a message is sent.

The legacy whitelisted `send_template` accepts optional `attach`,
`attach_filename`, `buttons` and `whatsapp_account` arguments, and checks read
permission on its reference before saving. The owning document-send app still
must enforce recipient, account, print and business permissions.

## Per-send template quick replies

`buttons` accepts a JSON array (or its JSON serialization):

```json
[{"index": 0, "payload": "doc:<signed-or-opaque-token>"}]
```

Indices identify the final zero-based Meta button positions, including static
URL/phone buttons and the existing product-card offset. Overrides must point to
approved Quick Reply buttons; duplicate or unknown indices and empty/invalid
payloads fail before upload. This transport contract caps outgoing payloads at
128 characters: use a compact signed token or a random opaque reference. Payloads
are passed unchanged; the transport does not invent tokens from record names.
Partial overrides are allowed; omitted buttons keep their legacy label payload.
`None`, `{}` and `[]` preserve legacy behavior. Interactive session messages keep
their existing `[{"id": "...", "title": "..."}]` shape.

## Inbound extension point

After ingestion, `whatsapp_incoming_committed` handlers receive the existing
snapshot plus:

- `button_payload`: `message.button.payload` for template replies, or the
  `button_reply.id` / `list_reply.id` for interactive replies. Long Text storage
  preserves the complete incoming value independently of the outgoing cap.
- `reply_to_message_id`: the provider context id of the message being answered.
- `is_reply`: whether reply context is present.

`message` remains the visible text for template replies and the id for interactive
replies, preserving existing consumers. `from`, `whatsapp_account`, `phone_id`,
`message_id`, row `name` and `live` remain available to handlers. No account token
is exposed. Prefix routing (`sn:` / `doc:`) belongs in one registry in the owning
app, registered through this hook; no second transport registry is needed.

Despite its historical name, this hook executes **inside the authenticated
receipt transaction**. Handlers must persist domain work without committing,
sending, switching users or making provider calls. A truthy return keeps the
existing media-fetch claim meaning. Consumers must verify token signature,
expiry, account scope and normalized sender, and handle repeat taps idempotently.
Use `live` to avoid turning simulated or historical traffic into real actions.

## Verification

The app-local `tests/test_template_transport.py` and `tests/test_button_payloads.py`
use provider/DB doubles. The root `tests/` collectors run those same suites without
Frappe. They cover PDF upload and link building, pre-send guards, native handoff,
existing freeform sends, webhook JSON fixtures, legacy labels/interactive ids,
and `sn:` / `doc:` payload round trips into the existing hook.

Meta payload shape references: [template components](https://www.postman.com/meta/whatsapp-business-platform/request/lwtlz1k/send-message-template-interactive)
and [document id/link and filename](https://whatsapp.github.io/WhatsApp-Nodejs-SDK/api-reference/types/DocumentMediaObject/).

## Lane handoff (2026-10-04)

Local source verification: 152 bench-free tests and 103 subtests passed, including
both new transport suites, native bridge, gateway/signature and receipt regression
suites. Seven changed Python files passed `py_compile`; the DocType and webhook
fixture JSON parsed successfully; `git diff --check` passed.

The full local collection also reported four CRM contract setup errors because
`CRM_SOURCE` was not configured; those checks must run against CI's pinned CRM
checkout. Ruff was unavailable in the system and reused tooling interpreter.
No Frappe migration, disposable-site suite, real Meta request, lab or production
operation was performed. The PM must migrate the new schema on a disposable site
and verify the document template with its approved account before release.

## Window evidence, private session documents and the outgoing default (wa-documentos-20261004)

- `frappe_whatsapp.window` is the one customer-service-window predicate.
  `evidence(phone_id, app_id, peer, lock=...)` reads only Processed `Meta Webhook
  Receipt` rows for that business number and app, with the provider timestamp in
  `(now - 24 h, now]`: future, malformed, unprocessed and exactly-24-hour-old
  evidence never counts. `is_open(account, number)` is the plain-read preview
  (`open`, exact `peer`, `closes_at`, `reason`). CRM's dispatcher uses the locked
  form. `peer_candidates()` accepts only Mexico's 52/521 mobile alias; every other
  number is matched exactly.
- Session (`content_type=document`) sends of a site-private file require a
  successful media upload; a failure refuses the send with a Spanish retry
  reason instead of falling back to a link. The display name comes from
  `attach_filename`, then the File row, then the URL basename.
- `utils.outgoing_default()` makes WhatsApp Settings the outgoing authority: an
  existing Settings choice wins even when inactive (no silent reroute); with no
  choice exactly one Active `is_default_outgoing` account is adopted, otherwise
  None (setup). Saving Settings moves the flag; flagging an account updates
  Settings. Incoming defaults are unchanged.
- Legacy `send_template` returns `{name, status}`; an `attach` must be a File
  attached to the reference that the caller can read, and every
  `whatsapp_document_send_guard` hook (business apps' sender policy) runs before
  the row is saved. The generic form-menu dialog is Spanish and writes its
  timeline comment only after the server accepted the send, saying whether it
  is queued or sent.
