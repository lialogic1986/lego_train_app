#!/usr/bin/env python3

import asyncio
import json
import random
import signal
import socket
import struct
import time
from dataclasses import dataclass
from typing import Dict, Optional

from bleak import BleakClient, BleakScanner


# ============================================================
# UUID
# ============================================================

UUID_INFO_CHR = "0210a133-147b-6e92-264d-8b6f2b107c9a"
UUID_CFG_RX = "0310a133-147b-6e92-264d-8b6f2b107c9a"
UUID_STATUS_TX = "0410a133-147b-6e92-264d-8b6f2b107c9a"
UUID_CMD = "0510a133-147b-6e92-264d-8b6f2b107c9a"

CMD_COMMIT = b"\x01"

LEGO_CHAR_UUID = "00001624-1212-efde-1623-785feabcd123"
LEGO_SERVICE_UUID = "00001623-1212-efde-1623-785feabcd123"


STARTUP_COMPLETION = 0x11
SUBCMD_WRITE_DIRECT_MODE_DATA = 0x51
MODE_POWER = 0x00


# ============================================================
# utils
# ============================================================

def now_ms():
    return int(time.time() * 1000)


def clamp_power(p):
    return max(-100, min(100, p))


def cmd_set_power(port, power):

    power = clamp_power(power)
    power_byte = power & 0xFF

    return bytes([
        0x08, 0x00, 0x81, port & 0xFF,
        STARTUP_COMPLETION,
        SUBCMD_WRITE_DIRECT_MODE_DATA,
        MODE_POWER,
        power_byte
    ])


def get_host_ip(peer="1.1.1.1"):

    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.connect((peer, 9))
    ip = s.getsockname()[0]
    s.close()

    return ip


# ============================================================
# DATA
# ============================================================

@dataclass
class TrainSlot:

    slot_id: str
    lego_addr: str
    motor_port: int

    lego_client: Optional[BleakClient] = None
    lego_connected: bool = False

    power: int = 0

    camera_key: Optional[str] = None


@dataclass
class CameraState:

    key: str
    name: str
    addr: str

    online: bool = False
    last_seen: int = 0

    busy: bool = False
    provisioned: bool = False

    bound_slot: Optional[str] = None


# ============================================================
# BROKER
# ============================================================

