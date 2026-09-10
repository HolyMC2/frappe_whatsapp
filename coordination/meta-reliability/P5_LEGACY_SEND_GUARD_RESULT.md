# P5 WhatsApp legacy send fence — 2026-09-10

Existing native customer conversations now require the real native dispatch capability at both WhatsApp transport boundaries. Legacy sends to a peer without a native conversation retain their existing route while holding the same conversation fence through HTTP. This package creates no conversation, person, consent, or intent and performs no nested commit/rollback.

## Source ownership

- CRM: new `crm/api/outbox_legacy.py`, new `crm/tests/test_outbox_legacy.py`.
- WA: new `frappe_whatsapp/legacy_outbox.py`, narrow message hooks in `frappe_whatsapp/transport.py`, new `frappe_whatsapp/frappe_whatsapp/tests/test_legacy_outbox.py`.
- Approved existing-test corrections: WA `test_transport.py` uses an actual valid message fixture and the physical request boundary; CRM `test_outbox_delivery.py` explicitly represents a historical row without the new provider key.
- No core outbox/controller, receipt hook, account-health, Marketing, Doco, or schema implementation edits. No commit, FF, deployment, migration, production mutation, or real provider call by this worker.

## Behavior

`guard_legacy_send(provider, account_id, peer_id, payload=...)` acquires the existing connection-owned conversation fence, performs a current locking identity lookup, and holds the fence through the caller's HTTP attempt. Human (including its current owner), Bot, Paused and Closed all deny ordinary legacy sends. The only exception checks the actual private `outbox._dispatch` tuple and calls real `require_dispatch` to revalidate the current Submitting intent, claim token, exact payload and eligibility. Flags, document provenance, roles and private-peer prefixes confer no grant.

An absent conversation remains absent. This preserves private staff-assistant routing without inspecting transcripts or creating customer identities. Existing private registrations cannot override an existing customer control row. CRM being absent preserves legacy compatibility; an installed but unavailable/broken CRM control schema holds the send.

Both `api()` and `raw()` inspect final JSON and the exact fixed HTTPS Graph/version/numeric phone-account `/messages` URL; `post()` delegates to `api()`. The exact unique current account row must match the selected account, be Active/Live and have the same version. Recipient strings are exact ASCII numeric provider IDs. JSON is frozen to canonical bytes, bounded to 64 KiB, depth 12, and 2,000 values, and rejects duplicate keys, mixed bodies, ambiguous recipients and unknown message shapes. Graph batches, method overrides, oversized or opaque/form pair bodies, multipart batch fields, byte keys and form/query bracket notation cannot hide behind the administrative path. Existing single-file media/Flow uploads retain their path.

The message-only `api()` branch uses the physical request function inside `transport.py`, with redirects disabled, connect/read timeout 5/20 s, streaming and the native gateway's 64 KiB response reader/deadline checks. It preserves a parsed Meta-shaped success dictionary only after exact recipient/message-ID evidence. Explicit Graph 4xx errors expose a static failure; redirects, 5xx, timeouts and malformed/oversize responses expose uncertainty. Existing `integration_request` error consumers receive only static data, including pre-HTTP rejection (a stale previous response is replaced). The helper does not log raw provider responses, request bodies, tokens or exception text. Other administrative API calls retain their Frappe helper.

Raw message calls retain their Response-like interface with no redirects, streaming and 5/20 s timeouts. The existing native gateway remains responsible for bounded response classification. Demo calls stay no-effect and return visibly synthetic IDs; they do not produce live native acceptance.

Generic read/presence payloads are held as `legacy_action_recipient_unverified`: this seam cannot prove the recipient from a read receipt's message ID. They are not treated as arbitrary permitted `/messages` administration.

## Atomicity and the observed MariaDB conflict

The first two-process proof showed a real `QueryDeadlockError` when a request primed an absent repeatable-read snapshot, waited for first-open/take, then tried the current locking read. The underlying numeric error is MariaDB 1020. Initial diagnostic evidence is retained in `p5-legacy-guard-initial-conflict.log`.

The final behavior is a pre-HTTP `legacy_control_conflict`, not a guessed absence or a nested rollback. The caller owns ending the transaction and starting a fresh request. The five-process proof verifies:

