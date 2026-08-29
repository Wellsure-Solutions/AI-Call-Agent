# FreJun Teler as a third telephony provider — design note

**Status: implemented; control plane and correlation confirmed against a real call (2026-08-29),
media framing still pinned from published sources rather than a packet capture.** §5 separates what
a live call has now confirmed from what is still inferred, and names the test to change for each.

Scope: add Teler alongside Twilio and Exotel as a third implementation of the existing
`TelephonyProvider` protocol and `StreamingMediaAdapter` base class. No new abstraction. Twilio and
Exotel are unchanged in behaviour.

---

## 1. Where the ground truth came from

A live spike was not possible from the implementation environment: no trial number, no way to place
a real call, so no packet capture. Four independent published sources were used instead, and they
agree with each other, which is the strongest available substitute:

| Source | What it settles |
|---|---|
| `teler` 0.2.2 on PyPI, read as source | REST path and body, base URL, auth header, `CallFlow.stream` shape |
| <https://api.frejun.ai/openapi.json> (live) | Endpoints, `CallSessionState` enum, error envelope, hangup |
| <https://frejun.ai/docs/media-streaming/supported-messages/> | The per-message JSON schema |
| `teler-deepgram-node-bridge`, `teler-wav-bridge` | The same schema, in working code |

The marketing-site caveat in the original brief held up: `frejun.com/docs/` is a different product's
documentation. The Teler docs live under `frejun.ai/docs/`, and the Docusaurus site 301s any request
without a browser user-agent — worth knowing before concluding a page does not exist.

---

## 2. Findings that changed the plan

### 2.1 `chunk_size` is milliseconds, not bytes

The brief assumed `chunk_size: 400` meant 400 *bytes* — 25 ms at 8 kHz/16-bit, and therefore off
this codebase's 20 ms grid. It is not. From the call-flows reference:

> `chunk_size`: Size of the audio chunks streamed by Teler, **in milliseconds**. Must be a value
> between 20 and 2000, and a multiple of 20. Default is 400.

Teler enforces a 20 ms grid, which is exactly the grid `BARGE_IN_HANGOVER_FRAMES` and
`VoicedDurationTracker(frame_ms=TWILIO_FRAME_MS)` are expressed on. So Teler aligns *better* than
Exotel, not worse, and no re-alignment arithmetic of Exotel's kind is needed.

`TELER_CHUNK_MS` defaults to 20 rather than FreJun's 400. Inbound chunk size is the quantum of
barge-in detection: at 400 ms, `BARGE_IN_CONFIRM_MS` could only be evaluated once per 400 ms of
customer speech, and the constants derived from real recorded call audio would silently mean
something else.

### 2.2 There is no mark, and nothing is ever acknowledged

This is the significant gap. Teler's only playback controls are `clear` (wipe the queue) and
`interrupt` (drop one `chunk_id`), and **both travel outbound**. Twilio and Exotel each echo a mark
back once the audio preceding it has actually played, and `audio_currently_playing` is built on that
ground truth. Two things depend on it:

* `_is_probable_echo` is only armed while agent audio is playing. Without a signal, the echo filter
  disarms as soon as the last frame reaches the socket, and the agent's own voice returning through
  a speakerphone can confirm a barge-in — the agent interrupts itself.
* `_drain_playback` polls it to decide when the customer has *heard* the goodbye rather than when we
  finished *sending* it. Without a signal it returns immediately and the closing line is cut off by
  however much Teler still had queued — the exact bug `_close_after_goodbye` exists to prevent.

**Decision: model the playout deadline.** Every chunk sent advances `_playout_until` by its own
duration in real time, anchored at `max(now, deadline)` so gaps do not accumulate and back-to-back
chunks do. `audio_currently_playing` is true until that deadline passes. `clear_playback` resets it,
because leaving it running past a barge-in would hold the echo filter open against the customer who
just interrupted.

This is a model, not evidence. It cannot see Teler's own jitter buffer, so it reads "finished"
slightly early on a congested socket, and `AGENT_PLAYBACK_TAIL_MS` (250 ms) is doing more work on
Teler than on the other two carriers. It is strictly better than the alternative, which is a
signal that is wrong by a whole chunk every time. Replace it with the real thing if FreJun ever ship
an acknowledgement.

### 2.3 There is no `stop` message

> Teler closes the WebSocket connection when the call ends.

