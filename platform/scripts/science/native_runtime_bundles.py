"""Authenticate prepared CLI bundles; never execute their contents on the VPS."""

import hashlib
import json
import re
from pathlib import Path

SCHEMA = "qiqc-native-runtime-bundles/v1"
PACKAGES = {
    "deepseek-harness": ("@deepseek-ai/dsh", "0.2.1-rc.2"),
    "pi-agent": ("@earendil-works/pi-coding-agent", "1.3.0"),
}
RELATIVE_MANIFEST = "competition-platform/adapters/native-runtime-bundles.json"


def read_manifest(path):
    path = Path(path)
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 65536:
        raise ValueError("native_runtime_manifest_unavailable")
    value = json.loads(path.read_bytes())
    if value.get("schema") != SCHEMA or set(value.get("packages", {})) != set(PACKAGES):
        raise ValueError("native_runtime_manifest_invalid")
    for kind, (package, version) in PACKAGES.items():
        row = value["packages"][kind]
        if (
            row.get("package") != package
            or row.get("version") != version
            or row.get("node_version") != "v24.9.0"
            or type(row.get("bytes")) is not int
            or not 0 < row["bytes"] <= 512 * 1024 * 1024
            or any(not re.fullmatch(r"[a-f0-9]{64}", str(row.get(k, "")))
                   for k in ("sha256", "lock_sha256"))
        ):
            raise ValueError("native_runtime_manifest_invalid")
    return value


def authenticated_bundle(kind, manifest_path, bundle_dir):
    if kind not in PACKAGES:
        raise ValueError("native_runtime_kind_unsupported")
    row = read_manifest(manifest_path)["packages"][kind]
    path = Path(bundle_dir) / (kind + ".tar.gz")
    if path.is_symlink() or not path.is_file() or path.stat().st_size != row["bytes"]:
        raise ValueError("native_runtime_bundle_unavailable")
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    if digest.hexdigest() != row["sha256"]:
        raise ValueError("native_runtime_bundle_changed")
    return path
