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
# underneath. Exotel's and Teler's *wire* format is 16-bit linear PCM, but
# their adapters transcode at the socket boundary precisely so that this
# profile -- and the .ulaw greeting/closing cache, the barge-in RMS maths, and
# conversation_engine's `encoding != "mulaw"` guard -- stay true for every
# carrier. Adding a second profile would have pushed PCM up through the
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
#
# A carrier missing from this set does not fail loudly -- it silently falls
# through to the browser profile and asks Deepgram for 48 kHz linear16 on a
# phone line, so every call connects and then produces noise. Add a carrier
# here in the same change that adds its adapter.
_TELEPHONY_TRANSPORTS = frozenset({"twilio", "exotel", "teler"})


def get_audio_profile(transport: str) -> AudioProfile:
    """Return the wire format used by the selected telephony transport."""

    if transport in _TELEPHONY_TRANSPORTS:
        return TELEPHONY_AUDIO_PROFILE
    return BROWSER_AUDIO_PROFILE
