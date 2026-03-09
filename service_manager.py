#!/usr/bin/env python3
import subprocess
import time
import signal
import sys

SERVICES = {
    "ble_broker": [
        "python3",
        "ble_broker.py"
    ],

    "ws_console": [
        "python3",
        "ws_console.py",
        "--host", "0.0.0.0",
        "--port", "8000"
    ],

    "viewer": [
        "python3",
        "viewer_udp_mjpeg_aruco_range.py",
        "--config",
        "host_config.json"
    ]
}

processes = {}


def start_service(name, cmd):
    print(f"[manager] starting {name}")
    p = subprocess.Popen(cmd)
    processes[name] = p


def stop_all():
    print("[manager] stopping all services")
    for p in processes.values():
        p.terminate()


def monitor_loop():
    while True:
        for name, p in list(processes.items()):
            if p.poll() is not None:
                print(f"[manager] {name} crashed -> restarting")
                start_service(name, SERVICES[name])
        time.sleep(2)


def main():
    for name, cmd in SERVICES.items():
        start_service(name, cmd)

    def handler(sig, frame):
        stop_all()
        sys.exit(0)

    signal.signal(signal.SIGINT, handler)
    signal.signal(signal.SIGTERM, handler)

    monitor_loop()


if __name__ == "__main__":
    main()
