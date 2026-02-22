#!/usr/bin/env python3
# ble_provisioning.py
#
# ESP32 (ESP-IDF NimBLE) provisioning client using bleak.
# - Scans for BLE devices with name prefix "Train-"
# - Connects, sends full JSON config in chunks to CFG_RX characteristic
# - Sends COMMIT command
# - Waits for STATUS notifications: DONE / ERROR:...
# - After DONE the ESP32 disables BLE and disappears from scan
#
# Protocol (must match your ESP firmware):
# CFG_RX write payload:
#   u32 session_id (LE)
#   u16 offset     (LE)
#   u16 total_len  (LE)
#   bytes data[]
#
# CMD write payload:
#   0x01 = COMMIT
#   0x02 = ABORT
#
# STATUS_TX notify payload: ASCII strings like "WAITING", "RECEIVED", "DONE", "ERROR:..."

import argparse
import asyncio
import json
import os
import random
import socket
import struct
import time
from dataclasses import dataclass
from typing import Dict, Optional, Tuple

from bleak import BleakClient, BleakScanner
from bleak.backends.device import BLEDevice
from bleak.backends.scanner import AdvertisementData


# ====== UUIDs: MUST match prov_ble.c ======
UUID_PROV_SVC = "0110a133-147b-6e92-264d-8b6f2b107c9a"      # 0x...1001
UUID_INFO_CHR = "0210a133-147b-6e92-264d-8b6f2b107c9a"      # 0x...1002
UUID_CFG_RX   = "0310a133-147b-6e92-264d-8b6f2b107c9a"      # 0x...1003
UUID_STATUS_TX= "0410a133-147b-6e92-264d-8b6f2b107c9a"      # 0x...1004
UUID_CMD      = "0510a133-147b-6e92-264d-8b6f2b107c9a"      # 0x...1005

CMD_COMMIT = b"\x01"
CMD_ABORT  = b"\x02"


def get_local_ip(prefer_iface: Optional[str] = None) -> str:
    """
    Best-effort: determine the primary local IPv4 address used for outbound traffic.
    Works without internet access too (doesn't actually send packets).
    """
    # If user forces iface IP via env/args - handle outside.
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        # 8.8.8.8 is used only to force OS route selection; no packets are required.
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


def now_unix_ms() -> int:
    return int(time.time() * 1000)


@dataclass
class ProvisioningConfig:
    ssid: str
    password: str
    host_ip: str
    ports: Dict[str, int]  # video/tele/ws
    unix_ms: int

    def to_json_bytes(self, train_id: Optional[str] = None) -> bytes:
        payload = {
            "v": 1,
            "wifi": {"ssid": self.ssid, "pass": self.password},
            "host": {"ip": self.host_ip, "ports": self.ports},
            "time": {"unix_ms": self.unix_ms},
        }
        if train_id:
            payload["train_id"] = train_id
        # Keep it compact to reduce BLE traffic
        s = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
        return s.encode("utf-8")


