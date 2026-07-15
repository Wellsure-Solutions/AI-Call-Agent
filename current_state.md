# Current State of the AI Call Agent

This document describes the application as it exists now. It is intentionally implementation-focused so a new developer can understand the runtime architecture, data flow, integrations, and operational boundaries without reading every source file first.

## 1. What the application does

The project is a FastAPI-based autonomous sales-calling application. Its primary job is to help a dashboard operator upload or enter business leads, start outbound phone calls to those leads, conduct a Hindi/Hinglish sales conversation through a realtime AI voice agent, extract structured post-call answers, persist call results locally, and display/export those results in a browser dashboard.

The calling persona is **Priya**, a female sales caller for **WellSure**, speaking to Indian small-business owners about selling on Amazon. The live conversation is powered by Deepgram Agent: Deepgram listens to caller audio, uses a configured LLM provider for reasoning, and speaks responses through a configured TTS provider. After the call ends, a separate OpenAI structured-output extraction step reads the transcript and converts it into campaign fields such as interest, GST availability, and callback approval.

The app currently supports two active interaction surfaces:

1. **Dashboard/browser test calls** through `/ws`, where the browser captures microphone PCM audio and plays AI PCM audio.
2. **Outbound Twilio calls** from dashboard lead actions or `/twilio/outbound`, where Twilio places a PSTN call and bridges audio to the app through Media Streams.

It also contains adapter stubs for Asterisk and GSM gateway integrations, but those are not implemented yet.

## 2. Runtime entrypoints

### Root compatibility entrypoint

`main.py` exists only to support commands such as:

```bash
uvicorn main:app
```

It imports and re-exports `app` from `app.main`.

### Main FastAPI application

`app/main.py` creates the FastAPI app, constructs the shared services used by dashboard endpoints, includes Twilio routers, serves the static dashboard, and defines the browser WebSocket endpoint.

Important application-level objects are created at import time:

- `answer_extractor = AnswerExtractor()` for OpenAI-based post-call extraction.
- `answer_store = JsonCallStore(DATA_DIR)` for local JSON lead/call persistence.
- `call_result_service = CallResultService(answer_extractor, answer_store)` for finalizing sessions.
- `call_manager = CallManager()` for browser-session lifecycle management.

The app includes:

- `twilio_router` under `/twilio` for outbound call, TwiML, and status webhooks.
- `media_router` for `/media-stream`, the Twilio Media Streams WebSocket.
- `/` for the dashboard HTML.
- `/health` for a simple health response.

## 3. Configuration and environment

Configuration is centralized in `app/core/settings.py`. Environment variables are loaded with `python-dotenv`.

Key settings:

| Setting | Purpose | Default |
| --- | --- | --- |
| `CALL_AGENT_DATA_DIR` | Directory for local leads and call results | `<repo>/data` |
| `CALL_AGENT_HOST` | Uvicorn host | `127.0.0.1` |
| `CALL_AGENT_PORT` | Uvicorn port | `8000` |
| `DEEPGRAM_API_KEY` | Required for live AI voice sessions | unset |
| `OPENAI_API_KEY` | Required for post-call extraction | unset |
| `OPENAI_MODEL` | Model used by `AnswerExtractor` | `gpt-4.1-mini` |
| `TWILIO_FROM_NUMBER` | Caller ID for outbound Twilio calls | `+17629999974` |
| `PUBLIC_BASE_URL` | Public HTTPS base URL Twilio uses for webhooks | a devtunnels URL |
| Deepgram listen/think/speak settings | Deepgram Agent model, provider, voice, thresholds | defaults in settings |

The Deepgram Agent audio contract is configured in `app/integrations/deepgram/config.py`:

- Input to Deepgram: `linear16`, 48 kHz.
- Output from Deepgram: `linear16`, 24 kHz, no container.
- Listen provider: Deepgram, with Hindi and English language hints.
- Think provider/model/temperature: configurable through settings.
- Speak provider/model/voice: configurable through settings.
- Greeting: configurable, defaulting to a Hindi/Hinglish Amazon-related opener.