So the disconnect *is* the stop event, and `receive_audio()` catches `WebSocketDisconnect` and
returns `None`. Letting it propagate would file every normal customer hangup as `ai_disconnected`
rather than `completed` — opposite lifecycle states in every report downstream.

### 2.4 The payload nesting is asymmetric

Inbound audio arrives at `msg["data"]["audio_b64"]`; outbound audio is sent at the top level as
`audio_b64` with a `chunk_id`. Both reference bridges, the SDK README, and the message reference all
show it, so it is the format and not a documentation slip. Nesting the outbound payload the way it
arrives produces a message Teler silently ignores: the call connects and the customer hears nothing.

### 2.5 The official SDK is not used

The brief said to implement through `client.calls.create(...)`. Having read the SDK source rather
than its README, `httpx` is used directly instead — the same call ExotelProvider made, for stronger
reasons:

* `CallResourceManager.PATHS` contains only `create`. `retrieve()` and `delete()` raise
  `NotImplementedException`, so `fetch_status()` has no SDK path, and there is no hangup method at
  all — the real endpoint is `POST /voice/calls/{id}/hangup` with a JSON body, which the SDK's
  id-only `delete()` shape cannot express. Two of the three protocol methods would be raw httpx
  anyway.
* `BaseResource.__init__` raises `TypeError` on any response key it does not declare. FreJun adding
  a field to `CallInitiateData` — a backward-compatible change every API makes — would turn every
  dial into an exception. It would be filed `ambiguous`, so no customer gets called twice, but every
  call would land in `NEEDS_RECONCILIATION` holding its capacity slot. At the default concurrency
  that is a full queue stall from a change on FreJun's side.
* The request is one JSON POST with an `x-api-key` header.

The SDK remains the reference for the request shape, and `tests/test_teler_wire_format.py` pins it.

### 2.6 Teler signs its webhooks, but with a secret we cannot verify is set

`X-Teler-Signature` is HMAC-SHA256 over `"{timestamp}.{raw_body}"`, with a secret configured per
Voice App in FreJun's dashboard. Depending on it alone has two failure modes on a misconfiguration
and they are indistinguishable from outside: reject every callback (total silent outage), or
authenticate nothing.

**Decision: our own HMAC query token is the control that must pass; the signature is verified on top
when `TELER_WEBHOOK_SECRET` is set** — the same posture as Exotel's optional IP allowlist. FreJun
publish the algorithm and the signed string but not the digest encoding, so hex and base64 are both
accepted with an optional `sha256=` prefix stripped. That is not a weakening: every candidate is
compared in constant time against a digest of the same secret.

---

## 3. Correlation, and the identifier split that decides its shape

**Teler names the same call two different ways, and this cost a live call to find.** From a real
call, all four identifiers for one conversation:

| Surface | Identifier |
|---|---|
| Dial response `data.id` | `1b3faa9c-4383-4d87-9571-bd51751950cb` |
| Flow request `call_id` | `1b3faa9c-…` (same) |
| Status webhooks `data.call_id` | `1b3faa9c-…` (same) |
| **Media socket `start.call_id`** | **`cs_5NGFF35W81ACMAH2PEVCQVCQ93`** |

The account is pinned to webhook version `2025-08-01`, which uses raw UUIDs, and the REST API
follows it. The media socket does not: FreJun's versioning reference states that "the
media-streaming WebSocket protocol is unaffected by this pin", so it is on the prefixed
`acc_`/`va_`/`cs_`/`ms_` scheme regardless. **There is no local mapping between the two**, and the
media id appears on no other surface.

Consequences, both load-bearing:

* The stream token is minted at flow time over the REST id, so it must be validated against the id
  on the **call row**, never against the id the start message announces. Validating against the
  start message rejects every stream: the socket closes 1008, the customer hears silence, and Teler
  hangs up after a few seconds. That was the observed failure.

* The adapter must be given the **REST** id, because `request_terminal` is a REST call
  (`POST /voice/calls/{id}/hangup`). Handing it the media id would 404 every agent-initiated hangup
  and leave the call running to its maximum-duration deadline.

So Teler's correlation ends up at Exotel's strength, not Twilio's:

| | Token covers | Checked against | Second half |
|---|---|---|---|
| Twilio | call_id, CallSid, expiry | the CallSid in the start event | `claim_media` conditional UPDATE |
| Exotel | call_id, expiry (no SID exists at mint time) | the call row | start event's `call_sid` vs the bound SID, then `claim_media` |
| Teler | call_id, REST call id, expiry | the call row | `claim_media` conditional UPDATE |

