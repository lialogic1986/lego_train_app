#!/usr/bin/env python3
# ble_provisioning.py

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
UUID_PROV_SVC = "0110a133-147b-6e92-264d-8b6f2b107c9a"
UUID_INFO_CHR = "0210a133-147b-6e92-264d-8b6f2b107c9a"
UUID_CFG_RX    = "0310a133-147b-6e92-264d-8b6f2b107c9a"
UUID_STATUS_TX = "0410a133-147b-6e92-264d-8b6f2b107c9a"
UUID_CMD       = "0510a133-147b-6e92-264d-8b6f2b107c9a"

CMD_COMMIT = b"\x01"
CMD_ABORT  = b"\x02"


def now_unix_ms() -> int:
    return int(time.time() * 1000)


def mask_secret(s: str) -> str:
    if not s:
        return ""
    if len(s) <= 2:
        return "*" * len(s)
    return s[0] + ("*" * (len(s) - 2)) + s[-1]


def _get_local_ip_via_route(peer_ip: str) -> Tuple[str, str]:
    """
    Determine source IP chosen by OS routing toward peer_ip.
    Works on Linux/Windows/macOS.
    """
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect((peer_ip, 9))  # no packets sent for UDP connect
        ip = s.getsockname()[0]
        s.close()
        return ip, f"route-to-{peer_ip}"
    except Exception as e:
        return "127.0.0.1", f"route-to-{peer_ip}-failed:{type(e).__name__}"


def _linux_default_iface() -> Optional[str]:
    """
    Linux-only: read /proc/net/route to get interface for default route.
    """
    try:
        with open("/proc/net/route", "r", encoding="utf-8") as f:
            # Iface  Destination Gateway Flags RefCnt Use Metric Mask ...
            for line in f.readlines()[1:]:
                parts = line.strip().split()
                if len(parts) >= 2:
                    iface, dest = parts[0], parts[1]
                    if dest == "00000000":
                        return iface
    except Exception:
        return None
    return None


def _get_ipv4_of_iface_linux(iface: str) -> Optional[str]:
    """
    Linux-only: best-effort IP discovery for iface without extra deps.
    Uses socket + ioctl if available, else None.
    """
    import fcntl  # Linux-only
    import struct as _struct

    SIOCGIFADDR = 0x8915
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        ifreq = _struct.pack("256s", iface.encode("utf-8")[:15])
        res = fcntl.ioctl(s.fileno(), SIOCGIFADDR, ifreq)
        ip = socket.inet_ntoa(res[20:24])
        s.close()
        return ip
    except Exception:
        return None


def get_host_ip(auto_peer: str = "1.1.1.1", prefer_iface: Optional[str] = None) -> Tuple[str, str]:
    """
    Returns (ip, reason).
    Strategy:
      1) If prefer_iface (Linux): get IPv4 for that iface
      2) If Linux: take iface of default route from /proc/net/route and get its IPv4
      3) Fallback: OS route selection to auto_peer (default 1.1.1.1)
    """
    # 1) Linux prefer iface
    if prefer_iface:
        if os.name == "posix":
            ip = _get_ipv4_of_iface_linux(prefer_iface)
            if ip:
                return ip, f"iface:{prefer_iface}"
        # if non-linux, ignore

    # 2) Linux default route iface
    if os.name == "posix":
        iface = _linux_default_iface()
        if iface:
            ip = _get_ipv4_of_iface_linux(iface)
            if ip:
                return ip, f"default-route-iface:{iface}"

    # 3) cross-platform fallback: route selection to peer
    ip, reason = _get_local_ip_via_route(auto_peer)
    return ip, reason


@dataclass
class ProvisioningConfig:
    ssid: str
    password: str
    host_ip: str
    ports: Dict[str, int]
    unix_ms: int

    def as_payload_dict(self, train_id: Optional[str] = None) -> Dict:
        payload = {
            "v": 1,
            "wifi": {"ssid": self.ssid, "pass": self.password},
            "host": {"ip": self.host_ip, "ports": self.ports},
            "time": {"unix_ms": self.unix_ms},
        }
        if train_id:
            payload["train_id"] = train_id
        return payload

    def to_json_bytes(self, train_id: Optional[str] = None) -> bytes:
        s = json.dumps(self.as_payload_dict(train_id=train_id),
                       separators=(",", ":"), ensure_ascii=False)
        return s.encode("utf-8")


