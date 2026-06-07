#!/usr/bin/env python3
import asyncio
import os
import queue
import threading
import time
import tkinter as tk
from dataclasses import dataclass, field
from tkinter import filedialog, ttk

import cv2

from event_client import EventClient


MODULE_OFFLINE_TIMEOUT = 10.0
SECTION_REMOVE_TIMEOUT = 30.0
PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
DEFAULT_FOTA_BIN_PATH = os.path.abspath(
    os.path.join(PROJECT_ROOT, "..", "lego_esp32cam", "build", "lego_esp32cam.bin")
)
POWER_STEP = 20
POWER_MIN = -100
POWER_MAX = 100
VIDEO_POLL_MS = 20
INITIAL_WINDOW_WIDTH = 1600
INITIAL_WINDOW_HEIGHT = 1350
MIN_WINDOW_WIDTH = 900
MIN_WINDOW_HEIGHT = 760
SCREEN_MARGIN = 80


def safe_filename_id(value: str) -> str:
    safe = []
    for ch in value:
        if ch.isalnum() or ch in ("-", "_", "."):
            safe.append(ch)
        else:
            safe.append("_")
    return "".join(safe) or "camera"


def clamp_power(v: int) -> int:
    return max(POWER_MIN, min(POWER_MAX, int(v)))


@dataclass
class TrainSectionState:
    section_id: str
    train_id: str = ""
    camera_id: str = ""
    camera_device_id: str = ""
    camera_fw: str = ""
    lego_id: str = ""
    camera_addr: str = ""
    lego_addr: str = ""
    camera_last_seen: float = 0.0
    lego_last_seen: float = 0.0
    power: int = 0
    terminal_lines: list[str] = field(default_factory=list)
    terminal_path: str = ""
    terminal_offset: int = 0
    video_path: str = ""
    video_mtime_ns: int = 0

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
        self.camera_device_var = tk.StringVar(value="device_id: -")
        self.camera_fw_var = tk.StringVar(value="fw: -")

        ttk.Label(info, textvariable=self.train_id_var).pack(anchor="w")
        ttk.Label(info, textvariable=self.lego_id_var).pack(anchor="w")
        ttk.Label(info, textvariable=self.camera_id_var).pack(anchor="w")
        ttk.Label(info, textvariable=self.camera_device_var).pack(anchor="w")
        ttk.Label(info, textvariable=self.camera_fw_var).pack(anchor="w")

        video_wrap = ttk.Frame(self.frame)
        video_wrap.pack(fill="x", padx=8, pady=(2, 6))

        self.video_canvas = tk.Canvas(
            video_wrap,
            width=640,
            height=480,
            bg="black",
            highlightthickness=1,
            highlightbackground="#555",
        )
        self.video_canvas.pack(anchor="w")
        self.video_photo = None
        self.video_image_id = None
        self.video_text_id = self.video_canvas.create_text(
            320,
            240,
            text="Camera offline",
            fill="#d8d8d8",
            font=("Arial", 14),
        )

        controls = ttk.Frame(self.frame)
        controls.pack(fill="x", padx=8, pady=(2, 6))

        self.btn_back = ttk.Button(
            controls,
            text="Forward -20%",
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
            text="Backward +20%",
            command=lambda: self._change_power(+POWER_STEP),
            state="disabled",
        )
        self.btn_fwd.pack(side="left", padx=4)

        self.btn_fota = ttk.Button(
            controls,
            text="FOTA...",
            command=self._on_fota,
            state="disabled",
        )
        self.btn_fota.pack(side="left", padx=(18, 4))

        term_wrap = ttk.Frame(self.frame)
        term_wrap.pack(fill="both", expand=True, padx=8, pady=(0, 8))

        self.term_label = ttk.Label(term_wrap, text="Camera terminal")
        self.term_label.pack(anchor="w")

        self.term = tk.Text(term_wrap, height=10, width=120, wrap="word")
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

        self.send_cb(
            "train_power",
            {
                "train_id": self.state.train_id,
                "power": self.state.power,
            },
        )

    def _stop_train(self):
        if not self.state or not self.state.train_id or not self.state.lego_online:
            return

        self.state.power = 0
        self.power_var.set("power: 0")
        self.send_cb("train_stop", {"train_id": self.state.train_id})

    def _on_send_terminal(self, _event=None):
        cmd = self.cmd_var.get().strip()
        if not cmd or not self.state or not self.state.camera_online:
            return

        payload = {"command": cmd}
        if self.state.camera_id:
            payload["camera_id"] = self.state.camera_id
        if self.state.train_id:
            payload["train_id"] = self.state.train_id

        self.send_cb("camera_terminal_input", payload)
        self.append_terminal(f"> {cmd}")
        self.cmd_var.set("")

    def _send_terminal_command(self, cmd: str, include_train_id: bool = True):
        if not self.state or not self.state.camera_online:
            return

        payload = {"command": cmd}
        if self.state.camera_id:
            payload["camera_id"] = self.state.camera_id
        if include_train_id and self.state.train_id:
            payload["train_id"] = self.state.train_id

        self.send_cb("camera_terminal_input", payload)
        self.append_terminal(f"> {cmd}")

    def _on_fota(self):
        if not self.state or not self.state.camera_online:
            return

        device_id = self.state.camera_device_id.strip()
        if not device_id:
            self.append_terminal("FOTA: no connected camera device_id yet")
            return

        initial_path = DEFAULT_FOTA_BIN_PATH
        initial_dir = os.path.dirname(initial_path) if os.path.isdir(os.path.dirname(initial_path)) else PROJECT_ROOT
        initial_file = os.path.basename(initial_path) if os.path.isfile(initial_path) else ""

        bin_path = filedialog.askopenfilename(
            parent=self.frame,
            title="Select firmware image",
            initialdir=initial_dir,
            initialfile=initial_file,
            filetypes=(("Firmware images", "*.bin"), ("All files", "*")),
        )
        if not bin_path:
            return

        bin_path = os.path.abspath(bin_path)
        if " " in bin_path:
            self.append_terminal(f"FOTA: path contains spaces: {bin_path}")
            return

        self._send_terminal_command(f"fota {device_id} {bin_path}", include_train_id=False)

    def append_terminal(self, line: str):
        self.term.configure(state="normal")
        self.term.insert("end", line.rstrip() + "\n")
        self.term.see("end")
        self.term.configure(state="disabled")

    def set_video_photo(self, photo: tk.PhotoImage):
        self.video_photo = photo
        if self.video_image_id is None:
            self.video_image_id = self.video_canvas.create_image(
                320,
                240,
                image=self.video_photo,
                anchor="center",
            )
        else:
            self.video_canvas.itemconfig(self.video_image_id, image=self.video_photo)
        self.video_canvas.itemconfig(self.video_text_id, text="")

    def clear_video(self, message: str):
        self.video_photo = None
        if self.video_image_id is not None:
            self.video_canvas.delete(self.video_image_id)
            self.video_image_id = None
        self.video_canvas.itemconfig(self.video_text_id, text=message)

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
        self.camera_device_var.set(f"device_id: {st.camera_device_id or '-'}")
        self.camera_fw_var.set(f"fw: {st.camera_fw or '-'}")

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
        fota_controls = "normal" if (st.camera_online and st.camera_device_id) else "disabled"
        self.btn_fota.config(state=fota_controls)

        self.term_label.config(
            text="Camera terminal" if st.camera_online else "Camera terminal (offline)"
        )

        if not st.camera_online:
            self.clear_video("Camera offline")
        elif self.video_photo is None:
            self.clear_video("Waiting for video...")


class TrainGuiApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("LEGO Train GUI")
        self._closing = False
        self._set_initial_window_size()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

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

    def _on_close(self):
        if self._closing:
            return

        self._closing = True
        self.bus.emit(
            "app_shutdown",
            {
                "source": "train_gui",
                "reason": "window_close",
            },
        )
        self.root.after(300, self.root.destroy)

    def _set_initial_window_size(self):
        screen_w = self.root.winfo_screenwidth()
        screen_h = self.root.winfo_screenheight()
        width = min(INITIAL_WINDOW_WIDTH, max(MIN_WINDOW_WIDTH, screen_w - SCREEN_MARGIN))
        height = min(INITIAL_WINDOW_HEIGHT, max(MIN_WINDOW_HEIGHT, screen_h - SCREEN_MARGIN))

        self.root.geometry(f"{width}x{height}")
        self.root.minsize(MIN_WINDOW_WIDTH, MIN_WINDOW_HEIGHT)

    def start(self):
        self.bus.start()
        self.root.after(100, self._poll_events)
        self.root.after(VIDEO_POLL_MS, self._poll_video_snapshots)
        self.root.after(1000, self._housekeeping)

    def _alloc_section_id(self) -> str:
        self._seq += 1
        return f"Train-{self._seq:02d}"

    def _default_terminal_path(self, camera_id: str) -> str:
        safe = safe_filename_id(camera_id)
        return os.path.join(self.ws_console_log_dir, f"ws_console_{safe}.log")

    def _default_video_path(self, camera_id: str) -> str:
        safe = safe_filename_id(camera_id)
        return os.path.join(self.ws_console_log_dir, f"video_{safe}.jpg")

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
        self.sections[section_id].video_path = self._default_video_path(camera_id)
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

    def _merge_section_into_train(
        self,
        train_id: str,
        camera_id: str = "",
        camera_addr: str = "",
        camera_device_id: str = "",
        camera_fw: str = "",
    ) -> TrainSectionState:
        train_section_id = self.train_to_section.get(train_id)
        cam_section_id = self.camera_to_section.get(camera_id) if camera_id else None

        # Если обе секции уже существуют и это разные секции — сливаем camera-only в train
        if train_section_id and cam_section_id and train_section_id in self.sections and cam_section_id in self.sections and train_section_id != cam_section_id:
            train_st = self.sections[train_section_id]
            cam_st = self.sections[cam_section_id]

            train_st.camera_id = camera_id or train_st.camera_id or cam_st.camera_id
            train_st.camera_addr = camera_addr or train_st.camera_addr or cam_st.camera_addr
            train_st.camera_device_id = camera_device_id or train_st.camera_device_id or cam_st.camera_device_id
            train_st.camera_fw = camera_fw or train_st.camera_fw or cam_st.camera_fw
            train_st.camera_last_seen = max(train_st.camera_last_seen, cam_st.camera_last_seen)

            if cam_st.terminal_path and not train_st.terminal_path:
                train_st.terminal_path = cam_st.terminal_path
            if cam_st.terminal_offset and train_st.terminal_offset == 0:
                train_st.terminal_offset = cam_st.terminal_offset
            if cam_st.terminal_lines:
                train_st.terminal_lines.extend(cam_st.terminal_lines)
            if cam_st.video_path and not train_st.video_path:
                train_st.video_path = cam_st.video_path
            if cam_st.video_mtime_ns and train_st.video_mtime_ns == 0:
                train_st.video_mtime_ns = cam_st.video_mtime_ns

            self.camera_to_section[camera_id] = train_section_id
            self._remove_section(cam_section_id)
            self._refresh_section(train_section_id)
            return train_st

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
            if not st.video_path:
                st.video_path = self._default_video_path(camera_id)
        if camera_addr:
            st.camera_addr = camera_addr
        if camera_device_id:
            st.camera_device_id = camera_device_id
        if camera_fw:
            st.camera_fw = camera_fw

        self.train_to_section[train_id] = train_id
        self._refresh_section(train_id)
        return st

    def _touch_camera_only(
        self,
        camera_id: str,
        camera_addr: str = "",
        camera_device_id: str = "",
        camera_fw: str = "",
    ):
        st = self._ensure_section_by_camera(camera_id)
        st.camera_id = camera_id
        if camera_addr:
            st.camera_addr = camera_addr
        if camera_device_id:
            st.camera_device_id = camera_device_id
        if camera_fw:
            st.camera_fw = camera_fw
        if not st.terminal_path:
            st.terminal_path = self._default_terminal_path(camera_id)
        if not st.video_path:
            st.video_path = self._default_video_path(camera_id)
        st.camera_last_seen = time.monotonic()
        self._refresh_section(st.section_id)

    def _touch_camera_bound(
        self,
        train_id: str,
        camera_id: str,
        camera_addr: str = "",
        camera_device_id: str = "",
        camera_fw: str = "",
    ):
        st = self._merge_section_into_train(train_id, camera_id, camera_addr, camera_device_id, camera_fw)
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

    def _make_video_photo(self, path: str) -> tk.PhotoImage | None:
        img = cv2.imread(path, cv2.IMREAD_COLOR)
        if img is None:
            return None

        max_w, max_h = 640, 480
        h, w = img.shape[:2]
        if w <= 0 or h <= 0:
            return None

        scale = min(max_w / w, max_h / h)
        out_w = max(1, int(w * scale))
        out_h = max(1, int(h * scale))
        if out_w != w or out_h != h:
            img = cv2.resize(img, (out_w, out_h), interpolation=cv2.INTER_AREA)

        ok, encoded = cv2.imencode(".ppm", img)
        if not ok:
            return None

        return tk.PhotoImage(data=encoded.tobytes(), format="PPM")

    def _read_video_snapshot(self, section_id: str):
        st = self.sections.get(section_id)
        widget = self.widgets.get(section_id)
        if not st or not widget or not st.camera_online:
            return

        if not st.video_path and st.camera_id:
            st.video_path = self._default_video_path(st.camera_id)
        if not st.video_path:
            return

        try:
            stat = os.stat(st.video_path)
        except FileNotFoundError:
            if widget.video_photo is None:
                widget.clear_video("Waiting for video...")
            return
        except OSError:
            return

        mtime_ns = getattr(stat, "st_mtime_ns", int(stat.st_mtime * 1_000_000_000))
        if mtime_ns == st.video_mtime_ns:
            return

        photo = self._make_video_photo(st.video_path)
        if photo is None:
            return

        st.video_mtime_ns = mtime_ns
        widget.set_video_photo(photo)

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

        elif etype in ("camera_hb", "camera_ws_connected", "camera_hello"):
            camera_id = data.get("camera_id")
            train_id = data.get("train_id", "")
            device_id = data.get("device_id", "")
            camera_fw = data.get("fw", "")
            if camera_id:
                if train_id:
                    self._touch_camera_bound(
                        train_id,
                        camera_id,
                        camera_device_id=device_id,
                        camera_fw=camera_fw,
                    )
                else:
                    self._touch_camera_only(
                        camera_id,
                        camera_device_id=device_id,
                        camera_fw=camera_fw,
                    )

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
                if data.get("device_id") == self.sections[section_id].camera_device_id:
                    self.sections[section_id].camera_device_id = ""
                    self.sections[section_id].camera_fw = ""
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

    def _poll_video_snapshots(self):
        for section_id in list(self.sections.keys()):
            self._read_video_snapshot(section_id)

        self.root.after(VIDEO_POLL_MS, self._poll_video_snapshots)

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
