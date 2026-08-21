from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class AudioProfile:
    """Audio format exchanged directly between an adapter and Deepgram."""

    encoding: str
    input_sample_rate: int
    output_sample_rate: int
    container: str = "none"


BROWSER_AUDIO_PROFILE = AudioProfile(
    encoding="linear16",
    input_sample_rate=48000,
    output_sample_rate=24000,
)

# Every phone leg speaks 8 kHz mu-law to Deepgram, whichever carrier is
# underneath. Exotel's *wire* format is 16-bit linear PCM, but ExotelAdapter
# transcodes at its socket boundary precisely so that this profile -- and the
# .ulaw greeting/closing cache, the barge-in RMS maths, and
# conversation_engine's `encoding != "mulaw"` guard -- stay true for both
# carriers. Adding a second profile would have pushed PCM up through the
# bridge and invalidated all of it.
TELEPHONY_AUDIO_PROFILE = AudioProfile(
    encoding="mulaw",
    input_sample_rate=8000,
    output_sample_rate=8000,
)

# Historical name, kept because it is imported elsewhere and reads correctly
# from a Twilio-only vantage point.
TWILIO_AUDIO_PROFILE = TELEPHONY_AUDIO_PROFILE

# Transports that carry a phone call rather than a browser microphone. Keyed
# on the call's persisted provider, never on the currently-selected one: a
# call enqueued under Twilio must keep reporting Twilio for its whole life.
_TELEPHONY_TRANSPORTS = frozenset({"twilio", "exotel"})


def get_audio_profile(transport: str) -> AudioProfile:
    """Return the wire format used by the selected telephony transport."""

    if transport in _TELEPHONY_TRANSPORTS:
        return TELEPHONY_AUDIO_PROFILE
    return BROWSER_AUDIO_PROFILE