class BLEBroker:

    def __init__(self, cfg):

        self.cfg = cfg

        self.scan_window = cfg["cam"]["scan_window_s"]

        self.slots: Dict[str, TrainSlot] = {}

        for sid, tc in cfg.get("trains", {}).items():

            self.slots[sid] = TrainSlot(
                sid,
                tc["lego_addr"],
                tc.get("motor_port", 1)
            )

        self.cameras: Dict[str, CameraState] = {}

        self._stop = asyncio.Event()

        self._tasks = []

        self._ble_lock = asyncio.Lock()

    # --------------------------------------------------------

    def log(self, *a):
        print(*a, flush=True)

    # ========================================================
    # BLE DISCOVERY
    # ========================================================

    async def ble_discovery_loop(self):

        self.log("[ble] discovery loop started")

        while not self._stop.is_set():

            async with self._ble_lock:

                devices = await BleakScanner.discover(
                    timeout=self.scan_window,
                    return_adv=True
                )

            for device, adv in devices.values():

                name = device.name or adv.local_name
                addr = device.address

                if name and name.startswith("Train-"):
                    self.upsert_camera(addr, name)

                if LEGO_SERVICE_UUID.lower() in [
                    s.lower() for s in adv.service_uuids or []
                ]:

                    slot = self.upsert_lego(addr)

                    if slot and not slot.lego_connected:
                        await self.connect_lego(slot)

            await asyncio.sleep(0.2)

    # ========================================================
    # CAMERA
    # ========================================================

    def upsert_camera(self, addr, name):

        key = name

        cam = self.cameras.get(key)

        if cam is None:

            cam = CameraState(
                key,
                name,
                addr,
                True,
                now_ms()
            )

            self.cameras[key] = cam

            self.log(f"[cam] discovered {name} ({addr})")

        else:

            if not cam.online:
                self.log("[cam] reboot detected", cam.name)
                cam.provisioned = False

            cam.addr = addr
            cam.online = True
            cam.last_seen = now_ms()

    async def camera_monitor_loop(self):

        while not self._stop.is_set():

            ts = now_ms()

            for cam in self.cameras.values():

                if cam.online and ts - cam.last_seen > 5000:

                    cam.online = False
                    self.log("[cam] offline", cam.name)

            await asyncio.sleep(1)

    # ========================================================
    # LEGO
    # ========================================================

    def upsert_lego(self, addr):

        for slot in self.slots.values():
            if slot.lego_addr.lower() == addr.lower():
                return slot

        slot_id = f"train_{len(self.slots)+1:02d}"

        slot = TrainSlot(
            slot_id,
            addr,
            1
        )

        self.slots[slot_id] = slot

        self.log(f"[lego] discovered new train {slot_id} ({addr})")

        return slot

    async def connect_lego(self, slot):

        try:

            async with self._ble_lock:

                client = BleakClient(slot.lego_addr, timeout=5)

                await client.connect()

            slot.lego_client = client
            slot.lego_connected = True

            await self.set_power(slot, 0)

            self.log(f"[lego] {slot.slot_id} connected")

        except Exception as e:

            self.log(f"[lego] connect failed {slot.slot_id}: {e}")

    async def set_power(self, slot, power):

        msg = cmd_set_power(slot.motor_port, power)

        await slot.lego_client.write_gatt_char(
            LEGO_CHAR_UUID,
            msg
        )

        slot.power = power

    async def keepalive_loop(self):

        while not self._stop.is_set():

            for slot in self.slots.values():

                if not slot.lego_connected:
                    continue

                try:

                    msg = cmd_set_power(slot.motor_port, slot.power)

                    await slot.lego_client.write_gatt_char(
                        LEGO_CHAR_UUID,
                        msg
                    )

                except Exception:

                    self.log("[lego] disconnected", slot.slot_id)

                    slot.lego_connected = False
                    slot.lego_client = None

            await asyncio.sleep(0.25)

    # ========================================================
    # PROVISIONING
    # ========================================================

    def build_payload(self, cam):

        prov = self.cfg["provisioning"]

        host_ip = get_host_ip()

        payload = {
            "v": 1,
            "wifi": {
                "ssid": prov["ssid"],
                "pass": prov["pass"]
            },
            "host": {
                "ip": host_ip,
                "ports": {
                    "video": prov["video_port"],
                    "tele": prov["tele_port"],
                    "ws": prov["ws_port"]
                }
            },
            "time": {
                "unix_ms": now_ms()
            },
            "train_id": cam.name
        }

        return json.dumps(payload).encode()

    async def provisioning_loop(self):

        while not self._stop.is_set():

            for cam in self.cameras.values():

                if cam.provisioned:
                    continue

                if cam.busy:
                    continue

                await self.provision_camera(cam)

            await asyncio.sleep(0.5)

    async def provision_camera(self, cam):

        cam.busy = True

        payload = self.build_payload(cam)

        done = asyncio.Event()

        def on_status(_, data):

            s = bytes(data).decode()

            if "DONE" in s:
                self.log(f"[{cam.name}] status: {s} ✅")
            else:
                self.log(f"[{cam.name}] status: {s}")

            if "DONE" in s or "ERROR" in s:
                done.set()

        try:

            async with self._ble_lock:

                async with BleakClient(cam.addr, timeout=10) as client:

                    await client.start_notify(
                        UUID_STATUS_TX,
                        on_status
                    )

                    try:

                        info = await client.read_gatt_char(UUID_INFO_CHR)

                        self.log(f"[{cam.name}] INFO", info.decode())

                    except Exception:
                        pass

                    await self.send_chunks(client, payload)

                    await client.write_gatt_char(
                        UUID_CMD,
                        CMD_COMMIT,
                        response=False
                    )

                    self.log(f"[{cam.name}] COMMIT")

                    await asyncio.wait_for(done.wait(), timeout=20)

            cam.provisioned = True

            self.log(f"[prov] {cam.name} DONE ✅")

        except Exception as e:

            self.log("[prov]", cam.name, "fail", e)

        cam.busy = False

    async def send_chunks(self, client, payload):

        total = len(payload)

        offset = 0

        sid = random.getrandbits(32)

        while offset < total:

            chunk = payload[offset:offset+160]

            hdr = struct.pack("<IHH", sid, offset, total)

            await client.write_gatt_char(
                UUID_CFG_RX,
                hdr + chunk,
                response=True
            )

            offset += len(chunk)

    # ========================================================
    # BINDING
    # ========================================================

    async def binding_loop(self):

        while not self._stop.is_set():

            for slot in self.slots.values():

                if not slot.lego_connected:
                    continue

                if slot.camera_key:
                    continue

                for cam in self.cameras.values():

                    if not cam.provisioned:
                        continue

                    if cam.bound_slot:
                        continue

                    slot.camera_key = cam.key
                    cam.bound_slot = slot.slot_id

                    self.log("[bind]", slot.slot_id, "<->", cam.name)

                    break

            await asyncio.sleep(0.3)

    # ========================================================

    async def start(self):

        self._tasks.append(
            asyncio.create_task(self.ble_discovery_loop())
        )

        self._tasks.append(
            asyncio.create_task(self.camera_monitor_loop())
        )

        self._tasks.append(
            asyncio.create_task(self.keepalive_loop())
        )

        self._tasks.append(
            asyncio.create_task(self.provisioning_loop())
        )

        self._tasks.append(
            asyncio.create_task(self.binding_loop())
        )

    async def stop(self):

        self._stop.set()

        for t in self._tasks:
            t.cancel()

        await asyncio.gather(*self._tasks, return_exceptions=True)


# ============================================================

async def main():

    with open("ble_config.json") as f:
        cfg = json.load(f)

    broker = BLEBroker(cfg)

    await broker.start()

    stop = asyncio.Event()

    loop = asyncio.get_running_loop()

    loop.add_signal_handler(signal.SIGINT, stop.set)

    await stop.wait()

    await broker.stop()


if __name__ == "__main__":

    asyncio.run(main())
