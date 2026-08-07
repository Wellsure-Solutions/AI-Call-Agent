from __future__ import annotations

"""Per-call instrumentation for the Twilio <-> Deepgram voice path.

Everything here is measurement only: no method in this module changes a
turn-taking, barge-in, or playback decision. That separation is deliberate --
tuning commits that follow can be judged against numbers this file produced
before anything moved.

Privacy contract, enforced by construction rather than by review:
  * Event payloads carry numbers, enum-like short strings, and ISO
    timestamps. Nothing else.
  * Assistant/user speech is reduced to a character count by the caller
    before it ever reaches this class -- `assistant_characters(count)` takes
    an int, not a string, so transcript text cannot be logged by accident.
  * Media tokens, CallSids, phone numbers, and provider keys are never
    passed in.

Timing anchors use `time.monotonic()` (immune to clock steps mid-call).
A single wall-clock stamp is recorded at bind so the analysis pass can line
these events up against the provider webhook events already in
`call_events`, which are wall-clock only.
"""

import asyncio
import logging
import math
import queue
import threading
import time
from contextlib import suppress
from datetime import datetime, timezone
from typing import Any, Callable

from app.telephony.audio.local_vad import rms_energy

logger = logging.getLogger(__name__)

# One buffered event is a (event_name, metadata) pair destined for the
# existing `call_events` table.
Event = tuple[str, dict[str, Any]]
EventSink = Callable[[list[Event]], None]

# An agent "turn" boundary, used only when Deepgram does not send
# AgentStartedSpeaking. If no agent frame has been handed to the transport
# for this long, the next frame starts a new turn.
_AGENT_IDLE_TURN_GAP_MS = 400

# Outbound frames are 20ms; treat the agent as still speaking for a little
# longer than one frame period so normal inter-frame scheduling jitter does
# not read as a gap.
_AGENT_PLAYING_WINDOW_MS = 100


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _rms_bucket(value: float) -> str:
    """Log2 bucket label for an RMS value.

    A histogram keeps per-call memory bounded and constant regardless of call
    length, which a sample reservoir does not. Buckets are wide enough that
    the shape is readable and narrow enough to site a threshold between the
    noise floor and speech.
    """
    if value < 1.0:
        return "0"
    return str(1 << int(math.log2(value)))


