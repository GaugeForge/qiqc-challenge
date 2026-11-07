"""Static task-data boundaries; never import task code or read hidden payloads.

These candidate data manifests are not generic runnable Harbor environments.
Dynamic code dependencies and target-sandbox isolation require separate tests.
"""

from __future__ import annotations

import json
import os
import re
import stat
from contextlib import contextmanager
from pathlib import Path, PurePosixPath

TASK_ID = re.compile(r"[a-z][a-z0-9_]{0,99}\Z")
MAX_FILE = 64 * 1024 * 1024

# Reviewed against upstream 97be24cda7f54766ebd8ec6c0d00392319f5e252:
# exact task, device and private physical input (never the whole hidden folder).
# Candidate builder additionally requires matching literal Compose declaration.
REVIEWED_QSIM_PRIVATE_INPUTS = {
    "distributed_nv_weighted_field_metrology": (
        "nv_network_8_v2",
        "configs/task_materials/distributed_nv_weighted_field_metrology/hidden/nv_network_8_v2.hidden.generated.yaml",
    ),
    "nonmarkovian_ghz_frequency_metrology": (
        "trapped_ion_16q_v0",
        "configs/task_materials/nonmarkovian_ghz_frequency_metrology/hidden/trapped_ion_16q_v0.hidden.generated.yaml",
    ),
    "structured_control_margin": (
        "spin_chain_control_v0",
        "configs/task_materials/structured_control_margin/hidden/hidden_device.json",
    ),
}


# Public device notebooks and qtype instructions loaded by the reviewed
# public_surface.py implementations. Exact device-to-qtype bindings prevent
# granting access to another device's notebook or the entire qtype catalog.
REVIEWED_DEVICE_PUBLIC_SURFACES = {
    "binomial_cavity_v0": "bosonic_cavity_qec",
    "gkp_cavity_v0": "bosonic_cavity_qec",
    "neutral_atom_dm_14q_ftbell_v1": "neutral_atom_dm_processor",
    "rydberg_bell513_v0": "neutral_atom_dm_processor",
    "rydberg_erasure_repair_v0": "neutral_atom_logical_processor",
    "rydberg_steane_v0": "neutral_atom_logical_processor",
}


def public_surface_inputs(device):
    qtype = REVIEWED_DEVICE_PUBLIC_SURFACES.get(device)
    if qtype is None:
        return ()
    return (f"configs/devices/{device}.notebook.yaml",
            f"configs/qtypes/{qtype}.agent_surface.yaml")


class ManifestError(ValueError):
    """A fixed code is safe to record; never include payload text in failures."""

    def __init__(self, code):
        self.code = code
        super().__init__(code)


def canonical_relative(value):
    if not isinstance(value, str) or not value or len(value) > 512:
        raise ManifestError("invalid_path")
    p = PurePosixPath(value)
    if (
        p.is_absolute()
        or "\\" in value
        or ":" in value
        or any(ord(c) < 32 for c in value)
    ):
        raise ManifestError("invalid_path")
    if any(part in ("", ".", "..") for part in value.split("/")):
        raise ManifestError("noncanonical_path")
    return p


@contextmanager
def secure_open(root: Path, relative: str):
    """Descriptor-relative traversal rejects symlinks in every component.

    O_NONBLOCK avoids hanging when an attacker swaps a file for a FIFO. Reading
    through the opened descriptor avoids a second path lookup during packaging.
    """
    parts = canonical_relative(relative).parts
    descriptors = []
    try:
        absolute_root = Path(root).absolute()
        current = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        descriptors.append(current)
        for part in absolute_root.parts[1:]:
            if part in (".", ".."):
                raise ManifestError("noncanonical_root")
            current = os.open(
                part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=current
            )
            descriptors.append(current)
        for part in parts[:-1]:
            current = os.open(
                part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=current
            )
            descriptors.append(current)
        fd = os.open(
            parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=current
        )
        descriptors.append(fd)
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise ManifestError("nonregular_input")
        if info.st_nlink != 1:
            raise ManifestError("hardlinked_input")
        if info.st_size > MAX_FILE:
            raise ManifestError("oversized_input")
        yield fd, info
    except OSError as exc:
        raise ManifestError("missing_or_linked_input") from exc
    finally:
        for fd in reversed(descriptors):
            os.close(fd)


def file_metadata(root, relative):
    # fstat only: hidden payload bytes are not read by the catalog audit.
    with secure_open(root, relative) as (_, info):
        return {"path": relative, "size_bytes": info.st_size}


