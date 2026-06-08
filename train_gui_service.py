#!/usr/bin/env python3
import asyncio
import json
import math
import os
import queue
import threading
import time
import tkinter as tk
from dataclasses import asdict, dataclass, field
from tkinter import filedialog, ttk

import cv2

from event_client import EventClient
from railway_map_editor import MAP_CONFIG_PATH, TRACK_LIBRARY, RailwayMapEditor, rotate_point


MODULE_OFFLINE_TIMEOUT = 10.0
SECTION_REMOVE_TIMEOUT = 30.0
PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
DEFAULT_FOTA_BIN_PATH = os.path.abspath(
    os.path.join(PROJECT_ROOT, "..", "lego_esp32cam", "build", "lego_esp32cam.bin")
)
POWER_STEP = 20
POWER_MIN = -100
POWER_MAX = 100
VIDEO_POLL_MS = 10
VIDEO_CANVAS_WIDTH = 640
VIDEO_CANVAS_HEIGHT = 480
INITIAL_WINDOW_WIDTH = 1600
INITIAL_WINDOW_HEIGHT = 1350
MIN_WINDOW_WIDTH = 900
MIN_WINDOW_HEIGHT = 760
SCREEN_MARGIN = 80
SECTION_WIDE_LAYOUT_MIN_WIDTH = 1900
SECTION_MAX_COLUMNS = 2
SECTION_GRID_PAD_X = 8
SECTION_GRID_PAD_Y = 6
REALTIME_MAP_HEIGHT = 360
REALTIME_MAP_PADDING_PX = 14
REALTIME_MAP_RELOAD_INTERVAL_S = 1.0
REALTIME_MAP_REDRAW_INTERVAL_S = 0.2
LIVE_INFO_UPDATE_INTERVAL_S = 0.25
TRAIN_MARKER_STALE_S = 8.0


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
    video_last_frame_s: float = 0.0
    video_fps: float = 0.0
    marker_id: str = ""
    marker_distance_m: float | None = None
    marker_distance_raw_m: float | None = None
    marker_area_px: float | None = None
    marker_dict: str = ""
    marker_last_seen: float = 0.0
    marker_speed_mps: float | None = None
    marker_branch: str = ""

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


