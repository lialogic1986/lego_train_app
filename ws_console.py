#!/usr/bin/env python3

import argparse
import asyncio
import hashlib
import json
import os
import socket
import sys
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, Optional, Tuple

import websockets
from websockets.legacy.server import WebSocketServerProtocol, serve

from event_client import EventClient


def guess_host_ip() -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()


def local_ip_for_peer(peer_ip: str) -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect((peer_ip, 9))
        return s.getsockname()[0]
    except Exception:
        return guess_host_ip()
    finally:
        s.close()


def sha256_file(path: str) -> Tuple[str, int]:
    h = hashlib.sha256()
    size = 0
    with open(path, "rb") as f:
        while True:
            chunk = f.read(1024 * 1024)
            if not chunk:
                break
            h.update(chunk)
            size += len(chunk)
    return h.hexdigest(), size


class OneFileHandler(BaseHTTPRequestHandler):
    file_path: str = ""
    url_path: str = "/fw.bin"

    def do_GET(self):
        if self.path.split("?", 1)[0] != self.url_path:
            self.send_error(404, "Not Found")
            return

        try:
            st = os.stat(self.file_path)
            total = st.st_size

            rng = self.headers.get("Range") or self.headers.get("range")
            start, end = 0, total - 1
            status = 200

            if rng and rng.startswith("bytes="):
                spec = rng[len("bytes="):].strip()
                a, b = spec.split("-", 1)
                if a:
                    start = int(a)
                if b:
                    end = int(b)
                if start < 0 or end < start or end >= total:
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{total}")
                    self.end_headers()
                    return
                status = 206

            length = end - start + 1

            self.send_response(status)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", str(length))
            if status == 206:
                self.send_header("Content-Range", f"bytes {start}-{end}/{total}")
            self.end_headers()

            with open(self.file_path, "rb") as f:
                f.seek(start)
                remaining = length
                while remaining > 0:
                    chunk = f.read(min(64 * 1024, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)

        except Exception as e:
            self.send_error(500, f"Error: {e}")

    def log_message(self, fmt, *args):
        return


@dataclass
class TempHttpServer:
    host: str
    port: int
    file_path: str
    url_path: str = "/fw.bin"

    _srv: Optional[ThreadingHTTPServer] = None
    _thr: Optional[threading.Thread] = None

    def start(self) -> int:
        handler_cls = type(
            "OneFileHandlerBound",
            (OneFileHandler,),
            {"file_path": self.file_path, "url_path": self.url_path},
        )
        self._srv = ThreadingHTTPServer((self.host, self.port), handler_cls)
        actual_port = self._srv.server_address[1]

        def run():
            try:
                self._srv.serve_forever()
            except Exception:
                pass

        self._thr = threading.Thread(target=run, daemon=True)
        self._thr.start()
        return actual_port

    def stop(self):
        if self._srv:
            try:
                self._srv.shutdown()
            except Exception:
                pass
            try:
                self._srv.server_close()
            except Exception:
                pass
        self._srv = None
        self._thr = None


@dataclass
class DeviceConn:
    device_id: str
    ws: WebSocketServerProtocol
    last_seen: float = field(default_factory=time.time)
    peer_ip: str = ""
    fw: str = ""


class ConsoleServer:
    def __init__(
        self,
        camera_id: str,
        train_id: str = "",
        out_path: Optional[str] = None,
        host_ip_override: Optional[str] = None,
    ):
        self.camera_id = camera_id
        self.train_id = train_id or ""

        self.devices: Dict[str, DeviceConn] = {}
        self.active_device: Optional[str] = None

        self.cmd_seq = 0
        self.pending_cmd: Dict[str, asyncio.Future] = {}

        self.out_fp = open(out_path, "a", buffering=1, encoding="utf-8") if out_path else None
        self._shutdown = asyncio.Event()
        self._bus_command_tasks: set[asyncio.Task] = set()
        self.host_ip_override = host_ip_override

        self.bus = EventClient(f"ws_console.{camera_id}")

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

    async def emit_bus(self, event_type: str, data: dict):
        try:
            await self.bus.emit(event_type, data)
        except Exception as e:
            self.log(f"bus emit failed: {e!r}")

    def parse_path(self, path: str) -> Tuple[Optional[str], Optional[str]]:
        parts = path.strip("/").split("/")
        if len(parts) == 2 and parts[0] == "ws" and parts[1]:
            return "ws", parts[1]
        return None, None

    def next_cmd_id(self) -> str:
        self.cmd_seq += 1
        return str(self.cmd_seq)

    async def ws_handler(self, ws: WebSocketServerProtocol, path: str):
        kind, device_id = self.parse_path(path)
        if kind != "ws" or not device_id:
            self.log(f"reject path={path!r} from {ws.remote_address}")
            await ws.close(code=1008, reason="Bad path. Use /ws/<device_id>")
            return

        peer_ip = ""
        try:
            peer_ip = ws.remote_address[0] if ws.remote_address else ""
        except Exception:
            peer_ip = ""

        self.devices[device_id] = DeviceConn(device_id=device_id, ws=ws, peer_ip=peer_ip)
        if self.active_device is None:
            self.active_device = device_id

        self.log(
            f"{device_id} connected from {ws.remote_address} "
            f"peer_ip={peer_ip} active={self.active_device}"
        )

        await self.emit_bus(
            "camera_ws_connected",
            {
                "train_id": self.train_id,
                "camera_id": self.camera_id,
                "peer_ip": peer_ip,
                "device_id": device_id,
            },
        )

        try:
            async for raw in ws:
                self.devices[device_id].last_seen = time.time()
                try:
                    msg = json.loads(raw)
                    if isinstance(msg, dict):
                        await self.on_msg(device_id, msg)
                    else:
                        self.log(f"{device_id} (non-dict json) {self._compact(msg)}")
                except Exception:
                    self.log(f"{device_id} (text) {raw}")

        except websockets.ConnectionClosed as e:
            self.log(f"{device_id} disconnected ({e.code}: {e.reason})")

        except Exception as e:
            self.log(f"{device_id} handler error: {e!r}")

        finally:
            self.devices.pop(device_id, None)
            if self.active_device == device_id:
                self.active_device = next(iter(self.devices.keys()), None)
                self.log(f"active device -> {self.active_device}")

            await self.emit_bus(
                "camera_ws_disconnected",
                {
                    "train_id": self.train_id,
                    "camera_id": self.camera_id,
                    "device_id": device_id,
                },
            )

    async def on_msg(self, device_id: str, msg: Dict[str, Any]) -> None:
        mtype = msg.get("type", "unknown")

        if mtype == "hello":
            fw = str(msg.get("fw") or "")
            if device_id in self.devices:
                self.devices[device_id].fw = fw

            self.log(f"{device_id} hello fw={fw or '-'} raw={self._compact(msg)}")
            await self.emit_bus(
                "camera_hello",
                {
                    "train_id": self.train_id,
                    "camera_id": self.camera_id,
                    "device_id": device_id,
                    "fw": fw,
                    "raw": msg,
                },
            )
            return

        if mtype == "hb":
            self.log(f"{device_id} ♥ hb uptime_ms={msg.get('uptime_ms')} raw={self._compact(msg)}")
            await self.emit_bus(
                "camera_hb",
                {
                    "train_id": self.train_id,
                    "camera_id": self.camera_id,
                    "device_id": device_id,
                    "uptime_ms": msg.get("uptime_ms"),
                    "raw": msg,
                },
            )
            return

        if mtype == "log":
            self.log(f"{device_id} {msg.get('msg', '')}")
            return

        if mtype == "ota_status":
            self.log(f"{device_id} ⬆️ ota_status {self._compact(msg)}")
            return

        if mtype == "cmd_result":
            cid = str(msg.get("id"))
            ok = msg.get("ok")
            if ok:
                self.log(f"{device_id} ✅ cmd_result id={cid} result={msg.get('result')}")
            else:
                self.log(f"{device_id} ❌ cmd_result id={cid} error={msg.get('error')}")

            fut = self.pending_cmd.pop(cid, None)
            if fut and not fut.done():
                fut.set_result(msg)
            return

        self.log(f"{device_id} {self._compact(msg)}")

    async def send_to(self, device_id: str, payload: Dict[str, Any]) -> None:
        if device_id not in self.devices:
            self.log(f"Device not connected: {device_id}")
            return

        try:
            await self.devices[device_id].ws.send(json.dumps(payload, ensure_ascii=False))
        except Exception as e:
            self.log(f"Send failed: {e!r}")

    async def send_cmd_and_wait(
        self,
        device_id: str,
        name: str,
        args: Dict[str, Any],
        timeout_s: int = 180,
    ) -> Dict[str, Any]:
        cid = self.next_cmd_id()
        payload = {
            "type": "cmd",
            "id": cid,
            "name": name,
            "args": args,
        }

        loop = asyncio.get_running_loop()
        fut = loop.create_future()
        self.pending_cmd[cid] = fut

        await self.send_to(device_id, payload)

        try:
            res = await asyncio.wait_for(fut, timeout=timeout_s)
            return res
        except asyncio.TimeoutError:
            self.pending_cmd.pop(cid, None)
            raise

    def print_help(self):
        self.log("Commands:")
        self.log(" devices - list connected devices")
        self.log(" use <id> - switch active device")
        self.log(" raw <id> {json} - send raw JSON to device")
        self.log(" fota <id> <bin_path> - start temp HTTP, send fota cmd, wait result, stop HTTP")
        self.log(" help - show this help")
        self.log(" exit / quit - stop")

    async def handle_command_line(self, line: str):
        line = line.strip()
        if not line:
            return

        cmd, *rest = line.split()
        cmd = cmd.lower()

        if cmd in ("quit", "exit"):
            self._shutdown.set()
            return

        if cmd == "help":
            self.print_help()
            return

        if cmd == "devices":
            if not self.devices:
                self.log("No devices connected.")
            else:
                for did, dc in self.devices.items():
                    age = time.time() - dc.last_seen
                    mark = "*" if did == self.active_device else " "
                    self.log(f"{mark} {did} peer_ip={dc.peer_ip} last_seen={age:.1f}s ago")
            return

        if cmd == "use":
            if not rest:
                self.log("Usage: use <device_id>")
                return
            did = rest[0]
            if did not in self.devices:
                self.log(f"Device not connected: {did}")
                return
            self.active_device = did
            self.log(f"active device -> {did}")
            return

        if cmd == "raw":
            if len(rest) < 2:
                self.log("Usage: raw <device_id> {json}")
                return
            did = rest[0]
            raw_json = line.split(None, 2)[2]
            try:
                payload = json.loads(raw_json)
                if not isinstance(payload, dict):
                    self.log("raw JSON must be an object/dict")
                    return
            except Exception as e:
                self.log(f"Bad JSON: {e}")
                return

            await self.send_to(did, payload)
            return

        if cmd == "fota":
            if len(rest) < 2:
                self.log("Usage: fota <device_id> <bin_path>")
                return

            did = rest[0]
            bin_path = rest[1]

            if did not in self.devices:
                self.log(f"Device not connected: {did}")
                return

            if not os.path.isfile(bin_path):
                self.log(f"File not found: {bin_path}")
                return

            digest, size = sha256_file(bin_path)

            peer_ip = self.devices[did].peer_ip
            if self.host_ip_override:
                host_ip = self.host_ip_override
                why = "override"
            else:
                host_ip = local_ip_for_peer(peer_ip) if peer_ip else guess_host_ip()
                why = f"route_to_peer({peer_ip})" if peer_ip else "fallback_guess"

            http = TempHttpServer(host="0.0.0.0", port=0, file_path=bin_path, url_path="/fw.bin")
            port = http.start()
            url = f"http://{host_ip}:{port}/fw.bin"

            self.log(f"FOTA using host_ip={host_ip} ({why}), peer_ip={peer_ip}")
            self.log(f"FOTA HTTP UP: {url}")
            self.log(f"  sha256={digest}")
            self.log(f"  size={size} bytes")

            try:
                args = {
                    "url": url,
                    "sha256": digest,
                    "size": size,
                }
                res = await self.send_cmd_and_wait(did, "fota", args, timeout_s=900)
                ok = bool(res.get("ok"))
                if ok:
                    self.log(f"FOTA OK for {did}")
                else:
                    self.log(f"FOTA FAIL for {did}: {res.get('error')}")
            except asyncio.TimeoutError:
                self.log("FOTA timeout waiting cmd_result")
            finally:
                http.stop()
                self.log("FOTA HTTP DOWN")
            return

        self.log("Unknown command. Type 'help'.")

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

            await self.handle_command_line(line)

    async def bus_loop(self):
        while not self._shutdown.is_set():
            evt = await self.bus.next_event()
            etype = evt.get("type")
            data = evt.get("data", {})

            if etype == "train_bound":
                evt_train_id = data.get("train_id", "")
                evt_camera_id = data.get("camera_id", "")
                if evt_camera_id == self.camera_id and evt_train_id:
                    if self.train_id != evt_train_id:
                        self.train_id = evt_train_id
                        self.log(f"[bus] bound to train_id={self.train_id}")
                continue

            if etype != "camera_terminal_input":
                continue

            evt_train_id = data.get("train_id", "")
            evt_camera_id = data.get("camera_id", "")
            command = (data.get("command") or "").strip()

            if not command:
                continue

            if evt_camera_id and evt_camera_id != self.camera_id:
                continue

            if evt_train_id and self.train_id and evt_train_id != self.train_id:
                continue

            target_device = None
            if self.active_device:
                target_device = self.active_device
            elif len(self.devices) == 1:
                target_device = next(iter(self.devices.keys()), None)

            if not target_device:
                self.log(f"bus command ignored: no connected target for command={command!r}")
                continue

            self.log(f"[bus] {target_device} <= {command}")
            task = asyncio.create_task(self.handle_command_line(command))
            self._bus_command_tasks.add(task)

            def _forget_done(done_task):
                self._bus_command_tasks.discard(done_task)
                try:
                    exc = done_task.exception()
                except asyncio.CancelledError:
                    return
                if exc:
                    self.log(f"bus command task failed: {exc!r}")

            task.add_done_callback(_forget_done)

    async def run(self, host: str, port: int):
        await self.bus.connect()

        self.log(f"WS server listening on ws://{host}:{port}/ws/")
        self.log(f"camera_id={self.camera_id} train_id={self.train_id or '-'}")
        if self.host_ip_override:
            self.log(f"Host IP override: {self.host_ip_override}")

        async with serve(
            self.ws_handler,
            host,
            port,
            ping_interval=None,
            compression=None,
            max_size=2 * 1024 * 1024,
        ):
            console_task = asyncio.create_task(self.console_loop())
            bus_task = asyncio.create_task(self.bus_loop())

            done, pending = await asyncio.wait(
                [console_task, bus_task],
                return_when=asyncio.FIRST_COMPLETED,
            )

            for t in pending:
                t.cancel()
            await asyncio.gather(*pending, return_exceptions=True)

            for t in list(self._bus_command_tasks):
                t.cancel()
            await asyncio.gather(*self._bus_command_tasks, return_exceptions=True)
            self._bus_command_tasks.clear()

            for t in done:
                exc = t.exception()
                if exc:
                    raise exc

        self.log("bye")
        await self.bus.close()

        if self.out_fp:
            self.out_fp.close()


def main():
    ap = argparse.ArgumentParser(description="WS console + FOTA + event_bus bridge")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--camera-id", required=True)
    ap.add_argument("--train-id", default="")
    ap.add_argument("--out", default=None)
    ap.add_argument("--host-ip", default=None)
    args = ap.parse_args()

    srv = ConsoleServer(
        camera_id=args.camera_id,
        train_id=args.train_id,
        out_path=args.out,
        host_ip_override=args.host_ip,
    )

    try:
        asyncio.run(srv.run(args.host, args.port))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
