#!/usr/bin/env python3
import argparse
import asyncio
import json
import sys
import time
from dataclasses import dataclass, field
from typing import Dict, Optional, Any, Tuple

# Требуется: pip install websockets
import websockets
from websockets.server import WebSocketServerProtocol


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
        print(s)
        if self.out_fp:
            self.out_fp.write(s + "\n")

    def parse_path(self, path: str) -> Tuple[Optional[str], Optional[str]]:
        """
        Ожидаем:
          /ws/<device_id>
        """
        parts = path.strip("/").split("/")
        if len(parts) == 2 and parts[0] == "ws" and parts[1]:
            return "ws", parts[1]
        return None, None

    def pretty_print_msg(self, device_id: str, msg: Dict[str, Any]) -> None:
        mtype = msg.get("type", "unknown")
        if mtype == "hb":
            uptime = msg.get("uptime_ms")
            self.log(f"{device_id}  ♥ hb  uptime_ms={uptime}  raw={self._compact(msg)}")
        elif mtype == "hello":
            fw = msg.get("fw")
            caps = msg.get("caps")
            self.log(f"{device_id}  👋 hello  fw={fw} caps={caps}")
        elif mtype == "log":
            # твой log_stream сейчас шлёт {"type":"log","ts":...,"msg":"..."}
            lmsg = msg.get("msg", "")
            self.log(f"{device_id}  🪵 {lmsg}")
        elif mtype == "cmd_result":
            cid = msg.get("id")
            ok = msg.get("ok")
            if ok:
                res = msg.get("result")
                self.log(f"{device_id}  ✅ cmd_result id={cid} result={res}")
            else:
                err = msg.get("error")
                self.log(f"{device_id}  ❌ cmd_result id={cid} error={err}")
        elif mtype == "ota_status":
            stage = msg.get("stage")
            prog = msg.get("progress")
            self.log(f"{device_id}  ⬆️ ota_status stage={stage} progress={prog}")
        else:
            self.log(f"{device_id}  📦 {self._compact(msg)}")

    @staticmethod
    def _compact(obj: Any) -> str:
        try:
            return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
        except Exception:
            return str(obj)

    def next_cmd_id(self) -> str:
        self.cmd_seq += 1
        return str(self.cmd_seq)

    async def ws_handler(self, ws: WebSocketServerProtocol):
        kind, device_id = self.parse_path(ws.path)
        if kind != "ws" or not device_id:
            await ws.close(code=1008, reason="Bad path. Use /ws/<device_id>")
            return

        # Регистрируем девайс
        self.devices[device_id] = DeviceConn(device_id=device_id, ws=ws)
        if self.active_device is None:
            self.active_device = device_id

        self.log(f"{device_id} connected from {ws.remote_address} (active={self.active_device})")

        try:
            async for raw in ws:
                self.devices[device_id].last_seen = time.time()
                # Печатаем "сырьё", если надо дебажить обрывы:
                # self.log(f"{device_id} RAW: {raw!r}")

                try:
                    msg = json.loads(raw)
                    if isinstance(msg, dict):
                        self.pretty_print_msg(device_id, msg)
                    else:
                        self.log(f"{device_id}  (non-dict json) {self._compact(msg)}")
                except Exception:
                    # не JSON — печатаем как есть
                    self.log(f"{device_id}  (text) {raw}")
        except websockets.ConnectionClosed as e:
            self.log(f"{device_id} disconnected ({e.code}: {e.reason})")
        finally:
            # Снимаем устройство
            self.devices.pop(device_id, None)
            if self.active_device == device_id:
                self.active_device = next(iter(self.devices.keys()), None)
                self.log(f"active device -> {self.active_device}")
            # если устройств не осталось — не выключаемся автоматически (удобнее)
            # self._shutdown.set()

    async def send_to_active(self, payload: Dict[str, Any]) -> None:
        if not self.active_device or self.active_device not in self.devices:
            self.log("No active device. Use: devices / use <id>")
            return
        dc = self.devices[self.active_device]
        try:
            await dc.ws.send(json.dumps(payload, ensure_ascii=False))
        except Exception as e:
            self.log(f"Send failed: {e}")

    async def console_loop(self):
        self.print_help()
        loop = asyncio.get_running_loop()

        while not self._shutdown.is_set():
            # input() блокирует — уводим в executor
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

            # Отправка RAW JSON
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

            # Удобные команды (шаблоны)
            if cmd == "reboot":
                payload = {"type": "cmd", "id": self.next_cmd_id(), "name": "reboot", "args": {}}
                await self.send_to_active(payload)
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
                payload = {"type": "cmd", "id": self.next_cmd_id(), "name": "speed_set", "args": {"speed": spd}}
                await self.send_to_active(payload)
                continue

            if cmd == "loglevel":
                if not rest:
                    self.log("Usage: loglevel <0..5>  (0 NONE,1 ERR,2 WARN,3 INFO,4 DEBUG,5 VERBOSE)")
                    continue
                try:
                    lvl = int(rest[0])
                except ValueError:
                    self.log("loglevel must be int")
                    continue
                payload = {"type": "cmd", "id": self.next_cmd_id(), "name": "log_level", "args": {"level": lvl}}
                await self.send_to_active(payload)
                continue

            if cmd == "ota":
                payload = {"type": "cmd", "id": self.next_cmd_id(), "name": "ota_check", "args": {}}
                await self.send_to_active(payload)
                continue

            self.log("Unknown command. Type 'help'.")

    def print_help(self):
        self.log("Commands:")
        self.log("  devices                  - list connected devices")
        self.log("  use <device_id>           - switch active device")
        self.log("  reboot                    - send reboot command")
        self.log("  speed <n>                 - send speed_set {speed:n}")
        self.log("  loglevel <0..5>           - set esp_log level")
        self.log("  ota                       - trigger ota_check (stub for now)")
        self.log("  raw {json}                - send raw JSON to active device")
        self.log("  help                      - show this help")
        self.log("  exit / quit               - stop")

    async def run(self, host: str, port: int):
        self.log(f"WS server listening on ws://{host}:{port}/ws/<device_id>")
        async with websockets.serve(
            self.ws_handler,
            host,
            port,
            ping_interval=20,
            ping_timeout=20,
            max_size=2 * 1024 * 1024,
        ):
            await self.console_loop()

        self.log("bye")
        if self.out_fp:
            self.out_fp.close()


def main():
    ap = argparse.ArgumentParser(description="ESP32 WS console: logs + commands in one terminal")
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
