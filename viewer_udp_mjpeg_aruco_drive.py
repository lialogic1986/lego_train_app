import argparse
import socket
import struct
import time
from collections import deque, Counter

import numpy as np
import cv2


HDR_FMT = "!2sHHHHH"
HDR_SIZE = struct.calcsize(HDR_FMT)

FRAME_TIMEOUT_S = 1.2


class FrameAssembler:
    def __init__(self):
        self.fid = None
        self.total = 0
        self.parts = {}
        self.last_ts = 0.0

    def reset(self):
        self.fid = None
        self.total = 0
        self.parts = {}
        self.last_ts = 0.0

    def add(self, fid, cid, total, payload):
        now = time.time()
        if self.fid is not None and (now - self.last_ts) > FRAME_TIMEOUT_S:
            self.reset()

        if self.fid != fid or self.total != total:
            self.fid = fid
            self.total = total
            self.parts = {}

        self.parts[cid] = payload
        self.last_ts = now

        if len(self.parts) == self.total:
            try:
                jpeg = b"".join(self.parts[i] for i in range(self.total))
            except KeyError:
                self.reset()
                return None
            self.reset()
            return jpeg
        return None


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
    # Устойчивее к компрессии/экрану/перспективе
    params.cornerRefinementMethod = cv2.aruco.CORNER_REFINE_SUBPIX
    params.adaptiveThreshWinSizeMin = 3
    params.adaptiveThreshWinSizeMax = 53
    params.adaptiveThreshWinSizeStep = 4
    params.minMarkerPerimeterRate = 0.02
    params.maxMarkerPerimeterRate = 4.0

    return cv2.aruco.ArucoDetector(aruco_dict, params)


def put_text(img, text, x, y, scale=0.75):
    cv2.putText(img, text, (x, y),
                cv2.FONT_HERSHEY_SIMPLEX, scale,
                (255, 255, 255), 2, cv2.LINE_AA)


def clamp01(x: float) -> float:
    return 0.0 if x < 0 else (1.0 if x > 1.0 else x)


def roi_from_fractions(w, h, x0f, y0f, x1f, y1f):
    x0 = int(w * clamp01(x0f))
    y0 = int(h * clamp01(y0f))
    x1 = int(w * clamp01(x1f))
    y1 = int(h * clamp01(y1f))
    x1 = max(x1, x0 + 1)
    y1 = max(y1, y0 + 1)
    return x0, y0, x1, y1


def profile_for_speed(speed: float):
    # Под вертикальный знак впереди:
    # ROI по центру, чуть выше середины, чтобы ловить раньше и меньше мусора по краям.
    if speed <= 0.2:   # 0.15
        return dict(
            HIST_N=5, HIT_K=3, COOLDOWN_S=1.0,
            ROI=(0.20, 0.15, 0.80, 0.90),  # центр, почти весь по высоте
            DET_EVERY=1
        )
    elif speed <= 0.4:  # 0.3
        return dict(
            HIST_N=3, HIT_K=2, COOLDOWN_S=0.7,
            ROI=(0.18, 0.12, 0.82, 0.88),
            DET_EVERY=1
        )
    else:              # 0.5
        return dict(
            HIST_N=3, HIT_K=2, COOLDOWN_S=0.5,
            ROI=(0.15, 0.10, 0.85, 0.85),
            DET_EVERY=1
        )


