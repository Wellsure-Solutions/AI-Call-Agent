# Exotel as a second telephony provider — design note

**Status: reviewed and implemented.** §9 records the decisions taken; §1.4 has an open
measurement to complete on the first real calls.

Scope: add Exotel alongside Twilio so `+91` destinations can be dialled from a real Indian
ExoPhone. Twilio stays fully supported and unchanged in behaviour; operators pick the active
provider at runtime from the dashboard.

Everything below was checked against the current code and against the Exotel documentation
fetched on 2026-08-21. Where the prompt, the code, and the Exotel docs disagree, the disagreement
is called out rather than smoothed over. Six items needed a decision; **§9 records what was
decided and how each was implemented.**

---

## 1. Findings that change the plan

Read this section first; the rest follows from it.

### 1.1 Exotel's `from` is the number being dialled, not the caller

The prompt warned about this and it is real. From the AgentStream developer guide's parameter
table for `POST /v1/accounts/{account_sid}/calls/connect`:

| Exotel parameter | Meaning per Exotel's docs | Twilio equivalent |
|---|---|---|
| `from` | "Number to dial — E.164 format (`+919876543210`)" | `to` |
| `callerid` | "Your Exophone (shown as caller ID)" | `from_` |
| `streamurl` | "Bot WebSocket URL — `wss://` or `ws://`, max 600 chars" | (TwiML `<Stream url>`) |
| `streamtype` | must be `bidirectional` | — |
| `statuscallback` | webhook URL for call status events | `status_callback` |
| `timelimit` | max duration in seconds (max `14400`) | — |
| `customfield` | metadata string, max 128 chars | — |
| `record` | `true` to record | `record` |

This is consistent with Exotel's classic *Connect Two Numbers* API, where `From` is the leg dialled
**first** and `To` the leg dialled second. AgentStream has no second human leg — the second leg is
the bot socket — so `from` is the only number dialled, i.e. the customer.

Getting this backwards dials our own ExoPhone and bills for it. `ExotelProvider.dial()`
therefore maps `to_number → from`, `EXOTEL_CALLER_ID → callerid`, with the mapping spelled out in a comment
and a unit test asserting the request body puts the
destination in `from` and the ExoPhone in `callerid`.

### 1.2 Exotel cannot give us mu-law on the inbound leg — the fallback path is mandatory

The prompt's preferred option ("request `content_type: audio/x-mulaw;rate=8000`") is not available.
Three primary sources and one production implementation agree:

- Developer guide: "Raw PCM (linear16) — uncompressed", "16-bit signed, little-endian", mono.
- Voicebot applet doc: "raw/slin (16-bit, 8kHz, mono PCM (little-endian)) encoded in base64."
- Beta extension guide: "16-bit Linear PCM (s16le)" at "8000 Hz", and explicitly *not* configurable
  — "No alternative codecs like G.711, PCMU, or mu-law are mentioned as options."
- Pipecat's shipped `ExotelFrameSerializer` treats the payload as PCM in both directions and never
  transcodes.

A web search surfaces a claim that `content_type: audio/x-mulaw;rate=8000` works in a
"start_stream request". No Exotel page supports it and the phrasing matches Plivo's Stream API,
which does have `contentType: audio/x-mulaw`. I am treating it as a conflation, not evidence.

One Exotel blog post says the **outbound** direction (bot → Exotel) accepts "PCM or PCMU". Even if
true, **inbound is unconditionally linear16**, so we must transcode on receive regardless.
Transcoding one direction and not the other buys nothing and doubles the number of formats in play.

**Decision: take the documented fallback.** Transcode slin16 ↔ mu-law at the Exotel adapter's
socket boundary only. Everything above `send_audio()` / `receive_audio()` — the barge-in RMS maths,
`PacedSender`, the `.ulaw` greeting/closing cache, `conversation_engine.py:471`'s
`encoding != "mulaw"` guard, `scripts/ulaw_to_wav.py` — continues to see 8 kHz mu-law and is not
touched. No second audio profile is added.