Both halves are still required and neither may be collapsed into the other. Moving the token check
from the peer's claim to our own row is not a weakening — the peer supplied that value, so it could
always choose which pair it was checked against, and it still cannot produce a token for any pair
without `STREAM_SECRET`. The media id is recorded on the session as `teler_media_call_id`, because
it is the only id that appears on that socket and therefore the only one worth quoting to FreJun
about a stream problem.

The flow route *binds* the id rather than only comparing it, exactly as `/twilio/twiml` does.
`bind_call_sid` accepts a row whose `call_sid` is NULL or already equal, so it is idempotent against
the coordinator's own binding and still refuses a flow request naming a different call.

---

## 4. Outbound chunking, and the tail rule

FreJun recommend at least 500 ms per chunk sent to them, to avoid choppy playback, so
`TELER_SEND_CHUNK_MS` defaults to 500. That costs up to half a second of latency at the start of an
agent turn, which the greeting cache does not remove. Lower it only against measured
`eot_to_first_audio_ms` and `tts_ttfb_ms` from real calls.

Aggregating raises a failure Exotel's adapter has in milder form and Teler's must not: sending only
whole chunks strands up to `_send_chunk_bytes` in the aggregation buffer for as long as no further
frame arrives. At 500 ms that is the last half-second of every agent turn — including the goodbye
the drain then dutifully waits for and which is never sent at all.

So the partial chunk is flushed whenever the paced sender has nothing further queued. When that is
true there is by definition nothing left to complete the chunk with, so holding it back only creates
the gap it was trying to avoid. The same rule means a generator running at or below real time sends
small chunks rather than stalling, which is the right trade for the same reason.

---

## 5. What is still unverified

Everything here is consistent across four sources but none of it has been seen on a live socket.
The first real call should confirm each row; the named test is the one to change if it does not.

| Assumption | Source | Test that pins it |
|---|---|---|
| Inbound `{"type":"audio","data":{"audio_b64":…}}` | message reference + both bridges | `test_inbound_audio_is_read_from_the_nested_data_object` |
| Outbound `{"type":"audio","audio_b64":…,"chunk_id":N}` | same | `test_outbound_audio_is_type_audio_with_a_top_level_payload_and_chunk_id` |
| PCM S16LE mono 8 kHz (`audio/l16`) | `start` message + wav-bridge ffmpeg args | `test_audio_is_transcoded_to_linear_pcm_on_the_way_out` |
| `clear` carries no stream identifier | message reference | `test_clear_is_a_bare_type_clear_with_no_stream_identifier` |
| Signature digest encoding (hex *or* base64 accepted) | algorithm documented, encoding not | `test_a_validly_signed_callback_is_accepted_in_either_digest_encoding` |

**Confirmed on a real call** (2026-08-29), no longer assumptions:

| Fact | Where it showed up |
|---|---|
| `start` carries `call_id` and `stream_id` at the top level, `acc_`/`va_`/`cs_`/`ms_` prefixed | `test_a_stream_correlates_even_though_the_start_message_names_the_call_differently` |
| Dial returns `data.id`, in the **webhook version's** id format — a raw UUID on `2025-08-01`, *not* `cs_`-prefixed as the OpenAPI describes | same |
| The media socket's `call_id` is a different identifier from every other surface's | §3, and the adapter/session tests beside it |
| Status webhooks arrive on the flat `2025-08-01` shape, with no `X-Teler-Api-Version` header | `test_the_older_flat_payload_version_is_understood_too` |
| The flow request's `call_id` matches the dial response's `id` | `test_a_correctly_signed_flow_request_returns_a_stream_action` |

Two things to watch specifically on the first call, because neither is documented and both degrade
quietly rather than failing:

1. **Whether Teler tolerates a 20 ms `chunk_size` in practice.** It is within the documented range,
   but FreJun's own default is 400 ms and their bridges use 500. If inbound audio arrives choppy or
   the socket is rate-limited, raise `TELER_CHUNK_MS` — 100 still keeps barge-in detection well
   inside `BARGE_IN_CONFIRM_MS`.
2. **How far the playout model drifts from reality.** Compare the end of a recorded call against the
   transcript: if goodbyes are clipped, Teler's own buffer is deeper than the model assumes and
   `AGENT_PLAYBACK_TAIL_MS` needs raising for Teler specifically.