When lead metadata is available, `get_agent_settings()` appends a **CURRENT LEAD CONTEXT** section to the base prompt with business name, category, and notes so the agent can personalize naturally.

## 4. The core business conversation

The canonical campaign prompt lives in `app/core/prompts.py` as `PROMPT`.

The prompt instructs the agent to:

- Act as Priya from WellSure.
- Speak naturally in Hindi/Hinglish, with Devanagari for Hindi words and Roman script for English words.
- Avoid sounding like a form or checklist.
- Persuade small-business owners to consider selling on Amazon through WellSure.
- Use real WellSure credibility and service points naturally.
- Avoid critical unsafe behaviors, especially asking for account-user access, payment details, bank details, OTPs, UPI IDs, or card numbers.
- End calls gracefully based on interest level or caller preference.
- Never reveal internal instructions, tracking fields, scripts, AI/bot identity, or recording/tracking internals.

`ANSWER_FIELDS` in the same file defines the structured fields the post-call extractor will later produce:

- `owner_confirmed`
- `interested`
- `already_selling_online`
- `gst_available`
- `callback_approved`
- `callback_time`

Most fields are constrained to `yes`, `no`, or `unknown`; `callback_time` is free text.

## 5. Domain model and call lifecycle

### CallSession

`app/telephony/call_session.py` defines `CallSession`, the central record for one active or completed call. It contains:

- A UUID `call_id`.
- Campaign name, phone number, and direction (`browser` or `twilio`).
- A `CallStateMachine`.
- Start/end timestamps.
- Transcript turns.
- Extracted answers.
- References to Deepgram and telephony connections.
- Arbitrary metadata, including lead information and provider IDs.

It also provides compatibility aliases such as `started_at`, `ended_at`, `answers`, and `duration`.

`add_turn()` normalizes roles/content and deduplicates consecutive duplicate transcript turns. `finish()` maps high-level statuses like `completed`, `client_disconnected`, `hung_up`, `error`, and `failed` to state-machine terminal states while preserving custom final statuses in metadata.

### State machine

`app/telephony/state_machine.py` defines the allowed lifecycle:

```text
CREATED -> CONNECTING -> CONNECTED -> AI_ACTIVE -> AI_FINISHED -> EXTRACTION -> COMPLETED
```

Failure/hangup paths can move to `FAILED` or `HUNG_UP` from non-terminal states. Terminal states are `COMPLETED`, `FAILED`, and `HUNG_UP`.

Some code paths use `safe_transition_to()`, which ignores invalid transitions instead of raising. This lets adapters finalize sessions defensively when an external provider disconnects or an AI close event arrives at an unexpected moment.

### CallManager

`app/telephony/call_manager.py` owns in-memory active sessions and simple event subscription/emission. It can create, look up, destroy, and list active sessions. Browser calls use the `CallManager` instance in `app/main.py`; Twilio routes have their own module-level `CallManager` in `app/telephony/twilio_routes.py`.

## 6. Realtime AI conversation engine

`app/core/conversation/conversation_engine.py` is provider-specific to Deepgram but telephony-agnostic. It is responsible for:

1. Opening a Deepgram Agent connection.
2. Sending Deepgram Agent settings, including prompt and lead context.
3. Forwarding inbound PCM audio frames to Deepgram.
4. Receiving Deepgram output audio and transcript/control events.
5. Adding cleaned transcript turns to the session.
6. Detecting terminal AI events or terminal assistant text.
7. Notifying the adapter bridge when the AI is done.

Important behavior:

- It refuses to start if `DEEPGRAM_API_KEY` is missing.
- `connection.start_listening()` runs in a daemon thread while the FastAPI event loop remains async.
- Binary Deepgram messages are treated as audio and sent to `on_audio`.
- `ConversationText` messages or messages with `role` and `content` are treated as transcript text.
- Assistant text is passed through `strip_spoken_internal_commands()` to remove leaked internal assignment-like snippets.
- `_closing_call` tool/function signals and some natural terminal phrases mark the call as ending.
- Deepgram errors are recorded in session metadata and emitted to the text callback.