def secure_read(root, relative):
    with secure_open(root, relative) as (fd, before):
        chunks = []
        size = 0
        while True:
            data = os.read(fd, 1024 * 1024)
            if not data:
                break
            size += len(data)
            if size > MAX_FILE:
                raise ManifestError("oversized_input")
            chunks.append(data)
        after = os.fstat(fd)
        if (
            size != before.st_size
            or after.st_mtime_ns != before.st_mtime_ns
            or after.st_ctime_ns != before.st_ctime_ns
        ):
            raise ManifestError("input_changed_during_read")
        return b"".join(chunks)


def identity(root, task):
    if not TASK_ID.fullmatch(task):
        raise ManifestError("invalid_task_id")
    relative = f"configs/tasks/{task}.yaml"
    values = {}
    # Read only the top-level identity header, not task descriptions or truth.
    with (
        secure_open(root, relative) as (fd, _),
        os.fdopen(os.dup(fd), "r", encoding="utf-8") as stream,
    ):
        for index, line in enumerate(stream):
            if index >= 64 or len(line) > 2048:
                raise ManifestError("invalid_identity_header")
            matched = re.fullmatch(
                r"(task_id|device_id):\s*([a-z][a-z0-9_]*)\s*\n?", line
            )
            if matched:
                key, value = matched.groups()
                if key in values:
                    raise ManifestError("duplicate_identity")
                values[key] = value
            if len(values) == 2:
                break
    if values.get("task_id") != task or not TASK_ID.fullmatch(
        values.get("device_id", "")
    ):
        raise ManifestError("task_identity_mismatch")
    return values["device_id"]


def file_tree(root, directory):
    base = root / canonical_relative(directory)
    if base.is_symlink() or not base.is_dir():
        raise ManifestError("missing_or_linked_directory")
    result = []
    for current, directories, files in os.walk(base, followlinks=False):
        for name in directories:
            if (Path(current) / name).is_symlink():
                raise ManifestError("linked_directory")
        for name in files:
            relative = (Path(current) / name).relative_to(root).as_posix()
            file_metadata(root, relative)
            result.append(relative)
    return sorted(result)


def classify_entry(task, device, role, relative):
    """Allow exact role-owned data families, not filename denylist guesses."""
    canonical_relative(relative)
    public = f"configs/task_materials/{task}/public/"
    tests = f"harbor_tasks/{task}/tests/"
    hidden = f"configs/task_materials/{task}/hidden/"
    runtime_private = f"configs/task_materials/{task}/runtime/"
    if role == "main":
        return relative == f"harbor_tasks/{task}/instruction.md" or relative.startswith(
            public
        )
    configs = {
        f"configs/tasks/{task}.yaml",
        f"configs/devices/{device}.public.yaml",
        f"configs/devices/{device}.hidden.example.yaml",
        *public_surface_inputs(device),
    }
    if role == "qsim":
        reviewed = REVIEWED_QSIM_PRIVATE_INPUTS.get(task) == (device, relative)
        return (
            reviewed
            or relative in configs
            or relative.startswith((public, runtime_private))
        )
    if role == "verifier":
        return relative in configs or relative.startswith(
            (public, hidden, runtime_private, tests)
        )
    return False


def validate_entries(root, task, device, role, paths):
    if not TASK_ID.fullmatch(task) or not TASK_ID.fullmatch(device):
        raise ManifestError("invalid_identity")
    if identity(root, task) != device:
        raise ManifestError("device_binding_mismatch")
    seen = set()
    entries = []
    for relative in paths:
        if relative in seen:
            raise ManifestError("duplicate_entry")
        if not classify_entry(task, device, role, relative):
            raise ManifestError("role_boundary_violation")
        seen.add(relative)
        entries.append(file_metadata(root, relative))
    required = {
        "main": {f"harbor_tasks/{task}/instruction.md"},
        "qsim": {
            f"configs/tasks/{task}.yaml",
            f"configs/devices/{device}.public.yaml",
            f"configs/devices/{device}.hidden.example.yaml",
            *public_surface_inputs(device),
        },
        "verifier": {f"harbor_tasks/{task}/tests/test.sh"},
    }
    if role not in required or not required[role].issubset(seen):
        raise ManifestError("required_input_missing")
    return sorted(entries, key=lambda item: item["path"])


def write_report(path, report):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2) + "\n")