class BleProvisioner:
    def __init__(
        self,
        name_prefix: str = "Train-",
        scan_interval_s: float = 2.0,
        connect_timeout_s: float = 10.0,
        op_timeout_s: float = 20.0,
        chunk_size: int = 160,
        cooldown_s: float = 2.0,
        verbose: bool = True,
    ):
        self.name_prefix = name_prefix
        self.scan_interval_s = scan_interval_s
        self.connect_timeout_s = connect_timeout_s
        self.op_timeout_s = op_timeout_s
        self.chunk_size = chunk_size  # payload bytes per chunk (excluding 8-byte header)
        self.cooldown_s = cooldown_s
        self.verbose = verbose

        self._seen_recent: Dict[str, float] = {}  # addr -> ts

    def _log(self, *a):
        if self.verbose:
            print(*a, flush=True)

    @staticmethod
    def _extract_name(device: BLEDevice, adv: Optional[AdvertisementData]) -> Optional[str]:
        if device.name:
            return device.name
        if adv and adv.local_name:
            return adv.local_name
        return None

    def _match_train(self, device: BLEDevice, adv: Optional[AdvertisementData]) -> Optional[str]:
        name = self._extract_name(device, adv)
        if not name:
            return None
        if name.startswith(self.name_prefix):
            return name
        return None

    def _mark_seen(self, addr: str):
        self._seen_recent[addr] = time.time()

    def _recently_processed(self, addr: str) -> bool:
        ts = self._seen_recent.get(addr)
        return ts is not None and (time.time() - ts) < 15.0  # avoid spamming reconnects

    async def run_forever(self, cfg: ProvisioningConfig) -> None:
        """
        Main loop: scan -> connect -> provision -> repeat forever.
        """
        self._log(f"[prov] scanning for devices with prefix {self.name_prefix!r} ...")
        while True:
            try:
                target = await self._scan_once()
                if target is None:
                    await asyncio.sleep(self.scan_interval_s)
                    continue

                addr, name = target
                if self._recently_processed(addr):
                    await asyncio.sleep(0.25)
                    continue

                self._log(f"[prov] found {name} ({addr}) -> provisioning...")
                ok = await self._provision_one(addr, name, cfg)
                self._mark_seen(addr)

                if ok:
                    self._log(f"[prov] {name} DONE ✅ (should disappear from BLE)")
                else:
                    self._log(f"[prov] {name} FAILED ❌")

                await asyncio.sleep(self.cooldown_s)

            except asyncio.CancelledError:
                raise
            except Exception as e:
                self._log(f"[prov] loop error: {type(e).__name__}: {e}")
                await asyncio.sleep(1.0)

    async def _scan_once(self) -> Optional[Tuple[str, str]]:
        """
        Returns (address, name) for first matching device found in a short scan window.
        """
        found: Optional[Tuple[str, str]] = None

        def detection_cb(device: BLEDevice, adv: AdvertisementData):
            nonlocal found
            if found is not None:
                return
            name = self._match_train(device, adv)
            if name:
                found = (device.address, name)

        scanner = BleakScanner(detection_cb)
        await scanner.start()
        try:
            await asyncio.sleep(self.scan_interval_s)
        finally:
            await scanner.stop()

        return found

    async def _provision_one(self, address: str, train_name: str, cfg: ProvisioningConfig) -> bool:
        """
        Connect and provision one device. Returns True on DONE.
        """
        # Build session config bytes (include train_id for visibility)
        payload = cfg.to_json_bytes(train_id=train_name)
        total_len = len(payload)
        if total_len == 0:
            self._log("[prov] payload empty? abort")
            return False

        # Random session id (u32)
        session_id = random.getrandbits(32)

        done_evt = asyncio.Event()
        err_msg: Dict[str, str] = {"err": ""}

        def on_status(_: int, data: bytearray):
            try:
                s = bytes(data).decode("utf-8", errors="replace")
            except Exception:
                s = repr(bytes(data))
            self._log(f"[{train_name}] status: {s}")
            if s.startswith("ERROR"):
                err_msg["err"] = s
                done_evt.set()
            elif "DONE" in s:
                done_evt.set()

        async with BleakClient(address, timeout=self.connect_timeout_s) as client:
            # Optional: sanity check service exists
            try:
                svcs = await client.get_services()
                if UUID_PROV_SVC.lower() not in [s.uuid.lower() for s in svcs]:
                    # Some platforms hide service list until after some delay; still try writes.
                    self._log(f"[{train_name}] warning: PROV service not in discovery list (will still try).")
            except Exception:
                pass

            # Subscribe to status notifications
            try:
                await client.start_notify(UUID_STATUS_TX, on_status)
            except Exception as e:
                self._log(f"[{train_name}] cannot subscribe to STATUS_TX: {e}")
                return False

            # (Optional) read INFO
            try:
                info = await client.read_gatt_char(UUID_INFO_CHR)
                self._log(f"[{train_name}] INFO: {info.decode('utf-8', errors='replace')}")
            except Exception:
                pass

            # Send chunks
            try:
                await self._send_cfg_chunks(client, session_id, payload)
            except Exception as e:
                self._log(f"[{train_name}] send chunks error: {e}")
                # Try ABORT (best effort)
                try:
                    await client.write_gatt_char(UUID_CMD, CMD_ABORT, response=True)
                except Exception:
                    pass
                return False

            # COMMIT (best-effort)
            try:
                await client.write_gatt_char(UUID_CMD, CMD_COMMIT, response=False)
                self._log(f"[{train_name}] COMMIT sent (no response)")
            except Exception as e:
                self._log(f"[{train_name}] COMMIT write failed (ignored, will rely on DONE): {e}")

            # Wait for DONE/ERROR
            try:
                await asyncio.wait_for(done_evt.wait(), timeout=self.op_timeout_s)
            except asyncio.TimeoutError:
                self._log(f"[{train_name}] timeout waiting DONE/ERROR")
                return False
            finally:
                try:
                    await client.stop_notify(UUID_STATUS_TX)
                except Exception:
                    pass

            if err_msg["err"]:
                self._log(f"[{train_name}] device error: {err_msg['err']}")
                return False
            return True

    async def _send_cfg_chunks(self, client: BleakClient, session_id: int, payload: bytes) -> None:
        """
        Chunked write to CFG_RX.
        """
        total_len = len(payload)
        # Header: <IHH (LE) => session_id, offset, total_len
        # data up to chunk_size
        offset = 0
        # Write Without Response is fine, but response=True is more reliable for prototyping.
        # We'll use response=True to avoid flooding and to get early error feedback.
        while offset < total_len:
            chunk = payload[offset : offset + self.chunk_size]
            hdr = struct.pack("<IHH", session_id, offset, total_len)
            pkt = hdr + chunk
            await client.write_gatt_char(UUID_CFG_RX, pkt, response=True)
            offset += len(chunk)

        # Small pause so ESP can finish assembly before COMMIT (optional but helps stability)
        await asyncio.sleep(0.05)


