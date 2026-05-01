#!/usr/bin/env python3

import asyncio
import json
import os
import signal
import socket
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Optional

from event_client import EventClient


PROJECT_ROOT = Path(__file__).resolve().parent
PYTHON = sys.executable
EVENT_BUS_HOST = "127.0.0.1"
EVENT_BUS_PORT = 8765
MANAGER_LOCK_PATH = PROJECT_ROOT / "tmp" / "service_manager.lock"


@dataclass
class CameraInstance:
    camera_id: str
    camera_addr: str
    video_port: int
    tele_port: int
    ws_port: int
    video_snapshot_path: str = ""
    viewer_process: Optional[subprocess.Popen] = None
    ws_console_process: Optional[subprocess.Popen] = None
    ws_console_out: str = ""


class ProcessLock:
    def __init__(self, path: Path):
        self.path = path
        self.fp = None

    def acquire(self) -> bool:
        self.path.parent.mkdir(exist_ok=True)
        self.fp = open(self.path, "a+", encoding="utf-8")
        self.fp.seek(0)

        if os.name == "nt":
            import msvcrt

            try:
                msvcrt.locking(self.fp.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                self.fp.close()
                self.fp = None
                return False
        else:
            import fcntl

            try:
                fcntl.flock(self.fp.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                self.fp.close()
                self.fp = None
                return False

        self.fp.seek(0)
        self.fp.truncate()
        self.fp.write(str(os.getpid()))
        self.fp.flush()
        return True

    def release(self):
        if not self.fp:
            return

        try:
            if os.name == "nt":
                import msvcrt

                self.fp.seek(0)
                msvcrt.locking(self.fp.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.fp.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass

        try:
            self.fp.close()
        finally:
            self.fp = None


def safe_filename_id(value: str) -> str:
    safe = []
    for ch in value:
        if ch.isalnum() or ch in ("-", "_", "."):
            safe.append(ch)
        else:
            safe.append("_")
    return "".join(safe) or "camera"


def is_tcp_port_open(host: str, port: int, timeout_s: float = 0.2) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout_s):
            return True
    except OSError:
        return False


def is_udp_port_available(host: str, port: int) -> bool:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.bind((host, port))
        return True
    except OSError:
        return False
    finally:
        sock.close()


class PortAllocator:
    def __init__(self):
        self.next_video = 5000
        self.next_tele = 7000
        self.next_ws = 8000

    def alloc_camera_ports(self):
        vp = self.next_video
        tp = self.next_tele
        wp = self.next_ws
        self.next_video += 1
        self.next_tele += 1
        self.next_ws += 1
        return vp, tp, wp


class ServiceManager:
    def __init__(self):
        print("[manager] starting", flush=True)

        self.root = PROJECT_ROOT
        self.python = PYTHON
        self.bus = EventClient("service_manager")
        self.alloc = PortAllocator()

        self.processes: Dict[str, subprocess.Popen] = {}
        self.camera_instances: Dict[str, CameraInstance] = {}
        self.lock: Optional[ProcessLock] = None
        self.stop_event: Optional[asyncio.Event] = None

        cfg_path = self.root / "service_manager_config.json"
        with open(cfg_path, "r", encoding="utf-8") as f:
            self.cfg = json.load(f)

        (self.root / "tmp").mkdir(exist_ok=True)

    def start_process(self, name: str, argv: list[str]):
        if name in self.processes and self.processes[name].poll() is None:
            return self.processes[name]

        print(f"[manager] start {name}: {' '.join(argv)}", flush=True)
        p = subprocess.Popen(argv, cwd=str(self.root))
        self.processes[name] = p
        return p

    def start_event_bus(self):
        if is_tcp_port_open(EVENT_BUS_HOST, EVENT_BUS_PORT):
            print(
                f"[manager] event_bus already running on ws://{EVENT_BUS_HOST}:{EVENT_BUS_PORT}",
                flush=True,
            )
            return None

        return self.start_process(
            "event_bus",
            [self.python, str((self.root / "event_bus.py").resolve())]
        )

    def start_common_services(self):
        for svc in self.cfg.get("common_services", []):
            name = svc["name"]
            argv = []
            for a in svc["argv"]:
                a = a.replace("{python}", self.python)
                if a.endswith(".py") and not a.startswith("/"):
                    a = str((self.root / a).resolve())
                argv.append(a)
            self.start_process(name, argv)

    async def ensure_camera_services(self, camera_id: str, camera_addr: str) -> CameraInstance:
        inst = self.camera_instances.get(camera_id)
        if inst:
            return inst

        video_port, tele_port, ws_port = self.alloc.alloc_camera_ports()
        safe_camera_id = safe_filename_id(camera_id)
        ws_log = self.root / "tmp" / f"ws_console_{safe_camera_id}.log"
        video_snapshot = self.root / "tmp" / f"video_{safe_camera_id}.jpg"

        viewer_name = f"viewer_{camera_id}"
        ws_name = f"ws_console_{camera_id}"

        viewer_cmd = [
            self.python,
            str((self.root / "viewer_udp_mjpeg_aruco_range.py").resolve()),
            "--config", "host_config.json",
            "--port", str(video_port),
            "--snapshot-path", str(video_snapshot),
            "--snapshot-fps", "50",
            "--no-window",
        ]

        ws_cmd = [
            self.python,
            str((self.root / "ws_console.py").resolve()),
            "--host", "0.0.0.0",
            "--port", str(ws_port),
            "--camera-id", camera_id,
            "--out", str(ws_log),
        ]

        viewer_proc = None
        if is_udp_port_available("0.0.0.0", video_port):
            viewer_proc = self.start_process(viewer_name, viewer_cmd)
        else:
            print(f"[manager] viewer port {video_port}/udp already in use; reusing existing listener", flush=True)

        ws_proc = None
        if is_tcp_port_open("127.0.0.1", ws_port):
            print(f"[manager] ws_console port {ws_port}/tcp already in use; reusing existing listener", flush=True)
        else:
            ws_proc = self.start_process(ws_name, ws_cmd)

        inst = CameraInstance(
            camera_id=camera_id,
            camera_addr=camera_addr,
            video_port=video_port,
            tele_port=tele_port,
            ws_port=ws_port,
            video_snapshot_path=str(video_snapshot),
            viewer_process=viewer_proc,
            ws_console_process=ws_proc,
            ws_console_out=str(ws_log),
        )
        self.camera_instances[camera_id] = inst
        return inst

    async def handle_event(self, evt: dict):
        etype = evt.get("type")
        data = evt.get("data", {})
        request_id = evt.get("request_id", "")

        if etype == "app_shutdown":
            source = data.get("source") or "event"
            print(f"[manager] shutdown requested by {source}", flush=True)
            if self.stop_event:
                self.stop_event.set()
            return

        if etype in ("camera_discovered", "camera_reboot_detected"):
            camera_id = data.get("camera_id")
            camera_addr = data.get("camera_addr")
            if camera_id and camera_addr:
                print(f"[manager] camera discovered: {camera_id}", flush=True)
                await self.ensure_camera_services(camera_id, camera_addr)
            return

        if etype == "camera_prepare_request":
            camera_id = data.get("camera_id")
            camera_addr = data.get("camera_addr")
            if not camera_id or not camera_addr:
                return

            inst = await self.ensure_camera_services(camera_id, camera_addr)

            await self.bus.emit(
                "camera_prepare_ready",
                {
                    "camera_id": camera_id,
                    "camera_addr": camera_addr,
                    "video_port": inst.video_port,
                    "tele_port": inst.tele_port,
                    "ws_port": inst.ws_port,
                },
                request_id=request_id,
            )
            return

        if etype == "lego_ready":
            train_id = data.get("train_id")
            if train_id:
                print(f"[manager] lego ready: {train_id}", flush=True)
            return

    async def run(self, stop_ev: asyncio.Event):
        self.stop_event = stop_ev
        await self.bus.connect()
        self.start_common_services()

        while not stop_ev.is_set():
            event_task = asyncio.create_task(self.bus.next_event())
            stop_task = asyncio.create_task(stop_ev.wait())

            done, pending = await asyncio.wait(
                {event_task, stop_task},
                return_when=asyncio.FIRST_COMPLETED,
            )

            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)

            if stop_task in done:
                event_task.cancel()
                await asyncio.gather(event_task, return_exceptions=True)
                break

            evt = event_task.result()
            await self.handle_event(evt)
            await asyncio.sleep(0.01)

    async def shutdown(self):
        try:
            await self.bus.close()
        except Exception:
            pass

        for name, proc in self.processes.items():
            try:
                if proc.poll() is None:
                    print(f"[manager] stop {name}", flush=True)
                    proc.terminate()
            except Exception:
                pass

        await asyncio.sleep(0.5)

        for name, proc in self.processes.items():
            try:
                if proc.poll() is None:
                    print(f"[manager] kill {name}", flush=True)
                    proc.kill()
            except Exception:
                pass

        if self.lock:
            self.lock.release()
            self.lock = None


async def main_async():
    lock = ProcessLock(MANAGER_LOCK_PATH)
    if not lock.acquire():
        print("[manager] another service_manager is already running; exiting", flush=True)
        return

    mgr = ServiceManager()
    mgr.lock = lock
    stop_ev = asyncio.Event()
    loop = asyncio.get_running_loop()

    def request_stop():
        stop_ev.set()

    signals = [signal.SIGINT, signal.SIGTERM]
    if hasattr(signal, "SIGHUP"):
        signals.append(signal.SIGHUP)

    for sig in signals:
        try:
            loop.add_signal_handler(sig, request_stop)
        except (NotImplementedError, RuntimeError):
            try:
                signal.signal(sig, lambda _sig, _frame: loop.call_soon_threadsafe(request_stop))
            except (OSError, ValueError):
                pass

    try:
        mgr.start_event_bus()

        try:
            await asyncio.wait_for(stop_ev.wait(), timeout=1.0)
        except asyncio.TimeoutError:
            pass

        if not stop_ev.is_set():
            run_task = asyncio.create_task(mgr.run(stop_ev))
            stop_task = asyncio.create_task(stop_ev.wait())

            done, pending = await asyncio.wait(
                {run_task, stop_task},
                return_when=asyncio.FIRST_COMPLETED,
            )

            if run_task in done:
                exc = run_task.exception()
                if exc:
                    raise exc

            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
    finally:
        await mgr.shutdown()


if __name__ == "__main__":
    try:
        asyncio.run(main_async())
    except KeyboardInterrupt:
        pass
