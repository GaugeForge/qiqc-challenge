"""Run inside a reviewed E2B main sandbox after runtime egress restriction.

Each fixed network attempt runs in a disposable child process. The parent
kills its process group at the deadline, including native DNS stalls. A timeout is
only a no-response smoke observation, never proof of complete network safety.
"""

import json
import os
import signal
import re
import subprocess
import sys
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

TARGETS = {
    "public_internet": "https://example.com",
    "e2b_management": "https://api.e2b.dev",
}


def inaccessible(path):
    try:
        return not Path(path).exists()
    except PermissionError:
        return True


def child_network_probe(name):
    # Only fixed organizer destinations. No task or contestant-controlled URL.
    url = TARGETS[name]
    try:
        with urlopen(Request(url), timeout=6) as response:
            return {"status": response.status, "transport": "response"}
    except HTTPError as exc:
        return {"status": exc.code, "transport": "response"}
    except (URLError, TimeoutError, OSError) as exc:
        return {"transport": type(exc).__name__}


def run_fixed_child(command, *, timeout_seconds):
    """The command is organizer code; this helper is invoked only in E2B/tests.

    Python signal handlers can wait for a native resolver to return. A separate
    parent can SIGKILL the entire child process group without that limitation.
    """
    with subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    ) as child:
        try:
            stdout, _ = child.communicate(timeout=timeout_seconds)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(child.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            child.communicate(timeout=2)
            return {"transport": "BoundaryProbeTimeout"}
        if child.returncode != 0 or len(stdout) > 4096:
            raise RuntimeError("boundary_probe_child_failed")
        value = json.loads(stdout)
        if not isinstance(value, dict) or set(value) - {"status", "transport"}:
            raise RuntimeError("boundary_probe_invalid_child_result")
        return value


def probe_url(url, *, timeout_seconds=6):
    if not 0 < timeout_seconds <= 6:
        raise ValueError("invalid fixed probe deadline")
    names = [name for name, target in TARGETS.items() if target == url]
    if len(names) != 1:
        raise ValueError("unknown fixed probe target")
    return run_fixed_child(
        [sys.executable, str(Path(__file__).resolve()), "--network-child", names[0]],
        timeout_seconds=timeout_seconds,
    )


def runtime_public_only(task_id, root=Path("/opt/runtime")):
    """Inspect names only, never read scientific payloads or private contents."""
    if not re.fullmatch(r"[a-z][a-z0-9_]{0,99}", task_id):
        raise ValueError("invalid task identity")
    if not root.exists():
        return True
    count = 0
    for directory, dirs, files in os.walk(root, followlinks=False):
        for name in dirs + files:
            path = Path(directory) / name
            if path.is_symlink():
                return False
            relative = path.relative_to(root).as_posix()
            count += 1
            if count > 4096:
                return False
            allowed_dirs = {"configs", "configs/task_materials", "configs/task_materials/" + task_id,
                            "harbor_tasks", "harbor_tasks/" + task_id}
            public_prefix = "configs/task_materials/" + task_id + "/public"
            allowed = (relative in allowed_dirs or relative == public_prefix or relative.startswith(public_prefix + "/")
                       or relative == "harbor_tasks/" + task_id + "/instruction.md"
                       or relative in {"benchmark-release.json", "deployment-manifest.json"})
            if not allowed:
                return False
    return True


def collect(task_id="tunable_coupler_cz_netzero"):
    result = {}
    result["private_paths_absent"] = runtime_public_only(task_id) and all(
        inaccessible(p)
        for p in [
            "/qsim_logs/private",
            "/tests/score_cz.py",
            "/logs/verifier/reward.txt",
            "/root/QQ-Challenge",
            "/probe.py",
            "/opt/probe.py",
        ]
    )
    result["master_credentials_absent"] = all(
        not os.environ.get(k)
        for k in [
            "E2B_API_KEY",
            "HY_API",
            "R2_SECRET_ACCESS_KEY",
            "AWS_SECRET_ACCESS_KEY",
        ]
    )
    result["qsim_remote_capability_absent"] = inaccessible("/home/user/qsim.json")
    for name, url in [
        ("public_internet", "https://example.com"),
        ("e2b_management", "https://api.e2b.dev"),
    ]:
        result[name] = probe_url(url)
    return result


if __name__ == "__main__":
    if (
        len(sys.argv) == 3
        and sys.argv[1] == "--network-child"
        and sys.argv[2] in TARGETS
    ):
        print(json.dumps(child_network_probe(sys.argv[2])))
    elif len(sys.argv) == 1:
        print(json.dumps(collect()))
    elif len(sys.argv) == 3 and sys.argv[1] == "--task":
        print(json.dumps(collect(sys.argv[2])))
    else:
        raise SystemExit("invalid fixed probe arguments")
