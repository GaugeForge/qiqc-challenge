"""Disposable verifier-side synthetic Webhook/WebSocket control; no file API."""

import base64
import hashlib
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Lock

counts = {"webhook": 0, "websocket": 0}
lock = Lock()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_args):
        pass

    def do_GET(self):
        if (
            self.path == "/websocket"
            and self.headers.get("Upgrade", "").lower() == "websocket"
        ):
            key = self.headers.get("Sec-WebSocket-Key", "")
            accept = base64.b64encode(
                hashlib.sha1(
                    (key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()
                ).digest()
            ).decode()
            with lock:
                counts["websocket"] += 1
                Path("/tmp/security-sink-counts.json").write_text(json.dumps(counts))
            self.send_response(101)
            self.send_header("Upgrade", "websocket")
            self.send_header("Connection", "Upgrade")
            self.send_header("Sec-WebSocket-Accept", accept)
            self.end_headers()
            return
        self.send_response(200 if self.path == "/health" else 404)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_POST(self):
        if (
            self.path != "/webhook"
            or int(self.headers.get("Content-Length", "0")) > 128
        ):
            self.send_response(404)
            self.end_headers()
            return
        self.rfile.read(int(self.headers.get("Content-Length", "0")))
        with lock:
            counts["webhook"] += 1
            Path("/tmp/security-sink-counts.json").write_text(json.dumps(counts))
        self.send_response(204)
        self.send_header("Content-Length", "0")
        self.end_headers()


if __name__ == "__main__":
    if (
        Path("/root/QQ-Challenge").exists()
        or not Path("/tmp/qiqc-security-e2b-only").is_file()
    ):
        raise RuntimeError("E2B-only fixed acceptance sink")
    Path("/tmp/security-sink-counts.json").write_text(json.dumps(counts))
    ThreadingHTTPServer(("0.0.0.0", 9999), Handler).serve_forever()
