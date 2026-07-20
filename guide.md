# AI Call Agent operations guide

## 1. System overview

The application is a FastAPI service for importing leads, queueing individual or batch outbound calls, serving the operator dashboard, handling Twilio Voice webhooks and Media Streams, running a Deepgram conversation, and extracting structured results with OpenAI. SQLite is the authoritative repository and coordination mechanism. JSON files under `data/` are migration inputs only.

A durable coordinator started by the FastAPI lifespan performs three kinds of work: outbound dial claims, provider deadline/reconciliation actions, and extraction-job claims. Multiple local Uvicorn workers can run coordinators because every ownership decision uses `BEGIN IMMEDIATE` and conditional SQLite updates.

## 2. Prerequisites and installation

Use a currently supported CPython 3.11 or newer release, a writable local filesystem, and a public HTTPS endpoint reachable by Twilio. Install dependencies in an isolated environment:

```bash
python -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt -r requirements-dev.txt
```

The pinned provider integrations verified for this implementation are Twilio `9.10.9` and OpenAI `1.x`. Re-run the test suite after upgrading either SDK.

## 3. Environment variables

| Variable | Required | Purpose | Safe example format / default | Sensitive |
|---|---:|---|---|---:|
| `CALL_AGENT_DATA_DIR` | No | Legacy inputs and default DB directory | `/srv/call-agent/data`; repository `data/` | No |
| `CALL_AGENT_DATABASE_PATH` | No | SQLite file | `/srv/call-agent/data/call_agent.sqlite3`; under data dir | No |
| `CALL_AGENT_HOST` | No | Listen address | `127.0.0.1` | No |
| `CALL_AGENT_PORT` | No | Listen port | `8000` | No |
| `CALL_AGENT_ADMIN_USERNAME` | Yes for operator use | HTTP Basic username | a non-default operator name; no default/fails closed | Yes |
| `CALL_AGENT_ADMIN_PASSWORD` | Yes for operator use | HTTP Basic password | randomly generated; no default/fails closed | Yes |
| `CALL_AGENT_STREAM_SECRET` | Yes for Twilio media | HMAC-SHA256 media token secret | random 32+ byte value; no default | Yes |
| `CALL_AGENT_MAX_CONCURRENT_CALLS` | No | Database-wide active-call ceiling | `1` | No |
| `CALL_AGENT_START_INTERVAL_SECONDS` | No | Minimum pacing after dial attempts | `2` | No |
| `CALL_AGENT_RING_TIMEOUT_SECONDS` | No | Twilio and durable ring deadline | `45` | No |
| `CALL_AGENT_MAX_CALL_SECONDS` | No | Durable maximum-call deadline | `900` | No |
| `CALL_AGENT_EXTRACTION_TIMEOUT_SECONDS` | No | OpenAI SDK network/request timeout | `30` | No |
| `CALL_AGENT_EXTRACTION_MAX_ATTEMPTS` | No | Durable attempt ceiling | `3` | No |
| `CALL_AGENT_EXTRACTION_RETRY_DELAY_SECONDS` | No | Exponential-backoff base | `5` | No |
| `TWILIO_ACCOUNT_SID` | Yes for phone calls | Twilio account | `AC...`; no default | Yes |
| `TWILIO_AUTH_TOKEN` | Yes for phone calls | SDK auth and webhook validation | secret token; no default | Yes |
| `TWILIO_FROM_NUMBER` | Yes for phone calls | Verified/capable caller number | E.164 test/example number; no default | No |
| `PUBLIC_BASE_URL` | Yes for phone calls | Exact externally visible HTTPS origin | `https://calls.example.invalid`; no default | No |
| `DEEPGRAM_API_KEY` | Yes for conversation | Deepgram agent access | provider key | Yes |
| `OPENAI_API_KEY` | Required only when extraction runs | Post-call extraction | provider key; absence never blocks raw persistence | Yes |
| `OPENAI_MODEL` | No | Extraction model | `gpt-4.1-mini` | No |
| `DEEPGRAM_*` | No | Listen/think/speak/greeting tuning | defaults in `app/core/settings.py` | API key only |

Generate secrets without putting them in shell history:

```bash
python -c 'import secrets; print(secrets.token_urlsafe(48))'
```

Store credentials in a restricted environment file or secret manager. Never commit it. Use a password manager-generated admin password and a separate media secret.

## 4. SQLite, permissions, WAL, backup, and restore

The service creates the database parent directory and opens a fresh connection per operation with foreign keys enabled, a ten-second busy timeout, explicit transactions, and WAL journaling. The service user needs read/write/create permissions on the database directory because SQLite creates `-wal` and `-shm` siblings.

All Uvicorn workers must see the **same filesystem-visible database file** with correct locking semantics. This design does not coordinate independent machines and must not be placed on an unsafe network filesystem. For horizontally distributed deployment, a different shared transactional database would be required and is outside this repository.

