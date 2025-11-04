#!/usr/bin/env python3

import depthai as dai
import time
import blobconverter


# ----------------------------------------------------------------------
# Configuration
# ----------------------------------------------------------------------
FLASH = False
FRAMERATE = 30
RTP_PAYLOAD_TYPE = 96
SSRC = 0xDEADBEEF
MAX_UDP_PAYLOAD = 1400
MAX_TCP_PAYLOAD = 60000
MAX_BUFFER_SIZE = 30
# ----------------------------------------------------------------------

pipeline = dai.Pipeline()

cam = pipeline.create(dai.node.ColorCamera)
cam.setFps(FRAMERATE)
cam.setInterleaved(False)
cam.setBoardSocket(dai.CameraBoardSocket.CAM_A)
cam.setPreviewSize(640, 360)
cam.setResolution(dai.ColorCameraProperties.SensorResolution.THE_720_P)

enc = pipeline.create(dai.node.VideoEncoder)
enc.setDefaultProfilePreset(FRAMERATE, dai.VideoEncoderProperties.Profile.H264_MAIN)

# detectionNetwork = pipeline.create(dai.node.YoloDetectionNetwork)
# # Network specific settings
# detectionNetwork.setConfidenceThreshold(0.5)
# detectionNetwork.setNumClasses(80)
# detectionNetwork.setCoordinateSize(4)
# detectionNetwork.setIouThreshold(0.5)
# # We have this model on DepthAI Zoo, so download it using blobconverter
# detectionNetwork.setBlobPath(blobconverter.from_zoo('yolov8n_coco_640x352', zoo_type='depthai'))
# detectionNetwork.input.setBlocking(False)

