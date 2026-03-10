#!/usr/bin/env python3

import asyncio
import json
import uuid
from typing import Any, Dict, Optional, Tuple

import websockets


BUS_URL = "ws://127.0.0.1:8765"


class EventClient:

    def __init__(self, source: str, url: str = BUS_URL):
        self.source = source
        self.url = url
        self.ws = None

        self.queue: asyncio.Queue[Dict[str, Any]] = asyncio.Queue()

        self._reader_task: Optional[asyncio.Task] = None
        self._pending: Dict[str, Tuple[str, asyncio.Future]] = {}

    async def connect(self):
        self.ws = await websockets.connect(self.url)
        self._reader_task = asyncio.create_task(self._reader_loop())

    async def close(self):
        if self._reader_task:
            self._reader_task.cancel()
            await asyncio.gather(self._reader_task, return_exceptions=True)
            self._reader_task = None

        if self.ws:
            await self.ws.close()
            self.ws = None

    async def _reader_loop(self):
        async for raw in self.ws:
            evt = json.loads(raw)

            req_id = evt.get("request_id")
            evt_type = evt.get("type")

            if req_id and req_id in self._pending:
                expected_type, fut = self._pending[req_id]

                if not fut.done() and evt_type == expected_type:
                    fut.set_result(evt)
                    continue

            await self.queue.put(evt)

    async def emit(self, event_type: str, data: dict, request_id: str = ""):
        evt = {
            "type": event_type,
            "source": self.source,
            "ts": 0,
            "data": data,
        }

        if request_id:
            evt["request_id"] = request_id

        await self.ws.send(json.dumps(evt, ensure_ascii=False))


    async def request(self, event_type: str, data: dict, response_type: str, timeout: float = 10.0):
        request_id = str(uuid.uuid4())

        fut = asyncio.get_running_loop().create_future()
        self._pending[request_id] = (response_type, fut)

        try:
            await self.emit(event_type, data, request_id=request_id)
            result = await asyncio.wait_for(fut, timeout=timeout)
            return result
        finally:
            self._pending.pop(request_id, None)


    async def next_event(self) -> Dict[str, Any]:
        return await self.queue.get()
