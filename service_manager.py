#!/usr/bin/env python3

import asyncio
import json
import signal
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Optional

from event_client import EventClient


PROJECT_ROOT = Path(__file__).resolve().parent
PYTHON = sys.executable


@dataclass
class CameraInstance:
    camera_id: str
    camera_addr: str
    video_port: int
    tele_port: int
    ws_port: int
    viewer_process: Optional[subprocess.Popen] = None
    ws_console_process: Optional[subprocess.Popen] = None
    ws_console_out: str = ""


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
        ws_log = self.root / "tmp" / f"ws_console_{camera_id}.log"

        viewer_name = f"viewer_{camera_id}"
        ws_name = f"ws_console_{camera_id}"

        viewer_cmd = [
            self.python,
            str((self.root / "viewer_udp_mjpeg_aruco_range.py").resolve()),
            "--config", "host_config.json",
            "--port", str(video_port),
        ]

        ws_cmd = [
            self.python,
            str((self.root / "ws_console.py").resolve()),
            "--host", "0.0.0.0",
            "--port", str(ws_port),
            "--camera-id", camera_id,
            "--out", str(ws_log),
        ]

        viewer_proc = self.start_process(viewer_name, viewer_cmd)
        ws_proc = self.start_process(ws_name, ws_cmd)

        inst = CameraInstance(
            camera_id=camera_id,
            camera_addr=camera_addr,
            video_port=video_port,
            tele_port=tele_port,
            ws_port=ws_port,
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

    async def run(self):
        await self.bus.connect()
        self.start_common_services()

        while True:
            evt = await self.bus.next_event()
            await self.handle_event(evt)
            await asyncio.sleep(0.01)

    async def shutdown(self):
        try:
            await self.bus.close()
        except Exception:
            pass

        for proc in self.processes.values():
            try:
                if proc.poll() is None:
                    proc.terminate()
            except Exception:
                pass

        await asyncio.sleep(0.5)

        for proc in self.processes.values():
            try:
                if proc.poll() is None:
                    proc.kill()
            except Exception:
                pass


async def main_async():
    mgr = ServiceManager()
    mgr.start_event_bus()

    await asyncio.sleep(1.0)

    stop_ev = asyncio.Event()
    loop = asyncio.get_running_loop()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop_ev.set)
        except NotImplementedError:
            pass

    run_task = asyncio.create_task(mgr.run())
    stop_task = asyncio.create_task(stop_ev.wait())

    done, pending = await asyncio.wait(
        {run_task, stop_task},
        return_when=asyncio.FIRST_COMPLETED,
    )

    if run_task in pending:
        run_task.cancel()
        await asyncio.gather(run_task, return_exceptions=True)

    stop_task.cancel()
    await asyncio.gather(stop_task, return_exceptions=True)

    await mgr.shutdown()


if __name__ == "__main__":
    asyncio.run(main_async())