# Script node
script = pipeline.create(dai.node.Script)
script.setProcessor(dai.ProcessorType.LEON_CSS)
script.setScript("""
import sys
import socket
import threading
import time
import re
import struct
from socketserver import ThreadingMixIn
import fcntl

PORT = 8080

# ----------------------------------------------------------------------
# Configuration
# ----------------------------------------------------------------------
FRAMERATE = 30
RTP_PAYLOAD_TYPE = 96
SSRC = 0xDEADBEEF
MAX_UDP_PAYLOAD = 1400
MAX_TCP_PAYLOAD = 60000
MAX_BUFFER_SIZE = 30
# ----------------------------------------------------------------------

def get_ip_address(ifname):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    return socket.inet_ntoa(fcntl.ioctl(
        s.fileno(),
        -1071617759,  # SIOCGIFADDR
        struct.pack('256s', ifname[:15].encode())
    )[20:24])

def depthai_h264_producer(frame_buffer, buffer_lock, stop_event):
    while not stop_event.is_set():
        image = node.io['enc'].get()
        if image is not None:
            nal = image.getData().tobytes()
            with buffer_lock:
                if len(frame_buffer) < MAX_BUFFER_SIZE:
                    frame_buffer.append(nal)
        else:
            time.sleep(1 / (FRAMERATE * 2))
                 
# ----------------------------------------------------------------------
# RTSP Handler
# ----------------------------------------------------------------------
class RTSPHandler:
    def __init__(self, client_sock, frame_buffer, buffer_lock, stop_event):
        self.sock = client_sock
        self.file = client_sock.makefile("rwb")
        self.frame_buffer = frame_buffer
        self.buffer_lock = buffer_lock
        self.stop_event = stop_event

        self.cseq = 0
        self.session = None

        self.use_tcp = False
        self.rtp_channel = 0
        self.rtcp_channel = 1

        self.client_rtp_port = None
        self.client_ip = None
        self.rtp_sock = None

        self.current_seq = 1
        self.current_timestamp = 0
        self.rtp_thread = None
        self.tcp_write_lock = threading.Lock()

    def run(self):
        try:
            while not self.stop_event.is_set():
                line = self.file.readline().decode(errors="ignore").strip()
                if not line:
                    break
                self._handle_request(line)
        except Exception as e:
            print("[RTSP] error:", e)
        finally:
            self.cleanup()

    def _handle_request(self, first_line):
        parts = first_line.split()
        if len(parts) < 2:
            return
        method = parts[0]
        url = parts[1]

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

    def _send(self, code, reason, extra=None, body=None):
        if extra is None:
            extra = {}
        lines = ["RTSP/1.0 {} {}".format(code, reason), "CSeq: {}".format(self.cseq)]
        if self.session:
            lines.append("Session: {}".format(self.session))
        for k, v in extra.items():
            lines.append("{}: {}".format(k, v))
        lines.append("")
        data = "\\r\\n".join(lines).encode() + b"\\r\\n"
        with self.tcp_write_lock:
            try:
                self.file.write(data)
                if body:
                    self.file.write(body)
                self.file.flush()
            except:
                pass

    def _options(self):
        self._send(200, "OK", {"Public": "OPTIONS, DESCRIBE, SETUP, PLAY, TEARDOWN"})

    def _describe(self):
        sdp = (
            "v=0\\r\\n"
            "o=- {} 1 IN IP4 0.0.0.0\\r\\n"
            "s=DepthAI H.264 Stream\\r\\n"
            "c=IN IP4 0.0.0.0\\r\\n"
            "t=0 0\\r\\n"
            "m=video 0 RTP/AVP {}\\r\\n"
            "a=rtpmap:{} H264/90000\\r\\n"
            "a=fmtp:{} packetization-mode=1;profile-level-id=42e01f\\r\\n"
            "a=control:track1\\r\\n"
        ).format(int(time.time()), RTP_PAYLOAD_TYPE, RTP_PAYLOAD_TYPE, RTP_PAYLOAD_TYPE).encode()
        self._send(200, "OK", {
            "Content-Type": "application/sdp",
            "Content-Length": str(len(sdp))
        }, sdp)

    def _setup(self, hdr):
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
                "Transport": "RTP/AVP/TCP;unicast;interleaved={}-{}".format(self.rtp_channel, self.rtcp_channel),
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
        server_rtp = self.rtp_sock.getsockname()[1]

        self.client_ip = self.sock.getpeername()[0]
        self.session = str(int(time.time() * 1000))

        self._send(200, "OK", {
            "Transport": "RTP/AVP;unicast;client_port={}-{};server_port={}-{}".format(
                self.client_rtp_port, self.client_rtcp_port, server_rtp, server_rtp + 1
            ),
            "Session": self.session
        })

    def _play(self):
        if not self.session:
            self._error(454)
            return

        self.current_seq = 1
        self.current_timestamp = 0
        self.rtp_thread = threading.Thread(target=self._rtp_sender, daemon=True)
        self.rtp_thread.start()

        self._send(200, "OK", {
            "RTP-Info": "url=track1;seq={};rtptime={}".format(self.current_seq, self.current_timestamp)
        })

    def _send_rtp_packet(self, packet):
        if self.use_tcp:
            frame = b"$" + bytes([self.rtp_channel]) + struct.pack("!H", len(packet)) + packet
            with self.tcp_write_lock:
                try:
                    self.sock.sendall(frame)
                except Exception as e:
                    print("[RTP-TCP] send failed:", e)
        else:
            try:
                self.rtp_sock.sendto(packet, (self.client_ip, self.client_rtp_port))
            except Exception as e:
                print("[RTP-UDP] send failed:", e)

    def _rtp_sender(self):
        seq = self.current_seq
        timestamp = self.current_timestamp
        clock_rate = 90000
        inc = clock_rate // FRAMERATE

        while not self.stop_event.is_set():
            nal = None
            with self.buffer_lock:
                if self.frame_buffer:
                    nal = self.frame_buffer.pop(0)

            if nal is None:
                time.sleep(1 / (FRAMERATE * 2))
                continue

            if len(nal) == 0:
                continue

            max_payload = MAX_TCP_PAYLOAD if self.use_tcp else MAX_UDP_PAYLOAD
            max_payload -= 14

            # Single NAL
            if len(nal) <= max_payload:
                marker = 1
                rtp_hdr = struct.pack("!BBHII", 0x80, (marker << 7) | RTP_PAYLOAD_TYPE, seq & 0xFFFF, timestamp, SSRC)
                packet = rtp_hdr + nal
                self._send_rtp_packet(packet)
                print("[RTP] Single NAL to {} B | seq={}".format(len(packet), seq))
                seq = (seq + 1) & 0xFFFF
                timestamp = (timestamp + inc) & 0xFFFFFFFF
                continue

            # FU-A Fragmentation
            nal_header = nal[0]
            fu_indicator = (nal_header & 0xE0) | 28
            fu_header_start = 0x80 | (nal_header & 0x1F)
            fu_header_end   = 0x40 | (nal_header & 0x1F)

            offset = 1
            first = True
            while offset < len(nal):
                payload_size = min(max_payload, len(nal) - offset)
                payload = nal[offset:offset + payload_size]

                fu_header = fu_header_start if first else (nal_header & 0x1F)
                first = False
                marker = 1 if (offset + payload_size) >= len(nal) else 0
                if marker:
                    fu_header |= 0x40

                rtp_hdr = struct.pack("!BBHII", 0x80, (marker << 7) | RTP_PAYLOAD_TYPE, seq & 0xFFFF, timestamp, SSRC)
                fu_packet = rtp_hdr + bytes([fu_indicator, fu_header]) + payload
                self._send_rtp_packet(fu_packet)

                s = "S" if fu_header & 0x80 else ""
                e = "E" if marker else ""
                print("[RTP] FU-A {}{} to {} B | seq={}".format(s, e, len(fu_packet), seq))

                offset += payload_size
                seq = (seq + 1) & 0xFFFF

            timestamp = (timestamp + inc) & 0xFFFFFFFF

        self.current_seq = seq
        self.current_timestamp = timestamp

    def _teardown(self):
        if self.rtp_thread:
            self.rtp_thread.join(timeout=1)
        self._send(200, "OK")
        self.cleanup()

    def _error(self, code):
        reasons = {400: "Bad Request", 404: "Not Found", 405: "Method Not Allowed", 454: "Session Not Found"}
        self._send(code, reasons.get(code, "Error"))

    def cleanup(self):
        if self.rtp_sock:
            self.rtp_sock.close()
        try:
            self.sock.close()
        except:
            pass


# ----------------------------------------------------------------------
# Server bootstrap
# ----------------------------------------------------------------------
print("\\n[RTSP] starting...")
PORT = 8554
node.warn(f"Serving at {get_ip_address('re0')}:{PORT}")
                
srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
srv.bind(("0.0.0.0", PORT))
srv.listen(5)
print("[RTSP] listening to rtsp://<IP>:{}".format(PORT))

frame_buffer = []
buffer_lock = threading.Lock()
stop_event = threading.Event()

producer_thread = threading.Thread(
    target=depthai_h264_producer,
    args=(frame_buffer, buffer_lock, stop_event),
    daemon=True
)
producer_thread.start()

try:
    while True:
        cli, addr = srv.accept()
        print("[RTSP] client", addr)
        h = RTSPHandler(cli, frame_buffer, buffer_lock, stop_event)
        threading.Thread(target=h.run, daemon=True).start()
except KeyboardInterrupt:
    print("\\n[RTSP] Shutting down...")
finally:
    stop_event.set()
    producer_thread.join(timeout=2)
""")