def _db_bucket(ratio: float) -> str:
    """3 dB buckets for a caller/agent amplitude ratio, clamped to +/-30 dB."""
    if ratio <= 0:
        return "<=-30"
    decibels = 20 * math.log10(ratio)
    clamped = max(-30.0, min(30.0, decibels))
    return str(int(clamped // 3) * 3)


class CallMetrics:
    """Collects timing, acoustic, and cost measurements for one call.

    Thread-safe: Deepgram's SDK delivers messages on its own listener thread
    while the Twilio adapter runs on the event loop, so both feed this object
    concurrently.
    """

    def __init__(
        self,
        call_id: str,
        sink: EventSink | None = None,
        *,
        voice_threshold: float = 400.0,
        silence_gap_ms: int = 1500,
        flush_every: int = 32,
    ) -> None:
        self.call_id = call_id
        self._sink = sink
        self._voice_threshold = voice_threshold
        self._silence_gap_ms = silence_gap_ms
        self._flush_every = max(1, flush_every)

        self._lock = threading.Lock()
        self._buffer: list[Event] = []

        self._bound_at: float | None = None
        self._bound_wall: str | None = None

        # Inbound (caller) state
        self._last_voiced_at: float | None = None
        self._inbound_frames = 0
        self._voiced_frames = 0
        self._silence_started_at: float | None = None
        self._silence_gaps = 0
        self._silence_total_ms = 0.0
        self._rms_quiet: dict[str, int] = {}   # agent silent: true line noise floor
        self._rms_playing: dict[str, int] = {}  # agent speaking: noise floor + echo
        self._echo_ratio: dict[str, int] = {}   # caller RMS / concurrent agent RMS

        # Outbound (agent) state
        self._last_outbound_at: float | None = None
        self._last_agent_rms: float = 0.0
        self._outbound_frames = 0
        self._first_audio_at: float | None = None
        self._turn_index = 0
        self._turn_open = False           # a provider turn is awaiting its first frame
        self._turn_started_at: float | None = None
        self._turn_source = "heuristic"   # or "provider" when AgentStartedSpeaking fires

        # Cost inputs
        self._assistant_characters = 0
        self._assistant_messages = 0
        self._user_messages = 0

        # Barge-in accounting
        self._barge_in_commits = 0
        self._barge_in_resumes = 0
        self._barge_in_timeouts = 0

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    def bind(self) -> None:
        """Anchor the call clock at the moment the media stream is bound.

        This is the earliest instant this process can act on; the true
        answer instant comes from Twilio's status webhook and is joined in
        during analysis using the wall-clock stamp recorded here.
        """
        with self._lock:
            if self._bound_at is not None:
                return
            self._bound_at = time.monotonic()
            self._bound_wall = _utcnow()
            self._append_locked("metrics_bound", {"voice_threshold": self._voice_threshold})

    def deepgram_started(self) -> None:
        """Deepgram settings have been sent; the agent can now produce audio."""
        self._stamp("metrics_agent_ready", {})

    # ------------------------------------------------------------------
    # Inbound caller audio
    # ------------------------------------------------------------------
    def observe_inbound(self, frame: bytes) -> None:
        """Measure one caller frame. Never changes any decision."""
        if not frame:
            return
        now = time.monotonic()
        energy = rms_energy(frame)
        voiced = energy >= self._voice_threshold
        with self._lock:
            self._inbound_frames += 1
            playing = self._agent_playing_locked(now)
            bucket = _rms_bucket(energy)
            if playing:
                self._rms_playing[bucket] = self._rms_playing.get(bucket, 0) + 1
                if self._last_agent_rms > 0:
                    ratio = _db_bucket(energy / self._last_agent_rms)
                    self._echo_ratio[ratio] = self._echo_ratio.get(ratio, 0) + 1
            else:
                self._rms_quiet[bucket] = self._rms_quiet.get(bucket, 0) + 1

            if voiced:
                self._voiced_frames += 1
                self._last_voiced_at = now
            self._track_silence_locked(now, voiced or playing)

    def _track_silence_locked(self, now: float, active: bool) -> None:
        if active:
            if self._silence_started_at is not None:
                gap_ms = (now - self._silence_started_at) * 1000
                if gap_ms >= self._silence_gap_ms:
                    self._silence_gaps += 1
                    self._silence_total_ms += gap_ms
                self._silence_started_at = None
        elif self._silence_started_at is None:
            self._silence_started_at = now

    # ------------------------------------------------------------------
    # Outbound agent audio
    # ------------------------------------------------------------------
    def observe_outbound(self, frame: bytes) -> None:
        """Called immediately after a frame is handed to the Twilio socket.

        This is as close to "the byte reached Twilio" as this process can
        observe. It is deliberately not "Deepgram produced audio" -- the gap
        between those two is exactly what the paced sender and the barge-in
        pause introduce, and it is part of what the customer hears.
        """
        now = time.monotonic()
        with self._lock:
            self._outbound_frames += 1
            self._last_agent_rms = rms_energy(frame) if frame else 0.0
            previous = self._last_outbound_at
            self._last_outbound_at = now

            new_turn = self._turn_open or (
                previous is None or (now - previous) * 1000 >= _AGENT_IDLE_TURN_GAP_MS
            )
            if not new_turn:
                return

            if self._first_audio_at is None:
                self._first_audio_at = now
                self._append_locked("metrics_greeting", {"bind_to_first_audio_ms": self._elapsed_ms_locked(now)})
            else:
                self._turn_index += 1
                payload: dict[str, Any] = {
                    "turn": self._turn_index,
                    "source": self._turn_source if self._turn_open else "heuristic",
                }
                if self._last_voiced_at is not None:
                    payload["eot_to_first_audio_ms"] = round((now - self._last_voiced_at) * 1000, 1)
                if self._turn_open and self._turn_started_at is not None:
                    # Time spent between Deepgram announcing the turn and the
                    # first byte actually leaving for Twilio: our own transport
                    # and pacing cost, isolated from provider latency.
                    payload["provider_signal_to_first_audio_ms"] = round(
                        (now - self._turn_started_at) * 1000, 1
                    )
                self._append_locked("metrics_turn", payload)
            self._turn_open = False
            self._turn_started_at = None
        self._maybe_flush()

    def _agent_playing_locked(self, now: float) -> bool:
        return (
            self._last_outbound_at is not None
            and (now - self._last_outbound_at) * 1000 <= _AGENT_PLAYING_WINDOW_MS
        )

    def _elapsed_ms_locked(self, now: float) -> float:
        if self._bound_at is None:
            return 0.0
        return round((now - self._bound_at) * 1000, 1)

    # ------------------------------------------------------------------
    # Deepgram provider signals (arrive on the SDK listener thread)
    # ------------------------------------------------------------------
    def agent_turn_started(self) -> None:
        """Deepgram reported AgentStartedSpeaking for a new turn."""
        with self._lock:
            self._turn_open = True
            self._turn_started_at = time.monotonic()
            self._turn_source = "provider"

    def latency_report(self, report: dict[str, Any]) -> None:
        """Deepgram's own STT/LLM/TTS split for the turn that just completed.

        These are provider-side seconds measured to the point audio left
        Deepgram, so they decompose -- but never replace -- the end-to-end
        number measured at the Twilio socket.
        """
        payload: dict[str, Any] = {"turn": self._turn_index}
        for source, target in (
            ("stt_latency", "stt_ms"),
            ("ttt_text_latency", "llm_first_text_ms"),
            ("ttt_token_latency", "llm_first_token_ms"),
            ("tts_latency", "tts_ttfb_ms"),
            ("total_latency", "provider_total_ms"),
        ):
            value = report.get(source)
            if isinstance(value, (int, float)):
                payload[target] = round(float(value) * 1000, 1)
        self._stamp("metrics_provider_latency", payload)

    def assistant_characters(self, count: int) -> None:
        """Cost input for TTS. Takes a count, never the text itself."""
        with self._lock:
            self._assistant_characters += max(0, int(count))
            self._assistant_messages += 1

    def user_message(self) -> None:
        with self._lock:
            self._user_messages += 1

    def provider_diagnostic(self, kind: str, code: str | None, description: str | None) -> None:
        """Record a Deepgram Warning/Error code so a silent call has a cause.

        A bad think-model id or a rejected speak provider surfaces here and
        nowhere else; without it the symptom is only "the call was silent".
        """
        self._stamp(
            "metrics_provider_" + kind,
            {"code": str(code or "")[:80], "description": str(description or "")[:200]},
        )

    # ------------------------------------------------------------------
    # Barge-in decisions
    # ------------------------------------------------------------------
    def barge_in_pause(self, agent_playing: bool) -> None:
        self._stamp("metrics_barge_in_pause", {"agent_playing": bool(agent_playing)})

    def barge_in_decision(
        self,
        decision: str,
        *,
        rms: float,
        voiced_ms: int,
        elapsed_ms: float,
        frames: int,
        agent_playing: bool,
        threshold: float | None = None,
    ) -> None:
        with self._lock:
            if decision == "commit":
                self._barge_in_commits += 1
            elif decision == "resume":
                self._barge_in_resumes += 1
            else:
                self._barge_in_timeouts += 1
        self._stamp(
            "metrics_barge_in",
            {
                "decision": decision,
                "rms": round(float(rms), 1),
                "threshold": round(float(self._voice_threshold if threshold is None else threshold), 1),
                "voiced_ms": int(voiced_ms),
                "elapsed_ms": round(float(elapsed_ms), 1),
                "frames": int(frames),
                "agent_playing": bool(agent_playing),
            },
        )

    # ------------------------------------------------------------------
    # Termination
    # ------------------------------------------------------------------
    def finish(self, media_end_reason: str) -> list[Event]:
        """Emit the per-call summary and hand back everything still buffered."""
        now = time.monotonic()
        with self._lock:
            self._track_silence_locked(now, True)
            media_seconds = 0.0 if self._bound_at is None else now - self._bound_at
            # Twilio bills voice per minute, rounded up, so a 20s call and a
            # 55s call cost the same. Both numbers are kept: one is truth,
            # the other is the invoice.
            billable_seconds = int(math.ceil(media_seconds / 60.0) * 60) if media_seconds > 0 else 0
            self._append_locked(
                "metrics_call",
                {
                    "media_end_reason": media_end_reason,
                    "media_seconds": round(media_seconds, 2),
                    "billable_seconds": billable_seconds,
                    "tts_characters": self._assistant_characters,
                    "agent_messages": self._assistant_messages,
                    "user_messages": self._user_messages,
                    "agent_turns": self._turn_index,
                    "inbound_frames": self._inbound_frames,
                    "voiced_frames": self._voiced_frames,
                    "outbound_frames": self._outbound_frames,
                    "silence_gaps": self._silence_gaps,
                    "silence_total_ms": round(self._silence_total_ms, 1),
                    "barge_in_commits": self._barge_in_commits,
                    "barge_in_resumes": self._barge_in_resumes,
                    "barge_in_timeouts": self._barge_in_timeouts,
                },
            )
            self._append_locked(
                "metrics_acoustics",
                {
                    "voice_threshold": self._voice_threshold,
                    "rms_agent_silent": dict(self._rms_quiet),
                    "rms_agent_speaking": dict(self._rms_playing),
                    "caller_over_agent_db": dict(self._echo_ratio),
                },
            )
            return self._drain_locked()

    # ------------------------------------------------------------------
    # Buffering
    # ------------------------------------------------------------------
    def _append_locked(self, name: str, payload: dict[str, Any]) -> None:
        """Buffer one event, stamped with its own wall-clock instant.

        The `call_events.timestamp` column records when a batch was flushed,
        which can trail the measurement by seconds. Analysis that lines these
        up against provider webhooks needs the real instant, so it travels
        inside the payload.
        """
        self._buffer.append((name, {"at": _utcnow(), **payload}))

    def _stamp(self, name: str, payload: dict[str, Any]) -> None:
        with self._lock:
            self._append_locked(name, payload)
        self._maybe_flush()

    def _maybe_flush(self) -> None:
        with self._lock:
            if len(self._buffer) < self._flush_every:
                return
            pending = self._drain_locked()
        self._emit(pending)

    def _drain_locked(self) -> list[Event]:
        pending, self._buffer = self._buffer, []
        return pending

    def drain(self) -> list[Event]:
        with self._lock:
            return self._drain_locked()

    def _emit(self, pending: list[Event]) -> None:
        if not pending or self._sink is None:
            return
        try:
            self._sink(pending)
        except Exception:  # pragma: no cover - instrumentation must never break a call
            pass


class MetricsWriter:
    """Drains buffered metric events to durable storage off the audio path.

    `CallMetrics` is fed from two places that must never block: the asyncio
    event loop driving the Twilio socket, and Deepgram's SDK listener
    thread. Both only ever do a thread-safe `SimpleQueue.put`. This task
    owns the actual SQLite write and moves it off the loop.

    Failures here are logged and dropped. Losing a measurement is always
    preferable to disturbing a live call.
    """

    def __init__(self, store: Any, call_id: str, flush_interval: float = 5.0) -> None:
        self._store = store
        self._call_id = call_id
        self._flush_interval = flush_interval
        self._queue: queue.SimpleQueue[list[Event]] = queue.SimpleQueue()
        self._task: asyncio.Task | None = None
        self._stopped = asyncio.Event()

    def sink(self, events: list[Event]) -> None:
        """Thread-safe, non-blocking, and callable from any thread."""
        if events:
            self._queue.put(events)

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run())

    async def _run(self) -> None:
        try:
            while not self._stopped.is_set():
                with suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(self._stopped.wait(), timeout=self._flush_interval)
                await self.flush()
        except asyncio.CancelledError:
            raise

    async def flush(self) -> None:
        pending: list[Event] = []
        while True:
            try:
                pending.extend(self._queue.get_nowait())
            except queue.Empty:
                break
        if not pending:
            return
        try:
            await asyncio.to_thread(self._store.record_events, self._call_id, pending)
        except Exception:
            logger.warning("call_metrics_persist_failed", extra={"call_id": self._call_id})

    async def stop(self) -> None:
        self._stopped.set()
        if self._task is not None:
            self._task.cancel()
            with suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        await self.flush()


def percentile(values: list[float], fraction: float) -> float | None:
    """Nearest-rank percentile.

    Deliberately not interpolated: with the call volumes this repo runs at,
    reporting a p90 that no individual call actually produced invites false
    precision.
    """
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, math.ceil(fraction * len(ordered)))
    return ordered[min(rank, len(ordered)) - 1]
