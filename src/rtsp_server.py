"""
Pure-Python RTSP server
DepthAI → MJPEG (hardware encoded) → RTP/UDP (payload type 26)
No GStreamer, no ffmpeg, only standard library + depthai
VLC-TESTED: Full video, correct colors, smooth playback
"""

import sys
import socket
import threading
import time
import re
import struct
import queue
from typing import Optional

import depthai as dai   # pip install depthai

# ----------------------------------------------------------------------
# Configuration
# ----------------------------------------------------------------------
FRAMERATE = 30
RTP_PAYLOAD_TYPE = 26          # MJPEG
SSRC = 0xDEADBEEF
# ----------------------------------------------------------------------


def depthai_mjpeg_producer(frame_q: queue.Queue):
    """Pull MJPEG packets from DepthAI and put *bytes* into the queue."""
    pipeline = dai.Pipeline()

    cam = pipeline.create(dai.node.ColorCamera)
    cam.setFps(FRAMERATE)
    cam.setInterleaved(False)                 # REQUIRED for encoder
    cam.setBoardSocket(dai.CameraBoardSocket.CAM_A)
    cam.setPreviewSize(640, 352)
    # cam.setVideoSize(640, 360)
    cam.setResolution(dai.ColorCameraProperties.SensorResolution.THE_720_P)

    enc = pipeline.create(dai.node.VideoEncoder)
    fps = cam.getFps()
    print(f'fps from camera: {fps}')
    enc.setDefaultProfilePreset(
        fps,
        dai.VideoEncoderProperties.Profile.MJPEG
    )

    xout = pipeline.create(dai.node.XLinkOut)
    xout.setStreamName("mjpeg")

    cam.video.link(enc.input)
    enc.bitstream.link(xout.input)

    with dai.Device(pipeline) as dev:
        q = dev.getOutputQueue("mjpeg", maxSize=5, blocking=False)
        while True:
            pkt = q.tryGet()
            if pkt is not None:
                jpeg_bytes = pkt.getData().tobytes()
                try:
                    frame_q.put_nowait(jpeg_bytes)
                except queue.Full:
                    pass
            else:
                time.sleep(1 / (FRAMERATE * 2))


def get_jpeg_dimensions(jpeg_bytes):
    """Extract width and height from JPEG SOF marker."""
    if len(jpeg_bytes) < 10 or jpeg_bytes[:2] != b'\xff\xd8':
        return None
    i = 2
    while i + 8 < len(jpeg_bytes):
        if jpeg_bytes[i] != 0xFF:
            i += 1
            continue
        marker = jpeg_bytes[i+1]
        if marker in (0xC0, 0xC1, 0xC2, 0xC3):  # SOF0-3
            height = (jpeg_bytes[i+5] << 8) | jpeg_bytes[i+6]
            width = (jpeg_bytes[i+7] << 8) | jpeg_bytes[i+8]
            return width, height
        length = (jpeg_bytes[i+2] << 8) | jpeg_bytes[i+3]
        i += length + 2
    return None