## 7. AudioBridge: adapter-neutral audio coordination

`app/telephony/audio/audio_bridge.py` sits between telephony adapters and the `ConversationEngine`.

It provides a small adapter-neutral contract:

- `start()` starts the Deepgram conversation engine.
- `receive_telephony_audio(frame)` normalizes inbound audio and passes it to Deepgram.
- `next_output()` lets adapters await outbound queue items.
- `stop(status)` stops the engine and either finalizes the result through `CallResultService` or directly finishes the session.

Outbound queue items are tuples:

- `("audio", bytes)` for TTS audio from Deepgram.
- `("text", str)` for JSON transcript/error payloads.
- `("control", '{"event": "closing_call"}')` when the AI is finished.

Currently `_normalize_to_pcm()` is a pass-through because the browser path already sends compatible PCM and the Twilio adapter performs its own codec conversion before calling the bridge.

## 8. Telephony adapters

### BaseTelephonyAdapter

`app/telephony/adapters/base.py` defines the abstract lifecycle methods every telephony adapter should implement:

- `connect()`
- `disconnect()`
- `send_audio()`
- `receive_audio()`
- `hangup()`
- `answer()`
- `start()`
- `stop()`

It also attaches a `CallSession` and stores the adapter on `session.telephony_connection`.

### BrowserAdapter

`app/telephony/adapters/browser_adapter.py` preserves the browser `/ws` behavior.

Flow:

1. Accept the FastAPI WebSocket.
2. Start the `AudioBridge` and Deepgram Agent.
3. Start a background task that reads bridge output and sends it to the browser.
4. Loop over browser binary messages and forward microphone PCM to the bridge.
5. On disconnect/error/control close, stop the bridge and finalize the session.

Text messages are sent to the browser so the dashboard test page can show live transcript lines. Control messages close the browser WebSocket with reason `agent_closing_call`.

### TwilioAdapter

`app/telephony/adapters/twilio_adapter.py` implements outbound Twilio Voice + Media Streams.

Twilio differs from the browser path because the REST call is placed first, and the media WebSocket connects later. To bridge that gap, `TwilioAdapter._pending` maps Twilio `call_sid` values to adapters waiting for their media stream.

Outbound call placement:

1. `connect()` calls Twilio REST `calls.create()`.
2. It sets `to`, `from_`, TwiML URL, status callback URL, status callback events, machine detection, and trimming.
3. It stores the returned `call_sid` in session metadata.
4. It registers itself in `_pending` by `call_sid`.

Media audio handling:

- Twilio sends 8 kHz μ-law audio payloads as base64 JSON media events.
- `receive_audio()` decodes base64, converts μ-law to linear PCM, and resamples from 8 kHz to 48 kHz for Deepgram input.
- Deepgram returns 24 kHz linear16 TTS audio.
- `send_audio()` resamples from 24 kHz to 8 kHz, converts linear PCM to μ-law, base64-encodes it, and sends Twilio media events back.
- A control close causes the adapter to update the Twilio call status to `completed` and close the WebSocket.

Live transcript text from the bridge is intentionally dropped in the Twilio adapter because Twilio Media Streams is audio/control oriented, not a browser transcript channel.

The adapter also has a `build_twiml()` helper that returns TwiML containing a `<Connect><Stream>` to `/media-stream`.

### Future adapters

`AsteriskAdapter` and `GsmGatewayAdapter` are stubs. Every method currently raises `NotImplementedError`. They show the intended extension points but do not provide working integrations.

## 9. Twilio route flow

`app/telephony/twilio_routes.py` defines all Twilio-specific API/webhook endpoints.

### `POST /twilio/outbound`

Starts an outbound sales call. It:

1. Creates a `CallSession` with phone and lead metadata.
2. Creates an `AudioBridge` and `TwilioAdapter`.
3. Attaches the adapter to the session.
4. Inserts an in-memory live-call status of `created`.
5. Calls `adapter.connect()` to place the Twilio call.
6. Updates live status to `dialing` with `call_sid`.
7. Returns `call_id`, `call_sid`, and `status`.