class RealtimeRailwayView:
    def __init__(self, parent, map_provider=None):
        self.map_provider = map_provider
        self.frame = ttk.LabelFrame(parent, text="Realtime railway")
        self.frame.columnconfigure(0, weight=1)
        self.frame.rowconfigure(0, weight=1)

        self.canvas = tk.Canvas(
            self.frame,
            height=REALTIME_MAP_HEIGHT,
            bg="#18241f",
            highlightthickness=1,
            highlightbackground="#415249",
        )
        self.canvas.grid(row=0, column=0, sticky="nsew", padx=8, pady=8)
        self.canvas.bind("<Configure>", lambda _event: self.redraw())

        self.elements: list[dict] = []
        self.markers: list[dict] = []
        self.markers_by_id: dict[int, dict] = {}
        self.last_mtime = 0.0
        self.last_reload_s = 0.0
        self.last_redraw_s = 0.0
        self.trains: dict[str, TrainSectionState] = {}

    def set_trains(self, trains: dict[str, TrainSectionState]):
        self.trains = trains

    def refresh(self, force: bool = False):
        now = time.monotonic()
        if not force and now - self.last_redraw_s < REALTIME_MAP_REDRAW_INTERVAL_S:
            return
        self.redraw()

    def _load_if_needed(self, force: bool = False):
        now = time.monotonic()
        if not force and now - self.last_reload_s < REALTIME_MAP_RELOAD_INTERVAL_S:
            return
        self.last_reload_s = now

        if self.map_provider:
            data = self.map_provider()
            self._apply_map_data(data)
            return

        try:
            mtime = os.path.getmtime(MAP_CONFIG_PATH)
        except OSError:
            self.elements = []
            self.markers = []
            self.markers_by_id = {}
            self.last_mtime = 0.0
            return

        if mtime == self.last_mtime:
            return

        try:
            with open(MAP_CONFIG_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            return

        self._apply_map_data(data)
        self.last_mtime = mtime

    def _apply_map_data(self, data: dict):
        self.elements = list(data.get("elements", []))
        self.markers = list(data.get("markers", []))
        self.markers_by_id = {}
        for marker in self.markers:
            try:
                self.markers_by_id[int(marker.get("marker_id"))] = marker
            except (TypeError, ValueError):
                continue

    def redraw(self):
        self._load_if_needed()
        self.last_redraw_s = time.monotonic()
        self.canvas.delete("all")

        w = max(1, self.canvas.winfo_width())
        h = max(1, self.canvas.winfo_height())
        if not self.elements and not self.markers:
            self.canvas.create_text(
                w // 2,
                h // 2,
                text="No railway_map.json",
                fill="#b7c8be",
                font=("Arial", 14),
            )
            return

        train_positions = self._train_positions()
        bounds = self._bounds()
        if not bounds:
            return

        min_x, min_y, max_x, max_y = bounds
        span_x = max(1.0, max_x - min_x)
        span_y = max(1.0, max_y - min_y)
        scale = min(
            (w - REALTIME_MAP_PADDING_PX * 2) / span_x,
            (h - REALTIME_MAP_PADDING_PX * 2) / span_y,
        )
        scale = max(0.05, scale)
        offset_x = (w - span_x * scale) / 2
        offset_y = (h - span_y * scale) / 2

        def sx(x_mm: float) -> float:
            return offset_x + (x_mm - min_x) * scale

        def sy(y_mm: float) -> float:
            return offset_y + (y_mm - min_y) * scale

        self._draw_background(w, h)
        for elem in self.elements:
            self._draw_track(elem, sx, sy, scale)
        for marker in self.markers:
            self._draw_marker(marker, sx, sy, scale)
        for train_id, pos in train_positions.items():
            self._draw_train(train_id, pos, sx, sy, scale)

    def _draw_background(self, w: int, h: int):
        self.canvas.create_rectangle(0, 0, w, h, fill="#18241f", outline="")
        step = 48
        for x in range(0, w + step, step):
            self.canvas.create_line(x, 0, x, h, fill="#20332b")
        for y in range(0, h + step, step):
            self.canvas.create_line(0, y, w, y, fill="#20332b")

    def _draw_track(self, elem: dict, sx, sy, scale: float):
        kind = elem.get("kind", "straight")
        if TRACK_LIBRARY.get(kind, {}).get("draw") == "straight":
            points = self._straight_points(elem)
        elif "curve" in kind:
            points = self._curve_points(elem)
        else:
            for points in self._switch_paths(elem):
                self._draw_track_path(points, sx, sy, scale)
            return
        self._draw_track_path(points, sx, sy, scale)

    def _draw_track_path(self, points: list[tuple[float, float]], sx, sy, scale: float):
        if len(points) < 2:
            return
        coords = []
        for x, y in points:
            coords.extend((sx(x), sy(y)))

        ballast_w = max(10, int(22 * min(scale, 1.4)))
        rail_w = max(3, int(5 * min(scale, 1.3)))
        self.canvas.create_line(*coords, fill="#4d4b45", width=ballast_w, capstyle="round", joinstyle="round", smooth=len(points) > 2)
        self.canvas.create_line(*coords, fill="#a9b0aa", width=rail_w, capstyle="round", joinstyle="round", smooth=len(points) > 2)

        if len(points) == 2:
            self._draw_sleepers(points[0], points[1], sx, sy, scale)

    def _draw_sleepers(self, p1, p2, sx, sy, scale: float):
        x1, y1 = p1
        x2, y2 = p2
        length = math.hypot(x2 - x1, y2 - y1)
        if length < 1:
            return
        ux = (x2 - x1) / length
        uy = (y2 - y1) / length
        px = -uy
        py = ux
        sleeper_half = 24
        every = 32
        count = max(1, int(length / every))
        for i in range(count + 1):
            t = i / max(1, count)
            x = x1 + (x2 - x1) * t
            y = y1 + (y2 - y1) * t
            self.canvas.create_line(
                sx(x - px * sleeper_half),
                sy(y - py * sleeper_half),
                sx(x + px * sleeper_half),
                sy(y + py * sleeper_half),
                fill="#806b4b",
                width=max(2, int(4 * min(scale, 1.0))),
            )

    def _draw_marker(self, marker: dict, sx, sy, scale: float):
        try:
            marker_id = int(marker.get("marker_id"))
            x_mm = float(marker.get("x_mm", marker.get("x", 0)))
            y_mm = float(marker.get("y_mm", marker.get("y", 0)))
            rotation = float(marker.get("rotation", 0))
        except (TypeError, ValueError):
            return

        x = sx(x_mm)
        y = sy(y_mm)
        size = max(8, min(20, 18 * scale))
        front = rotate_point(0, -38, rotation)
        fx = sx(x_mm + front[0])
        fy = sy(y_mm + front[1])
        self.canvas.create_polygon(x, y, fx - 8, fy + 8, fx + 8, fy + 8, fill="#2c89c8", outline="#8bd4ff")
        self.canvas.create_rectangle(x - size, y - size, x + size, y + size, fill="#f2f6f8", outline="#8bd4ff", width=2)
        self.canvas.create_text(x, y, text=str(marker_id), fill="#0d1b24", font=("Arial", max(8, int(size)), "bold"))

    def _draw_train(self, train_id: str, pos: dict, sx, sy, scale: float):
        x_mm = pos["x_mm"]
        y_mm = pos["y_mm"]
        rotation = pos["rotation"]
        x = sx(x_mm)
        y = sy(y_mm)
        front = rotate_point(0, -1, rotation)
        side = rotate_point(1, 0, rotation)
        length = max(24, min(54, 54 * scale))
        width = max(14, min(30, 30 * scale))
        pts = []
        for lx, ly in ((-width / 2, length / 2), (width / 2, length / 2), (width / 2, -length / 2), (0, -length * 0.75), (-width / 2, -length / 2)):
            px = x + side[0] * lx + front[0] * ly
            py = y + side[1] * lx + front[1] * ly
            pts.extend((px, py))
        fill = "#ffcf33" if pos.get("online", False) else "#8c8c8c"
        self.canvas.create_polygon(*pts, fill=fill, outline="#161616", width=2)
        label = train_id[-8:] if len(train_id) > 8 else train_id
        self.canvas.create_text(x, y + length * 0.75, text=label, fill="#e7f1ea", font=("Arial", 10, "bold"))

    def _render_info_panel(self, train_positions: dict[str, dict], unmapped_markers: list[dict]):
        self.info.configure(state="normal")
        self.info.delete("1.0", "end")
        self.info.insert(
            "end",
            f"Track: {len(self.elements)}   Map signs: {len(self.markers)}   Mapped trains: {len(train_positions)}\n",
            "title",
        )
        if self.markers_by_id:
            ids = ", ".join(str(mid) for mid in sorted(self.markers_by_id))
            self.info.insert("end", f"Map sign IDs: {ids}\n", "muted")

        if not train_positions and not unmapped_markers:
            self.info.insert("end", "\nWaiting for marker_seen...\n", "muted")
            self.info.configure(state="disabled")
            return

        for train_id, pos in sorted(train_positions.items()):
            self._append_train_info(train_id, pos)
        for item in unmapped_markers:
            self._append_unmapped_marker_info(item)

        self.info.configure(state="disabled")

    def _append_train_info(self, train_id: str, pos: dict):
        section_no, section_label = self._nearest_track_info(pos["x_mm"], pos["y_mm"])
        speed = "n/a" if pos["speed_mps"] is None else f"{pos['speed_mps']:.2f} m/s"
        next_distance = "-" if pos["next_point_distance_cm"] is None else f"{pos['next_point_distance_cm']:.1f} cm"

        self.info.insert("end", f"\n{train_id}\n", "title")
        self.info.insert("end", f"Section: {section_no} {section_label}\n")
        self.info.insert("end", f"Direction: {pos['branch'] or '-'}\n")
        self.info.insert("end", f"Distance to sign: {pos['distance_m']:.2f} m\n")
        self.info.insert("end", f"Power: {pos['power']}\n")
        self.info.insert("end", f"Speed: {speed}\n")
        self.info.insert("end", f"Next point: {next_distance}\n")

        if not pos["control_points"]:
            self.info.insert("end", "Control points: -\n", "muted")
            return

        self.info.insert("end", f"Control points ({pos['branch']}):\n")
        for idx, point in enumerate(pos["control_points"]):
            tags = ("interval",) if idx in pos["interval_indexes"] else ()
            distance = self._point_distance(point)
            action_type = point.get("action_type", point.get("type", "power"))
            value = point.get("value", point.get("power", ""))
            timeout = point.get("timeout_s", 0)
            prefix = ">>" if idx in pos["interval_indexes"] else "  "
            self.info.insert(
                "end",
                f"{prefix} {distance:6.1f} cm  {action_type:<7} value={value} timeout={timeout}\n",
                tags,
            )

    def _append_unmapped_marker_info(self, item: dict):
        self.info.insert("end", f"\n{item['title']}\n", "title")
        self.info.insert("end", "Marker is visible, but it is not placed on the current map.\n", "warn")
        self.info.insert("end", f"Sign ID: {item['marker_id']}\n")
        self.info.insert("end", f"Distance to sign: {item['distance_m']:.2f} m\n")
        self.info.insert("end", f"Direction: {item['branch'] or '-'}\n")
        self.info.insert("end", f"Power: {item['power']}\n")
        speed = "n/a" if item["speed_mps"] is None else f"{item['speed_mps']:.2f} m/s"
        self.info.insert("end", f"Speed: {speed}\n")

    def _nearest_track_label(self, x_mm: float, y_mm: float) -> str:
        return self._nearest_track_info(x_mm, y_mm)[1]

    def _nearest_track_info(self, x_mm: float, y_mm: float) -> tuple[str, str]:
        best = None
        for idx, elem in enumerate(self.elements, start=1):
            kind = elem.get("kind", "track")
            paths = self._switch_paths(elem) if "switch" in kind else [self._element_points(elem)]
            for path in paths:
                distance = self._distance_to_path(x_mm, y_mm, path)
                if best is None or distance < best[0]:
                    label = TRACK_LIBRARY.get(kind, {}).get("label", kind)
                    best = (distance, str(idx), label)
        if not best:
            return "-", "-"
        return best[1], best[2]

    def _distance_to_path(self, x_mm: float, y_mm: float, path: list[tuple[float, float]]) -> float:
        if not path:
            return float("inf")
        if len(path) == 1:
            return math.hypot(x_mm - path[0][0], y_mm - path[0][1])

        best = float("inf")
        for p1, p2 in zip(path, path[1:]):
            best = min(best, self._distance_to_segment(x_mm, y_mm, p1, p2))
        return best

    def _distance_to_segment(self, x_mm: float, y_mm: float, p1: tuple[float, float], p2: tuple[float, float]) -> float:
        x1, y1 = p1
        x2, y2 = p2
        dx = x2 - x1
        dy = y2 - y1
        denom = dx * dx + dy * dy
        if denom <= 0:
            return math.hypot(x_mm - x1, y_mm - y1)
        t = max(0.0, min(1.0, ((x_mm - x1) * dx + (y_mm - y1) * dy) / denom))
        proj_x = x1 + dx * t
        proj_y = y1 + dy * t
        return math.hypot(x_mm - proj_x, y_mm - proj_y)

    def _bounds(self) -> tuple[float, float, float, float] | None:
        points: list[tuple[float, float]] = []
        for elem in self.elements:
            points.extend(self._element_points(elem))
        for marker in self.markers:
            try:
                points.append((float(marker.get("x_mm", marker.get("x", 0))), float(marker.get("y_mm", marker.get("y", 0)))))
            except (TypeError, ValueError):
                continue
        if not points:
            return None
        min_x = min(p[0] for p in points) - 64
        max_x = max(p[0] for p in points) + 64
        min_y = min(p[1] for p in points) - 64
        max_y = max(p[1] for p in points) + 64
        return min_x, min_y, max_x, max_y

    def _element_points(self, elem: dict) -> list[tuple[float, float]]:
        kind = elem.get("kind", "straight")
        if TRACK_LIBRARY.get(kind, {}).get("draw") == "straight":
            return self._straight_points(elem)
        if "curve" in kind:
            return self._curve_points(elem)
        points = []
        for path in self._switch_paths(elem):
            points.extend(path)
        return points

    def _straight_points(self, elem: dict) -> list[tuple[float, float]]:
        x, y, rotation = self._element_base(elem)
        length = float(elem.get("length_mm", TRACK_LIBRARY.get(elem.get("kind", ""), {}).get("length_mm", 128.0)))
        half = length / 2
        p1 = rotate_point(-half, 0, rotation)
        p2 = rotate_point(half, 0, rotation)
        return [(x + p1[0], y + p1[1]), (x + p2[0], y + p2[1])]

    def _curve_points(self, elem: dict) -> list[tuple[float, float]]:
        x, y, rotation = self._element_base(elem)
        meta = TRACK_LIBRARY.get(elem.get("kind", ""), {})
        radius = float(meta.get("radius_studs", 40)) * 8.0
        signed_angle = float(meta.get("angle_deg", 22.5)) * (1 if elem.get("kind") == "curve_left" else -1)
        half_angle = abs(signed_angle) / 2
        chord = 2 * radius * math.sin(math.radians(half_angle))
        p0 = rotate_point(-chord / 2, 0, rotation)
        p1 = rotate_point(chord / 2, 0, rotation + signed_angle)
        return [(x + p0[0], y + p0[1]), (x, y), (x + p1[0], y + p1[1])]

    def _switch_paths(self, elem: dict) -> list[list[tuple[float, float]]]:
        x, y, rotation = self._element_base(elem)
        meta = TRACK_LIBRARY.get(elem.get("kind", ""), {})
        half = float(elem.get("length_mm", meta.get("length_mm", 256.0))) / 2
        angle = float(meta.get("branch_angle_deg", 22.5)) * (-1 if elem.get("kind") == "switch_left" else 1)
        main_a = rotate_point(-half, 0, rotation)
        main_b = rotate_point(half, 0, rotation)
        branch_b = rotate_point(half * math.cos(math.radians(angle)), half * math.sin(math.radians(angle)), rotation)
        return [
            [(x + main_a[0], y + main_a[1]), (x + main_b[0], y + main_b[1])],
            [(x, y), (x + branch_b[0], y + branch_b[1])],
        ]

    def _element_base(self, elem: dict) -> tuple[float, float, float]:
        return (
            float(elem.get("x_mm", elem.get("x", 0))),
            float(elem.get("y_mm", elem.get("y", 0))),
            float(elem.get("rotation", 0)),
        )

    def section_info(self, section_id: str, st: TrainSectionState) -> dict:
        self._load_if_needed()
        info = {
            "status": "waiting",
            "map_sign_ids": sorted(self.markers_by_id),
            "track_count": len(self.elements),
            "marker_count": len(self.markers),
        }

        if not st.marker_id or st.marker_distance_m is None:
            return info
        if time.monotonic() - st.marker_last_seen > TRAIN_MARKER_STALE_S:
            info["status"] = "stale"
            info["marker_id"] = st.marker_id
            return info

        try:
            marker = self.markers_by_id[int(st.marker_id)]
            marker_x = float(marker.get("x_mm", marker.get("x", 0)))
            marker_y = float(marker.get("y_mm", marker.get("y", 0)))
            rotation = float(marker.get("rotation", 0))
        except (KeyError, TypeError, ValueError):
            info.update({
                "status": "unmapped",
                "marker_id": st.marker_id,
                "distance_m": st.marker_distance_m,
                "branch": st.marker_branch or "approach",
            })
            return info

        front = rotate_point(0, -1, rotation)
        dist_mm = max(0.0, st.marker_distance_m * 1000.0)
        x_mm = marker_x + front[0] * dist_mm
        y_mm = marker_y + front[1] * dist_mm
        section_no, section_label = self._nearest_track_info(x_mm, y_mm)
        control = self._control_context(marker, st)
        info.update({
            "status": "mapped",
            "marker_id": st.marker_id,
            "distance_m": st.marker_distance_m,
            "x_mm": x_mm,
            "y_mm": y_mm,
            "section_no": section_no,
            "section_label": section_label,
            "branch": control["branch"],
            "control_points": control["points"],
            "interval_indexes": control["interval_indexes"],
            "next_point_distance_cm": control["next_point_distance_cm"],
        })
        return info

    def _train_positions(self) -> dict[str, dict]:
        now = time.monotonic()
        positions = {}
        for section_id, st in self.trains.items():
            if not st.marker_id or st.marker_distance_m is None:
                continue
            if now - st.marker_last_seen > TRAIN_MARKER_STALE_S:
                continue
            try:
                marker = self.markers_by_id[int(st.marker_id)]
                marker_x = float(marker.get("x_mm", marker.get("x", 0)))
                marker_y = float(marker.get("y_mm", marker.get("y", 0)))
                rotation = float(marker.get("rotation", 0))
            except (KeyError, TypeError, ValueError):
                continue

            front = rotate_point(0, -1, rotation)
            dist_mm = max(0.0, st.marker_distance_m * 1000.0)
            train_id = st.train_id or section_id
            control = self._control_context(marker, st)
            positions[train_id] = {
                "x_mm": marker_x + front[0] * dist_mm,
                "y_mm": marker_y + front[1] * dist_mm,
                "rotation": rotation,
                "online": st.lego_online or st.camera_online,
                "section_id": section_id,
                "marker_id": st.marker_id,
                "distance_m": st.marker_distance_m,
                "power": st.power,
                "speed_mps": st.marker_speed_mps,
                "branch": control["branch"],
                "control_points": control["points"],
                "interval_indexes": control["interval_indexes"],
                "next_point_distance_cm": control["next_point_distance_cm"],
            }
        return positions

    def _unmapped_marker_states(self, train_positions: dict[str, dict]) -> list[dict]:
        now = time.monotonic()
        mapped_sections = {pos["section_id"] for pos in train_positions.values()}
        out = []
        for section_id, st in self.trains.items():
            if section_id in mapped_sections:
                continue
            if not st.marker_id or st.marker_distance_m is None:
                continue
            if now - st.marker_last_seen > TRAIN_MARKER_STALE_S:
                continue
            try:
                marker_id = int(st.marker_id)
            except (TypeError, ValueError):
                marker_id = None
            if marker_id in self.markers_by_id:
                continue
            out.append(
                {
                    "title": st.train_id or st.camera_id or section_id,
                    "marker_id": st.marker_id,
                    "distance_m": st.marker_distance_m,
                    "speed_mps": st.marker_speed_mps,
                    "branch": st.marker_branch,
                    "power": st.power,
                }
            )
        return out

    def _control_context(self, marker: dict, st: TrainSectionState) -> dict:
        branch = st.marker_branch or "approach"
        raw_points = list(marker.get("actions", {}).get(branch, {}).get("points", []))
        points = self._sorted_control_points(branch, raw_points)
        distance_cm = (st.marker_distance_m or 0.0) * 100.0
        interval_indexes = self._control_interval_indexes(branch, points, distance_cm)
        next_point_distance_cm = self._next_point_distance_cm(branch, points, distance_cm)
        return {
            "branch": branch,
            "points": points,
            "interval_indexes": interval_indexes,
            "next_point_distance_cm": next_point_distance_cm,
        }

    def _sorted_control_points(self, branch: str, points: list[dict]) -> list[dict]:
        return sorted(points, key=self._point_distance, reverse=(branch == "approach"))

    def _control_interval_indexes(self, branch: str, points: list[dict], distance_cm: float) -> set[int]:
        if not points:
            return set()
        if len(points) == 1:
            return {0}
        for idx, (left, right) in enumerate(zip(points, points[1:])):
            d1 = self._point_distance(left)
            d2 = self._point_distance(right)
            if min(d1, d2) <= distance_cm <= max(d1, d2):
                return {idx, idx + 1}

        distances = [self._point_distance(p) for p in points]
        if branch == "approach":
            if distance_cm > max(distances):
                return {0}
            return {len(points) - 1}
        if distance_cm < min(distances):
            return {0}
        return {len(points) - 1}

    def _next_point_distance_cm(self, branch: str, points: list[dict], distance_cm: float) -> float | None:
        if branch == "approach":
            candidates = [self._point_distance(p) for p in points if self._point_distance(p) <= distance_cm]
            if not candidates:
                return None
            return max(0.0, distance_cm - max(candidates))

        candidates = [self._point_distance(p) for p in points if self._point_distance(p) >= distance_cm]
        if not candidates:
            return None
        return max(0.0, min(candidates) - distance_cm)

    def _point_distance(self, point: dict) -> float:
        try:
            return float(point.get("distance_cm", 0.0))
        except (TypeError, ValueError):
            return 0.0


class TrainSectionWidget:
    def __init__(self, dashboard_parent, diagnostics_parent, send_cb):
        self.send_cb = send_cb
        self.state: TrainSectionState | None = None

        self.frame = ttk.LabelFrame(dashboard_parent, text="Train")
        self.diagnostics_frame = ttk.LabelFrame(diagnostics_parent, text="Train diagnostics")

        dashboard_body = ttk.Frame(self.frame)
        dashboard_body.pack(fill="both", expand=True, padx=8, pady=8)
        dashboard_body.columnconfigure(0, weight=0)
        dashboard_body.columnconfigure(1, weight=1)
        dashboard_body.rowconfigure(0, weight=0)

        self.video_wrap = ttk.Frame(dashboard_body)
        self.video_wrap.grid(row=0, column=0, sticky="nw")

        self.side_wrap = ttk.Frame(dashboard_body)
        self.side_wrap.grid(row=0, column=1, sticky="new", padx=(10, 0))
        self.side_wrap.configure(height=VIDEO_CANVAS_HEIGHT)
        self.side_wrap.grid_propagate(False)
        self.side_wrap.columnconfigure(0, weight=1)

        status = ttk.Frame(self.side_wrap)
        status.grid(row=0, column=0, sticky="ew", pady=(0, 6))

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

        self.video_canvas = tk.Canvas(
            self.video_wrap,
            width=VIDEO_CANVAS_WIDTH,
            height=VIDEO_CANVAS_HEIGHT,
            bg="black",
            highlightthickness=1,
            highlightbackground="#555",
        )
        self.video_canvas.pack(anchor="w")
        self.video_photo = None
        self.video_image_id = None
        self.video_text_id = self.video_canvas.create_text(
            VIDEO_CANVAS_WIDTH // 2,
            VIDEO_CANVAS_HEIGHT // 2,
            text="Camera offline",
            fill="#d8d8d8",
            font=("Arial", 14),
        )

        controls = ttk.Frame(self.side_wrap)
        controls.grid(row=1, column=0, sticky="ew", pady=(0, 8))

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

        self.live_wrap = ttk.Frame(self.side_wrap)
        self.live_wrap.grid(row=2, column=0, sticky="nsew")
        self.live_wrap.columnconfigure(0, weight=1)
        self.live_wrap.columnconfigure(1, weight=1)
        self.live_wrap.rowconfigure(0, weight=1)
        self.side_wrap.rowconfigure(2, weight=1)

        self.live_info = self._make_live_text(self.live_wrap)
        self.live_info.grid(row=0, column=0, sticky="nsew", padx=(0, 4))
        self.live_points = self._make_live_text(self.live_wrap)
        self.live_points.grid(row=0, column=1, sticky="nsew", padx=(4, 0))

        self._build_diagnostics()

    def _make_live_text(self, parent):
        text = tk.Text(
            parent,
            width=32,
            wrap="none",
            borderwidth=1,
            relief="solid",
            font=("TkFixedFont", 8),
            padx=2,
            pady=2,
        )
        text.configure(state="disabled")
        text.tag_configure("title", font=("TkDefaultFont", 8, "bold"))
        text.tag_configure("muted", foreground="#606060")
        text.tag_configure("interval", background="#fff2a8")
        text.tag_configure("warn", foreground="#a55d00")
        return text

    def _build_diagnostics(self):
        info = ttk.Frame(self.diagnostics_frame)
        info.pack(fill="x", padx=8, pady=(8, 4))

        self.train_id_var = tk.StringVar(value="train_id: -")
        self.lego_id_var = tk.StringVar(value="lego_id: -")
        self.camera_id_var = tk.StringVar(value="camera_id: -")
        self.camera_device_var = tk.StringVar(value="device_id: -")
        self.camera_fw_var = tk.StringVar(value="fw: -")
        self.marker_info_var = tk.StringVar(value="marker: -")

        for var in (
            self.train_id_var,
            self.lego_id_var,
            self.camera_id_var,
            self.camera_device_var,
            self.camera_fw_var,
            self.marker_info_var,
            self.power_var,
        ):
            ttk.Label(info, textvariable=var).pack(anchor="w")

        fota_row = ttk.Frame(self.diagnostics_frame)
        fota_row.pack(fill="x", padx=8, pady=(4, 8))

        self.btn_fota = ttk.Button(
            fota_row,
            text="FOTA...",
            command=self._on_fota,
            state="disabled",
        )
        self.btn_fota.pack(side="left")

        term_wrap = ttk.Frame(self.diagnostics_frame)
        term_wrap.pack(fill="both", expand=True, padx=8, pady=(0, 8))

        self.term_label = ttk.Label(term_wrap, text="Camera terminal")
        self.term_label.pack(anchor="w")

        self.term = tk.Text(term_wrap, height=10, width=1, wrap="word")
        self.term.pack(fill="both", expand=True)
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
        self.diagnostics_frame.destroy()

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
            parent=self.diagnostics_frame,
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

    def set_live_info(self, parts: tuple[list[tuple[str, str]], list[tuple[str, str]]]):
        summary, points = parts
        self._write_live_text(self.live_info, summary)
        self._write_live_text(self.live_points, points)

    def _write_live_text(self, widget: tk.Text, parts: list[tuple[str, str]]):
        widget.configure(state="normal")
        widget.delete("1.0", "end")
        for text, tag in parts:
            widget.insert("end", text, tag or None)
        widget.configure(state="disabled")

    def set_video_photo(self, photo: tk.PhotoImage):
        self.video_photo = photo
        if self.video_image_id is None:
            self.video_image_id = self.video_canvas.create_image(
                VIDEO_CANVAS_WIDTH // 2,
                VIDEO_CANVAS_HEIGHT // 2,
                image=self.video_photo,
                anchor="center",
            )
        else:
            self.video_canvas.itemconfig(self.video_image_id, image=self.video_photo)
            self.video_canvas.coords(
                self.video_image_id,
                VIDEO_CANVAS_WIDTH // 2,
                VIDEO_CANVAS_HEIGHT // 2,
            )
        self.video_canvas.itemconfig(self.video_text_id, text="")

    def clear_video(self, message: str):
        self.video_photo = None
        if self.video_image_id is not None:
            self.video_canvas.delete(self.video_image_id)
            self.video_image_id = None
        self.video_canvas.coords(
            self.video_text_id,
            VIDEO_CANVAS_WIDTH // 2,
            VIDEO_CANVAS_HEIGHT // 2,
        )
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
        if st.marker_id:
            dist = "-" if st.marker_distance_m is None else f"{st.marker_distance_m:.2f}m"
            raw = "-" if st.marker_distance_raw_m is None else f"{st.marker_distance_raw_m:.2f}m"
            area = "-" if st.marker_area_px is None else f"{st.marker_area_px:.0f}px"
            marker_dict = st.marker_dict or "-"
            self.marker_info_var.set(
                f"marker: id={st.marker_id} dist={dist} raw={raw} area={area} dict={marker_dict}"
            )
        else:
            self.marker_info_var.set("marker: -")

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
        self._layout_row_count = 0
        self._diagnostics_row_count = 0
        self._last_live_info_s: dict[str, float] = {}
        self.ws_console_log_dir = os.path.join(os.path.dirname(__file__), "tmp")

        self.notebook = ttk.Notebook(root)
        self.notebook.pack(fill="both", expand=True)

        self.main = ttk.Frame(self.notebook)
        self.console_tab = ttk.Frame(self.notebook)
        self.map_tab = RailwayMapEditor(self.notebook)

        self.notebook.add(self.main, text="Dashboard")
        self.notebook.add(self.console_tab, text="Console")
        self.notebook.add(self.map_tab, text="Railway Map")

        ttk.Label(
            self.main,
            text="LEGO Train Dashboard",
            font=("Arial", 18, "bold"),
        ).pack(anchor="w", padx=8, pady=(8, 4))

        self.canvas = tk.Canvas(self.main, highlightthickness=0)
        self.scroll = ttk.Scrollbar(self.main, orient="vertical", command=self.canvas.yview)
        self.inner = ttk.Frame(self.canvas)
        self.realtime_map = RealtimeRailwayView(self.inner, self._current_map_data)

        self.inner.bind(
            "<Configure>",
            lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all"))
        )

        self.inner_window = self.canvas.create_window((0, 0), window=self.inner, anchor="nw")
        self.canvas.configure(yscrollcommand=self.scroll.set)
        self.canvas.bind("<Configure>", self._on_canvas_configure)

        self.canvas.pack(side="left", fill="both", expand=True)
        self.scroll.pack(side="right", fill="y")

        self.console_canvas = tk.Canvas(self.console_tab, highlightthickness=0)
        self.console_scroll = ttk.Scrollbar(self.console_tab, orient="vertical", command=self.console_canvas.yview)
        self.console_inner = ttk.Frame(self.console_canvas)

        self.console_inner.bind(
            "<Configure>",
            lambda e: self.console_canvas.configure(scrollregion=self.console_canvas.bbox("all"))
        )

        self.console_inner_window = self.console_canvas.create_window((0, 0), window=self.console_inner, anchor="nw")
        self.console_canvas.configure(yscrollcommand=self.console_scroll.set)
        self.console_canvas.bind("<Configure>", self._on_console_canvas_configure)

        self.console_canvas.pack(side="left", fill="both", expand=True)
        self.console_scroll.pack(side="right", fill="y")

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

    def _current_map_data(self) -> dict:
        return {
            "elements": [asdict(e) for e in self.map_tab.map.elements],
            "markers": [asdict(m) for m in self.map_tab.map.markers],
        }

    def _dashboard_info_parts(self, section_id: str) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
        st = self.sections.get(section_id)
        if not st:
            return [], []

        info = self.realtime_map.section_info(section_id, st)
        speed = "n/a" if st.marker_speed_mps is None else f"{st.marker_speed_mps:.2f} m/s"
        distance = "-" if st.marker_distance_m is None else f"{st.marker_distance_m:.2f} m"
        raw_distance = "-" if st.marker_distance_raw_m is None else f"{st.marker_distance_raw_m:.2f} m"
        fps = "-" if st.video_fps <= 0 else f"{st.video_fps:.1f}"

        summary: list[tuple[str, str]] = [
            ("Live\n", "title"),
            (f"Video FPS: {fps}\n", ""),
            (f"Marker: {st.marker_id or '-'}\n", ""),
            (f"Distance: {distance}  raw {raw_distance}\n", ""),
            (f"Power: {st.power}\n", ""),
            (f"Speed: {speed}\n", ""),
        ]
        points_parts: list[tuple[str, str]] = [("Control points\n", "title")]

        status = info.get("status")
        if status == "mapped":
            next_distance = info.get("next_point_distance_cm")
            next_text = "-" if next_distance is None else f"{next_distance:.1f} cm"
            summary.extend([
                (f"Section: {info['section_no']} {info['section_label']}\n", ""),
                (f"Direction: {info['branch']}\n", ""),
                (f"Position: x={info['x_mm']:.0f} y={info['y_mm']:.0f} mm\n", ""),
                (f"Next point: {next_text}\n", ""),
            ])
            points_parts.append((f"{info['branch']}\n", "muted"))
            points = info.get("control_points", [])
            interval_indexes = info.get("interval_indexes", set())
            if points:
                for idx, point in enumerate(points):
                    distance_cm = self.realtime_map._point_distance(point)
                    action_type = point.get("action_type", point.get("type", "power"))
                    value = point.get("value", point.get("power", ""))
                    timeout = point.get("timeout_s", 0)
                    prefix = ">>" if idx in interval_indexes else "  "
                    tag = "interval" if idx in interval_indexes else ""
                    points_parts.append((f"{prefix} {distance_cm:5.1f}cm {str(action_type)[:5]:<5} v={value} t={timeout}\n", tag))
            else:
                points_parts.append(("-\n", "muted"))
        elif status == "unmapped":
            ids = ", ".join(str(mid) for mid in info.get("map_sign_ids", [])) or "-"
            summary.extend([
                ("Marker is visible, but it is not placed on the current map.\n", "warn"),
                (f"Map sign IDs: {ids}\n", "muted"),
            ])
            points_parts.append(("-\n", "muted"))
        elif status == "stale":
            summary.append(("Marker data is stale.\n", "muted"))
            points_parts.append(("-\n", "muted"))
        else:
            ids = ", ".join(str(mid) for mid in info.get("map_sign_ids", [])) or "-"
            summary.extend([
                ("Waiting for marker_seen...\n", "muted"),
                (f"Map sign IDs: {ids}\n", "muted"),
            ])
            points_parts.append(("-\n", "muted"))

        return summary, points_parts

    def _on_canvas_configure(self, event):
        self.canvas.itemconfigure(self.inner_window, width=event.width)
        self._layout_sections(event.width)

    def _on_console_canvas_configure(self, event):
        self.console_canvas.itemconfigure(self.console_inner_window, width=event.width)
        self._layout_diagnostics()

    def _section_columns(self, available_width: int) -> int:
        if len(self.widgets) <= 1:
            return 1
        if available_width >= SECTION_WIDE_LAYOUT_MIN_WIDTH:
            return SECTION_MAX_COLUMNS
        return 1

    def _layout_sections(self, available_width: int | None = None):
        if available_width is None:
            available_width = max(1, self.canvas.winfo_width())

        columns = self._section_columns(available_width)
        for col in range(SECTION_MAX_COLUMNS):
            self.inner.grid_columnconfigure(
                col,
                weight=1 if col < columns else 0,
                uniform="train_sections" if col < columns else "",
            )

        section_ids = [sid for sid in self.sections.keys() if sid in self.widgets]
        row_count = (len(section_ids) + columns - 1) // columns

        for idx, section_id in enumerate(section_ids):
            row = idx // columns
            col = idx % columns
            self.widgets[section_id].frame.grid(
                row=row,
                column=col,
                sticky="new",
                padx=SECTION_GRID_PAD_X,
                pady=SECTION_GRID_PAD_Y,
            )

        for row in range(max(self._layout_row_count, row_count)):
            self.inner.grid_rowconfigure(row, weight=0)
        viewport_h = max(1, self.canvas.winfo_height())
        train_rows_h = row_count * (VIDEO_CANVAS_HEIGHT + 52)
        map_h = max(260, viewport_h - train_rows_h - 46)
        self.realtime_map.canvas.configure(height=map_h)
        self.realtime_map.frame.grid(
            row=row_count,
            column=0,
            columnspan=columns,
            sticky="nsew",
            padx=SECTION_GRID_PAD_X,
            pady=(SECTION_GRID_PAD_Y, SECTION_GRID_PAD_Y + 8),
        )
        self.inner.grid_rowconfigure(row_count, weight=1)
        self._layout_row_count = row_count

    def _layout_diagnostics(self):
        if not self.widgets:
            return

        self.console_inner.grid_columnconfigure(0, weight=1)
        section_ids = [sid for sid in self.sections.keys() if sid in self.widgets]
        for idx, section_id in enumerate(section_ids):
            self.widgets[section_id].diagnostics_frame.grid(
                row=idx,
                column=0,
                sticky="nsew",
                padx=SECTION_GRID_PAD_X,
                pady=SECTION_GRID_PAD_Y,
            )

        for row in range(max(self._diagnostics_row_count, len(section_ids))):
            self.console_inner.grid_rowconfigure(row, weight=1 if row < len(section_ids) else 0)
        self._diagnostics_row_count = len(section_ids)

    def _layout_all_sections(self):
        self._layout_sections()
        self._layout_diagnostics()
        self.realtime_map.set_trains(self.sections)

    def _ensure_section_by_train(self, train_id: str) -> TrainSectionState:
        if train_id not in self.sections:
            self.sections[train_id] = TrainSectionState(section_id=train_id, train_id=train_id)
        if train_id not in self.widgets:
            self.widgets[train_id] = TrainSectionWidget(self.inner, self.console_inner, self.bus.emit)
        self._refresh_section(train_id)
        return self.sections[train_id]

    def _ensure_section_by_camera(self, camera_id: str) -> TrainSectionState:
        section_id = self.camera_to_section.get(camera_id)
        if section_id and section_id in self.sections:
            self._refresh_section(section_id)
            return self.sections[section_id]

        section_id = self._alloc_section_id()
        self.sections[section_id] = TrainSectionState(section_id=section_id, camera_id=camera_id)
        self.widgets[section_id] = TrainSectionWidget(self.inner, self.console_inner, self.bus.emit)
        self.camera_to_section[camera_id] = section_id
        self.sections[section_id].terminal_path = self._default_terminal_path(camera_id)
        self.sections[section_id].video_path = self._default_video_path(camera_id)
        self._refresh_section(section_id)
        return self.sections[section_id]

    def _refresh_section(self, section_id: str, layout: bool = True, refresh_map: bool = False):
        if section_id in self.sections and section_id in self.widgets:
            self.widgets[section_id].set_state(self.sections[section_id])
            now = time.monotonic()
            if layout or now - self._last_live_info_s.get(section_id, 0.0) >= LIVE_INFO_UPDATE_INTERVAL_S:
                self.widgets[section_id].set_live_info(self._dashboard_info_parts(section_id))
                self._last_live_info_s[section_id] = now
            if layout:
                self._layout_all_sections()
            if refresh_map:
                self.realtime_map.refresh()

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
        self._layout_all_sections()

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

        max_w, max_h = VIDEO_CANVAS_WIDTH, VIDEO_CANVAS_HEIGHT
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
        now = time.monotonic()
        if st.video_last_frame_s > 0 and now > st.video_last_frame_s:
            instant_fps = 1.0 / max(0.001, now - st.video_last_frame_s)
            st.video_fps = instant_fps if st.video_fps <= 0 else (st.video_fps * 0.8 + instant_fps * 0.2)
        st.video_last_frame_s = now
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
                self._refresh_section(train_id, layout=False, refresh_map=True)

        elif etype == "marker_seen":
            train_id = data.get("train_id", "")
            camera_id = data.get("camera_id", "")
            section_id = train_id if train_id in self.sections else self.camera_to_section.get(camera_id, "")
            if not section_id and camera_id:
                st = self._ensure_section_by_camera(camera_id)
                section_id = st.section_id

            st = self.sections.get(section_id)
            if st:
                prev_marker_id = st.marker_id
                prev_distance_m = st.marker_distance_m
                prev_seen = st.marker_last_seen
                now = time.monotonic()

                st.marker_id = str(data.get("marker_id", ""))
                try:
                    distance_m = float(data.get("distance_m"))
                    st.marker_distance_m = distance_m
                except (TypeError, ValueError):
                    distance_m = None
                    st.marker_distance_m = None

                if (
                    distance_m is not None
                    and prev_distance_m is not None
                    and prev_marker_id == st.marker_id
                    and prev_seen > 0
                    and now > prev_seen
                ):
                    delta_m = distance_m - prev_distance_m
                    dt_s = now - prev_seen
                    st.marker_speed_mps = abs(delta_m) / dt_s
                    if delta_m < -0.002:
                        st.marker_branch = "approach"
                    elif delta_m > 0.002:
                        st.marker_branch = "retreat"
                else:
                    st.marker_speed_mps = None
                    if prev_marker_id != st.marker_id or not st.marker_branch:
                        st.marker_branch = "approach"

                try:
                    st.marker_distance_raw_m = float(data.get("distance_raw_m"))
                except (TypeError, ValueError):
                    st.marker_distance_raw_m = None
                try:
                    st.marker_area_px = float(data.get("area_px"))
                except (TypeError, ValueError):
                    st.marker_area_px = None
                st.marker_dict = str(data.get("dict", ""))
                st.marker_last_seen = now
                self._refresh_section(section_id, layout=False, refresh_map=True)

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
            self._refresh_section(section_id, layout=False)

            if st.removable:
                self._remove_section(section_id)

        self.realtime_map.refresh()
        self.root.after(1000, self._housekeeping)


def main():
    root = tk.Tk()
    app = TrainGuiApp(root)
    app.start()
    root.mainloop()


if __name__ == "__main__":
    main()
