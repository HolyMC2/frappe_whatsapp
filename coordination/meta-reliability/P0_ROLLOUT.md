# META-00: WhatsApp authenticity candidate

Local source acceptance, 2026-09-10 UTC. Production activation is **not authorized or performed**.

## Verified behavior

The POST boundary hashes the raw request bytes, requires a configured matching live app secret, rejects ambiguous app-secret reuse, and validates every entry/change against current Active/Live account app/WABA/phone mappings before calling an ingest consumer. Missing/tampered/malformed signatures, foreign accounts and absent HTTP context fail closed. The parsed signed bytes are the only payload consumed; form_dict cannot substitute data. Batch account validation finishes before the first write. Phone events require phone metadata. Account/template events are limited to the authenticated WABA's configured accounts. Templates and outgoing-message status updates are account-scoped. All entry/change/status arrays are traversed; durable recovery is the subsequent META-01 package.

Demo simulation uses the same scoped consumer behind repeated System Manager and Demo-account checks. It does not weaken HTTP authentication. No schema columns, indexes, subscriptions or automation flags are added by this package. The account field description changes to describe mandatory authentication.

## Current production facts

[Read-only metadata](production-readonly-20260910.json), observed 03:38:56 UTC, confirms both Active WhatsApp accounts have **no app secret**. Both map to app `2082376078930469` and WABA `2954965221375122`; phone IDs differ. The Page still has seven subscriptions and lacks `mention`, receipt and handover fields. Instagram account subscription inspection returned error 100; delivery is unverified.

[Second app inventory](second-app-readonly.json) identifies the second app as **Doco Chatwoot Connector** (`1418834030282521`); the configured app is **erpnext connector**. Its presence alone establishes neither operator ownership nor permission to detach it. Keep both attachments. No production event payload/customer messages were read. Graph calls were GET-only with DB session read-only; no secrets were emitted.

## Controlled activation sequence (requires separate production authorization)

1. Verify the actual app ownership and callback topology, including the second WABA app and legacy Facebook user callback. Inventory all tenants using this receiver; identify their app/WABA/phone mappings and enabled live accounts. The legacy user subscription is unsupported by this WhatsApp receiver; assess migration to a separate receiver or scoped removal separately, never widen WhatsApp trust to accept it.
2. Obtain the real secret for the already configured app through the established secret channel. Save each relevant WhatsApp Account's Password field through the Frappe document/password API, never `set_single_value` or plaintext SQL. Confirm only presence and matching app identity in diagnostics. Do not automatically copy Messenger's secret based on mere availability.
3. **Existing code begins enforcing site-wide signatures as soon as the first secret is configured.** Stage/verify all app mappings and secrets, then coordinate this configuration boundary and process/cache refresh during the approved window. Test a controlled genuine Meta-signed inbound from an operator-owned test contact. A synthetic HMAC unit test is not this delivery gate.
4. Deploy the reviewed exact source commit through the supported refresh process and coordinated restart. No DDL is needed for META-00; if metadata synchronization is needed, use the guarded migration wrapper and preserve its complete log. Check loaded source fingerprints across backend and workers.
5. Verify signed controlled delivery, unsigned/tampered POST rejection before log/message writes, foreign-account denial, GET challenge, multiple phone routing and account-scoped statuses. Do not send customer messages. Retain counts/timestamps/opaque receipt IDs; keep message bodies and secrets out of generic evidence.
6. Watch rejection/enrichment/processing counts. Actual delivery proof, production config approval and the rollout receipt are unresolved gates. Do not expand subscriptions or bots on the strength of local tests.

Rollback: retain the configured secrets and this strict boundary, or keep the callback unavailable while restoring a reviewed compatible build. Returning to the old no-secret acceptance path is not an acceptable security rollback. Restoring code cannot revoke external effects already submitted. Later receipt-schema rollout has a separate compatibility/rollback gate.

## Local evidence

`p0-regressions.log`: **79 passing tests**, real Frappe 16.31/MariaDB lab context, candidate imports from `/tmp/meta-wa-20260910`; requests/SMTP blocked and DB commit patched with final rollback. Includes 27 isolated signature/scope checks and existing database-backed webhook/demo/transport tests. These tests prove local receiver behavior, not provider delivery or production deployment. Syntax compilation and `git diff --check` pass. No frontend runtime code changed; browser/PWA acceptance remains a later UI slice.

Reference: Meta's [webhook authentication implementation contract](https://whatsapp.github.io/WhatsApp-Nodejs-SDK/api-reference/webhooks/start/) and [payload reference](https://www.postman.com/meta/whatsapp-business-platform/folder/tduohwq/webhook-payload-reference). The SDK reference is archived; current account/product delivery remains a live rollout check.