Dashboard lead actions reuse this function directly instead of making an HTTP request to their own server.

### `POST /twilio/twiml`

Twilio calls this after the recipient answers or the call needs TwiML instructions. The route returns XML immediately. It builds a WebSocket URL from `PUBLIC_BASE_URL`, converts `https://` to `wss://`, and instructs Twilio to stream to `/media-stream`.

The route intentionally does not perform expensive work because Twilio webhooks have tight response budgets.

### `POST /twilio/status`

Acknowledges Twilio status callbacks. It parses `CallSid` and `CallStatus` for logging, then returns HTTP 200. Durable status handling is not implemented here; the code comments suggest moving that work to a queue/background process later.

### `WS /media-stream`

This is where live Twilio audio is handled.

1. Accept the WebSocket.
2. Wait for Twilio's `start` event and extract `callSid` and `streamSid`.
3. Look up the matching adapter in `TwilioAdapter._pending`.
4. If no pending adapter exists, create a recovered session.
5. Bind the WebSocket and stream SID to the adapter.
6. Mark live status as `media_connected`, then `ai_active`.
7. Run `adapter.start()` to drive the audio loop.
8. On exit, mark status as `finished`, destroy the active session, and log completion.

## 10. Dashboard and browser UI

`app/static/index.html` is a single-file dashboard containing HTML, CSS, and JavaScript. It provides four main pages:

1. **Overview**: summary cards and simple bar charts for call volume/outcomes.
2. **Leads**: upload/paste/manual lead entry, column mapping, validation, imported lead list, and call actions.
3. **Calls**: live call status plus persisted call history with filters and detail modal.
4. **Browser Test Call**: microphone-to-WebSocket voice test for the AI agent.

The frontend communicates only with the FastAPI endpoints in this app. It does not use a frontend framework or build step.

Browser voice test details:

- Requests microphone access with `getUserMedia()`.
- Creates an `AudioContext` at 48 kHz.
- Installs an inline `AudioWorkletProcessor` that converts float samples to 16-bit PCM and sends them over `/ws`.
- Receives binary PCM from the server and plays it at 24 kHz.
- Receives JSON text/control messages for transcript display and call closure.

## 11. Lead management

Lead persistence is handled by `JsonCallStore` in `app/storage/json_store.py`.

Leads are stored in:

```text
<data_dir>/leads.json
```

Supported lead ingestion paths:

- Excel or CSV upload through `/api/leads/preview`.
- Pasted tabular data through `/api/leads/preview`.
- Manual single-lead entry through `/api/leads/manual`.

The preview endpoint parses source rows and returns headers/rows for dashboard column mapping. The import endpoint receives normalized mappings and calls `import_leads()`.

Validation rules:

- `business_name`, `phone_number`, and `category` are required.
- Phone numbers are cleaned and normalized.
- 10-digit numbers are assumed to be Indian numbers and converted to `+91...`.
- 12-digit numbers beginning with `91` are converted to `+...`.
- Very short phone numbers are rejected.
- Duplicate phone numbers are rejected based on digit-only comparison.
- Scientific notation phone values from spreadsheets are expanded where possible.

Each imported lead gets:

- `lead_id`
- `business_name`
- `phone_number`
- `category`
- `notes`
- `created_at`
- `status: "new"`

Lead statuses are updated when dashboard calls start, fail to start, or final results are saved.

## 12. Call result finalization and persistence

`CallResultService` in `app/services/call_service.py` is the adapter-agnostic finalization service.

When a bridge stops with a result service attached, it:

1. Finishes the session if needed.
2. Transitions toward extraction.
3. Calls `AnswerExtractor.extract(session)`.
4. Saves the completed call through `JsonCallStore.append_call()`.
5. Updates the related lead status if `lead_id` exists.
6. Updates the in-memory live-call status feed.

Call results are stored as one JSON file per call:

```text
<data_dir>/call_results/<call_id>.json
```

Each record contains:

- Call identifiers and lead metadata.
- Final call status.
- Boolean `interested` and `callback_requested` convenience fields.
- Summary string.
- Full transcript text.
- Structured extraction response.
- Duration.
- Timestamp.

