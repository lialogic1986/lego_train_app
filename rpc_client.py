import asyncio
import json
from typing import Any, Dict

class BLERpcClient:
    def __init__(self, host="127.0.0.1", port=11000):
        self.host = host
        self.port = port
        self._id = 0

    def _next_id(self) -> int:
        self._id += 1
        return self._id

    async def call(self, cmd: str, **kwargs) -> Dict[str, Any]:
        rid = self._next_id()
        req = {"id": rid, "cmd": cmd, **kwargs}

        reader, writer = await asyncio.open_connection(self.host, self.port)
        writer.write((json.dumps(req, ensure_ascii=False) + "\n").encode("utf-8"))
        await writer.drain()

        line = await reader.readline()
        writer.close()
        await writer.wait_closed()

        if not line:
            raise RuntimeError("no response")
        resp = json.loads(line.decode("utf-8", "replace"))
        if not resp.get("ok"):
            raise RuntimeError(resp.get("err", "unknown error"))
        return resp["data"]
