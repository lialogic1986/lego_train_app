#!/usr/bin/env python3
import json
import math
import os
import time
import uuid
from dataclasses import asdict, dataclass, field
from tkinter import messagebox, ttk
import tkinter as tk


MAP_CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "railway_map.json")
STUD_MM = 8.0
GRID_STUDS = 4
GRID_MM = STUD_MM * GRID_STUDS
PX_PER_MM = 1.0
CANVAS_W_MM = 2400
CANVAS_H_MM = 1600
CONNECT_SNAP_MM = 20.0
ACTION_TABLE_ROW_HEIGHT = 28
ACTION_TABLE_HEIGHT_ROWS = 6


def curve_len_mm(radius_studs: float, angle_deg: float) -> float:
    return math.radians(angle_deg) * radius_studs * STUD_MM


TRACK_LIBRARY = {
    "straight": {
        "label": "Straight 16",
        "length_mm": 16 * STUD_MM,
        "draw": "straight",
        "studs": 16,
    },
    "curve_left": {
        "label": "Curve L R40",
        "length_mm": curve_len_mm(40, 22.5),
        "draw": "curve_left",
        "radius_studs": 40,
        "angle_deg": 22.5,
    },
    "curve_right": {
        "label": "Curve R R40",
        "length_mm": curve_len_mm(40, 22.5),
        "draw": "curve_right",
        "radius_studs": 40,
        "angle_deg": 22.5,
    },
    "switch_left": {
        "label": "Switch L 32",
        "length_mm": 32 * STUD_MM,
        "draw": "switch_left",
        "studs": 32,
        "branch_angle_deg": 22.5,
    },
    "switch_right": {
        "label": "Switch R 32",
        "length_mm": 32 * STUD_MM,
        "draw": "switch_right",
        "studs": 32,
        "branch_angle_deg": 22.5,
    },
}

ACTION_TYPES = ("power", "reverse", "speed", "stop", "start")
BRANCHES = ("approach", "retreat")


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:8]}"


def snap_mm(v: float) -> int:
    return int(round(v / GRID_MM) * GRID_MM)


def rotate_point(x: float, y: float, deg: float) -> tuple[float, float]:
    rad = math.radians(deg % 360)
    c = math.cos(rad)
    s = math.sin(rad)
    return x * c - y * s, x * s + y * c


@dataclass
class TrackElement:
    id: str
    kind: str
    x_mm: int
    y_mm: int
    rotation: int = 0
    length_mm: float = 128.0


@dataclass
class ControlPoint:
    id: str
    distance_cm: float
    action_type: str = "power"
    value: float = 0.0
    timeout_s: float = 0.0


@dataclass
class BranchConfig:
    points: list[ControlPoint] = field(default_factory=list)


@dataclass
class MarkerActionConfig:
    interpolate_power: bool = True
    approach: BranchConfig = field(
        default_factory=lambda: BranchConfig(
            [
                ControlPoint(new_id("cp"), 50.0, "power", 50),
                ControlPoint(new_id("cp"), 40.0, "power", 40),
            ]
        )
    )
    retreat: BranchConfig = field(default_factory=BranchConfig)


@dataclass
class ArucoMarker:
    id: str
    marker_id: int
    x_mm: int
    y_mm: int
    rotation: int = 0
    actions: MarkerActionConfig = field(default_factory=MarkerActionConfig)


@dataclass
class RailwayMap:
    version: int = 2
    updated_ms: int = 0
    elements: list[TrackElement] = field(default_factory=list)
    markers: list[ArucoMarker] = field(default_factory=list)

    def total_length_m(self) -> float:
        return sum(e.length_mm for e in self.elements) / 1000.0


