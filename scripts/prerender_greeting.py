#!/usr/bin/env python3
"""Render the configured greeting once so calls don't pay for it every time.

Measured on a real answered call, the first agent audio arrived 2.0 seconds
after the media stream bound -- a websocket handshake, a settings
round-trip, and speech synthesis, all happening after the customer had
already picked up. The greeting is the one utterance known before the call
exists, so none of that needs to be on the critical path.

    python scripts/prerender_greeting.py            # render and cache
    python scripts/prerender_greeting.py --check    # is the cache current?
    python scripts/prerender_greeting.py --wav      # also write a listenable copy

Run this after changing DEEPGRAM_GREETING, the voice, the TTS model, or the
speak language. The cache key covers all of them, so a stale file is ignored
rather than played -- the call simply falls back to the old behaviour.

Rendering goes through the same Deepgram agent and the same speak provider
the calls use, so the cached greeting is in exactly the same voice as the
rest of the conversation. Rendering it through a different TTS path would
make the agent audibly change voice one sentence in.
"""

from __future__ import annotations

import argparse
import struct
import sys
import threading
import time
import wave
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.settings import (  # noqa: E402
    DEEPGRAM_API_KEY,
    DEEPGRAM_FALLBACK_CLOSING,
    DEEPGRAM_LISTEN_MODEL,
    GREETING_CACHE_DIR,
)
from app.integrations.deepgram.config import (  # noqa: E402
    _speak_provider_settings,
    closing_fingerprint_for_current_config,
    greeting_fingerprint_for_current_config,
    resolve_greeting,
)
from app.telephony.audio.greeting_cache import cache_path, load_greeting, save_greeting  # noqa: E402
from app.telephony.audio.local_vad import _MULAW_DECODE_TABLE  # noqa: E402

RENDER_TIMEOUT_SECONDS = 40


def render(text: str) -> bytes:
    from deepgram import DeepgramClient
    from deepgram.core.events import EventType
    from deepgram.agent.v1.types import (
        AgentV1Settings, AgentV1SettingsAgent, AgentV1SettingsAudio,
        AgentV1SettingsAudioInput, AgentV1SettingsAudioOutput,
    )

    settings = AgentV1Settings(
        audio=AgentV1SettingsAudio(
            input=AgentV1SettingsAudioInput(encoding="mulaw", sample_rate=8000),
            output=AgentV1SettingsAudioOutput(encoding="mulaw", sample_rate=8000, container="none"),
        ),
        agent=AgentV1SettingsAgent(
            listen={"provider": {"type": "deepgram", "version": "v2", "model": DEEPGRAM_LISTEN_MODEL}},
            # No audio is ever sent, so the model never runs. Only the
            # greeting is synthesised.
            think={"provider": {"type": "open_ai", "model": "gpt-4o-mini"}, "prompt": "Say nothing."},
            speak={"provider": _speak_provider_settings()},
            greeting=text,
        ),
    )

    audio = bytearray()
    done = threading.Event()
    failure: list[str] = []

    def on_message(message):
        if isinstance(message, bytes):
            audio.extend(message)
        elif str(getattr(message, "type", "")) == "AgentAudioDone":
            done.set()
        elif str(getattr(message, "type", "")) in {"Error", "Warning"}:
            failure.append(f"{getattr(message, 'code', '')}: {getattr(message, 'description', '')}")

    client = DeepgramClient(api_key=DEEPGRAM_API_KEY)
    context = client.agent.v1.connect()
    connection = context.__enter__()
    try:
        connection.on(EventType.MESSAGE, on_message)
        connection.on(EventType.ERROR, lambda e: failure.append(repr(e)[:200]))
        connection.send_settings(settings)
        threading.Thread(target=connection.start_listening, daemon=True).start()
        deadline = time.monotonic() + RENDER_TIMEOUT_SECONDS
        while not done.is_set() and time.monotonic() < deadline:
            try:
                connection.send_keep_alive()
            except Exception:
                break
            time.sleep(0.4)
    finally:
        try:
            context.__exit__(None, None, None)
        except Exception:
            pass

    if failure:
        raise RuntimeError("provider reported: " + "; ".join(failure[:3]))
    if not audio:
        raise RuntimeError("no audio returned; check DEEPGRAM_API_KEY and the speak provider settings")
    return bytes(audio)


def write_wav(destination: Path, mulaw: bytes) -> None:
    pcm = struct.pack(f"<{len(mulaw)}h", *(_MULAW_DECODE_TABLE[b] for b in mulaw))
    with wave.open(str(destination), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(8000)
        handle.writeframes(pcm)


def _miss_consequence(kind: str) -> str:
    if kind == "greeting":
        return "calls will wait on the provider to synthesise the greeting"
    return "a call the model refuses to close will end in silence"


def render_one(kind: str, text: str, fingerprint: str, args) -> int:
    """Render one cached phrase. Returns a process-exit-style status."""
    path = cache_path(GREETING_CACHE_DIR, fingerprint, kind)
    existing = load_greeting(GREETING_CACHE_DIR, fingerprint, kind)

    print(f"\n[{kind}]")
    print(f"  text       : {text!r}")
    print(f"  fingerprint: {fingerprint}")
    print(f"  cache path : {path}")

    if not text:
        print("  status     : not configured -- nothing to render")
        return 0
    if existing is not None and not args.force:
        print(f"  status     : CURRENT ({len(existing)} bytes, {len(existing) / 8000:.2f}s)")
        return 0
    if args.check:
        print(f"  status     : MISSING -- {_miss_consequence(kind)}")
        return 1
    if not DEEPGRAM_API_KEY:
        print("  status     : cannot render, DEEPGRAM_API_KEY is not set", file=sys.stderr)
        return 2

    print("  status     : rendering...")
    try:
        audio = render(text)
    except Exception as error:
        print(f"  status     : FAILED -- {error}", file=sys.stderr)
        return 3
    saved = save_greeting(GREETING_CACHE_DIR, fingerprint, audio, kind)
    print(f"  status     : WROTE {saved} ({len(audio)} bytes, {len(audio) / 8000:.2f}s)")
    if args.wav:
        listenable = saved.with_suffix(".wav")
        write_wav(listenable, audio)
        print(f"               {listenable}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--check", action="store_true", help="report cache state without rendering")
    parser.add_argument("--wav", action="store_true", help="also write a listenable .wav next to the cache")
    parser.add_argument("--force", action="store_true", help="re-render even if the cache is current")
    args = parser.parse_args()

    phrases = (
        # The resolved text, never the raw setting: rendering the raw one
        # would bake an unsubstituted "{Business Name}" into the cache and
        # play it to every caller.
        ("greeting", resolve_greeting(None), greeting_fingerprint_for_current_config()),
        ("closing", DEEPGRAM_FALLBACK_CLOSING, closing_fingerprint_for_current_config()),
    )
    # Worst status wins, so a missing closing still fails --check even when
    # the greeting is current.
    return max(render_one(kind, text, fingerprint, args) for kind, text, fingerprint in phrases)


if __name__ == "__main__":
    raise SystemExit(main())
