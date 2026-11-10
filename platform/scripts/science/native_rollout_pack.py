"""Bounded organizer archive jobs; execute scientific data packing only in E2B.

The host-side launcher starts a job once and observes its atomic status file.
Losing an E2B command response never authorizes a second pack of the same data.
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import stat
import tarfile
import time

MAX_EXPANDED = 192 * 1024 * 1024
MAX_COMPRESSED = 96 * 1024 * 1024
MAX_ENTRIES = 20_000
PACK_ERRORS = frozenset({
    "native_pack_entry_limit", "native_pack_expanded_limit",
    "native_pack_compressed_limit", "native_pack_unsafe_entry",
    "native_pack_file_changed", "native_pack_deadline",
    "native_pack_invalid_job", "native_pack_io_failure",
})


def check_deadline(deadline, clock=time.monotonic):
    if clock() >= deadline:
        raise ValueError("native_pack_deadline")


class BoundedWriter:
    def __init__(self, stream, limit, code, deadline):
        self.stream, self.limit, self.code, self.deadline = stream, limit, code, deadline
        self.count = 0

    def write(self, data):
        check_deadline(self.deadline)
        if self.count + len(data) > self.limit:
            raise ValueError(self.code)
        written = self.stream.write(data)
        if written != len(data):
            raise ValueError("native_pack_io_failure")
        self.count += written
        return written

    def flush(self):
        self.stream.flush()


def _files(root_fd, deadline, max_entries, counters, prefix="", depth=0):
    if depth > 64:
        raise ValueError("native_pack_unsafe_entry")
    names = []
    with os.scandir(root_fd) as entries:
        for entry in entries:
            check_deadline(deadline)
            counters["visited"] += 1
            if counters["visited"] > max_entries:
                raise ValueError("native_pack_entry_limit")
            if not entry.name.startswith(".control-"):
                names.append(entry.name)
    for name in sorted(names):
        check_deadline(deadline)
        relative = prefix + name
        if len(relative.encode("utf-8", "surrogateescape")) > 4096:
            raise ValueError("native_pack_unsafe_entry")
        info = os.stat(name, dir_fd=root_fd, follow_symlinks=False)
        if stat.S_ISDIR(info.st_mode):
            child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root_fd)
            try:
                yield from _files(child, deadline, max_entries, counters, relative + "/", depth + 1)
            finally:
                os.close(child)
        elif stat.S_ISREG(info.st_mode):
            yield root_fd, name, relative, info
        else:
            raise ValueError("native_pack_unsafe_entry")


def pack_tree(root, temporary, *, deadline, expanded_limit=MAX_EXPANDED,
              compressed_limit=MAX_COMPRESSED, entry_limit=MAX_ENTRIES):
    """Pack bounded regular files with no-follow descriptor-relative reads."""
    root, temporary = Path(root), Path(temporary)
    if (type(expanded_limit) is not int or expanded_limit < tarfile.RECORDSIZE
            or type(compressed_limit) is not int or compressed_limit < 1
            or type(entry_limit) is not int or not 1 <= entry_limit <= MAX_ENTRIES):
        raise ValueError("native_pack_invalid_job")
    counters = {"visited": 0, "files": 0, "headers": 0}
    root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "wb") as output:
            compressed = BoundedWriter(output, compressed_limit, "native_pack_compressed_limit", deadline)
            with gzip.GzipFile(filename="", mode="wb", fileobj=compressed, compresslevel=1, mtime=0) as gzip_file:
                expanded = BoundedWriter(gzip_file, expanded_limit, "native_pack_expanded_limit", deadline)
                for directory, name, relative, before in _files(root_fd, deadline, entry_limit, counters):
                    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
                    with os.fdopen(fd, "rb") as source:
                        info = os.fstat(source.fileno())
                        if (not stat.S_ISREG(info.st_mode) or
                                (info.st_dev, info.st_ino, info.st_size) != (before.st_dev, before.st_ino, before.st_size)):
                            raise ValueError("native_pack_file_changed")
                        member = tarfile.TarInfo(relative)
                        member.size, member.mode, member.mtime = info.st_size, 0o600, int(info.st_mtime)
                        header = member.tobuf(format=tarfile.PAX_FORMAT, encoding="utf-8", errors="surrogateescape")
                        counters["headers"] += 1 if len(header) == 512 else 2
                        counters["files"] += 1
                        if counters["headers"] > entry_limit:
                            raise ValueError("native_pack_entry_limit")
                        padded_size = (info.st_size + 511) // 512 * 512
                        final_size = ((expanded.count + len(header) + padded_size + 1024 + tarfile.RECORDSIZE - 1)
                                      // tarfile.RECORDSIZE * tarfile.RECORDSIZE)
                        if final_size > expanded_limit:
                            raise ValueError("native_pack_expanded_limit")
                        expanded.write(header)
                        remaining = info.st_size
                        while remaining:
                            chunk = source.read(min(remaining, 64 * 1024))
                            if not chunk:
                                raise ValueError("native_pack_file_changed")
                            expanded.write(chunk)
                            remaining -= len(chunk)
                        after = os.fstat(source.fileno())
                        if (after.st_size, after.st_mtime_ns, after.st_ctime_ns) != (info.st_size, info.st_mtime_ns, info.st_ctime_ns):
                            raise ValueError("native_pack_file_changed")
                        expanded.write(b"\0" * (padded_size - info.st_size))
                expanded.write(b"\0" * 1024)
                expanded.write(b"\0" * ((-expanded.count) % tarfile.RECORDSIZE))
            output.flush()
            os.fsync(output.fileno())
        digest = hashlib.sha256()
        with temporary.open("rb") as source:
            for chunk in iter(lambda: source.read(64 * 1024), b""):
                check_deadline(deadline)
                digest.update(chunk)
        return {"sha256": digest.hexdigest(), "compressed_size": compressed.count,
                "expanded_size": expanded.count, "file_count": counters["files"],
                "header_count": counters["headers"], "visited_entries": counters["visited"],
                "gzip_level": 1}
    finally:
        os.close(root_fd)


def _atomic_json(path, value):
    temporary = Path(str(path) + ".tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as output:
        json.dump(value, output, sort_keys=True)
        output.flush()
        os.fsync(output.fileno())
    os.replace(temporary, path)
    directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def pack_job(root, target, job_id, *, timeout=60, **limits):
    root, target = Path(root), Path(target)
    if (not re.fullmatch(r"[a-f0-9]{64}", job_id) or not root.is_absolute() or not target.is_absolute()
            or ".." in root.parts or ".." in target.parts or target.is_relative_to(root)):
        raise ValueError("native_pack_invalid_job")
    lock, temporary, status = (Path(str(target) + suffix) for suffix in (".lock", ".partial", ".status.json"))
    try:
        fd = os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    except FileExistsError:
        return {"status": "already_started"}  # Never start a second compressor.
    with os.fdopen(fd, "w") as output:
        output.write(job_id)
        output.flush()
        os.fsync(output.fileno())
    base = {"schema": "qiqc-native-pack-job/v1", "job_id": job_id}
    try:
        if target.exists() or target.is_symlink():
            raise ValueError("native_pack_invalid_job")
        result = pack_tree(root, temporary, deadline=time.monotonic() + timeout, **limits)
        os.replace(temporary, target)
        value = {**base, "status": "completed", **result}
        _atomic_json(status, value)
        return value
    except Exception as error:
        code = str(error) if type(error) is ValueError and str(error) in PACK_ERRORS else "native_pack_io_failure"
        temporary.unlink(missing_ok=True)
        _atomic_json(status, {**base, "status": "failed", "failure_code": code})
        raise ValueError(code) from None


def remote_pack(sandbox, command, python, module, root, target, *, deadline,
                clock=time.monotonic, delay=time.sleep):
    """One launch; poll atomic evidence if the command stream response is lost."""
    from bounded_files import RemoteFileError, read_remote_bytes

    job_id = hashlib.sha256(json.dumps([root, target], separators=(",", ":")).encode()).hexdigest()
    status_path = target + ".status.json"
    remaining = deadline - clock()
    if remaining <= 0:
        raise ValueError("native_pack_deadline")
    job_timeout = min(120, max(1, int(remaining - 5)))
    argv = [python, module, "--root", root, "--target", target, "--job-id", job_id, "--timeout", str(job_timeout)]
    launch = "nohup " + shlex.join(argv) + " >" + shlex.quote(target + ".pack.log") + " 2>&1 </dev/null &"
    try:
        command(launch, timeout=min(10, remaining))
    except Exception as error:
        if type(error).__name__ not in ("TimeoutException", "ReadTimeout", "ConnectTimeout", "RemoteProtocolError", "ConnectionError", "SandboxUnreachableException"):
            raise
        # Launch acknowledgement is uncertain. Only the matching final marker
        # can resolve it. Never launch again, even when no marker appears.
    pack_deadline = min(deadline, clock() + job_timeout + 5)
    while clock() < pack_deadline:
        try:
            raw = read_remote_bytes(sandbox.files, status_path, 8192, missing_ok=True,
                                    user="root", request_timeout=min(2, max(.1, pack_deadline-clock())),
                                    deadline=pack_deadline, clock=clock)
        except RemoteFileError as error:
            if str(error) not in ("Remote file transfer failed", "Remote file total deadline exceeded"):
                raise
            raw = None
        if raw is not None:
            value = json.loads(raw)
            if (not isinstance(value, dict) or value.get("schema") != "qiqc-native-pack-job/v1"
                    or value.get("job_id") != job_id):
                raise ValueError("native_pack_invalid_job")
            if value.get("status") == "failed" and value.get("failure_code") in PACK_ERRORS:
                raise ValueError(value["failure_code"])
            if (value.get("status") != "completed" or not isinstance(value.get("sha256"), str)
                    or not re.fullmatch(r"[a-f0-9]{64}", value["sha256"])
                    or type(value.get("compressed_size")) is not int or not 0 < value["compressed_size"] <= MAX_COMPRESSED
                    or type(value.get("expanded_size")) is not int or not 0 < value["expanded_size"] <= MAX_EXPANDED
                    or type(value.get("header_count")) is not int or not 0 <= value["header_count"] <= MAX_ENTRIES):
                raise ValueError("native_pack_invalid_job")
            return value
        delay(min(.25, max(0, pack_deadline-clock())))
    raise ValueError("native_pack_deadline")


def main():
    parser = argparse.ArgumentParser()
    for name in ("root", "target", "job-id"):
        parser.add_argument("--" + name, required=True)
    parser.add_argument("--timeout", type=int, default=60)
    args = parser.parse_args()
    if not 1 <= args.timeout <= 120:
        raise SystemExit(2)
    try:
        pack_job(args.root, args.target, args.job_id, timeout=args.timeout)
    except Exception:
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
