#!/usr/bin/env python3
"""Privileged fixed-parser bridge. No command/path supplied by the API is run."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import pwd
import re
import shutil
import socket
import socketserver
import stat
import struct
import subprocess
import tempfile
import threading
import time
import uuid

MAX_ZIP = 20 * 1024 * 1024
MAX_REQUEST_BYTES = 1024
REQUEST_READ_SECONDS = 2.0
MAX_TRANSPORT_HANDLERS = 8


def scan(config, name):
    if not isinstance(name, str) or not re.fullmatch(r"incoming-[0-9]+\.zip", name):
        raise ValueError("invalid quarantine file name")
    parent = os.open(config.quarantine, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=parent)
    finally:
        os.close(parent)
    with os.fdopen(descriptor, "rb") as source:
        info = os.fstat(source.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != config.api_uid or info.st_size > MAX_ZIP:
            raise ValueError("invalid quarantine source")
        payload = source.read(MAX_ZIP + 1)
    if len(payload) > MAX_ZIP:
        raise ValueError("package too large")
    # The container never binds an API-controlled path. Copy the opened bytes
    # into a helper-private directory to remove the path-swap window.
    folder = Path(tempfile.mkdtemp(prefix="scan-", dir=config.spool))
    container = "qiqc-scanner-" + uuid.uuid4().hex
    try:
        archive = folder / "harness.zip"
        archive.write_bytes(payload)
        archive.chmod(0o444)
        command = ["/usr/bin/docker", "run", "--rm", "--pull=never", "--name", container,
                   "--network", "none", "--read-only", "--cap-drop", "ALL",
                   "--security-opt", "no-new-privileges", "--pids-limit", "32",
                   "--memory", "256m", "--cpus", "0.5", "--user", "65534:65534",
                   "--mount", f"type=bind,src={archive},dst=/incoming/harness.zip,readonly",
                   "--mount", f"type=bind,src={config.parser},dst=/scanner.py,readonly",
                   config.image, "python3", "/scanner.py", "/incoming/harness.zip"]
        parser_sha = hashlib.sha256(Path(config.parser).read_bytes()).hexdigest()
        result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=40, check=False)
        if len(result.stdout) > 512 * 1024:
            raise ValueError("scanner report too large")
        response = json.loads(result.stdout)
        if not isinstance(response, dict) or not isinstance(response.get("ok"), bool):
            raise ValueError("invalid scanner report")
        if (response["ok"] and result.returncode != 0) or (not response["ok"] and response.get("error_kind") != "invalid_package"):
            raise ValueError("scanner process failed")
        if response["ok"]:
            report = response.get("report")
            if not isinstance(report, dict) or report.get("ruleset_version") != "qiqc-archive/2":
                raise ValueError("scanner ruleset mismatch")
            if hashlib.sha256(Path(config.parser).read_bytes()).hexdigest() != parser_sha:
                raise ValueError("scanner parser changed during inspection")
            report["parser_sha256"] = parser_sha
            report["scanner_image"] = config.image
        response["source_sha256"] = hashlib.sha256(payload).hexdigest()
        return response
    finally:
        subprocess.run(["/usr/bin/docker", "rm", "-f", container], stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, timeout=10, check=False)
        shutil.rmtree(folder)


class Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True
    # Keep the kernel accept backlog larger than the bounded handler pool. A
    # busy scanner should return its structured `unavailable` response rather
    # than making a burst client fail at connect(2) with EAGAIN.
    request_queue_size = MAX_TRANSPORT_HANDLERS * 4

    def __init__(self, *args, **kwargs):
        self.transport_slots = threading.BoundedSemaphore(MAX_TRANSPORT_HANDLERS)
        super().__init__(*args, **kwargs)

    def process_request(self, request, client_address):
        # Backpressure before creating threads. At most two handlers scan; the
        # remaining handlers read a bounded local protocol frame or return busy.
        self.transport_slots.acquire()
        try:
            super().process_request(request, client_address)
        except BaseException:
            self.transport_slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.transport_slots.release()


def read_request(connection):
    # An absolute deadline prevents slow trickle input from renewing a socket
    # timeout indefinitely. Only one <=1024-byte newline-terminated frame is read.
    deadline = time.monotonic() + REQUEST_READ_SECONDS
    line = bytearray()
    while len(line) <= MAX_REQUEST_BYTES:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError('scanner request deadline exceeded')
        connection.settimeout(remaining)
        byte = connection.recv(1)
        if not byte:
            raise ValueError('incomplete scanner request')
        line.extend(byte)
        if byte == b'\n':
            break
    if len(line) > MAX_REQUEST_BYTES or not line.endswith(b'\n'):
        raise ValueError('invalid scanner request')
    request = json.loads(line)
    if not isinstance(request, dict) or set(request) != {'filename'}:
        raise ValueError('invalid scanner request')
    return request


class Handler(socketserver.StreamRequestHandler):
    def handle(self):
        self.connection.settimeout(45)
        _, uid, _ = struct.unpack("3i", self.connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
        if uid not in {self.server.config.api_uid, 0}:
            self.wfile.write(b'{"ok":false,"error":"unauthorized scanner peer"}\n')
            return
        acquired = False
        try:
            # Consume the valid request before rejecting capacity. Closing a
            # Unix stream with unread input generates ECONNRESET at Go ReadAll,
            # hiding an otherwise complete explicit unaccepted/busy response.
            request = read_request(self.connection)
            self.connection.settimeout(45)
            acquired = self.server.slots.acquire(blocking=False)
            if not acquired:
                response = {"ok": False, "error_kind": "unavailable", "error": "scanner busy; retry later"}
            else:
                response = scan(self.server.config, request["filename"])
        except Exception:
            response = {"ok": False, "error_kind": "unavailable", "error": "isolated scanner unavailable or invalid input"}
        finally:
            if acquired:
                self.server.slots.release()
        try:
            self.connection.settimeout(2)
            self.wfile.write(json.dumps(response, ensure_ascii=False).encode() + b"\n")
        except OSError:
            # Abandoned/malformed clients do not prolong a transport slot.
            pass



def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--socket", required=True)
    parser.add_argument("--quarantine", required=True)
    parser.add_argument("--spool", required=True)
    parser.add_argument("--parser", required=True)
    parser.add_argument("--image", required=True)
    parser.add_argument("--api-user", required=True)
    args = parser.parse_args()
    args.api_uid = pwd.getpwnam(args.api_user).pw_uid
    if not re.fullmatch(r"sha256:[a-f0-9]{64}", args.image):
        raise SystemExit("scanner image must be an immutable local image ID")
    parser_info = Path(args.parser).stat()
    if parser_info.st_uid != 0 or parser_info.st_mode & 0o022:
        raise SystemExit("parser must be root-owned and not group/world writable")
    Path(args.spool).mkdir(parents=True, exist_ok=True, mode=0o700)
    socket_path = Path(args.socket)
    if socket_path.exists():
        if not stat.S_ISSOCK(socket_path.lstat().st_mode):
            raise SystemExit("refusing to replace non-socket")
        socket_path.unlink()
    with Server(args.socket, Handler) as server:
        server.config = args
        server.slots = threading.BoundedSemaphore(2)
        os.chmod(args.socket, 0o660)
        server.serve_forever()


if __name__ == "__main__":
    main()
