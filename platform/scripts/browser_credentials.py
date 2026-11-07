"""Private per-attempt account delivery records for concurrent browser checks."""

from __future__ import annotations

import fcntl
import json
import os
import secrets
from pathlib import Path


def _read_private(path):
    path = Path(path)
    if path.is_symlink() or not path.is_file() or path.stat().st_mode & 0o077:
        raise ValueError("private_regular_credential_seed_required")
    return json.loads(path.read_text())


def isolate(seed, directory):
    """Only copy the admin seed; never duplicate another attempt's team records."""
    value = _read_private(seed)
    directory = Path(directory)
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    if directory.is_symlink() or directory.stat().st_mode & 0o077:
        raise ValueError("private_attempt_credential_directory_required")
    path = directory / (secrets.token_hex(16) + ".json")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as stream:
        json.dump({"admin": value["admin"]}, stream, indent=2)
        stream.write("\n")
    return path


def record_initial_admin_change(seed, admin):
    """Bootstrap only: preserve all seed fields under a lock; teams use copies."""
    seed = Path(seed)
    _read_private(seed)
    fd = os.open(seed, os.O_RDWR | os.O_NOFOLLOW)
    with os.fdopen(fd, "r+") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        value = json.load(stream)
        if value["admin"]["email"] != admin["email"]:
            raise ValueError("administrator_seed_identity_changed")
        value["admin"] = admin
        stream.seek(0)
        stream.truncate()
        json.dump(value, stream, indent=2)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
