# P4 customer activity — exact existing control only

Implemented WA `customer_activity.py`, CRM `api/conversation_activity.py`, their
focused tests and the bounded message-receipt hookup in `utils/webhook.py`.
No schema change, commit, migration, restart or provider request was performed.

## Contract and ordering

WA `consume_customer_activity(receipt, scoped)` validates the actual durable
Processing receipt, an actual attempt (at least one), a live lease, its worker
context, canonical identity/hash and single exact message atom. Core independently
requires the live claim again after acquiring the conversation fence. It forwards the actual provider timestamp, account and peer to
core `internal_apply_customer_activity(provider, account_id, peer_id,
receipt_name=..., provider_timestamp=...)`. Core independently validates the
actual receipt and the current unique Active/Live account's App/business scope.
Neither caller flags nor a supplied timestamp can replace stored evidence.

Under the conversation fence, core excludes exact private assistant peers, checks
the existing conversation's current account/shop snapshot and reads its actual
`modified` boundary. No conversation or person identity is created here.

A customer reply can retire the existing Bot grant only when its epoch-second
provider timestamp is **strictly later** than the current control's modified time
and **not in the future**. A same-second tie is conservatively ignored. Receipt
arrival/processing time is never substituted. There is no arbitrary age cutoff:
a delayed reply newer than the grant still stops that grant's pending followup.

Only Bot becomes Human, unassigned, with bot_enabled=0. Human owner, Paused and
Closed controls remain unchanged. Generation advances through the existing core
transition service, immediately invalidating dispatch authority for an older Bot
generation. This helper does not claim it has separately cancelled run/intent
rows; those remain under their owning lifecycle services.

The immutable `customer_reply` control-event command key derives from exact
conversation identity and receipt name. Its input fingerprint includes provider
timestamp and immutable payload hash. Successful holds and ignored ordering
decisions are replayed without mutating newer control. Future evidence ignored
once cannot become a new command merely because time advances. Missing
conversations create neither identity nor a linked control event.

History, echoes, statuses and noncustomer system messages never become customer
reply holds. A generic receipt retry flag does not suppress a first valid hold.
Private peers are excluded without reading their transcript. Missing CRM/schema
returns an explicit unavailable result so raw receipt/projection can be retained;
installed-but-missing runtime or unrelated failures propagate to the outer worker.

## Implemented receipt hookup

In `utils.webhook.consume_receipt`, the hook now runs after existing current
scope validation and unsupported-field branches, but **before** `process_change(scoped)`:

```python
activity = None
if receipt.event_type == "message":
    if len(scopes) != 1:
        raise ReceiptError("customer_activity_account_ambiguous")
    from frappe_whatsapp.customer_activity import consume_customer_activity
    activity = consume_customer_activity(receipt, scopes[0])
for scoped in scopes:
    process_change(scoped)
return {"state": "Processed", "reason_code": (activity or {}).get("reason_code", "")}
```

A helper Ignored result does not discard the ordinary incoming message. Preserve
the existing outer worker transaction: message projection, control hold and
control audit either commit together or roll back together. No nested commit,
rollback, send or volatile-only queue is introduced. Missing provider timestamps fail explicitly and are never replaced with receipt
time. Existing legacy domain tests remain unchanged because they exercise their
explicit non-worker boundary.

## Verification

On `meta-reliability-test-20260910.lab.xoloitzcuintles.com`, using the existing
network/commit-blocked runner and rollback fixtures:

- **17/17 actual SQL tests passed**, covering Guest holds, old-generation refusal,
  idempotency after a newer Bot grant, delayed reply, timestamp ordering/ties,
  future replay, arrival-time irrelevance, preserved human/paused/closed controls,
  no identity creation, private principal exclusion, forged/tampered receipt,
  revoked account scope, and atomic rollback preserving earlier request work.
- **13/13 WA unit tests passed** for the exact trusted atom/core boundary,
  timestamps, mixed/sibling rejection, history/echo exclusion, unavailable core and
  failure propagation without nested transaction boundaries.
- These **30 activity tests** passed within the **66-test focused pipeline run**
  and the full **219-test WA regression run**.
  [Focused log](p5-wa-pipeline-focused.log), [complete regression log](p5-wa-pipeline-regressions.log).
- Python compilation and both candidate `git diff --check` checks passed.

The first run's sole failure was a cached lab Guest User object referencing
concurrent Doco auth-mail code absent from this fixed candidate overlay. Following
root authorization, candidate hooks were verified in the runner's developer-mode
context and only `User/Guest`'s isolated-site document cache was cleared. No
concurrent auth-mail source or hook was copied. The full focused rerun then
passed with the real Guest insert path unchanged.

The owned source/tests and webhook hookup are staged individually in backend
`/tmp/meta-wa-20260910`. Nine additional actual `run_receipt` pipeline tests prove
atomic domain rollback, raw-batch retention and missing-target retry using SQL
savepoint checkpoints in place of physical commits. Root owns final review and
any additional process/crash proofs; see [pipeline results](P5_WA_PIPELINE_RESULT.md).