`JsonCallStore` also supports exporting calls as JSON, CSV, or XLSX through `/api/export/{fmt}` and generating an XLSX lead-upload template through `/api/leads/template`.

## 13. Post-call answer extraction

`app/services/answer_extractor.py` uses the OpenAI Responses API with JSON Schema structured output.

Extraction behavior:

- Requires `OPENAI_API_KEY`.
- Imports `OpenAI` lazily when extraction runs.
- Sends a system prompt describing the role as a strict Hindi/Hinglish post-call QA analyst.
- Sends the call status and transcript plus the answer-field definitions.
- Requires all configured fields in the JSON schema.
- Parses `response.output_text` first, with a compatibility fallback for mocked/older response shapes.
- Normalizes invalid enum values to `unknown`.

If OpenAI is unavailable, the package is missing, or the response cannot be parsed, `AnswerExtractionError` is raised and final save fails. The failure is recorded in the in-memory call status tracker as `result_save_failed`.

## 14. Live call status

`app/services/call_status_tracker.py` implements a process-local in-memory status feed for dashboard-started calls.

It stores a dictionary by `call_id`, tracks `created_at`, `updated_at`, current `status`, metadata such as call SID and lead information, and an event list of status transitions.

This is intentionally not durable. Restarting the server clears live status, but persisted call results remain in the data directory.

## 15. API surface summary

### Dashboard/general

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/` | Serve dashboard HTML |
| `GET` | `/health` | Health check and data directory |
| `GET` | `/api/stats` | Dashboard aggregate stats |
| `GET` | `/api/live-calls` | In-memory live call statuses |

### Leads

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/api/leads` | List imported leads |
| `POST` | `/api/leads/preview` | Parse uploaded/pasted leads for preview/mapping |
| `POST` | `/api/leads/import` | Import mapped lead rows |
| `POST` | `/api/leads/manual` | Add a single lead manually |
| `POST` | `/api/leads/{lead_id}/call` | Start a Twilio call for one lead |
| `POST` | `/api/leads/call-batch` | Start Twilio calls for selected/filtered leads |
| `GET` | `/api/leads/template` | Download lead-upload template |

### Calls/results

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/api/calls` | List persisted call results |
| `GET` | `/api/calls/{call_id}` | Fetch one persisted call result |
| `GET` | `/api/export/{fmt}` | Export results as `xlsx`, `csv`, or `json` |

### Browser realtime

| Method | Path | Purpose |
| --- | --- | --- |
| `WS` | `/ws` | Browser microphone/audio test call |

### Twilio

| Method | Path | Purpose |
| --- | --- | --- |
| `POST` | `/twilio/outbound` | Place outbound Twilio sales call |
| `POST` | `/twilio/twiml` | Return TwiML that connects media stream |
| `POST` | `/twilio/status` | Acknowledge/log Twilio status callback |
| `WS` | `/media-stream` | Twilio Media Streams audio WebSocket |

## 16. End-to-end flows

### Browser test call flow

```text
Browser dashboard
  -> GET / serves index.html
  -> Browser requests microphone
  -> Browser opens WS /ws
  -> app creates browser CallSession
  -> BrowserAdapter accepts socket
  -> AudioBridge starts ConversationEngine
  -> ConversationEngine opens Deepgram Agent connection
  -> Browser sends 48 kHz linear16 PCM
  -> Deepgram receives audio and returns transcript/TTS audio
  -> BrowserAdapter sends transcript JSON and 24 kHz PCM back to browser
  -> AI terminal signal/client disconnect/error occurs
  -> AudioBridge finalizes call with CallResultService
  -> AnswerExtractor extracts structured answers through OpenAI
  -> JsonCallStore writes data/call_results/<call_id>.json
