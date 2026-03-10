#!/usr/bin/env python3

import asyncio
from typing import Set

import websockets


BUS_HOST = "127.0.0.1"
BUS_PORT = 8765


class EventBus:
    def __init__(self):
        self.clients: Set[websockets.WebSocketServerProtocol] = set()

    async def handler(self, ws):
        self.clients.add(ws)
        try:
            async for msg in ws:
                await self.broadcast(msg)
        finally:
            self.clients.discard(ws)

    async def broadcast(self, msg: str):
        dead = []
        for ws in list(self.clients):
            try:
                await ws.send(msg)
            except Exception:
                dead.append(ws)

        for ws in dead:
            self.clients.discard(ws)


async def main():
    bus = EventBus()

    async with websockets.serve(bus.handler, BUS_HOST, BUS_PORT):
        print(f"[event_bus] running on ws://{BUS_HOST}:{BUS_PORT}", flush=True)
        await asyncio.Future()


if __name__ == "__main__":
    asyncio.run(main())
