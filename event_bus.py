#!/usr/bin/env python3

import asyncio
import json
import websockets


class EventBus:

    def __init__(self):

        self.clients = set()

    async def handler(self, ws):

        self.clients.add(ws)

        try:

            async for msg in ws:

                await self.broadcast(msg)

        finally:

            self.clients.remove(ws)

    async def broadcast(self, msg):

        dead = []

        for ws in self.clients:

            try:
                await ws.send(msg)
            except:
                dead.append(ws)

        for ws in dead:
            self.clients.discard(ws)


async def main():

    bus = EventBus()

    async with websockets.serve(
        bus.handler,
        "127.0.0.1",
        8765
    ):

        print("[event_bus] running on ws://127.0.0.1:8765")

        await asyncio.Future()


if __name__ == "__main__":

    asyncio.run(main())
