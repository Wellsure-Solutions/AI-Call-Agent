# Autonomous Calling Agent Expansion Plan

## Current Base

The current codebase is now structured around a reusable Deepgram call bridge, a temporary prompt-driven answer schema, and an Excel answer store. It keeps the existing browser WebSocket flow working while making the core call lifecycle reusable for future Asterisk/GSM gateway integration.

## Level 1 — Stable Local Voice Agent

- Keep Deepgram as the real-time speech/listen/think/speak provider.
- Use the browser UI for manual test calls and audio debugging.
- Save one row per call to `data/call_answers.xlsx`.
- Keep temporary campaign questions in `prompts.py` so the prompt and Excel columns can change together without adding a database.
- Add operational health checks and environment-based configuration.

## Level 2 — Campaign and Prompt Modularity

- Move campaign configuration into versioned files, for example `campaigns/seller_acquisition.py` or JSON/YAML campaign files.
- Define each campaign with:
  - prompt text,
  - answer fields,
  - voice settings,
  - max call duration,
  - disqualification rules,
  - callback rules.
- Generate the Deepgram agent prompt and Excel columns from the same campaign definition.
- Add validation that every prompt field has a matching export column.

## Level 3 — Asterisk/GSM Gateway Integration

- Add a telephony adapter layer separate from Deepgram logic.
- Start with Asterisk ARI or AudioSocket depending on the gateway setup.
- Convert GSM/Asterisk audio to the same PCM format already expected by the Deepgram bridge.
- Track call metadata from Asterisk:
  - caller ID,
  - dialed number,
  - channel ID,
  - call direction,
  - hangup cause.
- Keep the browser adapter as a test adapter, not as production telephony code.

## Level 4 — Autonomous Dialing Workflow

- Add a lead input file for temporary campaigns, such as CSV/XLSX.
- Implement a dialer queue with rate limits, retry limits, and quiet hours.
- Add call states:
  - queued,
  - dialing,
  - connected,
  - completed,
  - failed,
  - retry_scheduled,
  - do_not_call.
- Export both call results and retry decisions to Excel until a database is introduced.
- Add safeguards for manual stop, max concurrency, and provider outages.

## Level 5 — Reliability and Observability

- Add structured JSON logs with call IDs and channel IDs.
- Store raw transcripts separately from answer summaries.
- Add metrics for:
  - call connect rate,
  - average duration,
  - completion rate,
  - callback approval rate,
  - extraction confidence.
- Add test fixtures for transcripts and answer extraction.
- Add alerting for Deepgram/Asterisk connection failures.

## Level 6 — Data Layer Upgrade

- Replace the temporary Excel-only export with a database when campaigns become stable.
- Suggested entities:
  - campaigns,
  - leads,
  - calls,
  - transcript turns,
  - extracted answers,
  - callback tasks.
- Keep Excel export as a reporting feature.
- Add migrations and admin tools.

## Level 7 — Production Controls

- Add authentication for the dashboard and control APIs.
- Add role-based access for campaign operators and admins.
- Add consent, DNC, audit-log, and retention policies according to the calling region.
- Add prompt safety checks and escalation rules.
- Add human handoff for qualified callbacks or uncertain conversations.

## Level 8 — Optimization and Intelligence

- Use post-call LLM extraction with confidence scores once the base flow is stable.
- Add A/B tests for prompts and voices.
- Add language and intent detection.
- Add automatic campaign performance summaries.
- Add a feedback loop from specialist callback outcomes into lead scoring.
