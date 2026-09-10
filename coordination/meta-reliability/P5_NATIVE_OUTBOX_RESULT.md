# P5 — bounded WhatsApp frozen-payload gateway

Implemented `frappe_whatsapp/native_outbox.py` and its focused test module.
The controller, Notification, Bulk, read-receipt and shared transport paths were
not changed by this slice. No commit, migration, restart, real provider request
or deployed app mutation was performed.

## Callable contract

`validate_payload(payload, *, account_id, peer_id) -> bytes` is pure. It validates
the complete vendor JSON, exact numeric account/peer, supported type/shape, tree
depth/size and byte budget. It returns canonical UTF-8 JSON or raises
`ValueError("frozen_payload_invalid")`. It does not resolve an account, source,
template, profile, recipient, media upload or credential.

`send_frozen(intent, payload)` first invokes
`crm.api.outbox.require_dispatch(intent.name, "WhatsApp", intent.account_id,
intent.peer_id, payload=payload)`. Missing core or denied authority returns
Blocked before account resolution. The core contract binds canonical payload
bytes and the current Submitting claim token as well as the exact scope; generic
document/receipt flags cannot supply that capability. The real core implementation
remains an integration dependency, mocked in this focused suite.

The adapter reuses the pure validator and reads current WhatsApp Account metadata
with `for_update=True`, requiring exactly one matching phone ID, Active status,
explicit Live mode and a bounded Graph version. It creates the account document
from those current values solely to perform the exact named password lookup;
there is no cache read or default-account fallback. Core continues to own
conversation scope/authority, external-origin exclusions and lock ordering.

Only a fixed `https://graph.facebook.com/{version}/{phone_id}/messages` POST is
issued through the existing `transport.raw` choke point. The configured generic
account URL is not used. Credentials go in the Authorization header only. The
request has connect/read limits of 5/20 seconds, disables redirects and streams
the response. Response bytes are capped at 64 KiB; a 30-second elapsed deadline
is checked before and between stream reads, with each blocked read still bounded
by the read timeout. The response is closed on every path. No request, response
body, raw exception, token or integration-request record is logged by the adapter.

## Results

| Evidence | Returned result |
| --- | --- |
| Valid 2xx JSON, exact single recipient and one bounded non-Demo `wamid.` ID | `{state: Accepted, provider_message_id: ...}` |
| Explicit 4xx JSON Graph error with positive integer code | Failed, static `provider_rejected`; HTTP 429 uses `provider_rate_limited` and `retryable: true` |
| Timeout/connection/stream error, 5xx, redirect, malformed/oversized/ambiguous response, wrong recipient, missing ID or synthetic ID | Unknown, static `provider_response_uncertain`, `retryable: false` |
| Authority, payload or account/configuration failure before transport | Blocked with a static reason and `retryable: false` |

Duplicate JSON keys are rejected. Accepted is submission evidence only; this
adapter does not claim delivery/read, retry a send or alter the durable lifecycle.

The initial shapes cover text, image/audio/video/document ID or HTTPS link,
reaction, rendered templates and button/list/navigate-Flow interactive payloads.
Local paths/uploads, read-receipt bodies, unknown top-level content, arbitrary
extra fields and unverified opaque callback data are refused. Frozen media links
are submitted as metadata; the adapter does not fetch their bytes. A queued local
media preparation strategy remains outside this gateway slice.

## Verification

On the isolated eight-app site
`meta-reliability-test-20260910.lab.xoloitzcuintles.com`:

- **27/27 focused unit-double tests passed** for authority and exact payload
  binding, pure creation validation, frozen bytes, account/token routing,
  malformed and bounded payloads, supported content, safe errors, uncertain
  outcomes, response caps/deadline, duplicate JSON and response closure.
- The existing no-direct-Meta-egress invariant also passed: **28/28 total**, in
  0.055 seconds. [Full log](p5-wa-native-outbox-tests.log).
- Python compilation and `git diff --check` passed.
- Only the two new Python files were copied into backend
  `/tmp/meta-wa-20260910` for tests. The runner blocks external requests and
  commits; this suite performs no database writes.

Exact runner command (from `muelle/`, output retained in the linked log):

```sh
docker compose exec -T -e META_LAB_SITE=meta-reliability-test-20260910.lab.xoloitzcuintles.com backend /home/frappe/frappe-bench/env/bin/python /tmp/meta-wa-20260910/coordination/meta-reliability/run_tests.py frappe_whatsapp.frappe_whatsapp.tests.test_native_outbox frappe_whatsapp.frappe_whatsapp.tests.test_transport.TestEgressInvariant
```

## Integration still required

The gateway is not yet connected to existing producers. The bypass inventory and
media preparation decision are in [the transport design](P5_WA_TRANSPORT_DESIGN.md).
Root integration on 2026-09-10 verified the real core dispatch context, account
revision, claim/body binding and Submitting boundary: 67 combined SQL/gateway
tests passed, including the actual core-to-adapter chain with only final HTTP
doubled. The committed multi-process CRM proof passed request replay, competing
workers, takeover, safe pre-submission recovery and Unknown after a submission
crash; an unrelated receipt could commit while the provider double was held.
See the CRM repository's `coordination/meta-reliability/P5_OUTBOX_ACCEPTANCE.md`
and its retained test/process logs. These prove the new native manual path;
automation readiness still returns false. Subsequent transport/controller integration must prevent old
direct sends and retain the P4 conversation-first locking guards. This report
claims only the bounded gateway and mocked provider behavior, not a complete
outbox rollout or live provider coverage.
