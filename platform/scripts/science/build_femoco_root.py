"""Reproduce the frozen FeMoco verifier's isolated scientific Python in E2B.

The chroot contains only Python, NumPy and SciPy, never benchmark code or truth.
Network access is restricted by the enclosing verifier sandbox before scoring.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import sysconfig
from pathlib import Path

PACKAGES = ("numpy==2.4.4", "scipy==1.17.1")


def main():
    if not Path("/opt/runtime/qiqcbench").is_dir() or Path("/root/QQ-Challenge").exists():
        raise RuntimeError("isolated runner preparation requires an E2B runtime")
    root = Path("/runner_root")
    if root.exists():
        shutil.rmtree(root)
    stdlib = Path(sysconfig.get_path("stdlib"))
    target_stdlib = root / f"usr/local/lib/python{sys.version_info.major}.{sys.version_info.minor}"
    shutil.copytree(stdlib, target_stdlib, ignore=shutil.ignore_patterns("site-packages", "dist-packages", "__pycache__"))
    python = root / "usr/local/bin/python"
    python.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(Path(sys.executable).resolve(), python)
    for library in Path(sys.base_prefix).joinpath('lib').glob('libpython*.so*'):
        if library.is_file():
            shutil.copy2(library.resolve(), root / 'usr/local/lib' / library.name)
    packages = target_stdlib / 'site-packages'
    subprocess.run([sys.executable, "-m", "pip", "install", "--quiet", "--no-deps", "--only-binary=:all:", "--target", str(packages), *PACKAGES], check=True)
    # Only the system libraries actually needed by this isolated interpreter
    # and these two wheel distributions enter the root.
    for binary in [Path(sys.executable).resolve(), *stdlib.joinpath("lib-dynload").glob("*.so"), *packages.rglob("*.so*")]:
        result = subprocess.run(["ldd", str(binary)], capture_output=True, text=True, check=False)
        for name in re.findall(r"(/[A-Za-z0-9_./+\-]+)", result.stdout):
            source = Path(name)
            if not source.is_file() or source.is_relative_to(root):
                continue
            target = root / source.relative_to("/")
            target.parent.mkdir(parents=True, exist_ok=True)
            if not target.exists():
                shutil.copy2(source, target)
            if source.is_relative_to(Path(sys.base_prefix) / 'lib'):
                relocated = root / 'usr/local/lib' / source.relative_to(Path(sys.base_prefix) / 'lib')
                relocated.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, relocated)
    for name in ("tmp", "work"):
        path = root / name
        path.mkdir(exist_ok=True)
        path.chmod(0o1777)
    for name in ("passwd", "group", "ld.so.cache"):
        source = Path("/etc") / name
        target = root / "etc" / name
        target.parent.mkdir(exist_ok=True)
        if source.exists():
            shutil.copy2(source, target)
    if any(root.rglob("qiqcbench")):
        raise RuntimeError("benchmark package entered isolated runner")
    # A trusted import probe runs inside the unprivileged chroot. No submitted
    # program is used during preparation.
    chroot = shutil.which("chroot")
    if chroot is None:
        raise RuntimeError("isolated runner requires chroot")
    result = subprocess.run([chroot, "--userspec=65534:65534", str(root), "/usr/local/bin/python", "-I", "-c", "import json,numpy,scipy; print(json.dumps({'numpy':numpy.__version__,'scipy':scipy.__version__}))"], check=True, capture_output=True, text=True, env={"OPENBLAS_NUM_THREADS":"1", "OMP_NUM_THREADS":"1"})
    versions = json.loads(result.stdout)
    if versions != {"numpy": "2.4.4", "scipy": "1.17.1"}:
        raise RuntimeError("isolated runner dependency version mismatch")
    print(json.dumps({"isolated_runner_ready": True, "packages": versions, "benchmark_package_present": False}))


if __name__ == "__main__":
    main()