Use SQLite's online backup mechanism rather than copying only the main file while running:

```bash
sqlite3 "$CALL_AGENT_DATABASE_PATH" ".backup '/secure/backups/call-agent.sqlite3'"
```

Test the backup with `PRAGMA integrity_check`. For restore, stop every worker, retain the current database as a rollback copy, restore the backup to the configured path with ownership/mode preserved, then restart. Do not mix a restored main file with stale WAL/SHM files.

## 5. Legacy migration

Startup migrations are transactional and idempotently recorded in `schema_metadata`. `data/leads.json` and `data/call_results/*.json` are read but never modified or deleted. Version 1 imports valid leads/calls while skipping malformed records and invalid missing lead foreign keys. Version 2 scans imported structured responses and legacy lead flags for DND, sets call/lead flags, and populates `suppression_list`. Repeated startup and simultaneous local workers do not duplicate rows.

Keep the source JSON as a backup until the database and exports have been verified.

## 6. Starting the application

Single worker:

```bash
uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Multiple local workers sharing the configured SQLite path:

```bash
uvicorn app.main:app --host 127.0.0.1 --port 8000 --workers 4
```

Terminate workers gracefully so they stop claiming new jobs. A claim obtained but not submitted is returned to the queue during orderly shutdown. A process killed during provider submission is conservatively quarantined for reconciliation rather than redialed.

## 7. Authentication and dashboard use

`GET /`, every `/api/*` route, `POST /twilio/outbound`, and browser `WS /ws` require HTTP Basic authentication. Comparisons are constant-time and missing server credentials fail closed. Browsers normally retain Basic credentials after the initial challenge. Put the application behind TLS; Basic credentials are only encoded, not encrypted.

Import CSV/XLSX or pasted leads through the dashboard, review rejected/duplicate rows, then queue selected calls or a batch. Both paths return durable call IDs immediately. DND, review-required, duplicate queued/active, and invalid phone checks happen inside the enqueue transaction.

## 8. Twilio setup and reverse proxies

Configure an outbound-capable Twilio number in `TWILIO_FROM_NUMBER`. `PUBLIC_BASE_URL` must be the exact public HTTPS origin Twilio sees, without a trailing slash. The application constructs call-specific URLs:

- `POST /twilio/twiml/{call_id}`
- `POST /twilio/status/{call_id}`
- `WSS /media-stream`

Do not manually configure a generic TwiML URL for coordinator-created calls; the REST request supplies it. Twilio signature validation reconstructs the URL from `PUBLIC_BASE_URL` plus the request path and validates form fields with the official SDK. Behind a proxy, preserve the request path and ensure `PUBLIC_BASE_URL` exactly matches the public scheme, host, optional port, and prefix. Incorrect public URL configuration causes a deliberate 403.

The installed Twilio SDK supports `timeout` on call creation; the ring timeout is passed there and also stored durably. It does not provide a call-creation idempotency key used by this implementation. Ambiguous submission failures are therefore never retried blindly.

## 9. Complete execution sequence

1. Import validation normalizes an E.164-compatible phone and bounds/sanitizes text.
2. Enqueue checks suppression, lead DND/review state, idempotency key, and unresolved phone jobs in one transaction; it inserts `calls` and `call_jobs` rows.
3. A coordinator atomically checks database-wide capacity and conditionally claims one queued job.
4. Twilio REST call creation runs off the event loop with the verified ring `timeout` parameter.
5. A successful response binds the CallSid and durable ring/max-call deadlines. A 4xx provider rejection becomes terminal; an uncertain transport/server outcome enters `needs_reconciliation` and blocks that phone.
6. Twilio requests the call-specific TwiML endpoint. Its signature is validated before the CallSid is atomically bound.
7. TwiML includes `call_id`, expiry, and an HMAC token in stream custom parameters.
8. The media socket reads only enough to obtain the start event, validates HMAC/expiry/CallSid, and transactionally claims single media ownership. A terminal call is rejected.
9. Only then is the Deepgram conversation started. Lead JSON is delimited and explicitly treated as untrusted data.
10. Media cleanup stores transcript, duration, media end reason, and `raw_persisted_at`. It does not invent or terminalize Twilio status and does not release provider capacity.
11. A terminal status webhook—or verified reconciliation lookup—stores provider status/time and releases the job slot. Raw/provider events may arrive in either order.
12. Connected raw persistence inserts a durable extraction job and returns without waiting for OpenAI retries.
13. An extraction worker claims one due job with a lease and calls `AsyncOpenAI` with the SDK's real request timeout and retries disabled.
14. Success transactionally stores the complete structured response and maps interest/callback flags.
15. `do_not_call_requested=yes` inserts suppression and updates the lead in the same transaction.
16. Failure schedules bounded exponential backoff. Exhaustion marks a connected lead `review_required`.

## 10. Durable states and recovery

Call lifecycle states include `QUEUED`, `CONNECTING`, `CONNECTED`, `AI_FINISHED`, provider terminal variants, media failure/hangup variants, and `NEEDS_RECONCILIATION`. `provider_status` remains the Twilio status. `media_end_reason` independently records completed, hangup, AI disconnect, or error. `raw_persisted_at`, `provider_terminal_at`, and `finalized_at` have distinct meanings.

Job states are `queued`, `claimed`, `active`, `canceling`, `needs_reconciliation`, and `terminal`. Claimed/active/canceling occupy global capacity. An expired dial claim is ambiguous: it is quarantined, no longer consumes calling capacity, remains visible to operators, and its phone stays blocked. It is never redialed. Late signed callbacks may recover its CallSid.

Ring and maximum-call deadlines are in SQLite. One worker leases a due provider action, fetches Twilio state, persists terminal state when proven, or requests `canceled` for pre-answer states and `completed` for connected states. Capacity remains occupied until terminal confirmation. Lookup/mutation failures use bounded durable backoff.

Extraction states are `pending`, `running`, `retry_pending`, `succeeded`, and `failed`. Leases and `next_attempt_at` survive restart. The lease exceeds the configured SDK timeout so normal timed-out attempts finish before another claim.

After restart, queued calls, deadline actions, reconciliation, and extraction retries resume from SQLite. Jobs with uncertain dial submissions do not automatically redial.

## 11. Outcomes, DND, and operator review

Busy, no-answer, failed, canceled, and completed attempts exist in `calls` even without media and are visible in history, statistics, and exports. Delayed nonterminal callbacks cannot regress a terminal provider status. A media failure remains visible even if Twilio later reports `completed`.

DND suppression is permanent by normalized phone. It applies equally to individual, batch, and direct outbound entry points. Do not remove suppression merely to retry a campaign. Resolve false positives through a controlled database/operator process with an audit trail outside this minimal UI.

`GET /api/operations` shows coordinator health and unresolved reconciliation records. A call with no known CallSid after an ambiguous submission cannot be safely queried by SID and requires an operator to inspect Twilio logs using time/destination metadata before resolving it. Review-required leads likewise require explicit operator investigation before clearing their flag.

## 12. Capacity tuning and monitoring

Raise `CALL_AGENT_MAX_CONCURRENT_CALLS` gradually after measuring Twilio capacity, Deepgram/OpenAI quotas, CPU, file I/O, and SQLite write contention. The limit is database-wide across local workers. Start pacing is also global in admission correctness, although each worker sleeps independently after its successful dial.

Monitor:

- `/health` for process/coordinator heartbeat;
- authenticated `/api/operations` for reconciliation;
- queued/active/canceling job counts;
- extraction retry/failed counts;
- `coordinator_iteration_failed`, `dial_failure_persistence_failed`, and provider webhook errors;
- SQLite disk/WAL growth and filesystem capacity.

Avoid logging transcripts, notes, full credentials, provider keys, or media tokens.

## 13. Troubleshooting

- **401 dashboard/API:** set both admin variables and send Basic credentials.
- **403 Twilio callback:** verify auth token and exact public URL/proxy path.
- **Media closes with policy violation:** verify stream secret, clock synchronization, CallSid binding, and TwiML custom parameters.
- **Calls remain reconciliation:** inspect `/api/operations`; verify Twilio credentials/network and provider state. Unknown-SID ambiguous dials require manual provider-log review.
- **Queue does not advance:** inspect active/canceling jobs and provider terminal confirmation; capacity is intentionally not released on a timer alone.
- **Extraction retries:** verify OpenAI key/model/quota. Raw transcripts remain durable. Permanent connected failures place leads in review.
- **Database locked:** ensure all workers use the same supported local filesystem, directory permissions are correct, and transactions are not held by external tools.

## 14. History, exports, and verification

`GET /api/calls?limit=200&offset=0` is bounded and preserves the existing `{"calls": [...]}` shape. Live calls are limited to 100. Statistics use full-database SQL aggregation. JSON/CSV/XLSX exports iterate the complete SQLite history; XLSX uses write-only mode and structured responses are objects in JSON and encoded exactly once in tabular exports.

Run before deployment:

```bash
python -m compileall -q app main.py
pytest -q
git diff --check
```

Tests use temporary SQLite files and fake provider clients; they must never use production credentials.

## 15. Known deployment constraints

This implementation coordinates processes sharing one SQLite database on a locking-safe filesystem; it is not a multi-host distributed queue. An ambiguous Twilio submission without a returned or callback-provided CallSid cannot be automatically reconciled by destination alone without risking correlation to the wrong call, so it remains blocked and operator-visible. Basic authentication should be placed behind HTTPS and may be replaced by an organization identity proxy in a larger deployment. JSON export necessarily materializes its response bytes; CSV/XLSX database reads are chunked, but the HTTP response is still assembled before delivery by the current FastAPI response path.
