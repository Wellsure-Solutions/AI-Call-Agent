# Exotel: first real call checklist

For the first outbound call placed through Exotel on a new deployment. Assumes Twilio already
works — if it does not, fix that first, because most of what follows is shared.

---

## 1. Environment

Set these alongside the existing Twilio and Deepgram variables:

```bash
EXOTEL_ACCOUNT_SID=...            # Exotel console -> API settings
EXOTEL_API_KEY=...                # Basic-auth username
EXOTEL_API_TOKEN=...              # Basic-auth password
EXOTEL_SUBDOMAIN=api.exotel.com   # Singapore (default); api.in.exotel.com for Mumbai
EXOTEL_CALLER_ID=+91XXXXXXXXXX    # your ExoPhone, E.164
```

Already required and reused as-is: `PUBLIC_BASE_URL` (exact public HTTPS origin, no trailing
slash), `CALL_AGENT_STREAM_SECRET`, `CALL_AGENT_ADMIN_USERNAME` / `_PASSWORD`, `DEEPGRAM_API_KEY`.

Optional:

```bash
CALL_AGENT_DEFAULT_PROVIDER=twilio   # seed only, ignored once saved in the dashboard
EXOTEL_SEND_CHUNK_BYTES=3200         # always rounded to a multiple of 320
EXOTEL_CALLBACK_ALLOWED_IPS=         # ask Exotel for their egress ranges
```

**`EXOTEL_CALLER_ID` is the ExoPhone you are calling *from*.** Exotel's `From` parameter is the
number being dialled. If a test call rings your own ExoPhone, these are swapped.

## 2. Enable AgentStream on the account

Voice streaming is not on by default. Ask Exotel support (hello@exotel.com) to enable AgentStream
for the account, and confirm the ExoPhone is outbound-capable. Nothing needs configuring in App
Bazaar: the dial request carries `StreamUrl` and `StatusCallback` per call.

## 3. Point the ExoPhone at this box

Exotel reaches the service at URLs derived from `PUBLIC_BASE_URL`, so the only requirement is that
it resolves publicly over HTTPS and terminates at this process with the request path preserved:

- `WSS  {PUBLIC_BASE_URL}/exotel/media-stream` — must accept websockets through the proxy
- `POST {PUBLIC_BASE_URL}/exotel/status/{call_id}`

If you are behind nginx or a tunnel, the websocket upgrade is the part that usually is not
configured. Check it before dialling:

```bash
curl -sS -o /dev/null -w '%{http_code}\n' "$PUBLIC_BASE_URL/health"     # expect 200
curl -sS -i -N -H 'Connection: Upgrade' -H 'Upgrade: websocket' \
     -H 'Sec-WebSocket-Version: 13' -H 'Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==' \
     "$PUBLIC_BASE_URL/exotel/media-stream" | head -1   # expect 101, not 200/404
```

## 4. Pre-flight

```bash
python -m compileall -q app main.py
pytest -q
python scripts/prerender_greeting.py --check    # exit 1 means the cache is stale
```

The greeting cache is shared with Twilio and needs no re-rendering for Exotel — it is mu-law on
both carriers, and the adapter transcodes on the way out.

Then start the service and confirm the provider reports as configured:

```bash
curl -sS -u "$CALL_AGENT_ADMIN_USERNAME:$CALL_AGENT_ADMIN_PASSWORD" \
     "$PUBLIC_BASE_URL/api/settings/telephony" | python -m json.tool
```

Expect `configured: true` for `exotel` and your ExoPhone in `caller_id`. If not, the `missing` list
names the settings to fix.

## 5. Select the provider and place one call

In the dashboard: **Settings → Telephony provider → Exotel → Save**. Or:

```bash
curl -sS -u "$CALL_AGENT_ADMIN_USERNAME:$CALL_AGENT_ADMIN_PASSWORD" \
     -X POST "$PUBLIC_BASE_URL/api/settings/telephony" \
     -H 'Content-Type: application/json' -d '{"provider":"exotel"}'
```

A 422 here means the credentials are incomplete — it names which. Then queue **one** call, to a
number you are holding:

```bash
curl -sS -u "$CALL_AGENT_ADMIN_USERNAME:$CALL_AGENT_ADMIN_PASSWORD" \
     -X POST "$PUBLIC_BASE_URL/twilio/outbound" \
     -H 'Content-Type: application/json' \
     -d '{"phone_number":"+91XXXXXXXXXX","business_name":"Test"}'
```

(The enqueue endpoint is still `/twilio/outbound`; it is provider-agnostic despite the path, and
the carrier is chosen by the setting. Renaming it would break existing callers.)

## 6. What to check, in order

**Did the right number ring?** The customer's handset should show your +91 ExoPhone. If your own
ExoPhone rang instead, `From`/`CallerId` are swapped.

