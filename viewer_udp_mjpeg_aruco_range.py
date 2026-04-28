import argparse
import socket
import struct
import time
from dataclasses import dataclass

import numpy as np
import cv2

from host_config import load_config, save_config, get_path, set_path


HDR_FMT = "!2sHHHHH"
HDR_SIZE = struct.calcsize(HDR_FMT)
FRAME_ID_MOD = 1 << 16
DEFAULT_FRAME_TIMEOUT_S = 0.10


DEFAULTS = {
    "version": 1,
    "net": {"listen_ip": "0.0.0.0", "port": 5000, "source_ip": ""},
    "aruco": {"dict": "4X4_50", "marker_size_m": 0.04},
    "range": {
        "k_area": 0.0,
        "calib_dist_m": 0.5,
        "ema_alpha": 0.35,
        "cooldown_s": 0.6,
        "th_approach_m": 0.6,
        "th_brake_m": 0.35,
        "th_stop_m": 0.2
    }
}



def now_s() -> float:
    return time.monotonic()



def is_newer_frame_id(new_fid: int, cur_fid: int) -> bool:
    """Compare 16-bit frame IDs with wraparound awareness."""
    diff = (new_fid - cur_fid) & (FRAME_ID_MOD - 1)
    return 0 < diff < (FRAME_ID_MOD // 2)


@dataclass
class AssemblerStats:
    completed: int = 0
    dropped_timeout: int = 0
    dropped_replaced: int = 0
    dropped_old: int = 0
    dropped_bad_hdr: int = 0
    dropped_incomplete_join: int = 0


class FrameAssembler:
    """
    Low-latency assembler for a single in-flight MJPEG frame.

    Policy:
    - keep only one currently assembling frame;
    - if a newer frame arrives, drop the old incomplete one immediately;
    - ignore older/out-of-order frames;
    - drop incomplete frame if it lives longer than timeout from the first chunk.
    """

    def __init__(self, frame_timeout_s: float = DEFAULT_FRAME_TIMEOUT_S):
        self.frame_timeout_s = frame_timeout_s
        self.stats = AssemblerStats()
        self.reset()

    def reset(self):
        self.fid = None
        self.total = 0
        self.parts = {}
        self.first_ts = 0.0
        self.last_ts = 0.0

    def _drop_current(self, reason: str):
        if self.fid is not None and len(self.parts) < self.total:
            if reason == "timeout":
                self.stats.dropped_timeout += 1
            elif reason == "replaced":
                self.stats.dropped_replaced += 1
        self.reset()

    def _start_new(self, fid: int, total: int, ts: float):
        self.fid = fid
        self.total = total
        self.parts = {}
        self.first_ts = ts
        self.last_ts = ts

    def add(self, fid: int, cid: int, total: int, payload: bytes):
        ts = now_s()

        if total <= 0 or cid >= total:
            self.stats.dropped_bad_hdr += 1
            return None

        if self.fid is not None and (ts - self.first_ts) > self.frame_timeout_s:
            self._drop_current("timeout")

        if self.fid is None:
            self._start_new(fid, total, ts)
        elif fid != self.fid:
            if is_newer_frame_id(fid, self.fid):
                self._drop_current("replaced")
                self._start_new(fid, total, ts)
            else:
                self.stats.dropped_old += 1
                return None
        elif total != self.total:
            # Same frame id but inconsistent total: restart this frame cleanly.
            self.stats.dropped_bad_hdr += 1
            self._drop_current("replaced")
            self._start_new(fid, total, ts)

        self.parts[cid] = payload
        self.last_ts = ts

        if len(self.parts) != self.total:
            return None

        try:
            jpeg = b"".join(self.parts[i] for i in range(self.total))
        except KeyError:
            self.stats.dropped_incomplete_join += 1
            self.reset()
            return None

        self.stats.completed += 1
        self.reset()
        return jpeg



def make_aruco_detector(dict_name: str):
    name_to_dict = {
        "4X4_50": cv2.aruco.DICT_4X4_50,
        "4X4_100": cv2.aruco.DICT_4X4_100,
        "5X5_50": cv2.aruco.DICT_5X5_50,
        "5X5_100": cv2.aruco.DICT_5X5_100,
        "6X6_50": cv2.aruco.DICT_6X6_50,
        "6X6_100": cv2.aruco.DICT_6X6_100,
        "APRILTAG_36h11": cv2.aruco.DICT_APRILTAG_36h11,
        "APRILTAG_25h9": cv2.aruco.DICT_APRILTAG_25h9,
    }
    if dict_name not in name_to_dict:
        raise ValueError(f"Unknown dict '{dict_name}'. Available: {list(name_to_dict.keys())}")

    aruco_dict = cv2.aruco.getPredefinedDictionary(name_to_dict[dict_name])

    params = cv2.aruco.DetectorParameters()
    params.cornerRefinementMethod = cv2.aruco.CORNER_REFINE_SUBPIX
    params.adaptiveThreshWinSizeMin = 3
    params.adaptiveThreshWinSizeMax = 53
    params.adaptiveThreshWinSizeStep = 4
    params.minMarkerPerimeterRate = 0.02
    params.maxMarkerPerimeterRate = 4.0

    return cv2.aruco.ArucoDetector(aruco_dict, params)



def quad_area_px(corners_4x2) -> float:
    c = corners_4x2.reshape(4, 2).astype(np.float32)
    return float(cv2.contourArea(c))



def dist_from_area(area_px: float, k_area: float) -> float:
    if area_px <= 1.0 or not np.isfinite(k_area) or k_area <= 0:
        return float("inf")
    return k_area / (area_px ** 0.5)



def ema(prev: float | None, x: float, alpha: float) -> float:
    if prev is None or not np.isfinite(prev):
        return x
    return (1.0 - alpha) * prev + alpha * x



def put(img, text, x, y, s=0.45):
    cv2.putText(img, text, (x, y),
                cv2.FONT_HERSHEY_SIMPLEX, s,
                (255, 255, 255), 1, cv2.LINE_AA)



def main():
    ap = argparse.ArgumentParser(description="UDP MJPEG (MJ framing) + ArUco + distance via marker area + shared JSON config")
    ap.add_argument("--config", default="host_config.json", help="Shared host config JSON path")

    # если эти аргументы не указать, значения будут взяты из JSON
    ap.add_argument("--listen-ip", default=None)
    ap.add_argument("--port", type=int, default=None)
    ap.add_argument("--source-ip", default=None)
    ap.add_argument("--dict", default=None)

    ap.add_argument("--window", default="ArUco Range (click window, press C)")
    ap.add_argument("--calib-dist", type=float, default=None)
    ap.add_argument("--ema", type=float, default=None)
    ap.add_argument("--cooldown", type=float, default=None)
    ap.add_argument("--frame-timeout-ms", type=float, default=DEFAULT_FRAME_TIMEOUT_S * 1000.0,
                    help="Drop incomplete frame after this many ms from the first chunk")

    ap.add_argument("--th-approach", type=float, default=None)
    ap.add_argument("--th-brake", type=float, default=None)
    ap.add_argument("--th-stop", type=float, default=None)

    args = ap.parse_args()

    if not hasattr(cv2, "aruco"):
        raise RuntimeError("cv2.aruco not found. Install: python -m pip install opencv-contrib-python")

    # load config + apply defaults
    cfg = load_config(args.config, defaults=DEFAULTS)

    # config -> args (CLI overrides config if provided)
    listen_ip = args.listen_ip if args.listen_ip is not None else get_path(cfg, "net.listen_ip", "0.0.0.0")
    port = args.port if args.port is not None else int(get_path(cfg, "net.port", 5000))
    source_ip = args.source_ip if args.source_ip is not None else get_path(cfg, "net.source_ip", "")
    dict_name = args.dict if args.dict is not None else get_path(cfg, "aruco.dict", "4X4_50")

    calib_dist = args.calib_dist if args.calib_dist is not None else float(get_path(cfg, "range.calib_dist_m", 0.5))
    ema_alpha = args.ema if args.ema is not None else float(get_path(cfg, "range.ema_alpha", 0.35))
    cooldown_s = args.cooldown if args.cooldown is not None else float(get_path(cfg, "range.cooldown_s", 0.6))

    th_approach = args.th_approach if args.th_approach is not None else float(get_path(cfg, "range.th_approach_m", 0.6))
    th_brake = args.th_brake if args.th_brake is not None else float(get_path(cfg, "range.th_brake_m", 0.35))
    th_stop = args.th_stop if args.th_stop is not None else float(get_path(cfg, "range.th_stop_m", 0.2))

    detector = make_aruco_detector(dict_name)

    source_ip_filter = (source_ip or "").strip() or None

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind((listen_ip, port))
    sock.settimeout(0.02)

    frame_timeout_s = max(0.01, args.frame_timeout_ms / 1000.0)
    asm = FrameAssembler(frame_timeout_s=frame_timeout_s)

    cv2.namedWindow(args.window, cv2.WINDOW_NORMAL)
    cv2.resizeWindow(args.window, 640, 480)

    # stats
    frames = 0
    fps_print = 0
    last_stat = now_s()

    # load persisted coefficient
    k_area = float(get_path(cfg, "range.k_area", 0.0))
    dist_f = None

    last_event_ts = {"APPROACH": 0.0, "BRAKE": 0.0, "STOP": 0.0}

    last_det_ms = 0.0
    last_rej_n = 0
    last_best_id = None
    last_best_area = 0.0
    last_best_dist = float("inf")

    print(f"[cfg] {args.config}")
    print(f"[net] listen={listen_ip}:{port} source_ip={source_ip_filter}")
    print(f"[aruco] dict={dict_name}")
    print(f"[range] k_area={k_area:.4f} calib_dist={calib_dist:.2f} ema={ema_alpha} cooldown={cooldown_s}")
    print(f"[th] approach<{th_approach:.2f} brake<{th_brake:.2f} stop<{th_stop:.2f}")
    print(f"[video] frame_timeout={frame_timeout_s * 1000.0:.0f} ms (single in-flight frame, drop old on newer frame)")

    while True:
        try:
            pkt, (src_ip, _src_port) = sock.recvfrom(65535)
        except socket.timeout:
            pkt = None

        if pkt is not None and source_ip_filter and src_ip != source_ip_filter:
            pkt = None

        if pkt is not None and len(pkt) >= HDR_SIZE:
            magic, fid, cid, total, plen, _ = struct.unpack(HDR_FMT, pkt[:HDR_SIZE])
            if magic == b"MJ" and plen <= (len(pkt) - HDR_SIZE):
                payload = pkt[HDR_SIZE:HDR_SIZE + plen]
                jpeg = asm.add(fid, cid, total, payload)

                if jpeg:
                    img = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR)
                    if img is None:
                        continue

                    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)

                    t0 = now_s()
                    corners, ids, rejected = detector.detectMarkers(gray)
                    last_det_ms = (now_s() - t0) * 1000.0
                    last_rej_n = 0 if rejected is None else len(rejected)

                    best = None  # (area_px, id, corners4)
                    if ids is not None and len(ids) > 0:
                        for i in range(len(ids)):
                            mid = int(ids[i][0])
                            c4 = corners[i].reshape(4, 2)
                            a = quad_area_px(c4)
                            if best is None or a > best[0]:
                                best = (a, mid, c4)

                        cv2.aruco.drawDetectedMarkers(img, corners, ids)

                    if best is not None:
                        area_px, mid, _c4 = best
                        last_best_id = mid
                        last_best_area = area_px

                        dist_raw = dist_from_area(area_px, k_area)
                        last_best_dist = dist_raw

                        if np.isfinite(dist_raw):
                            dist_f = ema(dist_f, dist_raw, ema_alpha)

                        now = now_s()
                        if dist_f is not None and np.isfinite(dist_f):
                            if dist_f < th_stop and (now - last_event_ts["STOP"]) >= cooldown_s:
                                last_event_ts["STOP"] = now
                                print(f"EVENT STOP: id={mid} dist={dist_f:.2f}m")
                            elif dist_f < th_brake and (now - last_event_ts["BRAKE"]) >= cooldown_s:
                                last_event_ts["BRAKE"] = now
                                print(f"EVENT BRAKE: id={mid} dist={dist_f:.2f}m")
                            elif dist_f < th_approach and (now - last_event_ts["APPROACH"]) >= cooldown_s:
                                last_event_ts["APPROACH"] = now
                                print(f"EVENT APPROACH: id={mid} dist={dist_f:.2f}m")

                    # FPS
                    frames += 1
                    now = now_s()
                    if now - last_stat >= 1.0:
                        fps_print = frames
                        frames = 0
                        last_stat = now

                    # HUD (compact for 320x240)
                    det_n = 0 if ids is None else len(ids)
                    put(img, f"FPS {fps_print} det {det_n} rej {last_rej_n} {last_det_ms:.1f}ms", 5, 16, 0.45)
                    put(img, f"Click window; press C to calib @ {calib_dist:.2f}m", 5, 34, 0.45)
                    put(img, f"k_area {k_area:.3f} dict {dict_name}", 5, 52, 0.45)

                    if last_best_id is None:
                        put(img, "Best: -", 5, 70, 0.50)
                    else:
                        df = "inf" if dist_f is None or not np.isfinite(dist_f) else f"{dist_f:.2f}m"
                        dr = "inf" if not np.isfinite(last_best_dist) else f"{last_best_dist:.2f}m"
                        put(img, f"Best ID {last_best_id} area {last_best_area:.0f}px", 5, 70, 0.50)
                        put(img, f"dist raw {dr} filt {df}", 5, 88, 0.50)

                    s = asm.stats
                    put(img, f"drop t/o {s.dropped_timeout} repl {s.dropped_replaced} old {s.dropped_old}", 5, 106, 0.42)
                    put(img, f"TH <{th_approach:.2f}/{th_brake:.2f}/{th_stop:.2f}m ESC/q exit", 5, 124, 0.42)

                    cv2.imshow(args.window, img)

        key = cv2.waitKey(1) & 0xFF
        if key in (27, ord('q')):
            break

        if key in (ord('c'), ord('C')):
            # press in OpenCV window
            if last_best_id is not None and last_best_area > 10:
                k_area = calib_dist * (last_best_area ** 0.5)
                dist_f = None

                # persist to shared config
                set_path(cfg, "range.k_area", float(k_area))
                save_config(args.config, cfg)

                print(f"CALIB: id={last_best_id} area={last_best_area:.0f}px -> k_area={k_area:.4f} SAVED to {args.config}")
            else:
                print("CALIB: no marker visible")

    cv2.destroyAllWindows()


if __name__ == "__main__":
    main()
