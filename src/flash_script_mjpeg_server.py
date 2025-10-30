#!/usr/bin/env python3

import depthai as dai
import time
import blobconverter


FLASH = False

# Start defining a pipeline
pipeline = dai.Pipeline()

# Define a source - color camera
cam = pipeline.create(dai.node.ColorCamera)
cam.setPreviewSize(640, 352)
cam.setInterleaved(False)
cam.setBoardSocket(dai.CameraBoardSocket.CAM_A)
cam.setResolution(dai.ColorCameraProperties.SensorResolution.THE_720_P)

# VideoEncoder
jpeg = pipeline.create(dai.node.VideoEncoder)
fps = cam.getFps()
print(f'fps from camera: {fps}')
jpeg.setDefaultProfilePreset(fps, dai.VideoEncoderProperties.Profile.MJPEG)

detectionNetwork = pipeline.create(dai.node.YoloDetectionNetwork)
# Network specific settings
detectionNetwork.setConfidenceThreshold(0.5)
detectionNetwork.setNumClasses(80)
detectionNetwork.setCoordinateSize(4)
detectionNetwork.setIouThreshold(0.5)
# We have this model on DepthAI Zoo, so download it using blobconverter
detectionNetwork.setBlobPath(blobconverter.from_zoo('yolov8n_coco_640x352', zoo_type='depthai'))
detectionNetwork.input.setBlocking(False)

# Script node
script = pipeline.create(dai.node.Script)
script.setProcessor(dai.ProcessorType.LEON_CSS)
script.setScript("""
    import time
    import socket
    import fcntl
    import struct
    from socketserver import ThreadingMixIn
    from http.server import BaseHTTPRequestHandler, HTTPServer

    PORT = 8080

    def get_ip_address(ifname):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        return socket.inet_ntoa(fcntl.ioctl(
            s.fileno(),
            -1071617759,  # SIOCGIFADDR
            struct.pack('256s', ifname[:15].encode())
        )[20:24])

    class ThreadingSimpleServer(ThreadingMixIn, HTTPServer):
        pass

    class HTTPHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == '/':
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'<h1>[DepthAI] Hello, world!</h1><p>Click <a href="img">here</a> for an image</p>')
            elif self.path == '/img':
                try:
                    self.send_response(200)
                    self.send_header('Content-type', 'multipart/x-mixed-replace; boundary=--jpgboundary')
                    self.end_headers()
                    fpsCounter = 0
                    timeCounter = time.time()
                    while True:
                        jpegImage = node.io['jpeg'].get()
                 
                        detections = node.io["detection_in"].get().detections
                        node.warn('Received frame + dets')
                        ts = jpegImage.getTimestamp()

                        det_arr = []
                        for det in detections:
                            det_arr.append(f"{det.label};{(det.confidence*100):.1f};{det.xmin:.4f};{det.ymin:.4f};{det.xmax:.4f};{det.ymax:.4f}")
                        det_str = "|".join(det_arr)

                        header = f"IMG {ts.total_seconds()} {len(det_str)}".ljust(32)
                        node.warn(f'>{header}<')

                        self.wfile.write("--jpgboundary".encode())
                        self.wfile.write(bytes([13, 10]))
                        self.send_header('Content-type', 'image/jpeg')
                        self.send_header('Content-length', str(len(jpegImage.getData())))
                        self.end_headers()
                        self.wfile.write(jpegImage.getData())
                        self.end_headers()

                        fpsCounter = fpsCounter + 1
                        if time.time() - timeCounter > 1:
                            node.warn(f'FPS: {fpsCounter}')
                            fpsCounter = 0
                            timeCounter = time.time()
                except Exception as ex:
                    node.warn(str(ex))

    with ThreadingSimpleServer(("", PORT), HTTPHandler) as httpd:
        node.warn(f"Serving at {get_ip_address('re0')}:{PORT}")
        httpd.serve_forever()
""")

# Connections
# Feed preview frames into the detection network
cam.preview.link(detectionNetwork.input)

# The detection network provides a passthrough frame, but the encoder expects NV12/YUV frames.
# Insert an ImageManip node to convert/resize/frame-type the passthrough into NV12 for the encoder.
manip = pipeline.create(dai.node.ImageManip)
manip.initialConfig.setResize(640, 352)
manip.initialConfig.setKeepAspectRatio(False)
manip.initialConfig.setFrameType(dai.RawImgFrame.Type.NV12)

# Link passthrough -> manip -> encoder
detectionNetwork.passthrough.link(manip.inputImage)
manip.out.link(jpeg.input)

# Also link detections to the script and encoder bitstream to the script
detectionNetwork.out.link(script.inputs['detection_in'])
jpeg.bitstream.link(script.inputs['jpeg'])

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