**If the dial failed, read the reason off the call row.** `reconciliation_error` carries Exotel's
own message, truncated with credentials and media tokens stripped — not just an exception class
name:

```bash
curl -sS -u "$CALL_AGENT_ADMIN_USERNAME:$CALL_AGENT_ADMIN_PASSWORD" \
     "$PUBLIC_BASE_URL/api/calls/<call_id>" | python -c \
     'import json,sys; print(json.load(sys.stdin)["reconciliation_error"])'
```

If *every* dial fails regardless of number, suspect request casing before credentials — see
guide.md §8a and `tests/test_exotel_wire_format.py`.

**Did the call row get the right provider?**

```bash
curl -sS -u "$CALL_AGENT_ADMIN_USERNAME:$CALL_AGENT_ADMIN_PASSWORD" \
     "$PUBLIC_BASE_URL/api/calls/<call_id>" | python -c \
     'import json,sys; c=json.load(sys.stdin); print(c["provider"], c["call_sid"], c["provider_status"], c["lifecycle_state"])'
```

`provider` must be `exotel` and `call_sid` must be populated — on Exotel the SID binds at dial
time, not on a later webhook, so an empty one means the dial response was not parsed.

**Are callbacks authenticating?**

```bash
curl -sS -u "$CALL_AGENT_ADMIN_USERNAME:$CALL_AGENT_ADMIN_PASSWORD" \
     "$PUBLIC_BASE_URL/api/operations" | python -c \
     'import json,sys; print(json.load(sys.stdin)["callback_auth_failures"])'
```

Any `exotel_status` count above zero means the status webhook is being rejected. See guide.md §13.

## 7. Metrics — what `scripts/call_metrics.py` should show

```bash
python scripts/call_metrics.py --call-id <call_id>
```

Read these in order; each one isolates a different failure.

| Metric | Expect | If it is wrong |
|---|---|---|
| `metrics_bound` present | one event | the media socket never bound — token, `PUBLIC_BASE_URL`, or websocket upgrade |
| `greeting_ms` (answer → greeting) | ≤ 500 ms | greeting cache stale (`prerender_greeting.py --check`) |
| `eot_to_first_audio_ms` | p50 ≤ 800 ms, p90 ≤ 1200 ms — **the same targets as Twilio** | see the split below |
| `tts_ttfb_ms`, `llm_first_token_ms` | unchanged from Twilio | provider-side, nothing to do with Exotel |
| `provider_signal_to_first_audio_ms` | this is the one Exotel can move | our transport and pacing — the chunk size lives here |
| `metrics_barge_in` decisions | a mix of `commit` and `resume`, few `timeout` | see guide.md §13 |
| `metrics_acoustics` RMS histograms | comparable to a Twilio call | a wildly different distribution means transcoding is wrong |

`eot_to_first_audio_ms` is directly comparable between the two carriers by construction: inbound
audio is timestamped at arrival and that timestamp is carried through the re-framer, so Exotel's
larger wire chunks do not make the number read low. If Exotel and Twilio disagree materially on the
same script, that is a real difference, not a measurement artefact.

## 8. Then decide the chunk size

The default `EXOTEL_SEND_CHUNK_BYTES=3200` satisfies both readings of Exotel's self-inconsistent
minimum. Once a handful of real calls are in, compare it against 1600:

```bash
# a few calls at the default, then:
EXOTEL_SEND_CHUNK_BYTES=1600   # restart, place the same number of calls
python scripts/call_metrics.py --limit 200 --json
```

Keep 1600 only if `eot_to_first_audio_ms` and `tts_ttfb_ms` improve **and** the audio shows no
degradation — listen to a capture, do not judge from the numbers alone:

```bash
python scripts/ulaw_to_wav.py "$CALL_AGENT_MEDIA_DUMP_DIR/<call_id>"   # L=caller, R=agent
```

Record both numbers in `docs/exotel-adapter.md` §1.4 so the choice is documented rather than
folklore. Turn `CALL_AGENT_MEDIA_DUMP_DIR` off again afterwards — it writes both sides of real
customer calls to disk.

## 9. Rolling out

Once one call is clean, switch to `auto` so `+91` goes to Exotel and everything else stays on
Twilio:

```bash
curl -sS -u "$CALL_AGENT_ADMIN_USERNAME:$CALL_AGENT_ADMIN_PASSWORD" \
     -X POST "$PUBLIC_BASE_URL/api/settings/telephony" \
     -H 'Content-Type: application/json' -d '{"provider":"auto"}'
```

Raise `CALL_AGENT_MAX_CONCURRENT_CALLS` only after measuring, as in guide.md §12 — Exotel's
concurrency limits are per account and separate from Twilio's.
