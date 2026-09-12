# Current implementation state

This document describes the calling-pipeline repair on `codex/implement-complete-repair-of-calling-pipeline`. See the complete operational instructions in [guide.md](guide.md).

## Architecture and persistence

FastAPI retains the dashboard, lead import, browser audio test, Twilio outbound calling and Media Streams, Deepgram conversation, OpenAI extraction, history, statistics, and exports. `SQLiteCallStore` is the active repository. JSON lead/result files are immutable, idempotent migration inputs only.

SQLite uses a fresh connection per operation, WAL journaling, foreign keys, a busy timeout, and explicit transactions. Schema version 3 contains `leads`, `calls`, `call_events`, `call_jobs`, `extraction_jobs`, `suppression_list`, `settings`, `settings_events`, and `schema_metadata`. Version 3 adds the per-call `provider` column (additive, defaulted to `twilio` so legacy rows are correct atomically) and the operator settings store. Additive migrations preserve databases created by the first PR implementation. Versioned legacy migrations import valid JSON and backfill historical DND suppression without modifying source files.

## Queue, concurrency, deadlines, and reconciliation

All single, batch, and direct outbound requests create a durable call/job before provider work. `BEGIN IMMEDIATE`, conditional ownership updates, and database-wide capacity accounting coordinate multiple local Uvicorn workers. The configured default is one concurrent call.

Expired dial claims are never redialed. They enter `NEEDS_RECONCILIATION`, cease consuming global calling capacity, remain operator-visible, and continue blocking their normalized phone. A late verified callback may bind the original CallSid. Known CallSids have durable ring and maximum-call deadlines. One worker leases each due action, verifies provider status, and requests cancellation/completion as appropriate. Capacity is released only by terminal webhook or verified terminal lookup, not by a timer or cancellation request alone.

The coordinator has per-iteration exception boundaries, bounded error backoff, health metadata, orderly claim relinquishment, and controlled handling around adapter construction, dialing, binding, reconciliation, and extraction.

## Telephony providers

Twilio and Exotel are both fully supported. Twilio cannot originate to India with an Indian caller
ID, so Exotel supplies a +91 ExoPhone; Twilio remains the path for everything else. The control
plane (dial, status lookup, terminal request, dial-error classification) sits behind a
`TelephonyProvider` protocol in `app/telephony/providers/`; the media plane is shared in
`StreamingMediaAdapter`, with each carrier overriding only wire framing and message names.

Exotel's stream is 16-bit linear PCM and is transcoded to mu-law at the socket, so the audio
profile, the `.ulaw` caches and all barge-in tuning are shared unchanged. Inbound chunks are
re-framed to 20 ms, with arrival timestamps carried through, so latency metrics stay comparable
between carriers.

The active provider is operator-settable at runtime and stored in the database, since workers share
it. `CALL_AGENT_DEFAULT_PROVIDER` seeds that row and is ignored thereafter. The provider is resolved
once at enqueue and persisted on the call row; every later stage reads it from there, so switching
providers has no effect on queued or in-flight calls. `auto` routes +91 to Exotel and the rest to
Twilio, falling back when the preferred carrier is unconfigured. DND suppression remains global
across providers.

## Correlation and lifecycle

Twilio REST calls use call-specific TwiML/status URLs and the supported ring `timeout` parameter. HTTP callbacks validate the official Twilio signature against `PUBLIC_BASE_URL`. TwiML atomically binds the CallSid and generates an expiring HMAC-SHA256 stream token. Media validates the token, expiry, durable CallSid, terminal state, and exclusive ownership before starting Deepgram. There is no process-local pending-adapter dictionary.

Lifecycle, provider status, media connection/end reason, business outcome, raw persistence, provider terminalization, finalization, extraction, and job state are separate fields. Raw persistence never fabricates provider status or releases an active provider slot. Provider terminalization and raw persistence are independently idempotent and valid in either order. Nonterminal callbacks cannot regress terminal state. Silent connected calls use `media_connected`, not transcript content.

## Extraction, DND, and review

Connected raw persistence transactionally creates an `extraction_jobs` row and WebSocket cleanup returns without inline retries. Coordinator workers atomically lease jobs. `AsyncOpenAI` uses the SDK's network timeout with SDK retries disabled; durable exponential backoff honors the configured delay and survives restart. Success stores the full structured response. Mapping is exact: interest only for `account_creation_interest=yes`, callback only for `callback_approved=yes`, and DND only for `do_not_call_requested=yes`.

Successful DND extraction updates the call and lead and inserts normalized-phone suppression in one transaction. Migration v2 applies the same protection to historical call responses and legacy lead flags. Permanent extraction failure marks only connected leads `review_required` and non-callable.

## Security

HTTP Basic authentication fails closed for `/`, `/api/*`, `/twilio/outbound`, and `/ws`; comparisons are constant-time. Twilio signatures and media HMAC expiry remain mandatory. Twilio/public URL/caller credentials have no unsafe defaults. Lead fields are normalized, bounded, control-character sanitized, serialized as delimited JSON, and explicitly treated as untrusted data. Provider secrets, full tokens, notes, and transcripts are not included in operational error logs.

## Endpoints

- `GET /` — authenticated dashboard.
- `/api/leads*` — authenticated preview/import/manual/list/call/batch/template operations.
- `GET /api/live-calls` — bounded durable nonterminal calls.
- `GET /api/calls?limit=&offset=` and `GET /api/calls/{call_id}` — bounded history/detail.
- `GET /api/stats` — full SQLite aggregate statistics.
- `GET /api/export/{json|csv|xlsx}` — complete SQLite history.
- `GET /api/operations` — authenticated coordinator health and reconciliation queue.
- `POST /twilio/outbound` — authenticated durable enqueue.
- `POST /twilio/twiml/{call_id}` and `/twilio/status/{call_id}` — signed provider callbacks.
- `POST /exotel/status/{call_id}` — HMAC-token-authenticated callback (Exotel signs nothing).
- `GET`/`POST /api/settings/telephony` — authenticated provider selection, failing closed on an unconfigured carrier.
- `WS /media-stream` — HMAC/CallSid-correlated Twilio media.
- `WS /exotel/media-stream` — separate endpoint; HMAC query token plus a durable CallSid match.
- `WS /ws` — authenticated browser voice test.
- `GET /health` — process and coordinator heartbeat.

## Configuration and compatibility

Configuration includes database path, Basic credentials, stream secret, maximum concurrency, start interval, ring/max-call deadlines, extraction timeout/attempts/retry delay, provider credentials/models, and bind host/port. The full required/optional/sensitive table is in `guide.md`.

Dashboard response shapes remain compatible; queue responses add durable IDs, history accepts optional pagination, and operational reconciliation is a compatible new endpoint. Audio profiles for browser and Twilio are unchanged. JSON parsing/templates remain compatibility utilities, not active persistence.

## Operational limits and restart behavior

Coordination is supported only for processes that share the same filesystem-visible SQLite file with correct file locking. It does not extend across independent hosts or unsafe network filesystems. Restart resumes queued jobs, deadlines, known-SID reconciliation, and extraction. Unknown-SID ambiguous submissions remain safely blocked for operator review because destination/time lookup cannot prove identity.

## Verification snapshot

The implementation is verified with compilation, the full pytest suite using temporary databases/fake providers, and `git diff --check`. Coverage includes lease quarantine/recovery, global admission, callback ordering, deadline ownership, capacity retention, durable extraction/retry/review, new and migrated DND, complete 201-call exports, full statistics, and bounded compatibility tracking. Exact results for the final branch head are recorded in the PR completion response.
