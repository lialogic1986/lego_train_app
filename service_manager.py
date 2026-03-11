#!/usr/bin/env python3

import asyncio
import json
import subprocess
import sys
from pathlib import Path

from event_client import EventClient


PROJECT_ROOT = Path(__file__).resolve().parent
PYTHON = sys.executable


class ServiceManager:

    def __init__(self):

        print("[manager] starting", flush=True)

        self.root = PROJECT_ROOT
        self.python = PYTHON

        self.bus = EventClient("service_manager")

        self.processes = {}
        self.camera_services = {}

        cfg_path = self.root / "service_manager_config.json"

        if cfg_path.exists():
            with open(cfg_path) as f:
                self.cfg = json.load(f)
        else:
            self.cfg = {}

        (self.root / "tmp").mkdir(exist_ok=True)

    def start_process(self, name, argv):

        if name in self.processes:
            return

        print(f"[manager] start {name}: {' '.join(argv)}", flush=True)

        p = subprocess.Popen(argv, cwd=str(self.root))
        self.processes[name] = p

    def start_event_bus(self):

        self.start_process(
            "event_bus",
            [self.python, str(self.root / "event_bus.py")]
        )

    def start_common_services(self):

        self.start_process(
            "ble_broker",
            [self.python, str(self.root / "ble_broker.py")]
        )

        self.start_process(
            "train_gui",
            [self.python, str(self.root / "train_gui_service.py")]
        )

    def ensure_camera_services(self, camera_id, camera_addr):

        if camera_id in self.camera_services:
            return

        viewer_name = f"viewer_{camera_id}"
        ws_name = f"ws_console_{camera_id}"

        ws_log = self.root / "tmp" / f"ws_console_{camera_id}.log"

        viewer_cmd = [
            self.python,
            str(self.root / "viewer_udp_mjpeg_aruco_range.py"),
            "--config", "host_config.json"
        ]

        ws_cmd = [
            self.python,
            str(self.root / "ws_console.py"),
            "--camera-id", camera_id,
            "--out", str(ws_log)
        ]

        self.start_process(viewer_name, viewer_cmd)
        self.start_process(ws_name, ws_cmd)

        self.camera_services[camera_id] = True

    async def run(self):

        await self.bus.connect()

        while True:

            evt = await self.bus.next_event()

            etype = evt.get("type")
            data = evt.get("data", {})

            if etype in (
                "camera_discovered",
                "camera_provisioned",
                "camera_reboot_detected"
            ):

                camera_id = data.get("camera_id")
                camera_addr = data.get("camera_addr")

                if camera_id:
                    print(f"[manager] camera discovered: {camera_id}", flush=True)
                    self.ensure_camera_services(camera_id, camera_addr)

            elif etype == "lego_ready":

                train_id = data.get("train_id")

                if train_id:
                    print(f"[manager] lego ready: {train_id}", flush=True)

            await asyncio.sleep(0.01)


async def main_async():

    mgr = ServiceManager()

    # 1️⃣ bus
    mgr.start_event_bus()

    # даём bus подняться
    await asyncio.sleep(1)

    # 2️⃣ остальные сервисы
    mgr.start_common_services()

    # 3️⃣ основной цикл
    await mgr.run()


if __name__ == "__main__":
    asyncio.run(main_async())
