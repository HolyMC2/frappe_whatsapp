# WA customer activity and native delivery — integrated receipt pipeline

The owned hooks are implemented in `utils/webhook.py`. No production change,
provider request, commit, migration, restart or shared-source replacement was
performed by this worker. Root's outbox/gateway commits and provider transitions
were consumed as dependencies without editing those files.

## Final behavior

- A durable `message` receipt invokes customer activity before ordinary message
  projection. The actual provider time can retire only an older existing Bot
  grant; Human, Paused and Closed controls remain preserved. An Ignored activity
  result still allows the ordinary incoming message to be retained.
- A durable `status` receipt invokes native delivery before any legacy WhatsApp
  Message row lock. Only an exact verified native match satisfies a missing
  legacy row. Otherwise the missing-target receipt remains Failed and retryable.
  When a legacy row exists, the lookup additionally requires its exact recipient
  and the existing update still runs. Wrong-peer legacy rows remain unchanged.
- Both WA helpers and both core helpers require the actual Processing row,
  attempts >= 1 and an unexpired lease, in addition to the existing no-HTTP,
  matching worker flag, immutable identity/payload and exact-scope checks. Core
  checks the lease again after acquiring the conversation fence.
- Domain work and final receipt outcome share the worker transaction. No helper
  commits, rolls back, sends or replaces the durable queue with an in-memory task.

See [customer activity](P4_CUSTOMER_ACTIVITY_RESULT.md) and
[native delivery](P5_OUTBOX_DELIVERY_RESULT.md) for the detailed contracts.

## Verification

On `meta-reliability-test-20260910.lab.xoloitzcuintles.com`, with the existing
network/commit-blocked runner:

| Suite | Passing tests |
| --- | ---: |
| Prior WA signature, receipt, transport, Demo and legacy webhook regressions | 94 |
| Coexistence | 32 |
| Native frozen gateway | 27 |
| Core customer activity + WA activity boundary | 30 |
| Core native delivery + WA delivery boundary | 27 |
| Actual receipt pipeline regressions | 9 |
| **Total** | **219** |

The focused activity/delivery/pipeline run passed **66/66 in 1.751 seconds**.
The complete regression run passed **219/219 in 3.337 seconds**.
[Focused log](p5-wa-pipeline-focused.log),
[complete regression log](p5-wa-pipeline-regressions.log).

All prior 94 tests passed without edits or new boundary mocks. Python compilation
and diff checks passed in both candidates. The ten owned source/test files were
staged individually into backend `/tmp/meta-wa-20260910`; no broad overlay copy
was used.

## What the worker tests actually prove

The new pipeline suite runs the real HMAC verifier, `webhook.post`, receipt
atomizer/writer, `run_receipt`, consumer, core control/delivery helpers and ordinary
message projection. It does not replace a domain handler with a success stub.
Failure injections wrap real projection handlers and raise only after their SQL
writes, proving the worker's rollback behavior.

To leave the isolated site unchanged, physical worker commits are represented by
SQL savepoint checkpoints; a whole-worker rollback restores its claim checkpoint.
Admission and earlier-worker fixtures therefore survive the domain rollback,
while the domain writes really roll back. This is an actual SQL transaction-path
test, not a separate-process crash/physical-commit proof.

The nine scenarios cover:

1. Guest inbound projection and Bot hold completing together.
2. Later domain failure rolling back both message and hold while keeping raw
   receipt, retry schedule and earlier request work.
3. Native-only delivery completing with no legacy WhatsApp Message.
4. Missing target remaining Failed, then the same receipt succeeding on attempt
   two after the exact native target exists.
5. Native plus exact-peer legacy updates while a same-ID wrong-peer row stays
   unchanged.
6. A wrong-peer legacy row failing to satisfy a missing native target.
7. Later legacy failure rolling back both native and legacy delivery changes.
8. A signed four-atom message/media/status batch retaining every raw atom while
   successful siblings complete and media/missing-target failures remain retryable.
9. Processed redelivery retaining one projection without consuming again.

## Remaining limits

These tests exercise fake provider input and blocked egress only. They make no
live Meta subscription/delivery or production claim. Account reconfiguration is
conservatively held by the existing signature scope and frozen account revision.
The helpers do not guess Unknown correlation or create conversation identities.
Final review, rollout and any further physical commit/crash/concurrency proofs
remain root-owned.

One isolated staging request encountered an automatic approval-review timeout;
the tool-authorized single retry succeeded. No action ran during the timeout and
no permission bypass or production access was used.
