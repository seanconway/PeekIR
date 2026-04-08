#!/usr/bin/env python3
"""
Run on the Raspberry Pi (with PureThermal/Lepton on /dev/video0).

Serves a single-frame JPEG at:  GET http://<pi-ip>:8765/snapshot

Requires: opencv-python (e.g. from SoftwareDemo: uv sync)
"""
from __future__ import annotations

import argparse
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Optional

import cv2

_cap: Optional[cv2.VideoCapture] = None


def _get_capture(device: int) -> cv2.VideoCapture:
    global _cap
    if _cap is not None and _cap.isOpened():
        return _cap
    cap = cv2.VideoCapture(device, cv2.CAP_V4L2)
    if not cap.isOpened():
        raise RuntimeError(f"Cannot open /dev/video{device}")
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, 160)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 120)
    # BGR3 matches v4l2 BGR3; if read fails, set format once with v4l2-ctl (see README steps)
    fourcc = cv2.VideoWriter_fourcc(*"BGR3")
    cap.set(cv2.CAP_PROP_FOURCC, fourcc)
    _cap = cap
    return cap


def capture_jpeg(device: int, quality: int = 85) -> bytes:
    cap = _get_capture(device)
    ok, frame = cap.read()
    if not ok or frame is None:
        raise RuntimeError("Frame grab failed (try: v4l2-ctl --set-fmt-video=...)")
    enc_ok, buf = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), quality])
    if not enc_ok:
        raise RuntimeError("JPEG encode failed")
    return buf.tobytes()


def make_handler(device: int):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if self.path.split("?", 1)[0] != "/snapshot":
                self.send_error(404, "Use GET /snapshot")
                return
            try:
                body = capture_jpeg(device)
            except Exception as e:
                self.send_error(500, str(e))
                return
            self.send_response(200)
            self.send_header("Content-Type", "image/jpeg")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, fmt: str, *args) -> None:
            print("%s - %s" % (self.address_string(), fmt % args))

    return Handler


def main() -> None:
    p = argparse.ArgumentParser(description="Pi IR snapshot HTTP server (PureThermal /dev/video0)")
    p.add_argument("--host", default="0.0.0.0", help="Bind address (0.0.0.0 = LAN)")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--device", type=int, default=0, help="V4L2 index (usually 0 for PureThermal)")
    args = p.parse_args()

    handler = make_handler(args.device)
    httpd = HTTPServer((args.host, args.port), handler)
    print(f"IR snapshot server: http://{args.host}:{args.port}/snapshot")
    print("Press Ctrl+C to stop.")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping.")
    finally:
        httpd.server_close()
        global _cap
        if _cap is not None:
            _cap.release()
            _cap = None


if __name__ == "__main__":
    main()
