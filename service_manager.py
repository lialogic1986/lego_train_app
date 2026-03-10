#!/usr/bin/env python3

import asyncio
import json
import signal
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

from event_client import EventClient


# ============================================================
# DATA STRUCTURES
# ============================================================

@dataclass
class CameraInstance:
    camera_id: str
    camera_addr: str
    video_port: int
    tele_port: int
    ws_port: int
    processes: List[subprocess.Popen]


@dataclass
class TrainInstance:
    train_id: str
    trainctl_port: int
    processes: List[subprocess.Popen]


# ============================================================
# PORT ALLOCATOR
# ============================================================

class PortAllocator:

    def __init__(self):
        self.next_video = 5000
        self.next_tele = 7000
        self.next_ws = 8000
        self.next_trainctl = 9000

    def alloc_camera_ports(self):

        vp = self.next_video
        tp = self.next_tele
        wp = self.next_ws

        self.next_video += 1
        self.next_tele += 1
        self.next_ws += 1

        return vp, tp, wp

    def alloc_trainctl_port(self):

        p = self.next_trainctl
        self.next_trainctl += 1

        return p


# ============================================================
# SERVICE MANAGER
# ============================================================

class ServiceManager:

    def __init__(self):

        self.root = Path(__file__).resolve().parent
        self.python = sys.executable

        self.bus = EventClient("service_manager")

        self.alloc = PortAllocator()

        self.common_processes: Dict[str, subprocess.Popen] = {}
        self.camera_instances: Dict[str, CameraInstance] = {}
        self.train_instances: Dict[str, TrainInstance] = {}

        self.all_processes: List[subprocess.Popen] = []

        cfg_path = self.root / "service_manager_config.json"

        with open(cfg_path, "r", encoding="utf-8") as f:
            self.cfg = json.load(f)

    # ========================================================
    # SPAWN HELPERS
    # ========================================================

    def _format_argv(self, argv: List[str], params: dict) -> List[str]:

        merged = {"python": self.python, **params}

        return [arg.format(**merged) for arg in argv]

    def _spawn(self, name: str, argv: List[str], optional: bool = False) -> Optional[subprocess.Popen]:

        if len(argv) >= 2 and argv[1].endswith(".py"):

            script_path = (self.root / argv[1]).resolve()

            argv = argv[:]
            argv[1] = str(script_path)

            if not script_path.exists():

                msg = f"[manager] missing script for {name}: {script_path}"

                if optional:
                    print(msg + " (optional skip)", flush=True)
                    return None

                print(msg, flush=True)
                return None

        print(f"[manager] start {name}: {' '.join(argv)}", flush=True)

        p = subprocess.Popen(argv, cwd=str(self.root))

        self.all_processes.append(p)

        return p

    # ========================================================
    # START SYSTEM
    # ========================================================

    async def start_event_bus(self):

        argv = [self.python, str((self.root / "event_bus.py").resolve())]

        p = subprocess.Popen(argv, cwd=str(self.root))

        self.common_processes["event_bus"] = p
        self.all_processes.append(p)

        await asyncio.sleep(0.5)

    async def connect_bus(self):

        await self.bus.connect()

    async def start_common_services(self):

        for svc in self.cfg.get("common_services", []):

            name = svc["name"]

            if name in self.common_processes:
                continue

            argv = self._format_argv(svc["argv"], {})

            p = self._spawn(name, argv, optional=bool(svc.get("optional", False)))

            if p:

                self.common_processes[name] = p

                await self.bus.emit("service_started", {"name": name})

    # ========================================================
    # CAMERA SERVICES
    # ========================================================

    async def ensure_camera_services(self, camera_id: str, camera_addr: str) -> Optional[CameraInstance]:

        inst = self.camera_instances.get(camera_id)

        if inst:
            return inst

        video_port, tele_port, ws_port = self.alloc.alloc_camera_ports()

        params = {
            "camera_id": camera_id,
            "camera_addr": camera_addr,
            "video_port": video_port,
            "tele_port": tele_port,
            "ws_port": ws_port,
        }

        processes = []
        ok_count = 0

        for svc in self.cfg.get("camera_services", []):

            name = svc["name"].format(**params)

            argv = self._format_argv(svc["argv"], params)

            p = self._spawn(name, argv, optional=bool(svc.get("optional", False)))

            if p:

                processes.append(p)
                ok_count += 1

                await self.bus.emit("service_started", {"name": name})

        if ok_count == 0:
            return None

        inst = CameraInstance(
            camera_id=camera_id,
            camera_addr=camera_addr,
            video_port=video_port,
            tele_port=tele_port,
            ws_port=ws_port,
            processes=processes,
        )

        self.camera_instances[camera_id] = inst

        return inst

    # ========================================================
    # TRAIN SERVICES
    # ========================================================

    async def ensure_train_service(self, train_id: str, train_addr: str = "") -> Optional[TrainInstance]:

        inst = self.train_instances.get(train_id)

        if inst:
            return inst

        trainctl_port = self.alloc.alloc_trainctl_port()

        params = {
            "train_id": train_id,
            "trainctl_port": trainctl_port,
            "train_addr": train_addr,
        }

        processes = []
        ok_count = 0

        for svc in self.cfg.get("train_services", []):

            name = svc["name"].format(**params)

            argv = self._format_argv(svc["argv"], params)

            p = self._spawn(name, argv, optional=bool(svc.get("optional", False)))

            if p:
                processes.append(p)
                ok_count += 1
                await self.bus.emit("service_started", {"name": name})

        if ok_count == 0:
            return None

        inst = TrainInstance(
            train_id=train_id,
            trainctl_port=trainctl_port,
            processes=processes,
        )

        self.train_instances[train_id] = inst

        return inst

    # ========================================================
    # EVENT BUS HANDLING
    # ========================================================

    async def handle_event(self, evt: dict):

        etype = evt.get("type")
        data = evt.get("data", {})
        request_id = evt.get("request_id", "")

        if etype == "camera_prepare_request":

            camera_id = data["camera_id"]
            camera_addr = data["camera_addr"]

            inst = await self.ensure_camera_services(camera_id, camera_addr)

            if inst is None:
                return

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

        elif etype == "lego_ready":

            train_id = data["train_id"]
            train_addr = data.get("lego_addr", "")

            await self.ensure_train_service(train_id, train_addr)

    # ========================================================
    # MAIN LOOP
    # ========================================================

    async def run(self):

        await self.start_event_bus()

        await self.connect_bus()

        await self.start_common_services()

        while True:

            evt = await self.bus.next_event()

            await self.handle_event(evt)

    # ========================================================
    # SHUTDOWN
    # ========================================================

    async def shutdown(self):

        await self.bus.close()

        for p in reversed(self.all_processes):
            try:
                p.terminate()
            except Exception:
                pass

        await asyncio.sleep(0.3)

        for p in reversed(self.all_processes):
            try:
                if p.poll() is None:
                    p.kill()
            except Exception:
                pass


# ============================================================
# MAIN
# ============================================================

async def main_async():

    print("[manager] starting", flush=True)

    mgr = ServiceManager()

    stop_ev = asyncio.Event()

    loop = asyncio.get_running_loop()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop_ev.set)
        except NotImplementedError:
            pass

    task = asyncio.create_task(mgr.run())

    stop_task = asyncio.create_task(stop_ev.wait())

    done, pending = await asyncio.wait(
        {task, stop_task},
        return_when=asyncio.FIRST_COMPLETED
    )

    if task in done:
        try:
            await task
        except Exception as e:
            print(f"[manager] fatal error: {type(e).__name__}: {e}", flush=True)
    else:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    stop_task.cancel()
    await asyncio.gather(stop_task, return_exceptions=True)

    await mgr.shutdown()


if __name__ == "__main__":
    asyncio.run(main_async())
