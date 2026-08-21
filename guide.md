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
| `CALL_AGENT_RECONCILIATION_MAX_ATTEMPTS` | No | Failed provider lookups before a call is quarantined and its slot freed | `8` | No |
| `CALL_AGENT_ABANDONED_JOB_GRACE_SECONDS` | No | Grace past the maximum-call deadline before the sweeper quarantines a slot-holding call | `300` | No |
| `TWILIO_ACCOUNT_SID` | Yes for phone calls | Twilio account | `AC...`; no default | Yes |
| `TWILIO_AUTH_TOKEN` | Yes for phone calls | SDK auth and webhook validation | secret token; no default | Yes |
| `TWILIO_FROM_NUMBER` | Yes for phone calls | Verified/capable caller number | E.164 test/example number; no default | No |
| `PUBLIC_BASE_URL` | Yes for phone calls | Exact externally visible HTTPS origin | `https://calls.example.invalid`; no default | No |
| `EXOTEL_ACCOUNT_SID` | Yes for Exotel calls | Exotel account SID | from the Exotel console; no default | Yes |
| `EXOTEL_API_KEY` | Yes for Exotel calls | REST Basic-auth username | from the Exotel console; no default | Yes |
| `EXOTEL_API_TOKEN` | Yes for Exotel calls | REST Basic-auth password | from the Exotel console; no default | Yes |
| `EXOTEL_SUBDOMAIN` | No | Exotel API region | `api.in.exotel.com` (Mumbai); `api.exotel.com` for Singapore | No |
| `EXOTEL_CALLER_ID` | Yes for Exotel calls | The +91 ExoPhone shown to the customer. **This is Exotel's `callerid`, not its `from`** | E.164; no default | No |
| `EXOTEL_SEND_CHUNK_BYTES` | No | Outbound PCM bytes per websocket message; always rounded to a multiple of 320 | `3200` | No |
| `EXOTEL_CALLBACK_ALLOWED_IPS` | No | Comma-separated IP allowlist for Exotel status callbacks; empty disables the check | unset (disabled) | No |
| `CALL_AGENT_DEFAULT_PROVIDER` | No | **Seed only.** Written to the database the first time it is opened and ignored from then on — once a provider is saved in the dashboard, the database is authoritative. See §8a | `twilio`; also `exotel` or `auto` | No |
| `DEEPGRAM_API_KEY` | Yes for conversation | Deepgram agent access | provider key | Yes |
| `OPENAI_API_KEY` | Required only when extraction runs | Post-call extraction | provider key; absence never blocks raw persistence | Yes |
| `OPENAI_MODEL` | No | Extraction model | `gpt-4.1-mini` | No |
| `DEEPGRAM_*` | No | Listen/think/speak/greeting tuning | defaults in `app/core/settings.py` | API key only |
| `CALL_AGENT_METRICS_ENABLED` | No | Per-turn latency/barge-in/cost instrumentation | `1`; set `0` to disable | No |
| `CALL_AGENT_METRICS_SILENCE_GAP_MS` | No | Gap counted as dead air | `1500` | No |
| `CALL_AGENT_METRICS_FLUSH_SECONDS` | No | Metric batch flush interval | `5` | No |
| `CALL_AGENT_MEDIA_DUMP_DIR` | No | Raw mu-law capture directory — **records customer calls** | unset (disabled) | Contains call audio |
| `CALL_AGENT_CLOSE_GRACE_SECONDS` | No | Grace for the agent's closing line after it calls `end_call` | `10` | No |
| `CALL_AGENT_AMD_ENABLED` | No | Answering-machine detection (adds Twilio's per-call AMD charge) | `1`; set `0` to disable | No |
| `CALL_AGENT_AMD_MODE` | No | `Enable` or `DetectMessageEnd` | `Enable` | No |
| `CALL_AGENT_AMD_*_MS` / `_SECONDS` | No | Twilio detection tuning | Twilio defaults | No |
| `DEEPGRAM_SPEAK_MODEL_ID` | No | TTS model | `eleven_flash_v2_5` | No |
| `DEEPGRAM_SPEAK_LANGUAGE` | No | Pins TTS language so code-mixed text doesn't shift accent; empty = auto-detect | `hi` | No |
| `CALL_AGENT_BARGE_IN_ENERGY_THRESHOLD` | No | Minimum clamp for the adaptive voice threshold | `250` | No |
| `CALL_AGENT_BARGE_IN_NOISE_MULTIPLIER` | No | How far above the measured noise floor speech must sit | `6` | No |
| `CALL_AGENT_BARGE_IN_HANGOVER_FRAMES` | No | Silent 20ms frames before a voiced run ends | `10` (200ms) | No |
| `CALL_AGENT_BARGE_IN_CONFIRM_MS` | No | Sustained voice that confirms an interruption | `600` | No |
| `CALL_AGENT_BARGE_IN_ECHO_MARGIN` | No | How far inbound must exceed played agent audio to count as the customer | `0.55` | No |
| `CALL_AGENT_BARGE_IN_MAX_PAUSE_MS` | No | Ambiguous-pause safety net | `2500` | No |
| `CALL_AGENT_GREETING_CACHE_DIR` | No | Pre-rendered greeting audio | `<data dir>/greetings` | No |

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

## 8a. Exotel setup and provider selection

Twilio cannot originate calls to India with an Indian caller ID
(<https://www.twilio.com/en-us/guidelines/in/voice>: outbound calls to India can only be made from
non-Indian numbers). Exotel is a licensed Indian carrier, so it provides a real +91 ExoPhone as the
CLI. Both providers are fully supported at the same time — this is not a migration. Twilio remains
the path for international destinations; Exotel is the path for +91.

### Credentials

Take the account SID, API key and API token from the Exotel console's API settings, and set
`EXOTEL_CALLER_ID` to your ExoPhone in E.164. `EXOTEL_SUBDOMAIN` selects the region and defaults to
Mumbai. Ask Exotel support to enable AgentStream (voice streaming) on the account; it is not on by
default.

**`EXOTEL_CALLER_ID` is Exotel's `callerid`, not its `from`.** On
`POST /v1/accounts/{sid}/calls/connect`, Exotel's `from` is the number being *dialled* and
`callerid` is the ExoPhone shown to them. This is the opposite of Twilio's `to`/`from`, and
inverting them dials your own ExoPhone and bills for it. The mapping is asserted in
`tests/test_exotel_provider.py`.

### Pointing the ExoPhone at this box

Nothing needs configuring in App Bazaar for outbound AgentStream calls. The dial request carries
`streamurl` (the websocket, with its token) and `statuscallback` (the status webhook, with its
token), both built from `PUBLIC_BASE_URL`, so the carrier is told where to reach you per call.
`PUBLIC_BASE_URL` must be the exact public HTTPS origin, without a trailing slash, exactly as for
Twilio.

The Exotel endpoints are:

- `POST /exotel/status/{call_id}` — authenticated by an HMAC query token, not a signature
- `WSS /exotel/media-stream` — a separate endpoint from Twilio's `/media-stream`

There is no TwiML step and no `/exotel/amd/*`: the AgentStream dial documents no answering-machine
detection, so AMD is off for Exotel regardless of `CALL_AGENT_AMD_ENABLED`. Inbound calls to the
ExoPhone are out of scope for this service.

### Choosing the active provider

Operators set the provider at runtime from the dashboard's **Settings** page, or via
`GET`/`POST /api/settings/telephony`. Three values:

| Value | Behaviour |
|---|---|
| `twilio` | Everything goes to Twilio |
| `exotel` | Everything goes to Exotel |
| `auto` | `+91` destinations go to Exotel, everything else to Twilio |

Selecting a provider whose credentials or caller ID are missing is refused with a 422 naming the
missing settings, so an operator cannot pick a carrier that cannot dial.

**`CALL_AGENT_DEFAULT_PROVIDER` is a seed value only.** It is written into the `settings` table the
first time the database is opened and is ignored from then on. Once a provider has been saved from
the dashboard, the database is authoritative and changing the environment variable does nothing.
This is deliberate — the setting is shared across workers, so it has to live somewhere all of them
can see — but it does mean an env var that silently stops mattering. To change the provider on a
running deployment, use the dashboard.

### Switching providers is safe while calls are in flight

The provider is resolved **once, when the call row is created**, and written to that row. The dial,
the media stream, the status callback, deadline reconciliation and the operator resolve endpoint
all read it from there, never from the current setting. Flipping the toggle therefore has **zero
effect on already-queued and in-flight calls** — they finish on the carrier they started on.

This matters more than it sounds. If reconciliation read the current setting, flipping the toggle
mid-flight would have the coordinator query Exotel for a Twilio CallSid; that lookup fails, burns
`CALL_AGENT_RECONCILIATION_MAX_ATTEMPTS`, and quarantines a perfectly healthy call while holding
its capacity slot the whole time. At the default concurrency of 1 that stalls the entire queue.

DND suppression is global across providers. A number suppressed while on Twilio stays suppressed on
Exotel.

### Audio

Exotel's bidirectional stream is 16-bit linear PCM at 8 kHz, and cannot be asked for mu-law. The
adapter transcodes at its socket boundary, so everything above it — the `.ulaw` greeting and
closing caches, the barge-in tuning in §12b, `scripts/ulaw_to_wav.py` — is unchanged and shared
with Twilio. Inbound chunks are re-framed to 20 ms before the barge-in path sees them, so every
constant in §12b means the same thing on both carriers.

`EXOTEL_SEND_CHUNK_BYTES` controls how much audio goes in each outbound message. It is always
rounded to a multiple of 320 bytes, which is Exotel's hard requirement. Exotel's stated minimum
("3.2k [100ms data]") is self-inconsistent at 8 kHz — 3200 bytes is 200 ms there — so the default
starts at 3200, which satisfies both readings. See `docs/exotel-adapter.md` for the measurement
that should decide whether to lower it.

## 9. Complete execution sequence

0. Answering-machine detection runs asynchronously alongside the call; a machine/fax verdict requests provider completion but never releases capacity itself.
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

### Exotel deltas

The sequence above is written for Twilio. On Exotel, steps 4-8 differ:

| Step | Twilio | Exotel |
|---|---|---|
| 0 | Async AMD runs alongside the call | No AMD — the AgentStream dial has none |
| 4 | REST call creation with a `timeout` ring parameter | `POST /calls/connect` with `from` = destination, `callerid` = ExoPhone, `streamurl`, `streamtype=bidirectional`. No ring timeout exists on this endpoint; the durable ring deadline is the only one |
| 5 | Same | Same. A 4xx is a proven rejection; a 5xx, a timeout, **or an HTTP 200 carrying an error payload** is ambiguous and enters `needs_reconciliation` |
| 6-7 | Twilio fetches signed TwiML, which binds the CallSid and mints the stream token | **No TwiML step.** The CallSid comes back in the dial response and is bound there; the stream token was already minted into `streamurl` |
| 8 | Media validates HMAC/expiry/CallSid from TwiML custom parameters | Media validates the HMAC and expiry from the query string, **and separately** checks the start event's `call_sid` against the SID bound at dial time before claiming ownership |

Step 8 is split for a structural reason. Exotel's stream URL has to be built before the dial
request is sent, so no CallSid exists yet and the token cannot cover one. The token proves the URL
came from us and has not expired; the database proves the stream belongs to that call. Both are
required. Do not collapse them.

Steps 1-3 and 9-16 are identical on both carriers.

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

## 12a. Call quality instrumentation

Every media call writes numeric measurements into `call_events` under `metrics_*` event names. Payloads contain numbers, short enum-like strings, and timestamps only — never transcript text, phone numbers, credentials, or media tokens.

| Event | Records |
|---|---|
| `metrics_bound` | Media stream bound; anchors the call clock |
| `metrics_greeting` | Stream bind → first agent audio byte handed to Twilio |
| `metrics_turn` | Caller's last voiced frame (EOT) → first agent audio byte; and the share of that spent in our own transport/pacing |
| `metrics_provider_latency` | Deepgram's per-turn `LatencyReport`: STT, LLM first token, TTS time-to-first-byte, provider end-to-end |
| `metrics_barge_in` / `metrics_barge_in_pause` | Every pause and its outcome (`commit`/`resume`/`timeout`) with the RMS, sustained voiced duration, elapsed ms, and whether agent audio was playing |
| `metrics_provider_warning` / `metrics_provider_error` | Deepgram warning/error codes — a silent call caused by a rejected model id or speak provider shows up only here |
| `metrics_call` | Per-call summary: media seconds, billable seconds, TTS characters, turns, dead-air gaps, barge-in tallies |
| `metrics_acoustics` | RMS histograms split by whether agent audio was playing, plus the caller-over-agent level ratio in dB |

Report percentiles across a batch:

```bash
python scripts/call_metrics.py --limit 200          # p50/p90/p99 per metric
python scripts/call_metrics.py --call-id <uuid>     # one call
python scripts/call_metrics.py --json               # machine-readable
```

The headline metric is `eot_to_first_audio_ms`. It is measured at the Twilio socket and includes the endpointing hold, so it is larger than Deepgram's `total_latency` by design — it is what the customer hears. Targets: p50 ≤ 800ms, p90 ≤ 1200ms; answer → greeting ≤ 500ms.

To check measurements against what a human actually hears, set `CALL_AGENT_MEDIA_DUMP_DIR` and convert a capture:

```bash
python scripts/ulaw_to_wav.py "$CALL_AGENT_MEDIA_DUMP_DIR/<call_id>"   # stereo: L=caller, R=agent
```

This writes both sides of real customer conversations to disk. Treat the directory as call recordings: restricted permissions, deliberate retention, deleted when the measurement is done, and never left enabled in steady-state production.

## 12b. Turn-taking and audio tuning

These defaults were derived by replaying a real recorded call and by benchmarking the live Deepgram agent, not chosen by feel. Re-derive them the same way rather than adjusting by ear.

**Barge-in.** The voice threshold is not fixed: it is `noise_floor x CALL_AGENT_BARGE_IN_NOISE_MULTIPLIER`, clamped between the configured minimum and 4000. The floor is measured over the first 500ms of the stream and then tracked continuously, but only while the agent is silent — inbound audio during agent speech is largely the agent's own echo.

Two settings decide whether a customer is heard:

- `CALL_AGENT_BARGE_IN_HANGOVER_FRAMES` — how long a gap may be before a voiced run is considered over. Too short and ordinary syllable gaps split one utterance into fragments that never reach the confirm threshold, so genuine interruptions are ignored entirely. This is what 3 frames (60ms) did.
- `CALL_AGENT_BARGE_IN_CONFIRM_MS` — how much sustained voice confirms a real interruption. On real call audio, run lengths are bimodal: backchannels ("haan", "acha") at or under 400ms, real turns at or over 600ms. Keep this in that gap. Raising it makes the agent harder to interrupt; lowering it makes it stop for acknowledgements.

`metrics_barge_in` events record the decision, the RMS, the live threshold, the sustained voiced duration, and whether agent audio was playing — enough to re-tune from a real batch instead of guessing.

**The greeting.** Synthesising it after pickup cost 2.0 seconds of dead air at the start of every measured call — a websocket handshake, a settings round-trip, and speech synthesis, all after the customer said hello. Render it once instead:

```bash
python scripts/prerender_greeting.py          # render and cache
python scripts/prerender_greeting.py --check  # is the cache current? (exit 1 if not)
```

**Re-run this after changing `DEEPGRAM_GREETING`, the voice, the TTS model, or `DEEPGRAM_SPEAK_LANGUAGE`.** The cache key covers all of them, so a stale file is ignored rather than played and the call silently falls back to the slow path. `--check` is suitable for a deploy gate.

**Latency.** `scripts/call_metrics.py` reports the split. If `eot_to_first_audio_ms` regresses, check which stage moved: `tts_ttfb_ms` is the TTS model, `llm_first_token_ms` is the think model and prompt length, and `provider_signal_to_first_audio_ms` is our own transport and pacing.

## 13. Troubleshooting

- **401 dashboard/API:** set both admin variables and send Basic credentials.
- **403 Twilio callback:** verify auth token and exact public URL/proxy path. `GET /health` and `/api/operations` now report `twilio_signature_failures`; a nonzero total across all three endpoints means `PUBLIC_BASE_URL` does not match the URL Twilio signed, and **no call will produce media** until it is corrected.
- **Calls never end on their own:** confirm the agent registered `end_call` — `/api/calls/{id}` events include `agent_requested_end_call` in the logs and `metrics_call.media_end_reason`. A silent agent with no audio at all is usually a rejected Deepgram setting; check `metrics_provider_error` events for the cause.
- **Voicemail reached:** `calls.answered_by` records Twilio's verdict. `unknown` is not treated as a machine by design.
- **Agent talks over the customer / ignores interruptions:** inspect `metrics_barge_in` events. Many `resume` decisions with high `voiced_ms` means `CALL_AGENT_BARGE_IN_CONFIRM_MS` is too high; many `commit` decisions with `agent_playing: true` and low `rms` relative to `threshold` means echo is leaking past `CALL_AGENT_BARGE_IN_ECHO_MARGIN`.
- **Agent interrupts itself:** speakerphone echo. Lower `CALL_AGENT_BARGE_IN_ECHO_MARGIN` toward 0 to filter more aggressively, at the cost of missing quiet customers.
- **Long silence after the customer picks up:** the greeting cache is missing or stale. Run `python scripts/prerender_greeting.py --check`.
- **Agent greets twice:** cached greeting audio is playing while the provider greeting is also configured. `get_agent_settings(greeting_already_played=True)` suppresses the provider one; this only happens if that flag is not being threaded through.
- **Accent shifts mid-sentence:** `DEEPGRAM_SPEAK_LANGUAGE` is unset or wrong. Code-mixed Devanagari/Roman text makes the voice re-infer language per phrase unless it is pinned.
- **Media closes with policy violation:** verify stream secret, clock synchronization, CallSid binding, and TwiML custom parameters.
- **Calls remain reconciliation:** inspect `/api/operations`; verify Twilio credentials/network and provider state. Unknown-SID ambiguous dials require manual provider-log review.
- **Queue does not advance / calls stuck in QUEUED:** `GET /api/operations` now reports `capacity`, including `capacity_occupied` and the oldest calls holding a slot. Something in that list is not finishing. Two safety nets clear it automatically — reconciliation gives up after `CALL_AGENT_RECONCILIATION_MAX_ATTEMPTS` failed provider lookups, and a sweep quarantines any call past its maximum-call deadline plus `CALL_AGENT_ABANDONED_JOB_GRACE_SECONDS`. Both move the call to `needs_reconciliation`: capacity is freed, but no outcome is invented, the phone stays blocked, and it is never redialed. To finish one off after checking the provider console:

  ```bash
  curl -u user:pass -X POST https://host/api/calls/<call_id>/resolve \
       -H 'Content-Type: application/json' -d '{"status":"failed","note":"no record in Twilio"}'
  ```

  Capacity is still never released by a timer alone: automatic release requires repeated recorded failures to obtain proof, and the override requires a human.
- **Extraction retries:** verify OpenAI key/model/quota. Raw transcripts remain durable. Permanent connected failures place leads in review.
- **403 Exotel callback:** Exotel signs nothing, so `/exotel/status/{call_id}` is authenticated by an HMAC query token this service minted at dial time. `GET /health` and `/api/operations` report `callback_auth_failures` (and the same snapshot under the retained `twilio_signature_failures` key); a nonzero `exotel_status` count means `CALL_AGENT_STREAM_SECRET` changed after the calls were placed, `PUBLIC_BASE_URL` is wrong so the callback never carried the token, or `EXOTEL_CALLBACK_ALLOWED_IPS` does not include Exotel's egress range.
- **Exotel dial fails immediately with no call placed:** check the console for a 4xx. A 4xx is recorded as `provider_rejected` and is terminal. If instead the call sits in `needs_reconciliation`, the submission was ambiguous — Exotel returned a 5xx, timed out, or answered HTTP 200 with an error body — and it is deliberately never redialed. Check the Exotel call log before resolving it.
- **Exotel call connects but the customer hears nothing / static:** almost always a framing problem. Confirm `EXOTEL_SEND_CHUNK_BYTES` is a multiple of 320 (it is rounded automatically, but a value under 320 is rounded *up* to one frame). Static rather than silence usually means the audio is being sent as mu-law instead of PCM — check `metrics_call` for outbound bytes and capture with `CALL_AGENT_MEDIA_DUMP_DIR`, which stores mu-law for both carriers.
- **Exotel media stream closes with policy violation:** the correlation has two halves and either can fail. Verify `CALL_AGENT_STREAM_SECRET` and clock synchronisation for the token half; for the database half, confirm the call row's `call_sid` matches what Exotel reports in its start event, and that the call's `provider` column really is `exotel`. A call queued under Twilio will be refused by the Exotel socket by design.
- **Calls still going out on the old provider after switching:** expected. The provider is resolved and persisted when the call row is created, so already-queued calls keep the carrier they were created with. Only newly queued calls use the new setting.
- **Changing `CALL_AGENT_DEFAULT_PROVIDER` does nothing:** also expected. It is a seed value used only when the settings row does not yet exist. Change the provider from the dashboard instead — see §8a.
- **The dashboard refuses to select a provider:** the 422 names the missing settings. Exotel needs `EXOTEL_ACCOUNT_SID`, `EXOTEL_API_KEY`, `EXOTEL_API_TOKEN`, `EXOTEL_CALLER_ID` and `PUBLIC_BASE_URL`. This is a deliberate fail-closed check; selecting an unconfigured carrier would produce a batch of failed calls instead of one clear error.
- **`ImportError: No module named 'audioop'` on Python 3.13:** should not happen — the G.711 codec in `app/telephony/audio/g711.py` is a table lookup precisely because `audioop` was removed in 3.13. If you see this, something new imported it; do not fix it by adding `audioop-lts`.
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
