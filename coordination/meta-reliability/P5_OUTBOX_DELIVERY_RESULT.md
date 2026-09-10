# P5 native intent delivery evidence

Implemented new CRM `api/outbox_delivery.py`, WA `delivery.py`, and their focused
tests, plus the bounded native/legacy status hookup in WA `utils/webhook.py`.
Root's `outbox.py`, `outbox_policy.py` and the message controller were not edited
by this worker. No schema, commit, provider request,
migration, restart or production change was performed.

## Trust and correlation

`apply_delivery_receipt(receipt_name, expected_entry=None, account_records=None)`
requires a real Processing WhatsApp status receipt, at least one actual attempt
and an unexpired lease in a background worker. The lease is checked again under
the conversation fence before domain writes. It
rechecks the stored identity key, payload hash, one status atom, exact receiving
account, recipient, provider message ID and optional caller entry/account list.
HTTP invocation, a flag without matching durable evidence, altered atoms,
ambiguous scope, history/message bodies and future/invalid timestamp evidence
cannot grant a delivery mutation.

It acquires the derived conversation fence **before** native intent row locks.
Exactly one intent must match provider + account + peer + provider message ID.
The current Active/Live receiving account's App/business identity, the intent's
frozen account revision and the conversation's current account/shop snapshot
must agree. It does not renew send eligibility or require the old human owner:
actual delivery can still be recorded after a transfer/release. Private customer
scope exclusions remain enforced.

No identity, WhatsApp Message or native intent is created. Unknown intents with
no stored provider ID cannot be inferred from payload, recipient similarity or
time. Missing targets return `matched:false`; the receipt edge must keep a
retryable missing-target outcome when no legacy row exists either. Duplicate
exact targets fail with `native_delivery_target_ambiguous`, without choosing one.

## Monotonic results

All writes use root's `_transition` service. Accepted can become Delivered, Read
or an async Failed. Delivered can only advance to Read. Failed with a known
provider ID can recover to Delivered/Read using root's added transitions. A late
failed/sent/delivered event cannot downgrade stronger evidence. Same-state replay
does not append a state-log entry or overwrite the first observed timestamp.

Delivered/read times come from the actual provider epoch seconds, interpreted as
UTC and converted to the site's timezone. The timestamp guard accepts plausible
seconds from 2000-01-01 through now; it rejects bools, milliseconds, uninitialized
clocks and future evidence. If Read arrives before Delivered, read_at is stored
without inventing a delivered_at value. Raw failed-event time/error content stays
in the private receipt; intent failure reason is only `provider_delivery_failed`.
An async Failed carrying a provider ID cannot enter manual retry.

The helper never sends, commits, rolls back, enqueues or logs raw provider errors.
The receipt worker owns the transaction joining delivery state and completion.

## Implemented WA hookup

The new WA bridge now runs at the **start** of `_apply_message_status`, before
its existing WhatsApp Message lookup/row lock:

```python
native = None
if frappe.flags.get("meta_webhook_receipt"):
    from frappe_whatsapp.delivery import fold_native_delivery
    native = fold_native_delivery(entry, accounts)
```

The bridge validates the actual worker receipt and passes its exact stored entry
and scoped account list to core. Do not accept an arbitrary `matched=True` flag
from a caller. In the existing missing-WhatsApp-Message branch:

```python
if not name:
    if native and native.get("matched"):
        return
    # Preserve the receipt worker's retryable missing-target behavior.
    ...
```

If a legacy row exists, continue the existing legacy update even after a native
match. The legacy lookup includes exact `to=entry.recipient_id` when
processing the verified status, in addition to the current account/provider-ID
filters; that exact recipient predicate is now implemented for verified worker
statuses. The bridge does not itself edit or create legacy rows.

## Verification

On `meta-reliability-test-20260910.lab.xoloitzcuintles.com` with the existing
network/commit-blocked runner and rollback fixtures:

- **18/18 SQL tests passed**. Fixtures traverse the actual HMAC verifier,
  `webhook.post` atomizer and receipt writer, then the actual native intent
  transition service. Coverage includes Guest, native-without-legacy-message,
  provider timestamps, ordering/replay, Failed recovery, no Unknown guessing,
  exact foreign account/peer separation, ambiguity, tampered worker evidence,
  current/frozen account changes, revoked owner independence, fencing and outer
  rollback preserving earlier work.
- **9/9 WA bridge unit tests passed**, covering trusted entry/account forwarding,
  unproven matches, forged context, altered payload, unavailable core/schema,
  and failure propagation without transaction boundaries.
- These **27 delivery tests** passed within the **66-test focused pipeline run**
  and the full **219-test WA regression run**.
  [Focused log](p5-wa-pipeline-focused.log), [complete regression log](p5-wa-pipeline-regressions.log).
- Python compilation and candidate diff checks passed. Four new Python files
  were staged individually into backend `/tmp/meta-wa-20260910`.

Root's staged outbox implementation supplied the Failed→Delivered/Read
transitions; this worker did not patch or replace it.

## Limits retained for review

An account moved to a different App/business or a changed frozen account revision
holds the callback rather than silently redirecting old send evidence. The
existing signature/scoping gate may hold such callbacks before this helper runs.
Those cases require an explicit reconciliation policy; this slice does not claim
automatic recovery across account reconfiguration.

The webhook hookup and actual worker retry path are now verified: missing
targets retain Failed receipts, then the same receipt completes after the exact
target exists. Pipeline tests map commit/rollback boundaries to SQL savepoints;
they do not claim a physical commit/crash proof. Broader producer integration
and final rollout remain root-owned. No live Meta delivery or production
coverage is claimed. See [pipeline results](P5_WA_PIPELINE_RESULT.md).
