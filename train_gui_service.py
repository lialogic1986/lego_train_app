#!/usr/bin/env python3
import asyncio
import os
import queue
import threading
import time
import tkinter as tk
from dataclasses import dataclass, field
from tkinter import ttk

from event_client import EventClient


MODULE_OFFLINE_TIMEOUT = 10.0
SECTION_REMOVE_TIMEOUT = 30.0
POWER_STEP = 10
POWER_MIN = -100
POWER_MAX = 100


def clamp_power(v: int) -> int:
    return max(POWER_MIN, min(POWER_MAX, int(v)))


@dataclass
class TrainSectionState:
    section_id: str
    train_id: str = ""
    camera_id: str = ""
    lego_id: str = ""
    camera_addr: str = ""
    lego_addr: str = ""
    camera_last_seen: float = 0.0
    lego_last_seen: float = 0.0
    power: int = 0
    terminal_lines: list[str] = field(default_factory=list)
    terminal_path: str = ""
    terminal_offset: int = 0

    @property
    def title(self) -> str:
        return self.train_id or self.section_id

    @property
    def camera_online(self) -> bool:
        return self.camera_last_seen > 0 and (time.monotonic() - self.camera_last_seen) < MODULE_OFFLINE_TIMEOUT

    @property
    def lego_online(self) -> bool:
        return self.lego_last_seen > 0 and (time.monotonic() - self.lego_last_seen) < MODULE_OFFLINE_TIMEOUT

    @property
    def removable(self) -> bool:
        now = time.monotonic()
        cam_dead = (self.camera_last_seen == 0.0) or ((now - self.camera_last_seen) >= SECTION_REMOVE_TIMEOUT)
        lego_dead = (self.lego_last_seen == 0.0) or ((now - self.lego_last_seen) >= SECTION_REMOVE_TIMEOUT)
        return cam_dead and lego_dead


class BusWorker:
    def __init__(self, inbox: queue.Queue):
        self.inbox = inbox
        self.loop = None
        self.client = None
        self.thread = None
        self._ready = threading.Event()

    def start(self):
        self.thread = threading.Thread(target=self._thread_main, daemon=True)
        self.thread.start()
        self._ready.wait()

    def _thread_main(self):
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        self.client = EventClient("train_gui")
        self._ready.set()
        self.loop.run_until_complete(self._main())

    async def _main(self):
        await self.client.connect()
        while True:
            evt = await self.client.next_event()
            self.inbox.put(evt)

    def emit(self, event_type: str, data: dict):
        if not self.loop or not self.client:
            return

        fut = asyncio.run_coroutine_threadsafe(
            self.client.emit(event_type, data),
            self.loop,
        )

        def _done(f):
            try:
                f.result()
            except Exception as e:
                print(f"[train_gui] emit failed: {e}", flush=True)

        fut.add_done_callback(_done)