class RailwayMapEditor(ttk.Frame):
    def __init__(self, parent):
        super().__init__(parent)
        self.map = RailwayMap()
        self.selected_kind = ""
        self.selected_id = ""
        self.drag_start: tuple[float, float] | None = None
        self.zoom = 1.0
        self.snap_var = tk.BooleanVar(value=True)
        self.interpolate_power_var = tk.BooleanVar(value=True)
        self.marker_id_var = tk.StringVar()
        self.cp_branch_var = tk.StringVar(value="approach")
        self.cp_distance_var = tk.StringVar(value="50")
        self.cp_type_var = tk.StringVar(value="power")
        self.cp_value_var = tk.StringVar(value="50")
        self.cp_timeout_var = tk.StringVar(value="0")
        self.status_var = tk.StringVar(value="")
        self.summary_var = tk.StringVar()
        self.branch_tables: dict[str, ttk.Treeview] = {}

        self._build_ui()
        self.load(silent=True)
        self.redraw()

    def sx(self, x_mm: float) -> float:
        return x_mm * PX_PER_MM * self.zoom

    def sy(self, y_mm: float) -> float:
        return y_mm * PX_PER_MM * self.zoom

    def sm(self, mm: float) -> float:
        return mm * PX_PER_MM * self.zoom

    def canvas_to_map(self, x_px: float, y_px: float) -> tuple[float, float]:
        return x_px / (PX_PER_MM * self.zoom), y_px / (PX_PER_MM * self.zoom)

    def _build_ui(self):
        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)
        style = ttk.Style(self)
        style.configure("Action.Treeview", rowheight=ACTION_TABLE_ROW_HEIGHT)
        style.configure("Action.Treeview.Heading", padding=(4, 4))

        toolbar = ttk.Frame(self)
        toolbar.grid(row=0, column=0, columnspan=2, sticky="ew", padx=8, pady=(8, 4))

        for kind, meta in TRACK_LIBRARY.items():
            ttk.Button(toolbar, text=f"+ {meta['label']}", command=lambda k=kind: self.add_track(k)).pack(side="left", padx=2)

        ttk.Button(toolbar, text="+ ArUco", command=self.add_marker).pack(side="left", padx=(12, 2))
        ttk.Button(toolbar, text="Rotate", command=self.rotate_selected).pack(side="left", padx=2)
        ttk.Button(toolbar, text="Delete", command=self.delete_selected).pack(side="left", padx=2)
        ttk.Checkbutton(toolbar, text="Snap", variable=self.snap_var).pack(side="left", padx=(12, 2))

        ttk.Button(toolbar, text="-", width=3, command=lambda: self.set_zoom(self.zoom / 1.25)).pack(side="left", padx=(14, 2))
        ttk.Button(toolbar, text="+", width=3, command=lambda: self.set_zoom(self.zoom * 1.25)).pack(side="left", padx=2)
        self.zoom_label = ttk.Label(toolbar, text="100%")
        self.zoom_label.pack(side="left", padx=(4, 10))

        ttk.Button(toolbar, text="Load", command=self.load).pack(side="right", padx=2)
        ttk.Button(toolbar, text="Save", command=self.save).pack(side="right", padx=2)

        canvas_wrap = ttk.Frame(self)
        canvas_wrap.grid(row=1, column=0, sticky="nsew", padx=(8, 4), pady=(0, 8))
        canvas_wrap.columnconfigure(0, weight=1)
        canvas_wrap.rowconfigure(0, weight=1)

        self.canvas = tk.Canvas(canvas_wrap, bg="#f6f6f6")
        self.canvas.grid(row=0, column=0, sticky="nsew")
        xscroll = ttk.Scrollbar(canvas_wrap, orient="horizontal", command=self.canvas.xview)
        yscroll = ttk.Scrollbar(canvas_wrap, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(xscrollcommand=xscroll.set, yscrollcommand=yscroll.set)
        xscroll.grid(row=1, column=0, sticky="ew")
        yscroll.grid(row=0, column=1, sticky="ns")

        self.canvas.bind("<Button-1>", self._on_canvas_down)
        self.canvas.bind("<B1-Motion>", self._on_canvas_drag)
        self.canvas.bind("<ButtonRelease-1>", self._on_canvas_up)
        self.canvas.bind("<MouseWheel>", self._on_mousewheel)

        props = ttk.Frame(self)
        props.grid(row=1, column=1, sticky="nsew", padx=(4, 8), pady=(0, 8))
        props.columnconfigure(0, weight=1)

        ttk.Label(props, text="Railway Map", font=("Arial", 13, "bold")).grid(row=0, column=0, sticky="w")
        ttk.Label(props, textvariable=self.summary_var).grid(row=1, column=0, sticky="w", pady=(2, 8))
        self.props_body = ttk.Frame(props)
        self.props_body.grid(row=2, column=0, sticky="nsew")
        props.rowconfigure(2, weight=1)
        ttk.Label(props, textvariable=self.status_var).grid(row=3, column=0, sticky="ew", pady=(8, 0))

    def set_zoom(self, zoom: float):
        self.zoom = max(0.35, min(3.0, zoom))
        self.zoom_label.configure(text=f"{int(self.zoom * 100)}%")
        self.redraw(refresh_properties=False)

    def _on_mousewheel(self, event):
        if event.state & 0x0004:
            self.set_zoom(self.zoom * (1.1 if event.delta > 0 else 1 / 1.1))

    def _center_point(self) -> tuple[int, int]:
        cx = self.canvas.canvasx(self.canvas.winfo_width() / 2)
        cy = self.canvas.canvasy(self.canvas.winfo_height() / 2)
        x, y = self.canvas_to_map(cx, cy)
        if self.snap_var.get():
            x, y = snap_mm(x), snap_mm(y)
        return int(x), int(y)

    def add_track(self, kind: str):
        meta = TRACK_LIBRARY[kind]
        x, y = self._center_point()
        elem = TrackElement(new_id("track"), kind, x, y, 0, float(meta["length_mm"]))
        self.map.elements.append(elem)
        self.select("track", elem.id)
        self.redraw()

    def add_marker(self):
        x, y = self._center_point()
        next_marker_id = max((m.marker_id for m in self.map.markers), default=0) + 1
        marker = ArucoMarker(new_id("aruco"), next_marker_id, x, y, 0)
        self.map.markers.append(marker)
        self.select("marker", marker.id)
        self.redraw()

    def select(self, kind: str, item_id: str):
        self.apply_marker_properties(refresh=False, show_errors=False)
        self.selected_kind = kind
        self.selected_id = item_id
        self._refresh_properties()

    def selected_track(self) -> TrackElement | None:
        return next((e for e in self.map.elements if e.id == self.selected_id), None)

    def selected_marker(self) -> ArucoMarker | None:
        return next((m for m in self.map.markers if m.id == self.selected_id), None)

    def rotate_selected(self):
        if self.selected_kind == "track":
            elem = self.selected_track()
            if elem:
                elem.rotation = (elem.rotation + 90) % 360
        elif self.selected_kind == "marker":
            marker = self.selected_marker()
            if marker:
                marker.rotation = (marker.rotation + 45) % 360
        self.redraw()

    def delete_selected(self):
        self.apply_marker_properties(refresh=False, show_errors=False)
        if self.selected_kind == "track":
            self.map.elements = [e for e in self.map.elements if e.id != self.selected_id]
        elif self.selected_kind == "marker":
            self.map.markers = [m for m in self.map.markers if m.id != self.selected_id]
        self.selected_kind = ""
        self.selected_id = ""
        self.redraw()

    def redraw(self, refresh_properties: bool = True):
        self.canvas.delete("all")
        self.canvas.configure(scrollregion=(0, 0, self.sx(CANVAS_W_MM), self.sy(CANVAS_H_MM)))
        self._draw_grid()
        for elem in self.map.elements:
            self._draw_track(elem)
        self._draw_connectors()
        for marker in self.map.markers:
            self._draw_marker(marker)
        self._update_summary()
        if refresh_properties:
            self._refresh_properties()

    def _draw_grid(self):
        step = self.sm(GRID_MM)
        major = GRID_MM * 4
        x = 0
        while x <= CANVAS_W_MM:
            color = "#d6d6d6" if x % major == 0 else "#ececec"
            self.canvas.create_line(self.sx(x), 0, self.sx(x), self.sy(CANVAS_H_MM), fill=color)
            x += GRID_MM
        y = 0
        while y <= CANVAS_H_MM:
            color = "#d6d6d6" if y % major == 0 else "#ececec"
            self.canvas.create_line(0, self.sy(y), self.sx(CANVAS_W_MM), self.sy(y), fill=color)
            y += GRID_MM

    def _draw_track(self, elem: TrackElement):
        selected = self.selected_kind == "track" and self.selected_id == elem.id
        tags = ("track", f"track:{elem.id}")
        width = 8 if selected else 5
        if TRACK_LIBRARY[elem.kind]["draw"] == "straight":
            self._draw_straight(elem, width, tags)
        elif "curve" in elem.kind:
            self._draw_curve(elem, width, tags)
        else:
            self._draw_switch(elem, width, tags)
        self.canvas.create_text(self.sx(elem.x_mm), self.sy(elem.y_mm - 18), text=TRACK_LIBRARY[elem.kind]["label"], fill="#222", tags=tags)

    def _draw_straight(self, elem: TrackElement, width: int, tags):
        half = elem.length_mm / 2
        p1 = rotate_point(-half, 0, elem.rotation)
        p2 = rotate_point(half, 0, elem.rotation)
        self.canvas.create_line(self.sx(elem.x_mm + p1[0]), self.sy(elem.y_mm + p1[1]),
                                self.sx(elem.x_mm + p2[0]), self.sy(elem.y_mm + p2[1]),
                                fill="#2e2e2e", width=width, capstyle="round", tags=tags)

    def _draw_curve(self, elem: TrackElement, width: int, tags):
        meta = TRACK_LIBRARY[elem.kind]
        r = meta["radius_studs"] * STUD_MM
        angle = meta["angle_deg"] * (1 if elem.kind == "curve_left" else -1)
        p0, p1 = self._curve_endpoints(elem, r, angle)
        self.canvas.create_line(self.sx(p0[0]), self.sy(p0[1]), self.sx(elem.x_mm), self.sy(elem.y_mm),
                                self.sx(p1[0]), self.sy(p1[1]), smooth=True, splinesteps=24,
                                fill="#2e2e2e", width=width, capstyle="round", tags=tags)

    def _draw_switch(self, elem: TrackElement, width: int, tags):
        half = elem.length_mm / 2
        main_a = rotate_point(-half, 0, elem.rotation)
        main_b = rotate_point(half, 0, elem.rotation)
        angle = TRACK_LIBRARY[elem.kind]["branch_angle_deg"] * (-1 if elem.kind == "switch_left" else 1)
        branch_b = rotate_point(half * math.cos(math.radians(angle)), half * math.sin(math.radians(angle)), elem.rotation)
        self.canvas.create_line(self.sx(elem.x_mm + main_a[0]), self.sy(elem.y_mm + main_a[1]),
                                self.sx(elem.x_mm + main_b[0]), self.sy(elem.y_mm + main_b[1]),
                                fill="#2e2e2e", width=width, capstyle="round", tags=tags)
        self.canvas.create_line(self.sx(elem.x_mm), self.sy(elem.y_mm),
                                self.sx(elem.x_mm + branch_b[0]), self.sy(elem.y_mm + branch_b[1]),
                                fill="#2e2e2e", width=width, capstyle="round", tags=tags)

    def _curve_endpoints(self, elem: TrackElement, radius_mm: float, signed_angle_deg: float):
        half_angle = abs(signed_angle_deg) / 2
        chord = 2 * radius_mm * math.sin(math.radians(half_angle))
        p0 = rotate_point(-chord / 2, 0, elem.rotation)
        p1 = rotate_point(chord / 2, 0, elem.rotation + signed_angle_deg)
        return (elem.x_mm + p0[0], elem.y_mm + p0[1]), (elem.x_mm + p1[0], elem.y_mm + p1[1])

    def _track_connectors(self, elem: TrackElement) -> list[tuple[float, float]]:
        if elem.kind == "straight":
            half = elem.length_mm / 2
            return [(elem.x_mm + x, elem.y_mm + y) for x, y in (rotate_point(-half, 0, elem.rotation), rotate_point(half, 0, elem.rotation))]
        if "curve" in elem.kind:
            meta = TRACK_LIBRARY[elem.kind]
            angle = meta["angle_deg"] * (1 if elem.kind == "curve_left" else -1)
            return list(self._curve_endpoints(elem, meta["radius_studs"] * STUD_MM, angle))
        half = elem.length_mm / 2
        angle = TRACK_LIBRARY[elem.kind]["branch_angle_deg"] * (-1 if elem.kind == "switch_left" else 1)
        main_a = rotate_point(-half, 0, elem.rotation)
        main_b = rotate_point(half, 0, elem.rotation)
        branch_b = rotate_point(half * math.cos(math.radians(angle)), half * math.sin(math.radians(angle)), elem.rotation)
        return [(elem.x_mm + main_a[0], elem.y_mm + main_a[1]), (elem.x_mm + main_b[0], elem.y_mm + main_b[1]), (elem.x_mm + branch_b[0], elem.y_mm + branch_b[1])]

    def _draw_connectors(self):
        for elem in self.map.elements:
            selected = self.selected_kind == "track" and self.selected_id == elem.id
            for x, y in self._track_connectors(elem):
                r = 6 if selected else 4
                self.canvas.create_oval(self.sx(x) - r, self.sy(y) - r, self.sx(x) + r, self.sy(y) + r,
                                        fill="#ffcc33", outline="#8a5a00", width=2)

    def _draw_marker(self, marker: ArucoMarker):
        selected = self.selected_kind == "marker" and self.selected_id == marker.id
        tags = ("marker", f"marker:{marker.id}")
        size = 28
        fill = "#f7f7f7" if not selected else "#ffe6a3"
        outline = "#1f77b4" if not selected else "#d28a00"
        x = self.sx(marker.x_mm)
        y = self.sy(marker.y_mm)
        self.canvas.create_rectangle(x - size, y - size, x + size, y + size, fill=fill, outline=outline, width=3, tags=tags)
        self.canvas.create_text(x, y, text=str(marker.marker_id), font=("Arial", 13, "bold"), fill="#111", tags=tags)
        front = rotate_point(0, -size - 20, marker.rotation)
        self.canvas.create_polygon(x, y, x + front[0] - 12, y + front[1] + 12, x + front[0] + 12, y + front[1] + 12,
                                   fill="#7cc7ff", outline=outline, tags=tags)
        self.canvas.create_line(x, y, x + front[0], y + front[1], fill=outline, arrow="last", width=3, tags=tags)

    def _on_canvas_down(self, event):
        x = self.canvas.canvasx(event.x)
        y = self.canvas.canvasy(event.y)
        item = self.canvas.find_closest(x, y)
        self.drag_start = self.canvas_to_map(x, y)
        if not item:
            return
        for tag in self.canvas.gettags(item[0]):
            if tag.startswith("track:"):
                self.select("track", tag.split(":", 1)[1])
                self.redraw()
                return
            if tag.startswith("marker:"):
                self.select("marker", tag.split(":", 1)[1])
                self.redraw()
                return

    def _on_canvas_drag(self, event):
        if not self.drag_start or not self.selected_id:
            return
        x, y = self.canvas_to_map(self.canvas.canvasx(event.x), self.canvas.canvasy(event.y))
        prev_x, prev_y = self.drag_start
        dx = x - prev_x
        dy = y - prev_y
        self.drag_start = (x, y)
        obj = self.selected_track() if self.selected_kind == "track" else self.selected_marker()
        if not obj:
            return
        obj.x_mm += int(dx)
        obj.y_mm += int(dy)
        self.redraw(refresh_properties=False)

    def _on_canvas_up(self, _event):
        obj = self.selected_track() if self.selected_kind == "track" else self.selected_marker()
        if obj and self.snap_var.get():
            obj.x_mm = snap_mm(obj.x_mm)
            obj.y_mm = snap_mm(obj.y_mm)
        if self.selected_kind == "track":
            self._snap_track_connectors()
        self.drag_start = None
        self.redraw()

    def _snap_track_connectors(self):
        elem = self.selected_track()
        if not elem:
            return
        own = self._track_connectors(elem)
        best = None
        for other in self.map.elements:
            if other.id == elem.id:
                continue
            for ox, oy in self._track_connectors(other):
                for sx, sy in own:
                    d = math.hypot(ox - sx, oy - sy)
                    if d <= CONNECT_SNAP_MM and (best is None or d < best[0]):
                        best = (d, ox - sx, oy - sy)
        if best:
            elem.x_mm += int(round(best[1]))
            elem.y_mm += int(round(best[2]))
            self.status_var.set("Snapped track connector")

    def _update_summary(self):
        self.summary_var.set(f"Track: {len(self.map.elements)} elements, {self.map.total_length_m():.2f} m; ArUco: {len(self.map.markers)}")

    def _refresh_properties(self):
        for child in self.props_body.winfo_children():
            child.destroy()
        self.branch_tables.clear()

        if self.selected_kind == "track":
            elem = self.selected_track()
            if not elem:
                return
            meta = TRACK_LIBRARY.get(elem.kind, {})
            ttk.Label(self.props_body, text="Track Element", font=("Arial", 11, "bold")).grid(row=0, column=0, sticky="w")
            ttk.Label(self.props_body, text=f"type: {meta.get('label', elem.kind)}").grid(row=1, column=0, sticky="w")
            ttk.Label(self.props_body, text=f"length: {elem.length_mm:.2f} mm").grid(row=2, column=0, sticky="w")
            ttk.Label(self.props_body, text=f"rotation: {elem.rotation} deg").grid(row=3, column=0, sticky="w")
            return

        marker = self.selected_marker() if self.selected_kind == "marker" else None
        if not marker:
            ttk.Label(self.props_body, text="Select track or ArUco marker").grid(row=0, column=0, sticky="w")
            return

        self.marker_id_var.set(str(marker.marker_id))
        self.interpolate_power_var.set(marker.actions.interpolate_power)
        body = self.props_body
        body.columnconfigure(1, weight=1)
        ttk.Label(body, text="ArUco Marker", font=("Arial", 11, "bold")).grid(row=0, column=0, columnspan=2, sticky="w")
        marker_id_entry = self._add_labeled_entry(body, 1, "marker_id", self.marker_id_var)
        marker_id_entry.bind("<Return>", lambda _event: self.apply_marker_properties())
        marker_id_entry.bind("<FocusOut>", lambda _event: self.apply_marker_properties(refresh=False, show_errors=False))
        ttk.Label(body, text=f"front rotation: {marker.rotation} deg").grid(row=2, column=0, columnspan=2, sticky="w")
        ttk.Checkbutton(body, text="Interpolate power every 1 cm", variable=self.interpolate_power_var).grid(row=3, column=0, columnspan=2, sticky="w", pady=(6, 0))

        row = 4
        for branch in BRANCHES:
            row = self._add_branch_table(body, row, branch, getattr(marker.actions, branch).points)

        controls = ttk.LabelFrame(body, text="Control point")
        controls.grid(row=row, column=0, columnspan=2, sticky="ew", pady=(8, 0))
        controls.columnconfigure(1, weight=1)
        ttk.Label(controls, text="branch").grid(row=0, column=0, sticky="w")
        ttk.OptionMenu(controls, self.cp_branch_var, self.cp_branch_var.get(), *BRANCHES).grid(row=0, column=1, sticky="ew")
        self._add_labeled_entry(controls, 1, "distance cm", self.cp_distance_var)
        ttk.Label(controls, text="type").grid(row=2, column=0, sticky="w")
        ttk.OptionMenu(controls, self.cp_type_var, self.cp_type_var.get(), *ACTION_TYPES).grid(row=2, column=1, sticky="ew")
        self._add_labeled_entry(controls, 3, "value", self.cp_value_var)
        self._add_labeled_entry(controls, 4, "timeout s", self.cp_timeout_var)
        ttk.Button(controls, text="Add point", command=self.add_control_point).grid(row=5, column=0, sticky="ew", pady=(6, 0))
        ttk.Button(controls, text="Delete selected point", command=self.delete_control_point).grid(row=5, column=1, sticky="ew", pady=(6, 0))
        ttk.Button(body, text="Apply Marker", command=self.apply_marker_properties).grid(row=row + 1, column=0, columnspan=2, sticky="ew", pady=(8, 0))

    def _add_labeled_entry(self, parent, row: int, label: str, var: tk.StringVar):
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", pady=(4, 0))
        entry = ttk.Entry(parent, textvariable=var)
        entry.grid(row=row, column=1, sticky="ew", pady=(4, 0))
        return entry

    def _add_branch_table(self, parent, row: int, branch: str, points: list[ControlPoint]) -> int:
        ttk.Label(parent, text=branch.title(), font=("Arial", 10, "bold")).grid(row=row, column=0, columnspan=2, sticky="w", pady=(10, 0))
        table_wrap = ttk.Frame(parent)
        table_wrap.grid(row=row + 1, column=0, columnspan=2, sticky="ew")
        table_wrap.columnconfigure(0, weight=1)

        table = ttk.Treeview(
            table_wrap,
            columns=("distance", "type", "value", "timeout"),
            show="headings",
            height=ACTION_TABLE_HEIGHT_ROWS,
            style="Action.Treeview",
        )
        for col, label, width in (
            ("distance", "Distance", 86),
            ("type", "Type", 86),
            ("value", "Value", 82),
            ("timeout", "Timeout", 82),
        ):
            table.heading(col, text=label)
            table.column(col, width=width, minwidth=width, anchor="center", stretch=True)

        scroll = ttk.Scrollbar(table_wrap, orient="vertical", command=table.yview)
        table.configure(yscrollcommand=scroll.set)
        table.grid(row=0, column=0, sticky="ew")
        scroll.grid(row=0, column=1, sticky="ns")

        self.branch_tables[branch] = table
        reverse_sort = branch == "approach"
        for cp in sorted(points, key=lambda p: p.distance_cm, reverse=reverse_sort):
            table.insert("", "end", iid=cp.id, values=(cp.distance_cm, cp.action_type, cp.value, cp.timeout_s))
        return row + 2

    def add_control_point(self):
        marker = self.selected_marker()
        if not marker:
            return
        try:
            cp = ControlPoint(
                new_id("cp"),
                float(self.cp_distance_var.get()),
                self.cp_type_var.get(),
                float(self.cp_value_var.get() or 0),
                float(self.cp_timeout_var.get() or 0),
            )
        except Exception as e:
            messagebox.showerror("Bad control point", str(e))
            return
        getattr(marker.actions, self.cp_branch_var.get()).points.append(cp)
        self.apply_marker_properties(refresh=False)
        self.redraw()

    def delete_control_point(self):
        marker = self.selected_marker()
        if not marker:
            return
        for branch, table in self.branch_tables.items():
            selected = table.selection()
            if selected:
                ids = set(selected)
                cfg = getattr(marker.actions, branch)
                cfg.points = [p for p in cfg.points if p.id not in ids]
                self.redraw()
                return

    def apply_marker_properties(self, refresh: bool = True, show_errors: bool = True) -> bool:
        marker = self.selected_marker()
        if not marker:
            return True
        try:
            marker.marker_id = int(self.marker_id_var.get())
            marker.actions.interpolate_power = bool(self.interpolate_power_var.get())
        except Exception as e:
            if show_errors:
                messagebox.showerror("Bad marker config", str(e))
            return False
        self.status_var.set("Marker properties applied")
        if refresh:
            self.redraw()
        return True

    def to_json(self) -> dict:
        self.map.updated_ms = int(time.time() * 1000)
        return {
            "version": self.map.version,
            "updated_ms": self.map.updated_ms,
            "units": {"track_length": "mm", "distance": "cm"},
            "track_library": TRACK_LIBRARY,
            "elements": [asdict(e) for e in self.map.elements],
            "markers": [asdict(m) for m in self.map.markers],
        }

    def save(self):
        if not self.apply_marker_properties(refresh=False):
            return
        payload = self.to_json()
        try:
            with open(MAP_CONFIG_PATH, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2, ensure_ascii=False)
            with open(MAP_CONFIG_PATH, "r", encoding="utf-8") as f:
                saved = json.load(f)
        except Exception as e:
            messagebox.showerror("Save failed", str(e))
            return
        marker_ids = [str(m.get("marker_id", "?")) for m in saved.get("markers", [])]
        self.status_var.set(f"Saved {MAP_CONFIG_PATH}; markers: {', '.join(marker_ids) or '-'}")

    def load(self, silent: bool = False):
        if not os.path.exists(MAP_CONFIG_PATH):
            if not silent:
                self.status_var.set("No railway_map.json yet")
            return
        try:
            with open(MAP_CONFIG_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            self.map = RailwayMap(
                version=max(2, int(data.get("version", 1))),
                updated_ms=int(data.get("updated_ms", 0)),
                elements=[self._load_track(e) for e in data.get("elements", [])],
                markers=[self._load_marker(m) for m in data.get("markers", [])],
            )
        except Exception as e:
            if not silent:
                messagebox.showerror("Load failed", str(e))
            return
        if not silent:
            self.status_var.set(f"Loaded {MAP_CONFIG_PATH}")
        self.redraw()

    def _load_track(self, raw: dict) -> TrackElement:
        if "x_mm" not in raw:
            raw = dict(raw)
            raw["x_mm"] = raw.pop("x", 0)
            raw["y_mm"] = raw.pop("y", 0)
        raw["length_mm"] = float(TRACK_LIBRARY.get(raw.get("kind", ""), {}).get("length_mm", raw.get("length_mm", 128.0)))
        return TrackElement(**raw)

    def _load_marker(self, raw: dict) -> ArucoMarker:
        if "x_mm" not in raw:
            raw = dict(raw)
            raw["x_mm"] = raw.pop("x", 0)
            raw["y_mm"] = raw.pop("y", 0)
        actions = raw.get("actions", {})
        marker = ArucoMarker(raw["id"], int(raw["marker_id"]), int(raw["x_mm"]), int(raw["y_mm"]), int(raw.get("rotation", 0)))
        marker.actions = self._load_actions(actions)
        return marker

    def _load_actions(self, raw: dict) -> MarkerActionConfig:
        cfg = MarkerActionConfig()
        cfg.interpolate_power = bool(raw.get("interpolate_power", True))
        if "approach" in raw or "retreat" in raw:
            cfg.approach = BranchConfig([self._load_point(p) for p in raw.get("approach", {}).get("points", [])])
            cfg.retreat = BranchConfig([self._load_point(p) for p in raw.get("retreat", {}).get("points", [])])
        else:
            cfg.approach = BranchConfig([self._legacy_power_point(p) for p in raw.get("approach_curve", [])])
            cfg.retreat = BranchConfig([self._legacy_power_point(p) for p in raw.get("retreat_curve", [])])
        return cfg

    def _load_point(self, raw: dict) -> ControlPoint:
        return ControlPoint(
            raw.get("id", new_id("cp")),
            float(raw.get("distance_cm", 0)),
            raw.get("action_type", raw.get("type", "power")),
            float(raw.get("value", raw.get("power", 0))),
            float(raw.get("timeout_s", 0)),
        )

    def _legacy_power_point(self, raw: dict) -> ControlPoint:
        return ControlPoint(new_id("cp"), float(raw.get("distance_cm", 0)), "power", float(raw.get("power", 0)), 0.0)
