# P5 WA transport — bounded read-only design

Status: source inspection only; no controller, producer, transport or outbox
bridge implementation. Core owns the durable intent lifecycle and will provide
the final callable signatures. This proposal reads the candidate private
`CRM Outbound Intent` schema; it does not assume an unimplemented API exists.

## Boundaries

1. Compose and validate the exact provider payload before queueing. Freeze the
   explicit WhatsApp Account identity, phone ID, peer, content, template language
   and rendered parameters, reply target, interactive/flow data and generated
   flow token. Do not put bearer credentials into an intent. Source documents and
   template definitions must not be re-rendered by the dispatch worker.
2. The CRM service owns action identity, payload hash, actor authorization,
   conversation generation, policy, claim/lease, the durable Submitting boundary
   and recovery. WA supplies bounded payload validation and one provider attempt.
3. Core acquires the conversation fence before intent/message row locks and keeps
   it across the Submitting commit and submission. WA's durable external-origin
   guard uses a current locking read inside that order. The provider adapter has
   no commits, rollbacks, enqueue, global account resolution or ownership changes.
4. The dispatcher must grant a private in-process capability bound to the exact
   intent, account and frozen request for that attempt. Caller JSON, document
   flags, a claimed user/role, or `ignore_permissions` cannot grant it. A generic
   unbound token that authorizes an arbitrary URL/body is insufficient.
5. At dispatch, re-read the exact account; require a unique active matching phone
   ID and the expected account record/shop/app scope. A changed/removed account,
   absent CRM/schema, missing credentials or payload/scope mismatch fails closed.
   Never choose a default account or silently resolve a different one. Build the
   Graph URL internally from a validated version and exact phone ID on the fixed
   provider host. Obtain current credentials only for that account.

## Existing paths requiring integration

| Current path | Required treatment |
| --- | --- |
| `WhatsAppMessage.before_insert → send_outgoing → notify` | Save/queue frozen intent; no synchronous provider submission. |
| `WhatsAppMessage.send_template` and module `send_template` | Freeze actual template name, language and all resolved components before queueing. |
| `WhatsAppNotification.notify` | Currently sends before creating its message row and may choose a global default; bind the account and source action before durable queueing. |
| `BulkWhatsAppMessage.resend_single_message` | Currently clears provider ID and calls `send_outgoing`; recovery must use authoritative intent state and preserve accepted/unknown evidence. |
| `WhatsAppMessage.send_read_receipt` | Shares `/messages` with message submission; require its own bounded authorized action or explicit hold. A `status=read` body must not become a generic bypass. |
| `transport.api/raw/post` | Reject message egress unless bound to the private dispatcher attempt, including direct legacy callers. |
| Template, Flow, account and media administration | Separate operations also use transport POST; preserve their existing authorized behavior rather than granting them message-send authority. |

The current receipt guard remains useful but is insufficient outside a receipt
worker. The choke-point gate must run before Demo emulation too; otherwise a
forged caller could appear successfully dispatched. Demo must be visibly distinct
from a provider acceptance and cannot silently substitute for a Live account.

## Media boundary needing a concrete decision

`_upload_local_media` currently uploads/transcodes while building the message,
changes content type, and falls back to a link after failure. Those operations
cannot remain an implicit mutable dispatch-time renderer. A bounded first slice
can freeze supported provider media IDs or validated explicit media links and
hold other media with a safe actionable reason. Supporting local uploads needs
an explicit durable preparation phase with stable source bytes/content hash and
fixed resulting payload before the message enters Queued. No upload or fallback
should occur after a message attempt becomes Submitting.

## Attempt result contract proposal

Return a small structured result; never return a raw Response, exception text,
request headers, URL with credentials, integration-request body or provider error
message. Core decides state transitions and retry policy.

| Evidence | Result meaning |
| --- | --- |
| Valid success response with one bounded provider message ID | Accepted, with that ID; does not claim delivery/read. |
| Definitive bounded Graph rejection response | Rejected with an allowlisted reason/code; core may distinguish retryable rejection from final failure. |
| Timeout, connection loss, unreadable response, ambiguous HTTP failure, or success without a valid ID | Unknown; never an automatic retry of the submission. |
| Scope, policy, capability, configuration or payload failure before HTTP | Blocked/Failed with a static safe reason and proof no request was attempted. |

The adapter should make one fixed-host request with explicit connect/read limits,
no redirects, bounded response consumption and strict acceptance parsing. A
generic exception must not be converted to a confident rejection. Current
`notify` handlers that inspect/log `frappe.flags.integration_request` need to be
removed from this dispatch path so they cannot erase the distinction.

## Focused proof plan after signatures arrive

- Frozen text/template/interactive payload survives edits to the originating
  message, template, reference or flow while queued.
- Generic insert/save, direct notify/transport, bulk retry and forged flags cannot
  bypass the capability. Persisted external origin still blocks cleared markers
  and forged new-document state, including a current-read race.
- Exact account/peer binding rejects account reassignment, duplicate phone scope,
  cross-shop/app drift, missing credentials and global-default fallthrough.
- One attempt returns Accepted only for valid evidence; Graph rejection is
  separate from timeout/ambiguous response Unknown, with sanitized results.
- Core integration proves generation cancellation, claim competition,
  Submitting crash recovery and the shared conversation-first lock order.
- All tests use fake provider responses and actual isolated SQL where meaningful;
  no provider mutation or live-send claim is part of this design work.