### 1.3 …but not with `audioop`

The prompt suggests `audioop.lin2ulaw` / `ulaw2lin`. The repo has already made the opposite
decision, deliberately. `app/telephony/audio/local_vad.py`'s own docstring:

> `audioop` (the stdlib mu-law codec) is deprecated and gone in Python 3.13, so decoding is a small
> hand-rolled lookup table instead.

`guide.md` says "CPython 3.11 or newer", so a 3.13 deployment is in scope and `import audioop`
would fail at import time — taking the whole app down, not just Exotel. Adding `audioop-lts` as a
dependency to work around that contradicts "no new heavy dependencies".

**Decision: no `audioop`.** `app/telephony/audio/g711.py` holds both directions as table
lookups, reusing the decode table that already exists in `local_vad.py` and adding the matching
encode table (the same G.711 algorithm `tests/fixtures.py::linear_to_mulaw` already implements, so
the round-trip is testable against an independent implementation already in the tree). Cost is one
`bytes.translate`-style table lookup per sample: 160 samples per 20 ms frame, negligible next to
the RMS pass already running on every frame.

Side benefit: `tests/test_audio_transport.py::test_twilio_adapter_does_not_resample_or_transcode_frames`
asserts `"audioop"`, `"lin2ulaw"`, `"ulaw2lin"` do not appear in `twilio_adapter.py`. Keeping the
codec out of `audioop` and out of that file keeps the test passing unchanged and honest.

### 1.4 Exotel's frame sizing does not match Twilio's, and Exotel's own docs are inconsistent

Agreed across all sources: **chunks must be a multiple of 320 bytes**. At 8 kHz/16-bit that is
exactly 20 ms — one Twilio 160-byte mu-law frame transcodes to exactly one 320-byte Exotel frame.
That alignment is lucky and I intend to lean on it.

Disputed: the minimum. The docs say "Minimum chunk size: 3.2k [100ms data]". At 8 kHz slin16,
3200 bytes is **200 ms**, not 100 ms — the arithmetic only works at 16 kHz. The blog says frames of
"approximately 100 ms", elsewhere "40–60 ms depending on latency requirements". The stated penalty
for undersized chunks is soft, not a rejection: "platform will wait for 20ms before sending next
chunk".

`PacedSender` is **not modified**. It keeps pacing internally at 160-byte / 20 ms mu-law
frames, so its real-time anchor, `buffered_seconds`, and the drain maths stay exactly as tuned. The
Exotel adapter adds a small outbound aggregation buffer: it accumulates `EXOTEL_SEND_CHUNK_MS`
worth of paced frames, transcodes, and emits one `media` message that is a multiple of 320 bytes by
construction. Marks are sent per wire chunk rather than every 5 frames.

**Decided: start at 3200 bytes** (`EXOTEL_SEND_CHUNK_BYTES`, expressed in wire bytes since that is
how the constraint is stated). That satisfies both readings of the minimum, and undershooting risks
jitter artefacts. Barge-in responsiveness is not hostage to it either way, because Exotel supports
`clear`, which empties its buffer immediately on a confirmed interruption.

Whether to keep 3200 or drop to 1600 is a measurement, not a reading. Place a handful of real calls
at each and compare `eot_to_first_audio_ms` and `tts_ttfb_ms`; keep 1600 only if it wins *and* shows
no audio degradation on a capture. The procedure is step 8 of `docs/exotel-first-call.md`.

| Chunk size | `eot_to_first_audio_ms` p50 / p90 | `tts_ttfb_ms` p50 | Audio verdict |
|---|---|---|---|
| 3200 bytes (200 ms) | _to be measured_ | _to be measured_ | _to be measured_ |
| 1600 bytes (100 ms) | _to be measured_ | _to be measured_ | _to be measured_ |

Fill this in from the first batch rather than leaving the default unexamined.