```

If `DEEPGRAM_API_KEY` is missing, `/ws` accepts the WebSocket, sends an error JSON payload, closes it, destroys the session, and does not start the Deepgram engine.

### Dashboard lead-to-Twilio flow

```text
Dashboard imports lead
  -> POST /api/leads/{lead_id}/call
  -> app loads lead from JsonCallStore
  -> app builds OutboundCallRequest
  -> start_outbound_call() creates Twilio CallSession
  -> TwilioAdapter.connect() places Twilio REST call
  -> Twilio receives /twilio/twiml instructions
  -> Twilio opens WS /media-stream
  -> media_stream binds callSid to pending TwilioAdapter
  -> TwilioAdapter converts Twilio audio to Deepgram PCM
  -> ConversationEngine runs the AI conversation
  -> Deepgram TTS PCM is converted back to Twilio μ-law media
  -> AI terminal signal/caller hangup/error occurs
  -> AudioBridge finalizes result through OpenAI extraction
  -> JsonCallStore persists call result and updates lead status
```

### Batch calling flow

```text
Dashboard selects visible leads
  -> POST /api/leads/call-batch
  -> app filters out leads already marked calling
  -> app loops up to max_calls
  -> each lead calls start_outbound_call()
  -> successes update lead status to calling
  -> failures update lead status to call_failed with last_error
```

The batch implementation starts calls sequentially in a loop. It does not currently implement concurrency limits beyond `max_calls`, queueing, retry scheduling, or provider-rate backoff.

## 17. Testing footprint

The repository includes tests for important non-realtime behavior:

- Prompt safety expectations.
- Answer extraction behavior.
- Call result service behavior.
- Call control / terminal phrase detection.
- Call status tracker.
- JSON and Excel storage behavior.
- Transcript sanitizer.
- Static configuration expectations.

The live Deepgram, Twilio, browser microphone, and OpenAI network paths are integration/runtime concerns and are not fully exercised by local unit tests.

## 18. Current limitations and risks

1. **Local JSON storage only**: leads and call results are file-based. This is simple but not ideal for multiple app instances or high write concurrency.
2. **Live status is in-memory**: `/api/live-calls` resets when the process restarts.
3. **Twilio and browser use separate CallManager instances**: this is workable now, but a shared application-scoped manager or repository would be cleaner for unified operations.
4. **Post-call save depends on OpenAI**: if `OPENAI_API_KEY` is missing or extraction fails, result persistence fails because extraction and save are in the same try block.
5. **Twilio status webhook only logs**: durable status reconciliation from Twilio callbacks is not implemented.
6. **Batch calls are sequential trigger loops**: no queue, scheduler, retry policy, campaign pacing, or rate limiting exists yet.
7. **Asterisk/GSM adapters are placeholders**: only browser and Twilio are functional realtime adapters.
8. **Audio conversion uses `audioop`**: `audioop` is deprecated in newer Python versions, so a future replacement may be needed.
9. **Dashboard is a single static HTML file**: easy to deploy, but complex UI changes may become hard to maintain.
10. **Prompt says not to reveal AI identity**: this is current behavior in the application prompt, but it may have legal/compliance implications depending on deployment jurisdiction and should be reviewed before production use.
11. **Secrets/default public URL are in settings defaults**: deployment should override defaults through environment variables and avoid relying on development tunnel values.

## 19. Mental model for future development

The cleanest way to reason about the app is as four layers:

1. **Dashboard/API layer**: FastAPI routes and static HTML manage leads, calls, stats, and exports.
2. **Telephony adapter layer**: Browser and Twilio adapters translate provider-specific socket/audio protocols into a common PCM-oriented bridge.
3. **Conversation layer**: `AudioBridge` and `ConversationEngine` run the Deepgram Agent session and maintain transcript/session state.
4. **Persistence/extraction layer**: `CallResultService`, `AnswerExtractor`, and `JsonCallStore` convert a completed session into durable structured call results.

When adding a new provider, implement a new `BaseTelephonyAdapter` that can attach a `CallSession`, feed PCM to `AudioBridge.receive_telephony_audio()`, and consume `AudioBridge.next_output()` for AI audio/control. When changing campaign logic, update `app/core/prompts.py` and the answer fields/schema together. When changing persistence, replace `JsonCallStore` behind the existing service and route interfaces.