class BleProvisioner:
    def __init__(
        self,
        name_prefix: str = "Train-",
        scan_window_s: float = 2.0,
        connect_timeout_s: float = 10.0,
        op_timeout_s: float = 20.0,
        chunk_size: int = 160,
        cooldown_s: float = 1.5,
        verbose: bool = True,
        show_json_per_device: bool = False,
        hide_password: bool = False,
    ):
        self.name_prefix = name_prefix
        self.scan_window_s = scan_window_s
        self.connect_timeout_s = connect_timeout_s
        self.op_timeout_s = op_timeout_s
        self.chunk_size = chunk_size
        self.cooldown_s = cooldown_s
        self.verbose = verbose
        self.show_json_per_device = show_json_per_device
        self.hide_password = hide_password
        self._seen_recent: Dict[str, float] = {}

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
        if name and name.startswith(self.name_prefix):
            return name
        return None

    def _mark_seen(self, addr: str):
        self._seen_recent[addr] = time.time()

    def _recently_processed(self, addr: str) -> bool:
        ts = self._seen_recent.get(addr)
        return ts is not None and (time.time() - ts) < 12.0

    async def run_forever(self, cfg: ProvisioningConfig) -> None:
        self._log(f"[prov] scanning for devices with prefix {self.name_prefix!r} ...")
        while True:
            try:
                target = await self._scan_once()
                if target is None:
                    await asyncio.sleep(0.2)
                    continue

                addr, name = target
                if self._recently_processed(addr):
                    await asyncio.sleep(0.2)
                    continue

                self._log(f"[prov] found {name} ({addr}) -> provisioning...")
                ok = await self._provision_one(addr, name, cfg)
                self._mark_seen(addr)

                self._log(f"[prov] {name} {'DONE ✅' if ok else 'FAILED ❌'}")
                await asyncio.sleep(self.cooldown_s)

            except asyncio.CancelledError:
                raise
            except Exception as e:
                self._log(f"[prov] loop error: {type(e).__name__}: {e}")
                await asyncio.sleep(0.8)

    async def _scan_once(self) -> Optional[Tuple[str, str]]:
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
            await asyncio.sleep(self.scan_window_s)
        finally:
            await scanner.stop()

        return found

    async def _provision_one(self, address: str, train_name: str, cfg: ProvisioningConfig) -> bool:
        payload = cfg.to_json_bytes(train_id=train_name)
        session_id = random.getrandbits(32)

        done_evt = asyncio.Event()
        err_msg: Dict[str, str] = {"err": ""}

        def on_status(_: int, data: bytearray):
            s = bytes(data).decode("utf-8", errors="replace")
            self._log(f"[{train_name}] status: {s}")
            if s.startswith("ERROR"):
                err_msg["err"] = s
                done_evt.set()
            elif "DONE" in s:
                done_evt.set()

        if self.show_json_per_device:
            self._log(f"\n----- CONFIG for {train_name} -----")
            if self.hide_password:
                d = cfg.as_payload_dict(train_id=train_name)
                d["wifi"]["pass"] = mask_secret(d["wifi"]["pass"])
                self._log(json.dumps(d, indent=2, ensure_ascii=False))
            else:
                self._log(payload.decode("utf-8", errors="replace"))
            self._log("----------------------------------\n")

        try:
            async with BleakClient(address, timeout=self.connect_timeout_s) as client:
                await client.start_notify(UUID_STATUS_TX, on_status)

                try:
                    info = await client.read_gatt_char(UUID_INFO_CHR)
                    self._log(f"[{train_name}] INFO: {info.decode('utf-8', errors='replace')}")
                except Exception:
                    pass

                await self._send_cfg_chunks(client, session_id, payload)

                # COMMIT best-effort (no response)
                try:
                    await client.write_gatt_char(UUID_CMD, CMD_COMMIT, response=False)
                    self._log(f"[{train_name}] COMMIT sent (no response)")
                except Exception as e:
                    self._log(f"[{train_name}] COMMIT write failed (ignored): {type(e).__name__}: {e}")

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

        except Exception as e:
            self._log(f"[{train_name}] connect/provision error: {type(e).__name__}: {e}")
            return False

    async def _send_cfg_chunks(self, client: BleakClient, session_id: int, payload: bytes) -> None:
        total_len = len(payload)
        offset = 0
        while offset < total_len:
            chunk = payload[offset: offset + self.chunk_size]
            hdr = struct.pack("<IHH", session_id, offset, total_len)
            pkt = hdr + chunk
            await client.write_gatt_char(UUID_CFG_RX, pkt, response=True)
            offset += len(chunk)
        await asyncio.sleep(0.05)