1. A legacy send already inside its physical HTTP double holds the absent-row fence; actual `get_or_create` plus `take` cannot complete until the HTTP double returns. Earlier caller writes survive the guard.
2. First-open/take winning the fence produces zero HTTP from the old-snapshot sender; the log retains error 1020 and its static conflict reason.
3. A fifth, fresh request sees the committed control row and gets `native_outbound_intent_required`, not a repeated generic failure.

One physical double and zero live provider attempts occur. Fictional conversations are closed and the fictional account is disabled after each proof; no fixture users are created. Core authority/fence functions and DB transactions are real, not mocked.

## Validation

- Final expanded suite: **252/252 passed in 3.580 s**: all 219 previously accepted WA/coexistence/native gateway/activity/delivery/pipeline regressions plus 33 new guard cases.
- Final five-process proof: **PASS**, including numeric 1020, preserved prior work and fresh-request denial.
- Python syntax compilation and focused `git diff --check`: passed.
- All SQL tests use the authorized isolated `meta-reliability-test-20260910.lab.xoloitzcuintles.com` site and `/tmp/meta-wa-20260910` candidate overlay. Network/SMTP are blocked; the standard suite rolls back fixtures. No shared site configuration flags were changed by this package.

Evidence:

- `p5-legacy-guard-regressions.log` — final 252-case result.
- `p5-legacy-guard-processes.log` — final five-process proof.
- `legacy_send_concurrency.py` — reproducible process proof.
- `p5-legacy-guard-focused.log` — earlier 31-case focused checkpoint (superseded by expanded final result).
- `p5-legacy-guard-initial-conflict.log` — original unexpected conflict and stack/class diagnosis.
- `p5-legacy-guard-schema-fixture-failure.log` — initial 250-case run's sole failure: new provider-key uniqueness rejected an old fixture's second duplicate Accepted target. The root-approved fix nulls only the first fixture's key to model pre-column historical data, preserving runtime uniqueness and ambiguous-delivery assertions.

Reproduction from `~/muelle-host/muelle`:

```sh
docker compose exec -T -e META_LAB_SITE=meta-reliability-test-20260910.lab.xoloitzcuintles.com backend /home/frappe/frappe-bench/env/bin/python /tmp/meta-wa-20260910/coordination/meta-reliability/run_tests.py frappe_whatsapp.frappe_whatsapp.tests.test_webhook_signature frappe_whatsapp.frappe_whatsapp.tests.test_webhook_receipts frappe_whatsapp.frappe_whatsapp.tests.test_transport frappe_whatsapp.frappe_whatsapp.tests.test_demo_inbound frappe_whatsapp.utils.test_conversation_webhook frappe_whatsapp.utils.test_webhook frappe_whatsapp.frappe_whatsapp.tests.test_coexistence frappe_whatsapp.frappe_whatsapp.tests.test_native_outbox crm.tests.test_conversation_activity crm.tests.test_outbox_delivery frappe_whatsapp.frappe_whatsapp.tests.test_customer_activity frappe_whatsapp.frappe_whatsapp.tests.test_delivery frappe_whatsapp.frappe_whatsapp.tests.test_receipt_pipeline crm.tests.test_outbox_legacy frappe_whatsapp.frappe_whatsapp.tests.test_legacy_outbox
docker compose exec -T backend /home/frappe/frappe-bench/env/bin/python /tmp/meta-wa-20260910/coordination/meta-reliability/legacy_send_concurrency.py
```

## Limits

This closes bypasses through the two WA transport functions for conversations already controlled by the native broker. It does not migrate every legacy producer, authorize automation, create durable intents for uncontrolled legacy sends, or change those callers' retry policies. `automation_ready` stays closed. Doco's direct storefront OTP POST and Marketing's direct DM POSTs remain outside this package; they need their separately assigned finite adapters. No live provider delivery claim is made. Response deadlines are checked around streamed chunks, with socket timeouts; this is not a separate hard process deadline. Existing producers may hold their own unrelated row locks before reaching the transport seam; a conflict safely prevents HTTP and leaves transaction handling to their caller.