class TrainSectionWidget:
    def __init__(self, parent, send_cb):
        self.send_cb = send_cb
        self.state: TrainSectionState | None = None

        self.frame = ttk.LabelFrame(parent, text="Train")
        self.frame.pack(fill="x", padx=8, pady=6)

        status = ttk.Frame(self.frame)
        status.pack(fill="x", padx=8, pady=(8, 4))

        ttk.Label(status, text="LEGO:").grid(row=0, column=0, sticky="w")
        self.lego_indicator = tk.Canvas(status, width=18, height=18, highlightthickness=0)
        self.lego_indicator.grid(row=0, column=1, padx=(4, 8))
        self.lego_dot = self.lego_indicator.create_oval(2, 2, 16, 16, fill="red", outline="black")
        self.lego_var = tk.StringVar(value="offline")
        ttk.Label(status, textvariable=self.lego_var, width=10).grid(row=0, column=2, sticky="w")

        ttk.Label(status, text="CAM:").grid(row=0, column=3, padx=(16, 0), sticky="w")
        self.cam_indicator = tk.Canvas(status, width=18, height=18, highlightthickness=0)
        self.cam_indicator.grid(row=0, column=4, padx=(4, 8))
        self.cam_dot = self.cam_indicator.create_oval(2, 2, 16, 16, fill="red", outline="black")
        self.cam_var = tk.StringVar(value="offline")
        ttk.Label(status, textvariable=self.cam_var, width=10).grid(row=0, column=5, sticky="w")

        self.power_var = tk.StringVar(value="power: 0")
        ttk.Label(status, textvariable=self.power_var).grid(row=0, column=6, padx=(20, 0), sticky="w")

        info = ttk.Frame(self.frame)
        info.pack(fill="x", padx=8, pady=(0, 4))

        self.train_id_var = tk.StringVar(value="train_id: -")
        self.lego_id_var = tk.StringVar(value="lego_id: -")
        self.camera_id_var = tk.StringVar(value="camera_id: -")

        ttk.Label(info, textvariable=self.train_id_var).pack(anchor="w")
        ttk.Label(info, textvariable=self.lego_id_var).pack(anchor="w")
        ttk.Label(info, textvariable=self.camera_id_var).pack(anchor="w")

        controls = ttk.Frame(self.frame)
        controls.pack(fill="x", padx=8, pady=(2, 6))

        self.btn_back = ttk.Button(
            controls,
            text="Backward -10%",
            command=lambda: self._change_power(-POWER_STEP),
            state="disabled",
        )
        self.btn_back.pack(side="left", padx=4)

        self.btn_stop = ttk.Button(
            controls,
            text="Stop",
            command=self._stop_train,
            state="disabled",
        )
        self.btn_stop.pack(side="left", padx=4)

        self.btn_fwd = ttk.Button(
            controls,
            text="Forward +10%",
            command=lambda: self._change_power(+POWER_STEP),
            state="disabled",
        )
        self.btn_fwd.pack(side="left", padx=4)

        term_wrap = ttk.Frame(self.frame)
        term_wrap.pack(fill="both", expand=True, padx=8, pady=(0, 8))

        self.term_label = ttk.Label(term_wrap, text="Camera terminal")
        self.term_label.pack(anchor="w")

        self.term = tk.Text(term_wrap, height=10, wrap="word")
        self.term.pack(fill="x", expand=True)
        self.term.configure(state="disabled")

        input_row = ttk.Frame(term_wrap)
        input_row.pack(fill="x", pady=(4, 0))

        self.cmd_var = tk.StringVar()
        self.cmd_entry = ttk.Entry(input_row, textvariable=self.cmd_var, state="disabled")
        self.cmd_entry.pack(side="left", fill="x", expand=True, padx=(0, 6))
        self.cmd_entry.bind("<Return>", self._on_send_terminal)

        self.cmd_btn = ttk.Button(input_row, text="Send", command=self._on_send_terminal, state="disabled")
        self.cmd_btn.pack(side="left")

    def destroy(self):
        self.frame.destroy()

    def _set_indicator(self, canvas: tk.Canvas, dot, online: bool, text_var: tk.StringVar):
        canvas.itemconfig(dot, fill=("green" if online else "red"))
        text_var.set("online" if online else "offline")

    def _change_power(self, delta: int):
        if not self.state or not self.state.train_id or not self.state.lego_online:
            return
        self.state.power = clamp_power(self.state.power + delta)
        self.power_var.set(f"power: {self.state.power}")
        self.send_cb("train_power", {"train_id": self.state.train_id, "power": self.state.power})

    def _stop_train(self):
        if not self.state or not self.state.train_id or not self.state.lego_online:
            return
        self.state.power = 0
        self.power_var.set("power: 0")
        self.send_cb("train_stop", {"train_id": self.state.train_id})

    def _on_send_terminal(self, _event=None):
        if not self.state or not self.state.camera_online:
            return
        cmd = self.cmd_var.get().strip()
        if not cmd:
            return

        payload = {"command": cmd}
        if self.state.camera_id:
            payload["camera_id"] = self.state.camera_id
        if self.state.train_id:
            payload["train_id"] = self.state.train_id

        self.send_cb("camera_terminal_input", payload)
        self.append_terminal(f"> {cmd}")
        self.cmd_var.set("")

    def append_terminal(self, line: str):
        self.term.configure(state="normal")
        self.term.insert("end", line.rstrip() + "\n")
        self.term.see("end")
        self.term.configure(state="disabled")

    def set_state(self, st: TrainSectionState):
        self.state = st

        self.frame.configure(text=st.title)
        self.train_id_var.set(f"train_id: {st.train_id or '-'}")

        lego_info = st.lego_id or "-"
        if st.lego_addr:
            lego_info += f" ({st.lego_addr})"
        self.lego_id_var.set(f"lego_id: {lego_info}")

        cam_info = st.camera_id or "-"
        if st.camera_addr:
            cam_info += f" ({st.camera_addr})"
        self.camera_id_var.set(f"camera_id: {cam_info}")

        self.power_var.set(f"power: {st.power}")

        self._set_indicator(self.lego_indicator, self.lego_dot, st.lego_online, self.lego_var)
        self._set_indicator(self.cam_indicator, self.cam_dot, st.camera_online, self.cam_var)

        lego_controls = "normal" if (st.lego_online and st.train_id) else "disabled"
        self.btn_back.config(state=lego_controls)
        self.btn_stop.config(state=lego_controls)
        self.btn_fwd.config(state=lego_controls)

        cam_controls = "normal" if st.camera_online else "disabled"
        self.cmd_entry.config(state=cam_controls)
        self.cmd_btn.config(state=cam_controls)

        self.term_label.config(
            text="Camera terminal" if st.camera_online else "Camera terminal (offline)"
        )


class TrainGuiApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("LEGO Train GUI")
        self.root.geometry("1600x900")

        self.inbox: queue.Queue = queue.Queue()
        self.bus = BusWorker(self.inbox)

        self.sections: dict[str, TrainSectionState] = {}
        self.widgets: dict[str, TrainSectionWidget] = {}

        self.camera_to_section: dict[str, str] = {}
        self.train_to_section: dict[str, str] = {}

        self._seq = 0
        self.ws_console_log_dir = os.path.join(os.path.dirname(__file__), "tmp")

        self.main = ttk.Frame(root)
        self.main.pack(fill="both", expand=True)

        ttk.Label(
            self.main,
            text="LEGO Train Dashboard",
            font=("Arial", 18, "bold"),
        ).pack(anchor="w", padx=8, pady=(8, 4))

        self.canvas = tk.Canvas(self.main, highlightthickness=0)
        self.scroll = ttk.Scrollbar(self.main, orient="vertical", command=self.canvas.yview)
        self.inner = ttk.Frame(self.canvas)

        self.inner.bind(
            "<Configure>",
            lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all"))
        )

        self.canvas.create_window((0, 0), window=self.inner, anchor="nw")
        self.canvas.configure(yscrollcommand=self.scroll.set)

        self.canvas.pack(side="left", fill="both", expand=True)
        self.scroll.pack(side="right", fill="y")

    def start(self):
        self.bus.start()
        self.root.after(100, self._poll_events)
        self.root.after(1000, self._housekeeping)

    def _alloc_section_id(self) -> str:
        self._seq += 1
        return f"Train-{self._seq:02d}"

    def _default_terminal_path(self, camera_id: str) -> str:
        safe = camera_id.replace("/", "_").replace("\\", "_").replace(" ", "_")
        return os.path.join(self.ws_console_log_dir, f"ws_console_{safe}.log")

    def _ensure_section_by_train(self, train_id: str) -> TrainSectionState:
        if train_id not in self.sections:
            self.sections[train_id] = TrainSectionState(section_id=train_id, train_id=train_id)
        if train_id not in self.widgets:
            self.widgets[train_id] = TrainSectionWidget(self.inner, self.bus.emit)
        self._refresh_section(train_id)
        return self.sections[train_id]

    def _ensure_section_by_camera(self, camera_id: str) -> TrainSectionState:
        section_id = self.camera_to_section.get(camera_id)
        if section_id and section_id in self.sections:
            self._refresh_section(section_id)
            return self.sections[section_id]

        section_id = self._alloc_section_id()
        self.sections[section_id] = TrainSectionState(section_id=section_id, camera_id=camera_id)
        self.widgets[section_id] = TrainSectionWidget(self.inner, self.bus.emit)
        self.camera_to_section[camera_id] = section_id
        self.sections[section_id].terminal_path = self._default_terminal_path(camera_id)
        self._refresh_section(section_id)
        return self.sections[section_id]

    def _refresh_section(self, section_id: str):
        if section_id in self.sections and section_id in self.widgets:
            self.widgets[section_id].set_state(self.sections[section_id])

    def _remove_section(self, section_id: str):
        st = self.sections.get(section_id)
        if not st:
            return

        if st.camera_id and self.camera_to_section.get(st.camera_id) == section_id:
            del self.camera_to_section[st.camera_id]
        if st.train_id and self.train_to_section.get(st.train_id) == section_id:
            del self.train_to_section[st.train_id]

        if section_id in self.widgets:
            self.widgets[section_id].destroy()
            del self.widgets[section_id]
        del self.sections[section_id]

    def _merge_section_into_train(self, train_id: str, camera_id: str = "", camera_addr: str = "") -> TrainSectionState:
        train_section_id = self.train_to_section.get(train_id)
        cam_section_id = self.camera_to_section.get(camera_id) if camera_id else None

        if train_section_id and train_section_id in self.sections:
            st = self.sections[train_section_id]
        elif cam_section_id and cam_section_id in self.sections:
            st = self.sections[cam_section_id]
            old_id = cam_section_id

            self.sections[train_id] = st
            self.widgets[train_id] = self.widgets[old_id]
            del self.sections[old_id]
            del self.widgets[old_id]

            if camera_id:
                self.camera_to_section[camera_id] = train_id
            self.train_to_section[train_id] = train_id

            st.section_id = train_id
            st.train_id = train_id
        else:
            st = self._ensure_section_by_train(train_id)

        st.train_id = train_id
        st.lego_id = train_id
        if camera_id:
            st.camera_id = camera_id
            self.camera_to_section[camera_id] = train_id
            if not st.terminal_path:
                st.terminal_path = self._default_terminal_path(camera_id)
        if camera_addr:
            st.camera_addr = camera_addr

        self.train_to_section[train_id] = train_id
        self._refresh_section(train_id)
        return st

    def _touch_camera_only(self, camera_id: str, camera_addr: str = ""):
        st = self._ensure_section_by_camera(camera_id)
        st.camera_id = camera_id
        if camera_addr:
            st.camera_addr = camera_addr
        if not st.terminal_path:
            st.terminal_path = self._default_terminal_path(camera_id)
        st.camera_last_seen = time.monotonic()
        self._refresh_section(st.section_id)

    def _touch_camera_bound(self, train_id: str, camera_id: str, camera_addr: str = ""):
        st = self._merge_section_into_train(train_id, camera_id, camera_addr)
        st.camera_last_seen = time.monotonic()
        self._refresh_section(train_id)

    def _touch_lego(self, train_id: str, lego_addr: str = ""):
        st = self._ensure_section_by_train(train_id)
        st.train_id = train_id
        st.lego_id = train_id
        if lego_addr:
            st.lego_addr = lego_addr
        st.lego_last_seen = time.monotonic()
        self.train_to_section[train_id] = train_id
        self._refresh_section(train_id)

    def _read_terminal_updates(self, section_id: str):
        st = self.sections.get(section_id)
        if not st or not st.terminal_path:
            return
        if not os.path.exists(st.terminal_path):
            return

        try:
            with open(st.terminal_path, "r", encoding="utf-8", errors="replace") as f:
                f.seek(st.terminal_offset)
                chunk = f.read()
                st.terminal_offset = f.tell()
        except Exception:
            return

        if not chunk:
            return

        for line in chunk.splitlines():
            st.terminal_lines.append(line)
            self.widgets[section_id].append_terminal(line)

    def handle_event(self, evt: dict):
        etype = evt.get("type")
        data = evt.get("data", {})

        if etype in ("camera_discovered", "camera_provisioned", "camera_reboot_detected"):
            camera_id = data.get("camera_id")
            train_id = data.get("train_id", "")
            camera_addr = data.get("camera_addr", "")
            if camera_id:
                if train_id:
                    self._touch_camera_bound(train_id, camera_id, camera_addr)
                else:
                    self._touch_camera_only(camera_id, camera_addr)

        elif etype in ("camera_hb", "camera_ws_connected"):
            camera_id = data.get("camera_id")
            train_id = data.get("train_id", "")
            if camera_id:
                if train_id:
                    self._touch_camera_bound(train_id, camera_id)
                else:
                    self._touch_camera_only(camera_id)

        elif etype in ("camera_ws_disconnected", "camera_offline"):
            train_id = data.get("train_id", "")
            camera_id = data.get("camera_id", "")
            section_id = None
            if train_id and train_id in self.sections:
                section_id = train_id
            elif camera_id:
                section_id = self.camera_to_section.get(camera_id)

            if section_id and section_id in self.sections:
                self.sections[section_id].camera_last_seen = 0.0
                self._refresh_section(section_id)

        elif etype in ("lego_discovered", "lego_ready"):
            train_id = data.get("train_id")
            if train_id:
                self._touch_lego(train_id, data.get("lego_addr", ""))

        elif etype == "lego_disconnected":
            train_id = data.get("train_id")
            if train_id and train_id in self.sections:
                self.sections[train_id].lego_last_seen = 0.0
                self._refresh_section(train_id)

        elif etype == "train_bound":
            train_id = data.get("train_id")
            camera_id = data.get("camera_id", "")
            camera_addr = data.get("camera_addr", "")
            if train_id and camera_id:
                self._touch_camera_bound(train_id, camera_id, camera_addr)

        elif etype == "train_state":
            train_id = data.get("train_id")
            if train_id:
                st = self._ensure_section_by_train(train_id)
                if "power" in data:
                    st.power = clamp_power(int(data.get("power", st.power)))
                st.lego_last_seen = time.monotonic()
                self._refresh_section(train_id)

    def _poll_events(self):
        try:
            while True:
                evt = self.inbox.get_nowait()
                self.handle_event(evt)
        except queue.Empty:
            pass

        self.root.after(100, self._poll_events)

    def _housekeeping(self):
        for section_id in list(self.sections.keys()):
            st = self.sections.get(section_id)
            if not st:
                continue

            self._read_terminal_updates(section_id)
            self._refresh_section(section_id)

            if st.removable:
                self._remove_section(section_id)

        self.root.after(1000, self._housekeeping)


def main():
    root = tk.Tk()
    app = TrainGuiApp(root)
    app.start()
    root.mainloop()


if __name__ == "__main__":
    main()