def main():
    ap = argparse.ArgumentParser(description="UDP MJPEG (MJ framing) + ArUco detector tuned for moving train and vertical signs")
    ap.add_argument("--listen-ip", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=5000)
    ap.add_argument("--source-ip", default="", help="Filter by source IP (empty = no filter)")
    ap.add_argument("--dict", default="4X4_50")
    ap.add_argument("--speed", type=float, default=0.3, help="Train speed m/s: 0.15 / 0.3 / 0.5")
    ap.add_argument("--show-ids", action="store_true", help="Print detected IDs")
    ap.add_argument("--window", default="Train Front Camera + ArUco")
    ap.add_argument("--roi", default="", help="Override ROI as x0,y0,x1,y1 fractions (e.g. 0.2,0.1,0.8,0.9)")
    args = ap.parse_args()

    if not hasattr(cv2, "aruco"):
        raise RuntimeError("cv2.aruco not found. Install: python -m pip install opencv-contrib-python")

    detector = make_aruco_detector(args.dict)

    prof = profile_for_speed(args.speed)
    HIST_N = prof["HIST_N"]
    HIT_K = prof["HIT_K"]
    COOLDOWN_S = prof["COOLDOWN_S"]
    DET_EVERY = prof["DET_EVERY"]
    roi_frac = prof["ROI"]

    if args.roi.strip():
        parts = [float(x.strip()) for x in args.roi.split(",")]
        if len(parts) != 4:
            raise ValueError("--roi must be 4 numbers: x0,y0,x1,y1")
        roi_frac = tuple(parts)

    source_ip_filter = args.source_ip.strip() or None

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind((args.listen_ip, args.port))
    sock.settimeout(0.2)

    asm = FrameAssembler()
    cv2.namedWindow(args.window, cv2.WINDOW_NORMAL)

    # stats
    frames = 0
    fps_print = 0
    last_stat = time.time()

    # debounce
    id_hist = deque(maxlen=HIST_N)
    last_fire = {}  # id -> time

    # draw last detections (if DET_EVERY > 1 later)
    last_ids = None
    last_corners = None
    last_rej_n = 0
    last_det_ms = 0.0

    frame_idx = 0

    while True:
        try:
            pkt, (src_ip, src_port) = sock.recvfrom(65535)
        except socket.timeout:
            pkt = None

        if pkt is not None:
            if source_ip_filter and src_ip != source_ip_filter:
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

                    frame_idx += 1
                    h, w = img.shape[:2]
                    x0, y0, x1, y1 = roi_from_fractions(w, h, *roi_frac)

                    roi = img[y0:y1, x0:x1]
                    gray = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY)

                    # детект
                    if DET_EVERY <= 1 or (frame_idx % DET_EVERY) == 0:
                        t0 = time.time()
                        corners, ids, rejected = detector.detectMarkers(gray)
                        last_det_ms = (time.time() - t0) * 1000.0

                        # shift corners back to full frame coords
                        if corners is not None and len(corners) > 0:
                            for c in corners:
                                c[:, :, 0] += x0
                                c[:, :, 1] += y0

                        last_corners = corners
                        last_ids = ids
                        last_rej_n = 0 if rejected is None else len(rejected)

                        cur_ids = []
                        if ids is not None and len(ids) > 0:
                            cur_ids = [int(v) for v in ids.flatten().tolist()]
                            if args.show_ids:
                                print("IDs:", cur_ids)
                        id_hist.append(cur_ids)

                        # debounce + cooldown
                        now = time.time()
                        flat = [i for frame_ids in id_hist for i in frame_ids]
                        cnt = Counter(flat)
                        stable = [mid for mid, c in cnt.items() if c >= HIT_K]

                        for mid in sorted(stable):
                            t_last = last_fire.get(mid, 0.0)
                            if (now - t_last) >= COOLDOWN_S:
                                last_fire[mid] = now
                                print(f"TRIGGER: ID={mid}  seen={cnt[mid]}/{HIST_N}  speed={args.speed} m/s")

                    # draw ROI rectangle
                    cv2.rectangle(img, (x0, y0), (x1 - 1, y1 - 1), (255, 255, 255), 1)

                    # draw detections
                    if last_ids is not None and len(last_ids) > 0 and last_corners is not None:
                        cv2.aruco.drawDetectedMarkers(img, last_corners, last_ids)

                    # FPS
                    frames += 1
                    now = time.time()
                    if now - last_stat >= 1.0:
                        fps_print = frames
                        frames = 0
                        last_stat = now

                    det_n = 0 if last_ids is None else int(len(last_ids))
                    put_text(img, f"FPS: {fps_print}", 10, 30, 1.0)
                    put_text(img, f"Detected: {det_n}  Rejected: {last_rej_n}  det: {last_det_ms:.1f} ms", 10, 65, 0.75)
                    put_text(img, f"Dict: {args.dict}  speed: {args.speed} m/s  debounce: {HIT_K}/{HIST_N}  cd: {COOLDOWN_S}s", 10, 95, 0.65)

                    cv2.imshow(args.window, img)

        if (cv2.waitKey(1) & 0xFF) == 27:
            break

    cv2.destroyAllWindows()


if __name__ == "__main__":
    main()

