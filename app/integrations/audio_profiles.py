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

TWILIO_AUDIO_PROFILE = AudioProfile(
    encoding="mulaw",
    input_sample_rate=8000,
    output_sample_rate=8000,
)


def get_audio_profile(transport: str) -> AudioProfile:
    """Return the wire format used by the selected telephony transport."""

    if transport == "twilio":
        return TWILIO_AUDIO_PROFILE
    return BROWSER_AUDIO_PROFILE