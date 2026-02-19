import socket
import struct
import time

import numpy as np
import cv2

LISTEN_IP = "0.0.0.0"
PORT = 5000

# ESP32-CAM IP (по tcpdump это 192.168.0.102)
SOURCE_IP_FILTER=None   # или None, если не нужно фильтровать

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

def main():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind((LISTEN_IP, PORT))
    sock.settimeout(0.2)

    asm = FrameAssembler()

    cv2.namedWindow("ESP32-CAM UDP MJPEG", cv2.WINDOW_NORMAL)

    frames = 0
    last_stat = time.time()

    while True:
        try:
            pkt, (src_ip, src_port) = sock.recvfrom(65535)
        except socket.timeout:
            pass
        else:
            if SOURCE_IP_FILTER and src_ip != SOURCE_IP_FILTER:
                continue
            if len(pkt) >= HDR_SIZE:
                magic, fid, cid, total, plen, _ = struct.unpack(HDR_FMT, pkt[:HDR_SIZE])
                if magic == b"MJ" and plen <= (len(pkt) - HDR_SIZE):
                    payload = pkt[HDR_SIZE:HDR_SIZE+plen]
                    jpeg = asm.add(fid, cid, total, payload)
                    if jpeg:
                        img = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR)
                        if img is not None:
                            cv2.imshow("ESP32-CAM UDP MJPEG", img)
                            frames += 1

        if (cv2.waitKey(1) & 0xFF) == 27:
            break

        now = time.time()
        if now - last_stat >= 1.0:
            print(f"FPS: {frames}")
            frames = 0
            last_stat = now

    cv2.destroyAllWindows()

if __name__ == "__main__":
    main()

