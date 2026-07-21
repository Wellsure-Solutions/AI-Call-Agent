from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class AudioInterface(Protocol):
    """PCM-only interface between telephony adapters and conversation engines."""

    async def receive_audio(self, pcm_frame: bytes) -> None:
        """Receive one PCM frame from telephony."""

    def on_audio(self, pcm_frame: bytes) -> None:
        """Emit one PCM frame back toward telephony."""

    def on_text(self, payload: str) -> None:
        """Emit an adapter-neutral text/control payload."""

    def on_finished(self) -> None:
        """Notify the bridge that AI conversation has finished."""
