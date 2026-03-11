#!/usr/bin/env python3
import asyncio
import json
import uuid

import websockets


class EventClient:
    def __init__(self, client_id: str, url: str = "ws://127.0.0.1:8765"):
        self.client_id = client_id
        self.url = url
        self.ws = None
        self._lock = asyncio.Lock()

    async def connect(self):
        while True:
            try:
                self.ws = await websockets.connect(self.url)

                # hello message для event_bus
                await self.ws.send(json.dumps({
                    "type": "hello",
                    "client_id": self.client_id,
                }))

                print(f"[{self.client_id}] connected to event_bus", flush=True)
                return

            except Exception:
                self.ws = None
                print(f"[{self.client_id}] waiting for event_bus...", flush=True)
                await asyncio.sleep(1.0)

    async def _ensure_connected(self):
        if self.ws is None:
            await self.connect()

    async def emit(self, etype: str, data: dict, request_id: str = ""):
        msg = {
            "type": etype,
            "data": data,
        }
        if request_id:
            msg["request_id"] = request_id

        payload = json.dumps(msg)

        async with self._lock:
            await self._ensure_connected()
            try:
                await self.ws.send(payload)
            except Exception:
                self.ws = None
                await self.connect()
                await self.ws.send(payload)

    async def next_event(self):
        while True:
            await self._ensure_connected()

            try:
                raw = await self.ws.recv()
                return json.loads(raw)

            except Exception:
                print(f"[{self.client_id}] reconnecting bus...", flush=True)
                self.ws = None
                await asyncio.sleep(0.2)

    async def request(
        self,
        etype: str,
        data: dict,
        response_type: str,
        timeout: float = 5.0,
    ):
        req_id = str(uuid.uuid4())

        await self.emit(etype, data, request_id=req_id)

        async def _wait_reply():
            while True:
                evt = await self.next_event()
                if evt.get("type") == response_type and evt.get("request_id") == req_id:
                    return evt

        return await asyncio.wait_for(_wait_reply(), timeout=timeout)

    async def close(self):
        if self.ws is not None:
            try:
                await self.ws.close()
            except Exception:
                pass
            self.ws = None