def print_config_banner(cfg: ProvisioningConfig, show_json: bool, hide_pass: bool, host_reason: str) -> None:
    print("\n========== PROVISIONING CONFIG ==========")
    print(f"SSID        : {cfg.ssid}")
    print(f"Password    : {mask_secret(cfg.password) if hide_pass else cfg.password}")
    print(f"Host IP     : {cfg.host_ip}   ({host_reason})")
    print(f"Video port  : {cfg.ports.get('video')}")
    print(f"Tele port   : {cfg.ports.get('tele')}")
    print(f"WS port     : {cfg.ports.get('ws')}")
    print(f"UTC ms      : {cfg.unix_ms}")
    print("=========================================\n")

    if show_json:
        d = cfg.as_payload_dict(train_id="(per-device)")
        if hide_pass:
            d["wifi"]["pass"] = mask_secret(d["wifi"]["pass"])
        print("JSON payload template:\n")
        print(json.dumps(d, indent=2, ensure_ascii=False))
        print()


async def main_async():
    ap = argparse.ArgumentParser(description="BLE provisioning for Train-* devices (bleak)")
    ap.add_argument("--prefix", default=os.environ.get("TRAIN_PREFIX", "Train-"))
    ap.add_argument("--ssid", default=os.environ.get("WIFI_SSID", ""))
    ap.add_argument("--pass", dest="password", default=os.environ.get("WIFI_PASS", ""))
    ap.add_argument("--host-ip", default=os.environ.get("HOST_IP", "auto"),
                    help="Host IPv4: auto|<ip> (default auto)")
    ap.add_argument("--host-ip-peer", default=os.environ.get("HOST_IP_PEER", "1.1.1.1"),
                    help="Peer IP for route-based auto detect (default 1.1.1.1)")
    ap.add_argument("--host-ip-iface", default=os.environ.get("HOST_IP_IFACE", ""),
                    help="Linux: prefer interface name (e.g. wlp0s20f3)")

    ap.add_argument("--video-port", type=int, default=int(os.environ.get("VIDEO_PORT", "5000")))
    ap.add_argument("--tele-port", type=int, default=int(os.environ.get("TELE_PORT", "8000")))
    ap.add_argument("--ws-port", type=int, default=int(os.environ.get("WS_PORT", "8000")))

    ap.add_argument("--scan-window", type=float, default=2.0)
    ap.add_argument("--connect-timeout", type=float, default=10.0)
    ap.add_argument("--op-timeout", type=float, default=20.0)
    ap.add_argument("--chunk", type=int, default=160)

    ap.add_argument("--show-json", action="store_true")
    ap.add_argument("--show-json-per-device", action="store_true")
    ap.add_argument("--hide-pass", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    if not args.ssid or not args.password:
        print("ERROR: --ssid and --pass are required (or set WIFI_SSID/WIFI_PASS env).")
        raise SystemExit(2)

    # Host IP selection
    host_reason = ""
    if args.host_ip and args.host_ip != "auto":
        host_ip = args.host_ip.strip()
        host_reason = "manual"
    else:
        host_ip, host_reason = get_host_ip(auto_peer=args.host_ip_peer, prefer_iface=(args.host_ip_iface or None))

    cfg = ProvisioningConfig(
        ssid=args.ssid,
        password=args.password,
        host_ip=host_ip,
        ports={"video": args.video_port, "tele": args.tele_port, "ws": args.ws_port},
        unix_ms=now_unix_ms(),
    )

    print_config_banner(cfg, show_json=args.show_json, hide_pass=args.hide_pass, host_reason=host_reason)

    if args.dry_run:
        print("Dry run complete. Exiting.")
        return

    prov = BleProvisioner(
        name_prefix=args.prefix,
        scan_window_s=args.scan_window,
        connect_timeout_s=args.connect_timeout,
        op_timeout_s=args.op_timeout,
        chunk_size=args.chunk,
        verbose=not args.quiet,
        show_json_per_device=args.show_json_per_device,
        hide_password=args.hide_pass,
    )

    await prov.run_forever(cfg)


def main():
    try:
        asyncio.run(main_async())
    except KeyboardInterrupt:
        print("\n[prov] stopped")


if __name__ == "__main__":
    main()