**Inbound is re-framed, and this is the important half.** Exotel chooses its own inbound chunk size
and we do not control it. Every barge-in constant in the repo is expressed in 20 ms frames —
`BARGE_IN_HANGOVER_FRAMES = 10` is literally "10 × 20 ms", and `VoicedDurationTracker` is
constructed with `frame_ms=TWILIO_FRAME_MS`. Handing it 100 ms frames would silently change the
hangover window from 200 ms to 1000 ms and invalidate tuning derived from real call audio. So
`ExotelAdapter.receive_audio()` decodes to mu-law and pushes into a re-framing buffer that yields
**exactly 160-byte / 20 ms mu-law frames** upward. The barge-in path cannot tell the two providers
apart, `test_barge_in_acoustics.py` and `test_turn_taking.py` keep asserting the same behaviour,
and no constant is re-derived.

### 1.5 Exotel's websocket protocol is snake_case, not Twilio's camelCase

Confirmed against the Voicebot applet doc and Pipecat's shipped serializer:

| | Twilio | Exotel |
|---|---|---|
| stream id key | `streamSid` | `stream_sid` |
| call id in start | `start.callSid` | `start.call_sid` |
| custom params | `start.customParameters` | `start.custom_parameters` |
| media payload | `media.payload` (base64 mu-law) | `media.payload` (base64 slin16) |
| outbound media | `{"event":"media","streamSid":…,"media":{"payload":…}}` | `{"event":"media","stream_sid":…,"media":{"payload":…}}` |
| clear | `{"event":"clear","streamSid":…}` | `{"event":"clear","stream_sid":…}` |
| mark | `{"event":"mark","streamSid":…,"mark":{"name":…}}` | `{"event":"mark","stream_sid":…,"mark":{"name":…}}` |

`start` also carries `account_sid`, `from`, `to`, and `media_format`. Exotel additionally sends
`sequence_number` on every message; we ignore it.

This one-key difference is the strongest argument for the prompt's preference of a **separate
`/exotel/media-stream` endpoint** over branching on message shape. A shared parser would have to
guess which key to trust, and that key is what the whole correlation and ownership claim hangs on.
Separate endpoint it is.

### 1.6 Exotel's status vocabulary already matches ours; its non-terminal vocabulary does not

Exotel reports `queued`, `in-progress`, `completed`, `failed`, `busy`, `no-answer`, `canceled`.
`PROVIDER_TERMINAL` in `sqlite_store.py` is `{completed, failed, busy, no-answer, canceled}` — an
exact match, so no store change is needed and the terminal mapping is close to identity.

The difference is on the non-terminal side. `call_coordinator._reconcile` currently hardcodes:

```python
requested = "canceled" if status in {"queued", "ringing", "initiated"} else "completed"
```

`ringing` and `initiated` are Twilio words; Exotel has no `ringing`. That decision moves into
`TelephonyProvider.request_terminal()`, which is why the protocol takes `requested` and owns the
mapping — per the prompt's point 4, in the provider, not the store and not the routes.

Exotel also exposes `Leg1Status` / `Leg2Status`, and its own docs warn the top-level `Status` can
read `completed` for backward compatibility while legs differ. We use the top-level `Status` only,
and treat anything unrecognised as non-terminal — which is the safe direction: an unknown status
never releases capacity, it just schedules another reconciliation attempt.

### 1.7 No answering-machine detection on the AgentStream dial

`AnsweredBy` exists on Exotel's Call object and as a `StatusCallbackEvents` subscription, but the
AgentStream `calls/connect` endpoint documents no machine-detection parameter and no async AMD
callback. Per the prompt ("verify support; gate off if absent"): **AMD is gated off for Exotel.**
`ExotelProvider` will expose `supports_amd = False`; `/exotel/amd/{call_id}` will not exist. The
existing Twilio AMD path is untouched. If Exotel later returns `AnsweredBy` on the status callback,
`record_answered_by` is already provider-agnostic and can be wired in without schema change.

### 1.8 Exotel returns XML unless you ask for JSON

