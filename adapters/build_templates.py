"""Build three minimal-manifest ZIPs deterministically from reviewed local adapter source.
Does not install dependencies or execute a harness. The runtime must already
exist in the isolated execution image; compilation here checks syntax only.
"""

import hashlib
import json
from pathlib import Path
import zipfile

ROOT = Path(__file__).resolve().parent
# Download name -> runtime written in the minimal harness.json. Everything else
# (package, supported version window, model interfaces, transport, trajectory) is derived
# by the platform's trusted parser, platform/qiqc/archive.py.
RUNTIMES = {"codex": "codex", "deepseek": "deepseek", "pi": "pi"}
README = (
    b"# QIQC starter harness\n\n"
    b"adapter/run.sh starts harness/runtime.py, which runs the native CLI named by\n"
    b"harness.json inside the isolated task container. There is no HTTP fallback.\n\n"
    b"harness.json only needs the runtime, for example {\"runtime\": \"codex\"}.\n"
    b"Allowed runtimes: codex, deepseek, pi. Optional fields: entrypoint (default\n"
    b"adapter/run.sh), name and description. The platform checks the supported runtime\n"
    b"version window and protocol and numbers your uploads v1, v2, ... automatically.\n"
)


def normalizer():
    import importlib.util

    path = ROOT.parent / "platform" / "qiqc" / "archive.py"
    spec = importlib.util.spec_from_file_location("qiqc_trusted_archive", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.normalize_manifest


def build():
    runtime = (ROOT / "runtime.py").read_bytes()
    compile(runtime, "harness/runtime.py", "exec")
    qsim = (ROOT / "qsim.py").read_bytes()
    compile(qsim, "harness/qsim.py", "exec")
    terminal = (ROOT / "terminal.py").read_bytes()
    compile(terminal, "harness/terminal.py", "exec")
    records = {}
    normalize = normalizer()
    for name, runtime_id in RUNTIMES.items():
        manifest = {"runtime": runtime_id}
        payload = {
            "harness/runtime.py": runtime,
            "harness/qsim.py": qsim,
            "harness/terminal.py": terminal,
            "harness/README.md": README,
            "adapter/run.sh": b'#!/bin/sh\nset -eu\nexec python3 "$(dirname "$0")/../harness/runtime.py"\n',
            "harness.json": (json.dumps(manifest, indent=2) + "\n").encode(),
        }
        target = ROOT / name / "harness.zip"
        target.parent.mkdir(exist_ok=True)
        with zipfile.ZipFile(target, "w") as z:
            for key, value in sorted(payload.items()):
                i = zipfile.ZipInfo(key, (2026, 10, 21, 0, 0, 0))
                i.external_attr = 0o100644 << 16
                i.compress_type = zipfile.ZIP_DEFLATED
                z.writestr(i, value)
        records[name] = {
            "sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
            "manifest": manifest,
            "derived": normalize(manifest),
        }
    (ROOT / "manifest.json").write_text(
        json.dumps(
            {
                "schema": "qiqc-harness/v1",
                "packages": records,
                "scope": "Source packages. Native image runtime availability and new ZIP cloud execution require separate verification.",
            },
            indent=2,
        )
        + "\n"
    )


if __name__ == "__main__":
    build()
