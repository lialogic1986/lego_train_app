#!/usr/bin/env python3
import asyncio
import json
import os
import time
from dataclasses import dataclass
from typing import Optional

from event_client import EventClient


PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
MAP_CONFIG_PATH = os.path.join(PROJECT_ROOT, "railway_map.json")
MAP_RELOAD_INTERVAL_S = 1.0
POWER_MIN = -100
POWER_MAX = 100
POWER_EMIT_MIN_INTERVAL_S = 0.15


def clamp_power(power: int) -> int:
    return max(POWER_MIN, min(POWER_MAX, int(power)))


@dataclass
class RuntimeMarker:
    marker_id: int
    actions: dict


class RailwayMapRuntime:
    def __init__(self):
        self.bus = EventClient("railway_map_runtime")
        self.markers: dict[int, RuntimeMarker] = {}
        self.camera_to_train: dict[str, str] = {}
        self.last_dist_cm: dict[tuple[str, int], float] = {}
        self.last_power: dict[str, int] = {}
        self.direction: dict[str, int] = {}
        self.last_power_emit_s: dict[str, float] = {}
        self.last_cm_bucket: dict[tuple[str, int, str], int] = {}
        self.triggered_points: dict[tuple[str, int, str], set[str]] = {}
        self.flow_branch: dict[tuple[str, int], str] = {}
        self.start_tasks: dict[tuple[str, int], asyncio.Task] = {}
        self.stop_tasks: dict[tuple[str, int], asyncio.Task] = {}
        self.map_mtime = 0.0

    def log(self, msg: str):
        print(f"[railway_map_runtime] {msg}", flush=True)

    async def start(self):
        await self.bus.connect()
        await self.reload_map(force=True)
        asyncio.create_task(self.reload_loop())
        await self.event_loop()

    async def reload_loop(self):
        while True:
            await asyncio.sleep(MAP_RELOAD_INTERVAL_S)
            await self.reload_map()

    async def reload_map(self, force: bool = False):
        try:
            mtime = os.path.getmtime(MAP_CONFIG_PATH)
        except OSError:
            if force:
                self.log("railway_map.json not found; automation idle")
            return

        if not force and mtime == self.map_mtime:
            return

        try:
            with open(MAP_CONFIG_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:
            self.log(f"map load failed: {e}")
            return

        markers = {}
        for raw in data.get("markers", []):
            try:
                marker_id = int(raw["marker_id"])
                markers[marker_id] = RuntimeMarker(marker_id, raw.get("actions", {}))
            except Exception:
                continue

        self.markers = markers
        self.map_mtime = mtime
        self.log(f"loaded {len(self.markers)} markers from railway_map.json")

    async def event_loop(self):
        while True:
            evt = await self.bus.next_event()
            etype = evt.get("type")
            data = evt.get("data", {})

            if etype == "train_bound":
                camera_id = data.get("camera_id", "")
                train_id = data.get("train_id", "")
                if camera_id and train_id:
                    self.camera_to_train[camera_id] = train_id
                continue

            if etype == "marker_seen":
                await self.handle_marker_seen(data)

    async def handle_marker_seen(self, data: dict):
        try:
            marker_id = int(data.get("marker_id"))
            distance_m = float(data.get("distance_m"))
        except (TypeError, ValueError):
            return

        marker = self.markers.get(marker_id)
        if not marker:
            return

        camera_id = data.get("camera_id", "")
        train_id = data.get("train_id") or self.camera_to_train.get(camera_id, "")
        if not train_id:
            return

        distance_cm = distance_m * 100.0
        key = (train_id, marker_id)
        prev = self.last_dist_cm.get(key)
        self.last_dist_cm[key] = distance_cm

        auto_branch = "approach" if prev is None or distance_cm <= prev else "retreat"
        branch = self.flow_branch.get(key, auto_branch)
        points = self.branch_points(marker.actions, branch)
        interpolate = bool(marker.actions.get("interpolate_power", True))

        if interpolate:
            await self.handle_interpolated_power(train_id, marker_id, branch, points, distance_cm)
            await self.handle_crossed_points(train_id, marker_id, branch, points, prev, distance_cm, apply_power=False)
        else:
            await self.handle_crossed_points(train_id, marker_id, branch, points, prev, distance_cm, apply_power=True)

    def branch_points(self, actions: dict, branch: str) -> list[dict]:
        if branch in actions:
            return list(actions.get(branch, {}).get("points", []))
        legacy_key = "approach_curve" if branch == "approach" else "retreat_curve"
        out = []
        for raw in actions.get(legacy_key, []) or []:
            out.append({
                "id": f"legacy_{branch}_{raw.get('distance_cm')}",
                "distance_cm": raw.get("distance_cm", 0),
                "action_type": "power",
                "value": raw.get("power", 0),
                "timeout_s": 0,
            })
        return out

    async def handle_interpolated_power(self, train_id: str, marker_id: int, branch: str, points: list[dict], distance_cm: float):
        bucket = int(round(distance_cm))
        key = (train_id, marker_id, branch)
        if self.last_cm_bucket.get(key) == bucket:
            return
        self.last_cm_bucket[key] = bucket

        power = self.power_for_distance(bucket, points, True)
        if power is not None:
            await self.emit_power(train_id, power, force=True)

    async def handle_crossed_points(
        self,
        train_id: str,
        marker_id: int,
        branch: str,
        points: list[dict],
        prev_cm: Optional[float],
        distance_cm: float,
        apply_power: bool,
    ):
        if prev_cm is None:
            return
        for point in points:
            if self.point_type(point) == "power" and not apply_power:
                continue
            if not self.crossed(branch, prev_cm, distance_cm, self.point_distance(point)):
                continue
            await self.apply_point(train_id, marker_id, branch, point)

    def crossed(self, branch: str, prev_cm: float, distance_cm: float, target_cm: float) -> bool:
        if branch == "approach":
            return prev_cm > target_cm >= distance_cm
        return prev_cm < target_cm <= distance_cm

    def power_for_distance(self, distance_cm: float, points_raw: list, interpolate: bool) -> Optional[int]:
        points = []
        for raw in points_raw or []:
            power = self.point_power_value(raw)
            if power is None:
                continue
            points.append((self.point_distance(raw), power))
        if not points:
            return None

        points.sort(key=lambda p: p[0], reverse=True)
        if len(points) == 1 or not interpolate:
            nearest = min(points, key=lambda p: abs(p[0] - distance_cm))
            return clamp_power(nearest[1])

        if distance_cm >= points[0][0]:
            return clamp_power(points[0][1])
        if distance_cm <= points[-1][0]:
            return clamp_power(points[-1][1])

        for left, right in zip(points, points[1:]):
            d1, p1 = left
            d2, p2 = right
            if d1 >= distance_cm >= d2:
                span = max(0.001, d1 - d2)
                t = (d1 - distance_cm) / span
                return clamp_power(round(p1 + (p2 - p1) * t))

        return None

    def point_power_value(self, point: dict) -> Optional[int]:
        action_type = self.point_type(point)
        if action_type == "speed":
            return None
        if action_type == "stop":
            return 0

        try:
            value = int(float(point.get("value", point.get("power", 0))))
        except Exception:
            value = 0

        if action_type in ("power", "reverse", "start"):
            return clamp_power(value)
        return None

    async def apply_point(self, train_id: str, marker_id: int, branch: str, point: dict):
        point_id = str(point.get("id") or f"{branch}_{self.point_distance(point)}_{self.point_type(point)}")
        triggered_key = (train_id, marker_id, branch)
        triggered = self.triggered_points.setdefault(triggered_key, set())
        if point_id in triggered:
            return
        triggered.add(point_id)

        action_type = self.point_type(point)
        if action_type == "power":
            await self.emit_power(train_id, int(float(point.get("value", 0))))
        elif action_type == "reverse":
            cur_dir = self.direction.get(train_id, 1)
            new_dir = -cur_dir
            self.direction[train_id] = new_dir
            magnitude = abs(int(float(point.get("value") or self.last_power.get(train_id, 40) or 40)))
            await self.emit_power(train_id, new_dir * magnitude)
            self.switch_flow_branch(train_id, marker_id, branch)
        elif action_type == "stop":
            timeout_s = max(0.0, float(point.get("timeout_s", 0.0)))
            asyncio.create_task(self.delayed_stop(train_id, timeout_s))
        elif action_type == "start":
            timeout_s = max(0.0, float(point.get("timeout_s", 0.0)))
            power = clamp_power(int(float(point.get("value", 40))))
            asyncio.create_task(self.delayed_start(train_id, timeout_s, power))
        elif action_type == "speed":
            self.log(f"speed point ignored for now train_id={train_id} value={point.get('value')}")

    def switch_flow_branch(self, train_id: str, marker_id: int, branch: str):
        next_branch = "retreat" if branch == "approach" else "approach"
        flow_key = (train_id, marker_id)
        self.flow_branch[flow_key] = next_branch
        self.triggered_points.pop((train_id, marker_id, next_branch), None)
        self.last_cm_bucket.pop((train_id, marker_id, next_branch), None)
        self.log(f"reverse switched flow train_id={train_id} marker={marker_id} {branch}->{next_branch}")

    def point_distance(self, point: dict) -> float:
        return float(point.get("distance_cm", 0.0))

    def point_type(self, point: dict) -> str:
        return str(point.get("action_type", point.get("type", "power")))

    async def emit_power(self, train_id: str, power: int, force: bool = False):
        now = time.monotonic()
        if self.last_power.get(train_id) == power:
            return
        if not force and now - self.last_power_emit_s.get(train_id, 0.0) < POWER_EMIT_MIN_INTERVAL_S:
            return

        self.last_power[train_id] = power
        if power > 0:
            self.direction[train_id] = 1
        elif power < 0:
            self.direction[train_id] = -1
        self.last_power_emit_s[train_id] = now
        await self.bus.emit("train_power", {"train_id": train_id, "power": power, "source": "railway_map"})

    async def delayed_stop(self, train_id: str, timeout_s: float):
        await asyncio.sleep(timeout_s)
        await self.bus.emit("train_stop", {"train_id": train_id, "source": "railway_map"})
        self.last_power[train_id] = 0

    async def delayed_start(self, train_id: str, timeout_s: float, power: int):
        await asyncio.sleep(timeout_s)
        await self.emit_power(train_id, power)


async def main():
    runtime = RailwayMapRuntime()
    await runtime.start()


if __name__ == "__main__":
    asyncio.run(main())
