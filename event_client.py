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
        self._send_lock = asyncio.Lock()

        self._reader_task = None
        self._incoming_queue: asyncio.Queue = asyncio.Queue()
        self._pending_requests: dict[str, tuple[str, asyncio.Future]] = {}

        self._connected_event = asyncio.Event()
        self._closing = False

    async def connect(self):
        if self.ws is not None and self._reader_task is not None and not self._reader_task.done():
            return

        while not self._closing:
            try:
                self.ws = await websockets.connect(self.url)

                await self.ws.send(json.dumps({
                    "type": "hello",
                    "client_id": self.client_id,
                }))

                self._connected_event.set()
                self._reader_task = asyncio.create_task(self._reader_loop())

                print(f"[{self.client_id}] connected to event_bus", flush=True)
                return

            except Exception:
                self.ws = None
                self._connected_event.clear()
                print(f"[{self.client_id}] waiting for event_bus...", flush=True)
                await asyncio.sleep(1.0)

    async def _handle_disconnect(self):
        self._connected_event.clear()

        if self.ws is not None:
            try:
                await self.ws.close()
            except Exception:
                pass
        self.ws = None

        for req_id, (_rtype, fut) in list(self._pending_requests.items()):
            if not fut.done():
                fut.set_exception(ConnectionError("event_bus disconnected"))
        self._pending_requests.clear()

    async def _reader_loop(self):
        try:
            while not self._closing and self.ws is not None:
                raw = await self.ws.recv()
                evt = json.loads(raw)

                req_id = evt.get("request_id")
                if req_id and req_id in self._pending_requests:
                    expected_type, fut = self._pending_requests[req_id]

                    # Игнорируем эхо собственного request-а и ждём именно response_type
                    if evt.get("type") == expected_type:
                        self._pending_requests.pop(req_id, None)
                        if not fut.done():
                            fut.set_result(evt)
                        continue

                await self._incoming_queue.put(evt)

        except Exception:
            if not self._closing:
                print(f"[{self.client_id}] reconnecting bus...", flush=True)
        finally:
            await self._handle_disconnect()

            if not self._closing:
                await asyncio.sleep(0.2)
                await self.connect()

    async def _ensure_connected(self):
        if self.ws is None or self._reader_task is None or self._reader_task.done():
            await self.connect()
        await self._connected_event.wait()

    async def emit(self, etype: str, data: dict, request_id: str = ""):
        msg = {
            "type": etype,
            "data": data,
        }
        if request_id:
            msg["request_id"] = request_id

        payload = json.dumps(msg)

        async with self._send_lock:
            await self._ensure_connected()
            try:
                await self.ws.send(payload)
            except Exception:
                await self._handle_disconnect()
                await self.connect()
                await self.ws.send(payload)

    async def next_event(self):
        while True:
            await self._ensure_connected()
            evt = await self._incoming_queue.get()
            return evt

    async def request(
        self,
        etype: str,
        data: dict,
        response_type: str,
        timeout: float = 5.0,
    ):
        req_id = str(uuid.uuid4())
        loop = asyncio.get_running_loop()
        fut = loop.create_future()
        self._pending_requests[req_id] = (response_type, fut)

        try:
            await self.emit(etype, data, request_id=req_id)
            evt = await asyncio.wait_for(fut, timeout=timeout)
            return evt
        finally:
            self._pending_requests.pop(req_id, None)

    async def close(self):
        self._closing = True
        self._connected_event.clear()

        if self._reader_task is not None:
            self._reader_task.cancel()
            await asyncio.gather(self._reader_task, return_exceptions=True)
            self._reader_task = None

        if self.ws is not None:
            try:
                await self.ws.close()
            except Exception:
                pass
            self.ws = None
