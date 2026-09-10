# P4 — WhatsApp Business app echoes

Implemented in the isolated WA candidate. No provider requests, sends, restarts,
production writes, commits, or migrations were performed by this worker. Root
reviewed and migrated the additive JSON schema separately (`ac0f917`).

## Ingestion and scope

The already authenticated Meta `object/entry/changes` path now decomposes
`smb_message_echoes` from `change.value.message_echoes[]` into durable atoms.
Every sibling is validated before yielding the first atom, and the public receiver
materializes all changes before calling the durable receipt store. A bad later
echo also prevents earlier ordinary message/status receipts from being written.

Validation bounds collection size, exact numeric business/peer numbers, message
ID, epoch-second timestamp, type, text and media metadata. The business sender
must equal the signed metadata's displayed business number after presentation
punctuation removal only; no country, suffix or person-identity normalization is
performed. Mixed message/status/contact/history arrays within an echo change,
wrong-field echo arrays, foreign accounts and malformed content are rejected.

Identity includes the receipt provider/App/account namespace and exact peer plus
provider message ID. Batch reorder/splitting and redelivery preserve identity and
first stored evidence. The worker rechecks the actual durable Processing receipt,
its current account scope, the single atom and exact stored payload before any
projection. Caller-supplied flags alone cannot authorize projection insertion.

## Projection and controls

Supported text, image, audio, video and document echoes become actual Outgoing
WhatsApp Message rows. They retain exact account, business sender, peer and
provider message ID; text/captions are escaped for the HTML editor. The timestamp
is converted from provider UTC to site time. Media retains only ID, MIME type,
hash and optional filename; no media is downloaded and no external URL becomes
an attachment. The complete raw event remains in its existing private receipt.

The three additive fields are `external_receipt` (hidden Data),
`external_sent_at` (Datetime) and `external_media` (Code/JSON); all are read-only
and excluded from copies. A deterministic primary name makes projection replay
stable. Duplicate insert handling rolls back only its savepoint and catches only
Frappe's two unique-collision exception classes; unrelated errors propagate.
Wrong-account/peer/provider-ID/origin collisions fail with
`external_projection_collision` and are never overwritten.

A narrow, non-whitelisted controller seam uses `db_insert` only under a private
identity token bound to that exact in-memory document and a strict value whitelist.
Required defaults, name, owner, timestamps and new-document state are explicit.
This deliberately bypasses generic send, profile, CRM phone-routing and arbitrary
DocType hooks. It does not grant Guest normal create or receipt-read permission.
Ordinary document APIs cannot forge, rewrite, strip or rename external origin.
The actual send method checks persisted origin even if a client clears all
external fields and forges `__islocal`; external rows cannot be sent again.
Persisted origin is read with `for_update=True`, so a repeatable-read snapshot
cannot hide an external row created after the transaction snapshot.

Own-cloud correlation requires exactly one existing local Outgoing message with
the same WhatsApp Account, exact peer and provider message ID, and no external
origin. A matching App ID alone is irrelevant. A unique match creates no duplicate
and leaves conversation authority unchanged. Multiple exact local matches are
conservatively treated as external: the local rows stay unchanged, a single
immutable external projection is stored, and bot authority is held. Operators
receive the static receipt reason `echo_correlation_ambiguous`; this does not
claim that either local row was matched or delivered.

When core CRM is ready, correlation/projection acquires the conversation fence
before message locks, then calls `internal_apply_provider_event` in the same
outer receipt transaction. Uncorrelated external output switches to Human and
invalidates bot generation; no path enables Bot. The core receipt/action key
makes a replay idempotent. A retry flag does not suppress a first necessary hold
after a rolled-back attempt. Callback errors propagate to the durable worker.
Missing CRM or its control schema preserves receipt/projection and reports
`conversation_control_unavailable`, without claiming an ownership change or
starting autonomous behavior.

Unknown echo content types are retained as unsupported; their exact external
peer still receives a conservative hold when core is available. `history` and
`smb_app_state_sync` remain Ignored with `coexistence_sync_unsupported`; they do
not project messages, change control or merge contacts. The BSP-specific history
and state-sync envelope was not added to the public Meta receiver.

## Verification

On `meta-reliability-test-20260910.lab.xoloitzcuintles.com`, using the existing
network-blocked, commit-blocked runner and rollback/savepoint fixtures:

- **32/32 coexistence tests passed** after the final locking-read hardening
  (0.526 seconds), including signed full/mixed batches and
  actual SQL projection, Guest exclusions, metadata, exact correlation,
  ambiguity, collision, duplicate savepoint, retry/replay, core generation,
  immutable resend/edit/rename guards, unsupported types and absent core.
  [Focused log](p4-wa-coexistence-tests.log).
- **98/98 tests passed** for coexistence plus the existing signature, receipt,
  transport and demo suites. [Regression log](p4-wa-regressions.log).
- **28/28 additional existing conversation-webhook/helper tests passed**.
  [Helper regression log](p4-wa-helper-regressions.log).
- Together these cover the new 32 tests and all prior 94 WA regressions.
- Python compilation and `git diff --check` passed. All runtime/test files were
  staged individually into `/tmp/meta-wa-20260910` for root's integration review.

An initial SQL test exposed Frappe treating an explicitly named dict as loaded;
the private seam now explicitly sets new-document state. No assertion or guard
was weakened to pass that test.

## Remaining rollout boundary

The source shape is based on the
[360dialog primary BSP webhook reference](https://docs.360dialog.com/docs/messaging/webhook/webhook-reference.md#coexistence-events),
which shows raw Meta SMB echoes and separate BSP examples for history/state sync.
The official Meta reference was unavailable during the broader task. Controlled
live coexistence delivery remains necessary to verify provider envelope details
and operational behavior before coverage can be claimed. These tests prove local
logic and actual SQL behavior, not a live provider subscription or delivery.

The projection stores safe media metadata only; media retrieval and the richer
conversation UI are separate work. P5 must retain the persisted-origin send guard
when updating the WhatsApp Message controller and use the same conversation-first
lock order for dispatch.
