"""
Pure-Python RTSP server
DepthAI → H.264 → RTP/AVP or RTP/AVP/TCP
- Full FU-A fragmentation (RFC 6184)
- Safe for UDP (MTU) and TCP interleaved (!H limit)
- VLC-tested: vlc rtsp://IP:8554/stream [--rtsp-tcp]
"""

import sys
import socket
import threading
import time
import re
import struct
import queue
from typing import Optional

import depthai as dai

# ----------------------------------------------------------------------
# Configuration
# ----------------------------------------------------------------------
FRAMERATE = 30
RTP_PAYLOAD_TYPE = 96
SSRC = 0xDEADBEEF
MAX_UDP_PAYLOAD = 1400      # Safe MTU
MAX_TCP_PAYLOAD = 60000     # < 65535 for !H
# ----------------------------------------------------------------------


def depthai_h264_producer(frame_q: queue.Queue):
    pipeline = dai.Pipeline()

    cam = pipeline.create(dai.node.ColorCamera)
    cam.setFps(FRAMERATE)
    cam.setInterleaved(False)
    cam.setBoardSocket(dai.CameraBoardSocket.CAM_A)
    cam.setPreviewSize(640, 360)

    enc = pipeline.create(dai.node.VideoEncoder)
    enc.setDefaultProfilePreset(FRAMERATE, dai.VideoEncoderProperties.Profile.H264_MAIN)

    xout = pipeline.create(dai.node.XLinkOut)
    xout.setStreamName("h264")

    cam.video.link(enc.input)
    enc.bitstream.link(xout.input)

    with dai.Device(pipeline) as dev:
        q = dev.getOutputQueue("h264", maxSize=30, blocking=False)
        print(f"[H264] Streaming 640x360@{FRAMERATE}fps")
        while True:
            pkt = q.tryGet()
            if pkt is not None:
                try:
                    frame_q.put_nowait(pkt.getData().tobytes())
                except queue.Full:
                    pass
            else:
                time.sleep(1 / (FRAMERATE * 2))


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

        self.use_tcp = False
        self.rtp_channel = 0
        self.rtcp_channel = 1

        self.client_rtp_port: Optional[int] = None
        self.client_ip: Optional[str] = None
        self.rtp_sock: Optional[socket.socket] = None

        self.current_seq = 1
        self.current_timestamp = 0
        self.rtp_thread = None
        self.stop_event = threading.Event()
        self.tcp_write_lock = threading.Lock()

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
        data = "\r\n".join(lines).encode() + b"\r\n"
        with self.tcp_write_lock:
            try:
                self.file.write(data)
                if body:
                    self.file.write(body)
                self.file.flush()
            except Exception:
                pass

    # ------------------------------------------------------------------
    def _options(self):
        self._send(200, "OK", {"Public": "OPTIONS, DESCRIBE, SETUP, PLAY, TEARDOWN"})

    # ------------------------------------------------------------------
    def _describe(self):
        sdp = (
            f"v=0\r\n"
            f"o=- {int(time.time())} 1 IN IP4 0.0.0.0\r\n"
            f"s=DepthAI H.264 Stream\r\n"
            f"c=IN IP4 0.0.0.0\r\n"
            f"t=0 0\r\n"
            f"m=video 0 RTP/AVP {RTP_PAYLOAD_TYPE}\r\n"
            f"a=rtpmap:{RTP_PAYLOAD_TYPE} H264/90000\r\n"
            f"a=fmtp:{RTP_PAYLOAD_TYPE} packetization-mode=1;profile-level-id=42e01f\r\n"
            f"a=control:track1\r\n"
        ).encode()
        self._send(200, "OK", {
            "Content-Type": "application/sdp",
            "Content-Length": str(len(sdp))
        }, sdp)

    # ------------------------------------------------------------------
    def _setup(self, hdr: dict):
        trans = hdr.get("Transport", "")

        if "RTP/AVP/TCP" in trans or "interleaved=" in trans:
            self.use_tcp = True
            m = re.search(r"interleaved=(\d+)(?:-(\d+))?", trans)
            if m:
                self.rtp_channel = int(m.group(1))
                self.rtcp_channel = int(m.group(2)) if m.group(2) else self.rtp_channel + 1
            else:
                self.rtp_channel = 0
                self.rtcp_channel = 1

            self.session = str(int(time.time() * 1000))
            self._send(200, "OK", {
                "Transport": f"RTP/AVP/TCP;unicast;interleaved={self.rtp_channel}-{self.rtcp_channel}",
                "Session": self.session
            })
            return

        m = re.search(r"client_port=(\d+)(?:-(\d+))?", trans)
        if not m:
            self._error(400)
            return

        self.client_rtp_port = int(m.group(1))
        self.client_rtcp_port = int(m.group(2)) if m.group(2) else self.client_rtp_port + 1

        self.rtp_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.rtp_sock.bind(('', 0))
        server_rtpeed = self.rtp_sock.getsockname()[1]

        self.client_ip = self.sock.getpeername()[0]
        self.session = str(int(time.time() * 1000))

        self._send(200, "OK", {
            "Transport": f"RTP/AVP;unicast;"
                         f"client_port={self.client_rtp_port}-{self.client_rtcp_port};"
                         f"server_port={server_rtp}-{server_rtp + 1}",
            "Session": self.session
        })

    # ------------------------------------------------------------------
    def _play(self):
        if not self.session:
            self._error(454)
            return

        self.stop_event.clear()
        self.current_seq = 1
        self.current_timestamp = 0
        self.rtp_thread = threading.Thread(target=self._rtp_sender, daemon=True)
        self.rtp_thread.start()

        self._send(200, "OK", {
            "RTP-Info": f"url=track1;seq={self.current_seq};rtptime={self.current_timestamp}"
        })

    # ------------------------------------------------------------------
    def _send_rtp_packet(self, packet: bytes):
        """Send via UDP or TCP interleaved."""
        if self.use_tcp:
            frame = b"$" + bytes([self.rtp_channel]) + struct.pack("!H", len(packet)) + packet
            with self.tcp_write_lock:
                try:
                    self.sock.sendall(frame)
                except Exception as e:
                    print(f"[RTP-TCP] send failed: {e}")
        else:
            try:
                self.rtp_sock.sendto(packet, (self.client_ip, self.client_rtp_port))
            except Exception as e:
                print(f"[RTP-UDP] send failed: {e}")

    # ------------------------------------------------------------------
    def _rtp_sender(self):
        seq = self.current_seq
        timestamp = self.current_timestamp
        clock_rate = 90000
        inc = clock_rate // FRAMERATE

        while not self.stop_event.is_set():
            try:
                nal = self.frame_q.get(timeout=0.5)
            except queue.Empty:
                continue

            if len(nal) == 0:
                continue

            # Determine max payload size
            max_payload = MAX_TCP_PAYLOAD if self.use_tcp else MAX_UDP_PAYLOAD
            max_payload -= 14  # RTP header (12) + FU-A header (2)

            # Single NAL Unit Mode (if small)
            if len(nal) <= max_payload:
                marker = 1
                rtp_hdr = struct.pack(
                    "!BBHII",
                    0x80,
                    (marker << 7) | RTP_PAYLOAD_TYPE,
                    seq & 0xFFFF,
                    timestamp,
                    SSRC
                )
                packet = rtp_hdr + nal
                self._send_rtp_packet(packet)
                #print(f"[RTP] Single NAL → {len(packet)} B | seq={seq}")
                seq = (seq + 1) & 0xFFFF
                timestamp = (timestamp + inc) & 0xFFFFFFFF
                continue

            # --- FU-A Fragmentation ---
            nal_header = nal[0]
            fu_indicator = (nal_header & 0xE0) | 28  # FU-A
            fu_header_start = 0x80 | (nal_header & 0x1F)
            fu_header_end = 0x40 | (nal_header & 0x1F)

            offset = 1
            first = True
            while offset < len(nal):
                payload_size = min(max_payload, len(nal) - offset)
                payload = nal[offset:offset + payload_size]

                if first:
                    fu_header = fu_header_start
                    first = False
                else:
                    fu_header = nal_header & 0x1F
                marker = 1 if (offset + payload_size) >= len(nal) else 0
                if marker:
                    fu_header |= 0x40  # End bit

                rtp_hdr = struct.pack(
                    "!BBHII",
                    0x80,
                    (marker << 7) | RTP_PAYLOAD_TYPE,
                    seq & 0xFFFF,
                    timestamp,
                    SSRC
                )
                fu_packet = rtp_hdr + bytes([fu_indicator, fu_header]) + payload
                self._send_rtp_packet(fu_packet)

                print(f"[RTP] FU-A {'S' if fu_header & 0x80 else 'M'}{'E' if marker else ''} → {len(fu_packet)} B | seq={seq}")

                offset += payload_size
                seq = (seq + 1) & 0xFFFF

            timestamp = (timestamp + inc) & 0xFFFFFFFF

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
    print(f"[RTSP] listening → rtsp://<IP>:{port}/stream")

    frame_q: queue.Queue = queue.Queue(maxsize=30)
    threading.Thread(target=depthai_h264_producer, args=(frame_q,), daemon=True).start()

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