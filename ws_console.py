#!/usr/bin/env python3
import argparse
import asyncio
import json
import sys
import time
from dataclasses import dataclass, field
from typing import Dict, Optional, Any, Tuple

import websockets
from websockets.legacy.server import serve, WebSocketServerProtocol


@dataclass
class DeviceConn:
    device_id: str
    ws: WebSocketServerProtocol
    last_seen: float = field(default_factory=time.time)


class ConsoleServer:
    def __init__(self, out_path: Optional[str] = None):
        self.devices: Dict[str, DeviceConn] = {}
        self.active_device: Optional[str] = None
        self.cmd_seq = 0
        self.out_fp = open(out_path, "a", buffering=1, encoding="utf-8") if out_path else None
        self._shutdown = asyncio.Event()

    def log(self, line: str) -> None:
        ts = time.strftime("%H:%M:%S")
        s = f"[{ts}] {line}"
        print(s, flush=True)
        if self.out_fp:
            self.out_fp.write(s + "\n")

    @staticmethod
    def _compact(obj: Any) -> str:
        try:
            return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
        except Exception:
            return str(obj)

    def parse_path(self, path: str) -> Tuple[Optional[str], Optional[str]]:
        # Ожидаем /ws/<device_id>
        parts = path.strip("/").split("/")
        if len(parts) == 2 and parts[0] == "ws" and parts[1]:
            return "ws", parts[1]
        return None, None

    def pretty_print_msg(self, device_id: str, msg: Dict[str, Any]) -> None:
        mtype = msg.get("type", "unknown")

        if mtype == "hb":
            self.log(f"{device_id}  ♥ hb  uptime_ms={msg.get('uptime_ms')}  raw={self._compact(msg)}")
            return

        if mtype == "hello":
            self.log(f"{device_id}  👋 hello  fw={msg.get('fw')} caps={msg.get('caps')}")
            return

        if mtype == "log":
            # {"type":"log","ts":...,"msg":"..."}
            self.log(f"{device_id}  🪵 {msg.get('msg','')}")
            return

        if mtype == "cmd_result":
            cid = msg.get("id")
            ok = msg.get("ok")
            if ok:
                self.log(f"{device_id}  ✅ cmd_result id={cid} result={msg.get('result')}")
            else:
                self.log(f"{device_id}  ❌ cmd_result id={cid} error={msg.get('error')}")
            return

        # Если твой ping модуль шлёт отдельные события:
        # например {"type":"ping","stage":"start"...} или {"type":"ping_result"...}
        if mtype in ("ping", "ping_result", "ping_status"):
            self.log(f"{device_id}  📡 {mtype} {self._compact(msg)}")
            return

        self.log(f"{device_id}  📦 {self._compact(msg)}")

    def next_cmd_id(self) -> str:
        self.cmd_seq += 1
        return str(self.cmd_seq)

    async def ws_handler(self, ws: WebSocketServerProtocol, path: str):
        kind, device_id = self.parse_path(path)
        if kind != "ws" or not device_id:
            self.log(f"reject path={path!r} from {ws.remote_address}")
            await ws.close(code=1008, reason="Bad path. Use /ws/<device_id>")
            return

        self.devices[device_id] = DeviceConn(device_id=device_id, ws=ws)
        if self.active_device is None:
            self.active_device = device_id

        self.log(f"{device_id} connected from {ws.remote_address} path={path} (active={self.active_device})")

        try:
            async for raw in ws:
                self.devices[device_id].last_seen = time.time()
                try:
                    msg = json.loads(raw)
                    if isinstance(msg, dict):
                        self.pretty_print_msg(device_id, msg)
                    else:
                        self.log(f"{device_id}  (non-dict json) {self._compact(msg)}")
                except Exception:
                    self.log(f"{device_id}  (text) {raw}")
        except websockets.ConnectionClosed as e:
            self.log(f"{device_id} disconnected ({e.code}: {e.reason})")
        except Exception as e:
            self.log(f"{device_id} handler error: {e!r}")
        finally:
            self.devices.pop(device_id, None)
            if self.active_device == device_id:
                self.active_device = next(iter(self.devices.keys()), None)
                self.log(f"active device -> {self.active_device}")

    async def send_to_active(self, payload: Dict[str, Any]) -> None:
        if not self.active_device or self.active_device not in self.devices:
            self.log("No active device. Use: devices / use <id>")
            return
        dc = self.devices[self.active_device]
        try:
            await dc.ws.send(json.dumps(payload, ensure_ascii=False))
        except Exception as e:
            self.log(f"Send failed: {e!r}")

    async def send_cmd(self, name: str, args: Dict[str, Any]) -> None:
        payload = {"type": "cmd", "id": self.next_cmd_id(), "name": name, "args": args}
        await self.send_to_active(payload)

    def print_help(self):
        self.log("Commands:")
        self.log("  devices                         - list connected devices")
        self.log("  use <device_id>                 - switch active device")
        self.log("  reboot                          - send reboot command")
        self.log("  speed <n>                       - speed_set {speed:n}")
        self.log("  loglevel <0..5>                 - set esp_log level")
        self.log("  ota                             - trigger ota_check (stub/your impl)")
        self.log("  ping <host> [count] [size]      - start ping test (defaults: 4 pings, 56 bytes)")
        self.log("        [interval_ms] [timeout_ms]")
        self.log("        example: ping 192.168.1.1 10 64 200 1000")
        self.log("  raw {json}                      - send raw JSON to active device")
        self.log("  help                            - show this help")
        self.log("  exit / quit                     - stop")

    async def console_loop(self):
        self.print_help()
        loop = asyncio.get_running_loop()

        while not self._shutdown.is_set():
            try:
                line = await loop.run_in_executor(None, sys.stdin.readline)
            except (EOFError, KeyboardInterrupt):
                self._shutdown.set()
                break

            if not line:
                self._shutdown.set()
                break

            line = line.strip()
            if not line:
                continue

            cmd, *rest = line.split()
            cmd = cmd.lower()

            if cmd in ("quit", "exit"):
                self._shutdown.set()
                break

            if cmd == "help":
                self.print_help()
                continue

            if cmd == "devices":
                if not self.devices:
                    self.log("No devices connected.")
                else:
                    for did, dc in self.devices.items():
                        age = time.time() - dc.last_seen
                        mark = "*" if did == self.active_device else " "
                        self.log(f"{mark} {did}  last_seen={age:.1f}s ago")
                continue

            if cmd == "use":
                if not rest:
                    self.log("Usage: use <device_id>")
                    continue
                did = rest[0]
                if did not in self.devices:
                    self.log(f"Device not connected: {did}")
                    continue
                self.active_device = did
                self.log(f"active device -> {did}")
                continue

            if cmd == "raw":
                raw_json = line[len("raw"):].strip()
                if not raw_json:
                    self.log("Usage: raw {json}")
                    continue
                try:
                    payload = json.loads(raw_json)
                    if not isinstance(payload, dict):
                        self.log("raw JSON must be an object/dict")
                        continue
                except Exception as e:
                    self.log(f"Bad JSON: {e}")
                    continue
                await self.send_to_active(payload)
                continue

            if cmd == "reboot":
                await self.send_cmd("reboot", {})
                continue

            if cmd == "speed":
                if not rest:
                    self.log("Usage: speed <0..100>")
                    continue
                try:
                    spd = int(rest[0])
                except ValueError:
                    self.log("speed must be int")
                    continue
                await self.send_cmd("speed_set", {"speed": spd})
                continue

            if cmd == "loglevel":
                if not rest:
                    self.log("Usage: loglevel <0..5>")
                    continue
                try:
                    lvl = int(rest[0])
                except ValueError:
                    self.log("loglevel must be int")
                    continue
                await self.send_cmd("log_level", {"level": lvl})
                continue

            if cmd == "ota":
                await self.send_cmd("ota_check", {})
                continue

            if cmd == "ping":
                # ping <device_id> [count] [interval_ms] [timeout_ms]
                # пример: ping train-01 10 200 1000
                if not rest:
                    self.log("Usage: ping <device_id> [count] [interval_ms] [timeout_ms]")
                    continue

                did = rest[0]
                if did not in self.devices:
                    self.log(f"Device not connected: {did}")
                    continue

                # временно переключаем active на нужный поезд
                self.active_device = did

                count = 4
                interval_ms = 1000
                timeout_ms = 1000

                def parse_int(idx: int, default: int) -> int:
                    if len(rest) > idx:
                        try:
                            return int(rest[idx])
                        except ValueError:
                            return default
                    return default

                count = parse_int(1, count)
                interval_ms = parse_int(2, interval_ms)
                timeout_ms = parse_int(3, timeout_ms)

                args = {
                    "count": count,
                    "interval_ms": interval_ms,
                    "timeout_ms": timeout_ms,
                }

                await self.send_cmd("ping", args)
                self.log(f"PING cmd -> {did} count={count} interval_ms={interval_ms} timeout_ms={timeout_ms}")
                continue

            self.log("Unknown command. Type 'help'.")

    async def run(self, host: str, port: int):
        self.log(f"WS server listening on ws://{host}:{port}/ws/<device_id>")
        # Важно для ESP: проще handshake (как с uvicorn)
        async with serve(
            self.ws_handler,
            host,
            port,
            ping_interval=None,   # server ping off
            compression=None,     # no permessage-deflate
            max_size=2 * 1024 * 1024,
        ):
            await self.console_loop()

        self.log("bye")
        if self.out_fp:
            self.out_fp.close()


def main():
    ap = argparse.ArgumentParser(description="ESP32 WS console: logs + commands + ping test")
    ap.add_argument("--host", default="0.0.0.0", help="bind host (default: 0.0.0.0)")
    ap.add_argument("--port", type=int, default=8000, help="bind port (default: 8000)")
    ap.add_argument("--out", default=None, help="optional file to append full console output")
    args = ap.parse_args()

    srv = ConsoleServer(out_path=args.out)
    try:
        asyncio.run(srv.run(args.host, args.port))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
