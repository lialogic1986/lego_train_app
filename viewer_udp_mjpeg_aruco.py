import argparse
import socket
import struct
import time

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
    # Немного “дружелюбнее” к экрану телефона / компрессии
    params.cornerRefinementMethod = cv2.aruco.CORNER_REFINE_SUBPIX
    params.adaptiveThreshWinSizeMin = 3
    params.adaptiveThreshWinSizeMax = 53
    params.adaptiveThreshWinSizeStep = 4
    params.minMarkerPerimeterRate = 0.02
    params.maxMarkerPerimeterRate = 4.0

    return cv2.aruco.ArucoDetector(aruco_dict, params)


def put_text(img, text, x, y, scale=0.8):
    cv2.putText(img, text, (x, y),
                cv2.FONT_HERSHEY_SIMPLEX, scale,
                (255, 255, 255), 2, cv2.LINE_AA)


def main():
    ap = argparse.ArgumentParser(description="ESP32-CAM UDP MJPEG viewer + ArUco (MJ framing)")
    ap.add_argument("--listen-ip", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=5000)
    ap.add_argument("--source-ip", default="", help="Filter by source IP (empty = no filter)")
    ap.add_argument("--dict", default="4X4_50",
                    help="Dict: 4X4_50, 5X5_100, APRILTAG_36h11, ...")
    ap.add_argument("--show-ids", action="store_true", help="Print detected IDs")
    ap.add_argument("--det-every", type=int, default=1, help="Detect every N frames (1=every frame)")
    ap.add_argument("--window", default="ESP32-CAM UDP MJPEG + ArUco")
    args = ap.parse_args()

    source_ip_filter = args.source_ip.strip() or None

    if not hasattr(cv2, "aruco"):
        raise RuntimeError("cv2.aruco not found. Install: python -m pip install opencv-contrib-python")

    detector = make_aruco_detector(args.dict)

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind((args.listen_ip, args.port))
    sock.settimeout(0.2)

    asm = FrameAssembler()

    cv2.namedWindow(args.window, cv2.WINDOW_NORMAL)

    frames = 0
    fps_print = 0
    last_stat = time.time()

    frame_idx = 0

    # Для удобства: если детект реже, будем рисовать последние найденные маркеры
    last_ids = None
    last_corners = None
    last_rej_n = 0

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

                    # Детектим по серому
                    if args.det_every <= 1 or (frame_idx % args.det_every) == 0:
                        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
                        corners, ids, rejected = detector.detectMarkers(gray)

                        last_corners = corners
                        last_ids = ids
                        last_rej_n = 0 if rejected is None else len(rejected)

                        if args.show_ids and ids is not None and len(ids) > 0:
                            print("IDs:", [int(x) for x in ids.flatten().tolist()])

                    # Рисуем последние найденные
                    if last_ids is not None and len(last_ids) > 0 and last_corners is not None:
                        cv2.aruco.drawDetectedMarkers(img, last_corners, last_ids)

                    # FPS как у тебя
                    frames += 1
                    now = time.time()
                    if now - last_stat >= 1.0:
                        fps_print = frames
                        frames = 0
                        last_stat = now

                    det_n = 0 if last_ids is None else int(len(last_ids))
                    put_text(img, f"FPS: {fps_print}", 10, 30, 1.0)
                    put_text(img, f"Detected: {det_n}  Rejected: {last_rej_n}", 10, 65, 0.8)
                    put_text(img, f"Dict: {args.dict}", 10, 95, 0.7)

                    cv2.imshow(args.window, img)

        if (cv2.waitKey(1) & 0xFF) == 27:  # ESC
            break

    cv2.destroyAllWindows()


if __name__ == "__main__":
    main()

