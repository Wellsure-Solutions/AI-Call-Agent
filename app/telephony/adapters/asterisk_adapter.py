from __future__ import annotations

from app.telephony.adapters.base import BaseTelephonyAdapter


class AsteriskAdapter(BaseTelephonyAdapter):
    """Future adapter stub. Implement provider-specific signaling/audio later."""

    async def connect(self) -> None:
        raise NotImplementedError("AsteriskAdapter is a future integration stub.")

    async def disconnect(self) -> None:
        raise NotImplementedError("AsteriskAdapter is a future integration stub.")

    async def send_audio(self, pcm_frame: bytes) -> None:
        raise NotImplementedError("AsteriskAdapter is a future integration stub.")

    async def receive_audio(self) -> bytes | None:
        raise NotImplementedError("AsteriskAdapter is a future integration stub.")

    async def hangup(self) -> None:
        raise NotImplementedError("AsteriskAdapter is a future integration stub.")

    async def answer(self) -> None:
        raise NotImplementedError("AsteriskAdapter is a future integration stub.")

    async def start(self) -> None:
        raise NotImplementedError("AsteriskAdapter is a future integration stub.")

    async def stop(self) -> None:
        raise NotImplementedError("AsteriskAdapter is a future integration stub.")
