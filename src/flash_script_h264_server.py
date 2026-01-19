#!/usr/bin/env python3
import depthai as dai
import time

# ----------------------------------------------------------------------
# Configuration
# ----------------------------------------------------------------------
FRAMERATE = 30
PORT = 8554
RTP_PAYLOAD_TYPE = 96
SSRC = 0xDEADBEEF
MAX_UDP_PAYLOAD = 1400
# ----------------------------------------------------------------------

pipeline = dai.Pipeline()

cam = pipeline.create(dai.node.ColorCamera)
cam.setFps(FRAMERATE)
cam.setInterleaved(False)
cam.setBoardSocket(dai.CameraBoardSocket.CAM_A)
cam.setVideoSize(640, 360)  # Critical: matches encoder input

enc = pipeline.create(dai.node.VideoEncoder)
enc.setDefaultProfilePreset(FRAMERATE, dai.VideoEncoderProperties.Profile.H264_MAIN)

script = pipeline.create(dai.node.Script)
script.setProcessor(dai.ProcessorType.LEON_CSS)

script.setScript("""
import socket
import threading
import time
import re
import struct
import select

# Global shared buffer
frame_buffer = []
buffer_lock = threading.Lock()

# Non-blocking H.264 producer
def producer():
    while True:
        pkt = node.io['enc'].tryGet()
        if pkt:
            nal = pkt.getData().tobytes()
            with buffer_lock:
                if len(frame_buffer) < 30:
                    frame_buffer.append(nal)
        time.sleep(0.001)  # Prevent busy-loop

threading.Thread(target=producer, daemon=True).start()

class Client:
    def __init__(self, sock):
        self.sock = sock
        self.file = sock.makefile('rwb')
        self.session = "12345"
        self.use_tcp = False
        self.rtp_channel = 0
        self.client_ip = sock.getpeername()[0]
        self.client_rtp_port = None
        self.rtp_sock = None
        self.seq = 1
        self.ts = 0
        self.alive = True
        self.lock = threading.Lock()

    def send(self, code, reason, headers=None, body=None):
        if headers is None: headers = {}
        lines = [f"RTSP/1.0 {code} {reason}", f"CSeq: 0"]
        if self.session: lines.append(f"Session: {self.session}")
        for k, v in headers.items():
            lines.append(f"{k}: {v}")
        lines.append("")
        msg = "\\r\\n".join(lines).encode() + b"\\r\\n"
        if body: msg += body
        with self.lock:
            try:
                self.file.write(msg)
                self.file.flush()
            except:
                self.alive = False

    def handle(self):
        while self.alive:
            line = self.file.readline().decode(errors='ignore').strip()
            if not line: break
            parts = line.split()
            if len(parts) < 2: continue
            method = parts[0]

            headers = {}
            while True:
                h = self.file.readline().decode(errors='ignore').strip()
                if not h: break
                if ':' in h:
                    k, v = h.split(':', 1)
                    headers[k.strip()] = v.strip()

            trans = headers.get("Transport", "")

            if method == "OPTIONS":
                self.send(200, "OK", {"Public": "OPTIONS, DESCRIBE, SETUP, PLAY, TEARDOWN"})

            elif method == "DESCRIBE":
                sdp = (
                    "v=0\\r\\n"
                    "o=- 1 1 IN IP4 0.0.0.0\\r\\n"
                    "s=DepthAI H264\\r\\n"
                    "c=IN IP4 0.0.0.0\\r\\n"
                    "t=0 0\\r\\n"
                    "m=video 0 RTP/AVP 96\\r\\n"
                    "a=rtpmap:96 H264/90000\\r\\n"
                    "a=fmtp:96 packetization-mode=1\\r\\n"
                    "a=control:track1\\r\\n"
                ).encode()
                self.send(200, "OK", {"Content-Type": "application/sdp", "Content-Length": len(sdp)}, sdp)

            elif method == "SETUP":
                if "RTP/AVP/TCP" in trans or "interleaved=" in trans:
                    self.use_tcp = True
                    m = re.search(r"interleaved=(\\d+)", trans)
                    self.rtp_channel = int(m.group(1)) if m else 0
                    self.send(200, "OK", {"Transport": f"RTP/AVP/TCP;interleaved={self.rtp_channel}-{self.rtp_channel+1}", "Session": self.session})
                else:
                    # UDP mode – this is what VLC sends by default
                    m = re.search(r"client_port=(\\d+)-(\\d+)", trans)
                    if not m:
                        self.send(400, "Bad Request")
                        continue
                    self.client_rtp_port = int(m.group(1))
                    self.rtp_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                    self.rtp_sock.bind(('', 0))
                    server_port = self.rtp_sock.getsockname()[1]
                    self.send(200, "OK", {
                        "Transport": f"RTP/AVP;unicast;client_port={self.client_rtp_port}-{int(m.group(2))};server_port={server_port}-{server_port+1}",
                        "Session": self.session
                    })

            elif method == "PLAY":
                threading.Thread(target=self.stream, daemon=True).start()
                self.send(200, "OK", {"RTP-Info": f"url=track1;seq={self.seq}"})

            elif method == "TEARDOWN":
                self.alive = False
                self.send(200, "OK")

    def send_rtp(self, packet):
        if self.use_tcp:
            frame = b"$" + bytes([self.rtp_channel]) + struct.pack("!H", len(packet)) + packet
            try: self.sock.sendall(frame)
            except: self.alive = False
        elif self.rtp_sock and self.client_rtp_port:
            try: self.rtp_sock.sendto(packet, (self.client_ip, self.client_rtp_port))
            except: pass

    def stream(self):
        inc = 90000 // 30
        while self.alive:
            nal = None
            with buffer_lock:
                if frame_buffer: nal = frame_buffer.pop(0)
            if not nal:
                time.sleep(0.005)
                continue

            max_pl = 60000 if self.use_tcp else 1400
            max_pl -= 14

            if len(nal) <= max_pl:
                hdr = struct.pack("!BBHII", 0x80, (1<<7)|96, self.seq & 0xFFFF, self.ts, 0xDEADBEEF)
                self.send_rtp(hdr + nal)
                self.seq += 1
            else:
                # FU-A fragmentation (critical for large I-frames over UDP)
                h = nal[0]
                fu_ind = (h & 0xE0) | 28
                offset = 1
                first = True
                while offset < len(nal):
                    sz = min(max_pl, len(nal) - offset)
                    marker = 1 if offset + sz >= len(nal) else 0
                    fu_h = (0x80 if first else 0) | (0x40 if marker else 0) | (h & 0x1F)
                    first = False
                    hdr = struct.pack("!BBHII", 0x80, (marker<<7)|96, self.seq & 0xFFFF, self.ts, 0xDEADBEEF)
                    pkt = hdr + bytes([fu_ind, fu_h]) + nal[offset:offset+sz]
                    self.send_rtp(pkt)
                    self.seq += 1
                    offset += sz
            self.ts = (self.ts + inc) & 0xFFFFFFFF

# Main server – non-blocking with select()
srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
srv.bind(("0.0.0.0", 8554))
srv.listen(5)
node.warn("RTSP server listening on port 8554")

while True:
    r, _, _ = select.select([srv], [], [], 1.0)
    if srv in r:
        cli, addr = srv.accept()
        node.warn(f"New client: {addr}")
        threading.Thread(target=Client(cli).handle, daemon=True).start()
""")

# Connect pipeline
cam.video.link(enc.input)
enc.bitstream.link(script.inputs['enc'])

with dai.Device(pipeline) as device:
    print(f"RTSP URL: rtsp://{device.getMxId()}:8554")
    while True:
        time.sleep(1)