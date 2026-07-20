from __future__ import annotations
import asyncio
from uuid import uuid4
from app.storage.sqlite_store import SQLiteCallStore
from app.telephony.adapters.twilio_adapter import TwilioAdapter
from app.telephony.call_session import CallSession

class DurableCallCoordinator:
    def __init__(self, store: SQLiteCallStore, maximum: int, start_interval: float, adapter_factory=TwilioAdapter):
        self.store=store; self.maximum=maximum; self.start_interval=start_interval; self.adapter_factory=adapter_factory
        self.owner=str(uuid4()); self._stop=asyncio.Event()
    async def run(self):
        while not self._stop.is_set():
            job=await asyncio.to_thread(self.store.claim_job,self.owner,self.maximum)
            if not job:
                try: await asyncio.wait_for(self._stop.wait(),.25)
                except asyncio.TimeoutError: pass
                continue
            await self._dial(job)
            if self.start_interval: await asyncio.sleep(self.start_interval)
    async def _dial(self, call):
        session=CallSession(call_id=call["call_id"],phone_number=call["phone_number"],direction="twilio",metadata={"lead_id":call.get("lead_id"),"business_name":call.get("business_name"),"category":call.get("category"),"notes":call.get("notes")})
        adapter=self.adapter_factory(); adapter.attach(session)
        try:
            await adapter.connect()
            await asyncio.to_thread(self.store.bind_call_sid,call["call_id"],adapter.call_sid)
        except Exception as exc:
            await self.store.aprovider_status(call["call_id"],"failed",adapter.call_sid)
    def stop(self): self._stop.set()