# Connections
# Feed preview frames into the detection network
# cam.preview.link(detectionNetwork.input)

# The detection network provides a passthrough frame, but the encoder expects NV12/YUV frames.
# Insert an ImageManip node to convert/resize/frame-type the passthrough into NV12 for the encoder.
# manip = pipeline.create(dai.node.ImageManip)
# manip.initialConfig.setResize(640, 352)
# manip.initialConfig.setKeepAspectRatio(False)
# manip.initialConfig.setFrameType(dai.RawImgFrame.Type.NV12)

# Link passthrough -> manip -> encoder
# detectionNetwork.passthrough.link(manip.inputImage)
# manip.out.link(jpeg.input)

# Also link detections to the script and encoder bitstream to the script
# detectionNetwork.out.link(script.inputs['detection_in'])
enc.bitstream.link(script.inputs['enc'])
cam.video.link(enc.input)

if FLASH: 
    # Flash the pipeline
    (f, bl) = dai.DeviceBootloader.getFirstAvailableDevice()

    bootloader = dai.DeviceBootloader(bl)

    progress = lambda p : print(f'Flashing progress: {p*100:.1f}%')

    bootloader.flash(progress, pipeline)
else:
    # Connect to device with pipeline
    with dai.Device(pipeline) as device:
        while not device.isClosed():
            time.sleep(1)
