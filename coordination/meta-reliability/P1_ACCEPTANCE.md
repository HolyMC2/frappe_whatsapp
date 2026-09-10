# Durable Meta intake: local acceptance record

Local source acceptance passed, 2026-09-10 UTC. This record distinguishes
implemented intake from later acquisition, automation and production gates.

## Runtime contract

All signed entries, changes, messages and statuses are validated before inserting
any receipt. Unique identity includes provider, signing scope, receiving account,
event type and provider event ID. Full-batch identity locks are sorted; an actual
database deadlock rejects the transaction and must retry in a fresh transaction.
The helper never turns it into a successful acknowledgement or rolls back earlier
caller work. A duplicate identity preserves the original payload.

HTTP admission does no remote lookup or customer send. Receipt commit precedes
enqueue. The minute sweeper recovers lost enqueue and expired worker claims;
attempts and backoff are bounded. Domain writes commit with Processed. A failed
worker retains its receipt and a content-free reason code. Generic document APIs
cannot create, edit, replay or delete receipt rows. Raw evidence is private to
System Manager; it is not a customer transcript endpoint.

Messenger profile and FB/IG mention/story enrichment use child receipts committed
with the core message/mention. Their failure does not erase the core record.
Missing or expired story media can remain unavailable; retained failure evidence
is not a promise to recover expired content. WhatsApp media currently retries its
individual message atom: the durable raw receipt survives even when that message
projection waits for successful retrieval. Sibling messages remain independent.

WhatsApp status folding locks the exact account's outgoing message and preserves
delivered/read evidence against reordered sent/failed callbacks. Other delivery,
handover, account-health and advanced events are retained with explicit unsupported
outcomes until their respective slice implements them. A subscription alone does
not make those events supported.

## Compatibility and deliberate holds

- Schema commits are additive: WA `fb76c8d`, marketing `61cb7ab`. No historical
  backfill, person merging, consent grant, record deletion or bot auto-enable.
- Doco `6e85377` keeps replayable WhatsApp intake outside the private staff
  assistant. Its settings, execution identity and business actions are not
  entered from receipt/replay context. Desk assistant behavior is unchanged.
- WhatsApp transport rejects provider mutations in receipt/replay context.
  Marketing menu and Chatflow execution are held there until the native outbound
  intent/ownership boundary exists. Saved configuration is preserved.
- Receipt-driven comment auto-lead is held. Lead Ads events are retained as
  Ignored / acquisition_policy_required pending the form-specific Inquiry slice;
  it must provide explicit guarded reprocessing because Ignored is terminal.
  Legacy direct handlers are not a safe acquisition path to enable by default.
- Unversioned comment edits/visibility events and mention enrichment cannot
  overwrite already stored content merely because they arrived later. Removals
  preserve tombstones. Raw event evidence remains available for reconciliation.
- Reaction arrival-order deltas are not folded into a supposedly accurate total.
  Their raw receipt remains unsupported pending a justified projection.

## Evidence and limits

The isolated site is `meta-reliability-test-20260910.lab.xoloitzcuintles.com`.
Started with exactly Frappe+WhatsApp; subsequently installed CRM, Doco, ERPNext,
scanner_kit, mercado and marketing for integration checks. Both guarded migrations
resolved every installed app with zero orphans. Complete retained logs reached
after_migrate without traceback or orphan DocType deletion; database inspection
found zero Deleted Document rows for DocType. Maintenance, scheduler pause and
email mute remain enabled. Production sites were not migrated.

- `p1-regressions.log`: 94 passing WhatsApp SQL/signature/Demo/transport checks,
  including the receipt/replay egress guard; focused transport results are retained too.
- `p1-concurrency.log`: actual independent processes prove reversed overlapping
  batches, duplicate delivery with earlier writes retained, two workers/one effect,
  failed enrichment recovery, crashes after claim and local writes, and exhausted
  lease recovery. The consumer in this lifecycle harness writes a fictional ToDo;
  it does not stand in for the real provider adapter proof below.
- `p1-actual-consumer.log`: actual signed Guest WhatsApp admission and actual
  consumer/controllers persist text/media, fold read→delivered→sent monotonically
  and replay without duplicate effects. All external submissions are blocked.
- Marketing's `coordination/meta-reliability/CONSENT_RESULT.md` records the actual
  chain-head defect, passing consent regressions, committed signed Guest FB/IG
  consumers, profile recovery, opt-out and independent append-race proof. Existing
  ledger hashes and consent meanings remain unchanged.

Marketing's `p1-social-regressions.log` records 189 passing combined social,
adapter, consent and private-assistant tests, including bounded story downloads
and unversioned-update preservation. Story download policy currently accepts
only HTTPS/443 `lookaside.fbsbx.com/ig_messaging_cdn/`, refuses redirects, caps
streams at 10 MiB and validates media. Other CDN shapes remain unsupported;
actual provider URL delivery is a separate controlled test.

Marketing's `social_concurrency.log` records the final passing three-scenario,
13-process Guest-worker proof after earlier diagnostic runs. The forced
mention-absent-read → concurrent comment commit → mention resume encountered
MariaDB error 1020: the failed worker retained its receipt and rolled back its
partial mention; a fresh worker converged on attempt two. The reverse order
retired the canonical mention, and exact redelivery changed no receipts or
projections. All proof-owned fixtures were cleaned. This is evidence of safe
conflict recovery, not a claim that database conflicts never occur.

No claim of production activation, real Meta event delivery, browser verification
of a new chat UI or complete P3/P4/P5 capability is made.

## Rollout / rollback gate

Install the additive schema under the guarded migration procedure, then activate
the matching WA/marketing/Doco runtime bundle across backend and workers. Verify
loaded fingerprints, scheduler recovery and existing configuration before opening
intake. Missing schema fails admission instead of discarding events. Activation
requires the P0 app-secret/account configuration and controlled real delivery
proof in P0_ROLLOUT.md; current production app secrets are still absent.

On rollback retain strict P0 authentication, private receipt tables, pending and
failed evidence and the Doco receipt guard. Stop incompatible consumers; do not
drop evidence tables or return to unsigned synchronous intake. Existing terminal
Ignored events require explicit versioned reprocessing when a later supported
consumer is enabled. No customer sends, subscriptions, production config changes,
source push or deployment have been performed by this local package.