Exotel's v1 API returns XML by default; appending `.json` to the path returns JSON. Pipecat's
example parses `<Sid>` out of XML for exactly this reason. `ExotelProvider.dial()` will request
`…/calls/connect.json` and parse `{"Call": {"Sid": …, "Status": …}}`, with a narrow XML `<Sid>`
fallback if the body is not JSON — a dial whose SID we fail to parse is an *ambiguous* submission,
not a failure, so getting this wrong costs a quarantined call.

Note also that the AgentStream guide spells the path lowercase (`/v1/accounts/{sid}/calls/connect`)
with lowercase parameters, while the classic v1 docs use `/v1/Accounts/{sid}/Calls/connect` with
TitleCase. I will follow the AgentStream spelling since that is the endpoint contract we are using,
and keep the parameter names in one module-level dict so a mismatch found on the first real call is
a one-line fix rather than a hunt.

### 1.9 No callback signature exists

Exotel does not sign status callbacks. The websocket side offers IP allowlisting or Basic auth
embedded in the URL. `_valid_signature` cannot be reused. See §5.

---

## 2. The provider interface

`app/telephony/providers/` holds the control plane only. The media plane stays in
`app/telephony/adapters/`.

```python
# app/telephony/providers/base.py
class DialResult(NamedTuple):
    provider_sid: str | None      # None => submission accepted but unidentifiable
    provider_status: str | None   # normalized, if the response carried one

class TelephonyProvider(Protocol):
    name: str
    audio_profile: AudioProfile
    supports_amd: bool

    async def dial(self, *, call_id: str, to_number: str, ring_timeout: int,
                   stream_url: str, status_callback_url: str) -> DialResult: ...
    async def fetch_status(self, provider_sid: str) -> str: ...
    async def request_terminal(self, provider_sid: str, requested: str) -> None: ...
    def classify_dial_error(self, error: Exception) -> Literal["rejected", "ambiguous"]: ...
    def is_configured(self) -> tuple[bool, list[str]]: ...   # for the settings UI
    def caller_id(self) -> str: ...                          # for the settings UI
```

