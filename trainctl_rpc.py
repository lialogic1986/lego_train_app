#!/usr/bin/env python3
import argparse
import asyncio
import sys
import termios
import tty
from contextlib import contextmanager
from ble_rpc_client import BLERpcClient

@contextmanager
def raw_terminal():
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        yield
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)

async def keyboard_loop(state, stop_event: asyncio.Event):
    loop = asyncio.get_running_loop()
    def read_one_char():
        return sys.stdin.read(1)

    print(
        "\nControls:\n"
        "  w: power +step\n"
        "  s: power -step\n"
        "  space: stop (0)\n"
        "  b: brake\n"
        "  q: quit\n"
    )

    with raw_terminal():
        while not stop_event.is_set():
            ch = await loop.run_in_executor(None, read_one_char)
            if not ch:
                continue
            if ch == "q":
                stop_event.set()
                return
            elif ch == "w":
                state["power"] = min(100, state["power"] + state["step"])
                state["dirty"] = True
            elif ch == "s":
                state["power"] = max(-100, state["power"] - state["step"])
                state["dirty"] = True
            elif ch == " ":
                state["power"] = 0
                state["dirty"] = True
            elif ch == "b":
                state["brake"] = True
                state["dirty"] = True

            extra = " (BRAKE)" if state.get("brake") else ""
            sys.stdout.write(f"\rPower: {state['power']:4d}{extra}   ")
            sys.stdout.flush()

async def sender_loop(rpc: BLERpcClient, train_id: str, state, stop_event: asyncio.Event):
    sys.stdout.write(f"\rPower: {state['power']:4d}   ")
    sys.stdout.flush()

    while not stop_event.is_set():
        if state.get("dirty"):
            if state.get("brake"):
                await rpc.call("train_brake", train_id=train_id)
                state["brake"] = False
                state["power"] = 0
            else:
                await rpc.call("train_set_power", train_id=train_id, power=state["power"])
            state["dirty"] = False
        await asyncio.sleep(0.02)

def main():
    p = argparse.ArgumentParser("LEGO train controller via BLE broker")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=11000)
    p.add_argument("--train-id", required=True, help="train_01 or train_02")
    p.add_argument("--step", type=int, default=10)
    args = p.parse_args()

    async def run():
        rpc = BLERpcClient(args.host, args.port)
        state = {"power": 0, "step": args.step, "dirty": True, "brake": False}
        stop_event = asyncio.Event()
        kb = asyncio.create_task(keyboard_loop(state, stop_event))
        tx = asyncio.create_task(sender_loop(rpc, args.train_id, state, stop_event))
        await asyncio.gather(kb, tx)

    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass

if __name__ == "__main__":
    main()