async def main_async():
    ap = argparse.ArgumentParser(description="BLE provisioning for Train-* devices (bleak)")
    ap.add_argument("--prefix", default=os.environ.get("TRAIN_PREFIX", "Train-"), help="BLE name prefix to match (default Train-)")
    ap.add_argument("--ssid", default=os.environ.get("WIFI_SSID", ""), help="Wi-Fi SSID")
    ap.add_argument("--pass", dest="password", default=os.environ.get("WIFI_PASS", ""), help="Wi-Fi password")
    ap.add_argument("--host-ip", default=os.environ.get("HOST_IP", ""), help="Host IPv4 (default: auto-detect)")
    ap.add_argument("--video-port", type=int, default=int(os.environ.get("VIDEO_PORT", "5000")))
    ap.add_argument("--tele-port", type=int, default=int(os.environ.get("TELE_PORT", "5001")))
    ap.add_argument("--ws-port", type=int, default=int(os.environ.get("WS_PORT", "8765")))
    ap.add_argument("--scan-interval", type=float, default=2.0)
    ap.add_argument("--connect-timeout", type=float, default=10.0)
    ap.add_argument("--op-timeout", type=float, default=20.0)
    ap.add_argument("--chunk", type=int, default=160, help="payload bytes per chunk (default 160)")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    if not args.ssid or not args.password:
        print("ERROR: --ssid and --pass are required (or set WIFI_SSID/WIFI_PASS env).", flush=True)
        raise SystemExit(2)

    host_ip = args.host_ip.strip() or get_local_ip()
    cfg = ProvisioningConfig(
        ssid=args.ssid,
        password=args.password,
        host_ip=host_ip,
        ports={"video": args.video_port, "tele": args.tele_port, "ws": args.ws_port},
        unix_ms=now_unix_ms(),
    )

    prov = BleProvisioner(
        name_prefix=args.prefix,
        scan_interval_s=args.scan_interval,
        connect_timeout_s=args.connect_timeout,
        op_timeout_s=args.op_timeout,
        chunk_size=args.chunk,
        verbose=not args.quiet,
    )

    print(f"[prov] host_ip={host_ip} ports={cfg.ports} utc_ms={cfg.unix_ms}", flush=True)
    await prov.run_forever(cfg)


def main():
    try:
        asyncio.run(main_async())
    except KeyboardInterrupt:
        print("\n[prov] stopped", flush=True)


if __name__ == "__main__":
    main()