`TwilioProvider` wraps the `twilio.rest.Client` calls lifted out of `TwilioAdapter`
(`connect()`'s REST body, `fetch_status`, `update_status`, `build_twiml`). `ExotelProvider` uses
`httpx` with Basic auth. No Exotel SDK: there is no officially maintained one.

**`classify_dial_error` is the load-bearing part.** `call_coordinator.py:6` imports
`TwilioRestException` directly and inspects `error.status` to choose `mark_dial_rejected` vs
`mark_dial_ambiguous`. That import moves into `TwilioProvider`; the coordinator will call
`provider.classify_dial_error(error)`. Semantics are preserved exactly on both sides:

- **`rejected`** — the provider is known to have refused. Twilio: `TwilioRestException` with
  `400 <= status < 500`. Exotel: `httpx.HTTPStatusError` with `400 <= status < 500`.
- **`ambiguous`** — everything else: timeouts, connection errors, 5xx, unparseable bodies, and any
  unrecognised exception type. Default is `ambiguous`, because that is the direction that never
  double-dials a customer.

`NEEDS_RECONCILIATION` behaviour is unchanged, and a dial that returns no SID still takes the
existing ambiguous path. **Nothing blind-redials.** One asymmetry worth naming: a 4xx from Exotel is
"rejected" only if we are confident Exotel does not accept-then-4xx. The docs give no reason to
think it does, and the conservative reading (treat 4xx as ambiguous) would quarantine every bad
number instead of failing it cleanly, which is worse. I am matching Twilio's semantics; flagging it
because it is a judgement call, not a documented guarantee.

### Media plane

`BaseTelephonyAdapter` is untouched. `StreamingMediaAdapter` sits between it and the two
telephony adapters, holding the shared, hard-won logic verbatim — soft barge-in (pause / echo
rejection / confirm / resume / timeout), `PacedSender` lifecycle, mark bookkeeping,
`audio_currently_playing`, `_drain_playback` / `_drain_tick` / `_await_close_signal` /
`_close_after_goodbye`, idle watching, and the receive loop in `start()`.

`TwilioAdapter` and `ExotelAdapter` override **only** wire framing and message names:
`send_audio`, `receive_audio`, `clear_playback`, `_send_mark`, and the provider hangup call. The
barge-in algorithm and every constant are moved as-is, not re-tuned. `ExotelAdapter` adds the
transcode + re-framing at its socket boundary and nothing else.

One existing test conflicted with this shape — see §9(a).

---

## 3. Audio path, end to end

```
Deepgram (mu-law 8k)
  → AudioBridge → PacedSender (160B / 20ms mu-law, unchanged)
    → TwilioAdapter.send_audio  → base64 → {"streamSid": …}
    → ExotelAdapter.send_audio  → aggregate N frames → ulaw2lin → base64 → {"stream_sid": …}

Twilio  media → base64-decode → 160B mu-law ─┐
Exotel  media → base64-decode → lin2ulaw → reframe to 160B ─┴→ RMS / VAD / barge-in / Deepgram
```

`get_audio_profile()` moves off `session.direction` as a transport string and onto the call's
**persisted provider**: `twilio` and `exotel` both return the existing mu-law profile (kept exported
as `TWILIO_AUDIO_PROFILE` so nothing importing it breaks, with a provider-neutral alias added), and
`browser` keeps `BROWSER_AUDIO_PROFILE` exactly as today. `conversation_engine.py:471`'s
`encoding != "mulaw"` guard then does the right thing for Exotel with no edit: the cached `.ulaw`
closing plays on Exotel calls too.

The greeting cache needs no change and no second copy — it stays 8 kHz mu-law, and the Exotel
adapter transcodes it on the way out like any other audio.

---

## 4. Stream token in the query string

Exotel allows **max 3 custom params, ≤256 characters total**, and `streamurl` ≤600 chars. Current
`stream_token()` is SHA-256 hex — 64 chars. Budget:

```
?call_id=<36>&expiry=<10>&token=<64>
 8 + 36  +  1 + 7 + 10  +  1 + 6 + 64  = 133 characters, 3 parameters
```

**It fits with room to spare. No truncation of the HMAC is needed** — the prompt allowed for it;
the arithmetic says it is unnecessary, so the full 256-bit digest is kept. With a
`https://calls.example.com` origin the whole `streamurl` lands near 190 chars, well under 600.

The one real difference from Twilio: Twilio binds the CallSid in the TwiML webhook and the token is
computed over `call_id:sid:expiry` *after* the SID is known. Exotel has no TwiML step and the SID
does not exist until the dial response — but the stream URL must be built *before* the dial. So the
Exotel token is computed over `call_id:expiry` with the SID omitted, and the SID is verified
separately at media-claim time against the durably-bound row (`aclaim_media(call_id, sid, owner)`
already requires the SID to match, and returns nothing if it does not).

That is a deliberate, narrow reduction in what the HMAC covers, and it does not weaken the
correlation: the token proves the URL came from us and has not expired; `claim_media`'s conditional
UPDATE proves the SID matches the call and that nobody else owns the media. Both must pass. The
constant-time comparison and the expiry check are unchanged, and I will keep the Exotel token
function distinct from `stream_token()` so the two payload shapes can never be confused. Expiry
stays at 300 s.

Also, since the params arrive twice — in the URL query string *and* echoed in
`start.custom_parameters` — the endpoint reads them from **`custom_parameters`**, matching the
Twilio path's habit of trusting only what arrived inside the stream, and cross-checks the query
string if present.

---

## 5. Callback authentication

`/exotel/status/{call_id}` gets an unguessable HMAC query token derived from the same
`STREAM_SECRET`, over `call_id` plus an expiry — compared with `hmac.compare_digest`, same as
everywhere else. The URL is generated per call at dial time and handed to Exotel as
`statuscallback`, so it never needs to be configured in the Exotel console. Optional IP allowlist
via a new `EXOTEL_CALLBACK_ALLOWED_IPS` (empty = disabled, since Exotel publishes ranges only on
request).

The status-callback token's expiry must outlive the call, not 300 s — it is bounded by
`MAX_CALL_SECONDS` plus a margin, not reused from the media token.

**Counter rename.** `_signature_failures` / `signature_failure_health()` become provider-neutral
`callback_auth_failures`, and `_record_signature_failure` is reused unchanged by the Exotel routes.
`/health` and `/api/operations` will emit **both** keys — the new `callback_auth_failures` and the
existing `twilio_signature_failures` as an alias pointing at the same snapshot — so existing
dashboards keep working. `tests/test_signature_alarm.py` reaches into
`twilio_routes._signature_failures` and `twilio_routes.signature_failure_health()` directly; both
names stay exported from `twilio_routes` as aliases, so that file passes **unchanged**.

---

## 6. Runtime provider selection

Three separate mechanisms, deliberately not collapsed into one.

### 6.1 The setting lives in SQLite

A `settings` table, created in the same idempotent `BEGIN IMMEDIATE` migration as everything else:

```sql
CREATE TABLE IF NOT EXISTS settings(
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
```

I prefer a dedicated `settings` table over reusing `schema_metadata`: `schema_metadata` records
migration facts that must never be operator-editable, and a UI that writes into it is one typo away
from re-running or skipping a migration.

One row, `active_telephony_provider`. `CALL_AGENT_DEFAULT_PROVIDER` is used **only** as the seed
when the row does not exist — inserted with `INSERT … ON CONFLICT DO NOTHING`, so two workers
racing at startup converge and the env var never overwrites an operator's choice afterwards. This
gets its own paragraph in `guide.md`, because an env var that silently stops mattering is exactly
the kind of thing that burns an afternoon.

Read fresh at enqueue time (see 6.2), through a 2-second TTL cache to keep the read off the hot
path without letting workers disagree for meaningfully long. Never read once at coordinator
construction.

### 6.2 Provider is resolved once, at enqueue, and persisted on the call row

Schema version 3, additive, idempotent:

```sql
ALTER TABLE calls ADD COLUMN provider TEXT NOT NULL DEFAULT 'twilio';
```

The `NOT NULL DEFAULT 'twilio'` **is** the backfill — SQLite applies it to every existing row in the
`ALTER`, so legacy rows are all Twilio by definition with no separate `UPDATE` pass and no
possibility of a partial backfill. Guarded by the existing `PRAGMA table_info(calls)` pattern in
`_migrate_schema_v2`, so repeated startup and concurrent workers are safe; the version marker moves
to `'3'`.

`enqueue_call()` resolves the provider once, inside the existing enqueue transaction, and writes it
to the row. **Every later stage reads `call["provider"]`** — dial, media stream, status callback,
deadline reconciliation, the operator resolve endpoint. Nothing downstream ever reads the current
setting.

This is the requirement the prompt calls most important, and the failure it prevents is worth
restating: if reconciliation read the *current* setting, flipping the toggle mid-flight would make
the coordinator ask Exotel about a Twilio CallSid. That lookup fails, burns
`RECONCILIATION_MAX_ATTEMPTS`, and quarantines a healthy call while it holds its capacity slot the
whole time. At the default concurrency of 1 that is a full queue stall. Switching providers must
have **zero effect on in-flight calls**, and per-call persistence is what makes that true rather
than merely likely.

The coordinator's `adapter_factory` parameter is kept (several tests inject fakes through it) and
joined by a provider registry keyed on the call row's `provider`.

### 6.3 The UI

`GET /api/settings/telephony` → active provider, the list of providers, and per provider whether
credentials are configured and the caller ID that will be used.
`POST /api/settings/telephony` → sets it; **422 with a clear message if that provider's credentials
or caller ID are missing.** Rendered in the existing dashboard template as a radio group plus save,
behind the same Basic auth as everything under `/api/*`, with a visible note that the change applies
to newly queued calls only.

Every change writes a `call_events`-style audit row with old value, new value, and timestamp.
`call_events.call_id` is `NOT NULL REFERENCES calls(call_id)`, so a settings change has no call to
hang off — I will add a sibling `settings_events` table rather than weaken that foreign key or
invent a fake call row. No credentials are ever logged or returned; the endpoint returns
`configured: true/false` and the caller ID (a published business number), never key material.

---

## 7. Routing and configuration

`app/telephony/exotel_routes.py` is mounted at `/exotel`, mirroring the Twilio routes minus TwiML:
`POST /exotel/status/{call_id}`, and a **separate** `WS /exotel/media-stream` (§1.5). Registered in
`app/main.py` next to the existing `include_router` calls. No `/exotel/amd/*` (§1.7).

Settings added, following the existing `_env` pattern and the fail-closed-on-missing-secret
convention:

```
EXOTEL_ACCOUNT_SID
EXOTEL_API_KEY
EXOTEL_API_TOKEN
EXOTEL_SUBDOMAIN=api.in.exotel.com
EXOTEL_CALLER_ID                     # the +91 ExoPhone, E.164
EXOTEL_SEND_CHUNK_MS=100             # outbound wire chunk; always a multiple of 320 bytes
EXOTEL_CALLBACK_ALLOWED_IPS=         # optional, empty = disabled
CALL_AGENT_DEFAULT_PROVIDER=twilio   # seed only; the database is authoritative once set
```

`check_startup_configuration()` reports Twilio as before; Exotel gaps surface through the
settings endpoint's fail-closed check rather than as startup noise on a Twilio-only deployment,
which would otherwise warn when Exotel is the active
provider, so a Twilio-only deployment gets no new noise.

`guide.md` has the env table rows, an Exotel subsection under §8 (provider setup, ExoPhone → box
wiring, the seed-vs-database rule), §9 execution-sequence deltas (no TwiML step; SID binds at dial;
AMD absent), and §13 troubleshooting entries.

---

## 8. Untouched by design

The job/lease state machine, capacity accounting, the `one_active_phone` index, extraction, and the
`NEEDS_RECONCILIATION` semantics. DND suppression stays global across providers — it is keyed on
normalized phone in `suppression_list` with no provider column, and a number suppressed on Twilio
stays suppressed on Exotel. Constant-time comparisons, HMAC expiry, the single-media-ownership
claim, and "capacity is never released by a timer alone" are all preserved. Nothing logs
transcripts, phone numbers, credentials, or media tokens.

---

## 9. Decisions taken

**(a) The pump moved to the shared base class.** `StreamingMediaAdapter` owns the outbound pump;
`tests/test_turn_taking.py` was repointed at that file with a comment noting the assertion is on
file contents, not behaviour, and that converting it to a behavioural test is a separate change
that should not land inside a provider refactor. It was the only existing test that needed
touching. `TwilioAdapter` keeps `_send_to_twilio` and `_complete_twilio_call` as thin aliases so
other callers and tests are unaffected.

**(b) Chunk size starts at 3200 bytes, to be decided by measurement.** See §1.4 for the table to
fill in and `docs/exotel-first-call.md` step 8 for the procedure.

**(c) `auto` is built.** A third value of the setting, not a layer over it: an explicit selection is
authoritative and is never overridden by destination. `resolve_provider(phone_number)` is called
once, at enqueue, and slots into the same persistence path. An unconfigured preferred provider
falls back rather than failing the enqueue.

**(d) Exotel 4xx is `rejected`, with the HTTP-200 guard.** `_parse_call` raises `ExotelDialError`
— deliberately not an `httpx.HTTPStatusError` — when a 200 carries an error payload or an
unparseable body, so `classify_dial_error` files it as ambiguous. Ambiguous is the default for
everything not positively identified as a 4xx. Nothing blind-redials.

**(e) The SID binding is restored in the media route.** The token proves the URL was not forged;
the database proves the stream belongs to that call. `/exotel/media-stream` checks the start
event's `call_sid` against the SID bound at dial time *and* against the call's persisted provider,
then `claim_media` re-checks it inside the conditional UPDATE that enforces exclusive ownership.
The reasoning is written into `callback_urls.exotel_stream_token` and the route, explicitly warning
against collapsing the two halves.

**(f) `settings_events` is a separate table.** `call_events.call_id` stays NOT NULL and keeps its
foreign key.

**Two additions requested during review, both implemented:**

- **Codec tested against reference vectors, not round-trips.** `tests/test_g711_codec.py` pins the
  tables against landmark values derived by hand from the G.711 definition, against `audioop` where
  it still exists, and against the independent encoder in `tests/fixtures.py`. This immediately
  earned its keep: the first draft had the sign convention inverted at the extremes (`0x00` is the
  most *negative* code, not the most positive), which a round-trip test would have passed. It also
  surfaced two real properties now pinned explicitly — mu-law's redundant negative zero (`0x7F`)
  cannot round-trip byte-for-byte and becomes `0xFF`, the other spelling of silence, exactly as
  `audioop` does; and our encoder differs from `audioop`'s on ~0.6% of samples because `audioop`
  quantises to 14 bits first, while ours is the exact inverse of the decode table the barge-in path
  already uses.

- **The metrics clock is carried through the re-framer.** `CallMetrics.observe_inbound` takes an
  optional `at`; `ExotelAdapter` stamps each chunk at arrival and assigns each 20 ms sub-frame
  `arrival - (frames_remaining × 20ms)`, so a caller who stops speaking mid-chunk is not recorded as
  having stopped at the chunk boundary. Two tests cover it: one asserts the sub-frame timestamps
  span the chunk in audio order, and one drives the same audio through both adapters and asserts
  they report comparable latency.

## 10. Implementation order

1. Extract the provider interface with Twilio behind it — no behaviour change, tests green.
2. Add the `provider` column, per-call persistence, and the settings store — still Twilio-only,
   still green.
3. Add Exotel: provider, adapter, routes, G.711 codec, re-framing.
4. Dashboard settings UI.
5. Docs — `guide.md` updates and the first-real-call checklist.

Each landed as a separate commit, with `python -m compileall -q app main.py`, `pytest -q` and
`git diff --check` green at each step.

Test counts: **231** at `8d9cafe` before any change, 241 after (a), 261 after (b), 368 after (c),
384 after (d). No existing test changed except the one repoint in §9(a).

---

## Sources

- [AgentStream developer guide](https://developer.exotel.com/docs/agentstream/developer-guide)
- [VoiceBot Applet — Exotel AgentStream](https://docs.exotel.com/exotel-agentstream/voicebot-applet)
- [Working with the Stream and Voicebot Applet](https://support.exotel.com/support/solutions/articles/3000108630-working-with-the-stream-and-voicebot-applet)
- [Updated Extension Guide: Stream and Voicebot Applet (Beta)](https://support.exotel.com/support/solutions/articles/3000132302-updated-extension-guide-working-with-the-stream-and-voicebot-applet-beta-)
- [Call Status](https://support.exotel.com/support/solutions/articles/27323-call-status)
- [Connect Two Numbers API](https://developer.exotel.com/api/make-a-call-api)
- [Exotel Voice v1 API index](https://developer.exotel.com/api)
- [Build a Real-Time Speech-to-Speech AI Voice Assistant on Exotel AgentStream](https://exotel.com/blog/build-a-real-time-speech-to-speech-ai-voice-assistant-on-exotel-agentstream-bidirectional-with-openai-realtime-python/)
- [Pipecat Exotel WebSocket integration](https://docs.pipecat.ai/pipecat/telephony/exotel-websockets) and its shipped [`ExotelFrameSerializer`](https://github.com/pipecat-ai/pipecat/blob/main/src/pipecat/serializers/exotel.py)
- [Pipecat Exotel outbound example](https://github.com/pipecat-ai/pipecat-examples/tree/main/exotel-chatbot)