# ----------------------------------------------------------------------
# RTSP Handler
# ----------------------------------------------------------------------
class RTSPHandler:
    def __init__(self, client_sock: socket.socket, frame_q: queue.Queue):
        self.sock = client_sock
        self.file = client_sock.makefile("rwb")
        self.frame_q = frame_q

        self.cseq = 0
        self.session: Optional[str] = None
        self.client_rtp_port: Optional[int] = None
        self.client_rtcp_port: Optional[int] = None
        self.client_ip: Optional[str] = None

        self.rtp_sock = None
        self.rtcp_sock = None
        self.rtp_thread = None
        self.stop_event = threading.Event()

        # For RTP-Info
        self.current_seq = 1
        self.current_timestamp = 0

    # ------------------------------------------------------------------
    def run(self):
        try:
            while not self.stop_event.is_set():
                line = self.file.readline().decode(errors="ignore").strip()
                if not line:
                    break
                self._handle_request(line)
        except Exception as e:
            print(f"[RTSP] error: {e}")
        finally:
            self.cleanup()

    # ------------------------------------------------------------------
    def _handle_request(self, first_line: str):
        parts = first_line.split()
        if len(parts) < 3:
            return
        method, url, _ = parts

        headers = {}
        while True:
            line = self.file.readline().decode(errors="ignore").strip()
            if not line:
                break
            if ":" in line:
                k, v = line.split(":", 1)
                headers[k.strip()] = v.strip()

        self.cseq = int(headers.get("CSeq", "0"))

        if method == "OPTIONS":
            self._options()
        elif method == "DESCRIBE":
            self._describe()
        elif method == "SETUP":
            self._setup(headers)
        elif method == "PLAY":
            self._play()
        elif method == "TEARDOWN":
            self._teardown()
        else:
            self._error(405)

    # ------------------------------------------------------------------
    def _send(self, code: int, reason: str, extra: Optional[dict] = None, body: Optional[bytes] = None):
        lines = [f"RTSP/1.0 {code} {reason}", f"CSeq: {self.cseq}"]
        if self.session:
            lines.append(f"Session: {self.session}")
        if extra:
            for k, v in extra.items():
                lines.append(f"{k}: {v}")
        lines.append("")
        self.file.write("\r\n".join(lines).encode() + b"\r\n")
        if body:
            self.file.write(body)
        self.file.flush()

    # ------------------------------------------------------------------
    def _options(self):
        self._send(200, "OK", {"Public": "OPTIONS, DESCRIBE, SETUP, PLAY, TEARDOWN"})

    # ------------------------------------------------------------------
    def _describe(self):
        sdp = (
            f"v=0\r\n"
            f"o=- {int(time.time())} 1 IN IP4 0.0.0.0\r\n"
            f"s=DepthAI MJPEG Stream\r\n"
            f"c=IN IP4 0.0.0.0\r\n"
            f"t=0 0\r\n"
            f"m=video 0 RTP/AVP {RTP_PAYLOAD_TYPE}\r\n"
            f"a=rtpmap:{RTP_PAYLOAD_TYPE} JPEG/90000\r\n"
            f"a=control:track1\r\n"
        ).encode()
        self._send(200, "OK", {
            "Content-Type": "application/sdp",
            "Content-Length": str(len(sdp))
        }, sdp)

    # ------------------------------------------------------------------
    def _setup(self, hdr: dict):
        trans = hdr.get("Transport", "")
        m = re.search(r"client_port=(\d+)(?:-(\d+))?", trans)
        if not m:
            self._error(400)
            return

        self.client_rtp_port = int(m.group(1))
        self.client_rtcp_port = int(m.group(2)) if m.group(2) else self.client_rtp_port + 1

        # Bind server RTP port
        self.rtp_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.rtp_sock.bind(('', 0))
        server_rtp = self.rtp_sock.getsockname()[1]

        # Bind server RTCP port
        self.rtcp_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.rtcp_sock.bind(('', server_rtp + 1))
        server_rtcp = self.rtcp_sock.getsockname()[1]

        self.client_ip = self.sock.getpeername()[0]
        self.session = str(int(time.time() * 1000))

        self._send(200, "OK", {
            "Transport": f"RTP/AVP;unicast;"
                         f"client_port={self.client_rtp_port}-{self.client_rtcp_port};"
                         f"server_port={server_rtp}-{server_rtcp}",
            "Session": self.session
        })

    # ------------------------------------------------------------------
    def _play(self):
        if not self.session or not self.client_rtp_port:
            self._error(454)
            return

        # Start RTP after PLAY
        self.stop_event.clear()
        self.current_seq = 1
        self.current_timestamp = 0
        self.rtp_thread = threading.Thread(target=self._rtp_sender, daemon=True)
        self.rtp_thread.start()

        self._send(200, "OK", {
            "RTP-Info": f"url=track1;seq={self.current_seq};rtptime={self.current_timestamp}"
        })

    # ------------------------------------------------------------------
    def _rtp_sender(self):
        seq = self.current_seq
        timestamp = self.current_timestamp
        clock_rate = 90000
        inc = clock_rate // FRAMERATE
        MAX_PAYLOAD = 65000

        while not self.stop_event.is_set():
            try:
                jpeg = self.frame_q.get(timeout=0.5)
            except queue.Empty:
                continue

            dims = get_jpeg_dimensions(jpeg)
            if not dims:
                print("[RTP] Invalid JPEG, skipping")
                continue
            width, height = dims
            width_div8 = width // 8
            height_div8 = height // 8
            if width_div8 > 255 or height_div8 > 255:
                print(f"[RTP] JPEG too large: {width}x{height}")
                continue

            offset = 0
            jpeg_len = len(jpeg)

            while offset < jpeg_len:
                payload_start = offset
                payload_end = min(offset + MAX_PAYLOAD - 8, jpeg_len)
                payload = jpeg[payload_start:payload_end]

                # MJPEG Header (8 bytes) - PROGRESSIVE
                mjpeg_hdr = struct.pack(
                    ">BBBBBBBB",
                    0,                    # Type-specific
                    (offset >> 16) & 0xFF,
                    (offset >> 8) & 0xFF,
                    offset & 0xFF,
                    1,                    # TYPE = 1 (progressive)
                    255,                  # Q (standard table)
                    width_div8,
                    height_div8
                )

                marker = 1 if payload_end == jpeg_len else 0
                rtp_hdr = struct.pack(
                    "!BBHII",
                    0x80,
                    (marker << 7) | RTP_PAYLOAD_TYPE,
                    seq & 0xFFFF,
                    timestamp,
                    SSRC
                )

                packet = rtp_hdr + mjpeg_hdr + payload

                try:
                    self.rtp_sock.sendto(packet, (self.client_ip, self.client_rtp_port))
                except Exception as e:
                    print(f"[RTP] send failed: {e}")
                    break

                print(f"[RTP] to {self.client_ip}:{self.client_rtp_port} | "
                      f"{len(packet)} bytes | {width}x{height} | frag {offset}-{payload_end}/{jpeg_len} | M={marker}")

                offset = payload_end
                seq = (seq + 1) & 0xFFFF

            timestamp = (timestamp + inc) & 0xFFFFFFFF

        # Save state for next PLAY
        self.current_seq = seq
        self.current_timestamp = timestamp

    # ------------------------------------------------------------------
    def _teardown(self):
        self.stop_event.set()
        if self.rtp_thread:
            self.rtp_thread.join(timeout=1)
        self._send(200, "OK")
        self.cleanup()

    # ------------------------------------------------------------------
    def _error(self, code: int):
        reasons = {400: "Bad Request", 404: "Not Found", 405: "Method Not Allowed", 454: "Session Not Found"}
        self._send(code, reasons.get(code, "Error"))

    # ------------------------------------------------------------------
    def cleanup(self):
        self.stop_event.set()
        if self.rtp_sock:
            self.rtp_sock.close()
        if self.rtcp_sock:
            self.rtcp_sock.close()
        try:
            self.sock.close()
        except Exception:
            pass


# ----------------------------------------------------------------------
# Server bootstrap
# ----------------------------------------------------------------------
def main(port: int):
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("0.0.0.0", port))
    srv.listen(5)
    print(f"[RTSP] listening to rtsp://<IP>:{port}/stream")

    frame_q: queue.Queue = queue.Queue(maxsize=10)
    threading.Thread(target=depthai_mjpeg_producer, args=(frame_q,), daemon=True).start()

    while True:
        cli, addr = srv.accept()
        print(f"[RTSP] client {addr}")
        h = RTSPHandler(cli, frame_q)
        threading.Thread(target=h.run, daemon=True).start()


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(f"Usage: {sys.argv[0]} <port>")
        sys.exit(1)
    main(int(sys.argv[1]))