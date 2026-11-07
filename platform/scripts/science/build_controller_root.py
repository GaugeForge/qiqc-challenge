"""E2B-only reproduction of reviewed controller-Python Docker stage.

Never execute this on VPS. Copies only stdlib + interpreter runtime libraries;
no benchmark package, hidden material or submitted code enters the chroot.
"""

import re
import shutil
import stat
import subprocess
import sys
import sysconfig
import os
from pathlib import Path

if not Path("/opt/runtime/qiqcbench").is_dir() or Path("/root/QQ-Challenge").exists():
    raise RuntimeError(
        "controller root preparation is restricted to the provisioned E2B sandbox"
    )

root = Path("/controller_runner_root")
if root.exists():
    shutil.rmtree(root)
stdlib = Path(sysconfig.get_path("stdlib"))
target_stdlib = root / f"usr/local/lib/python{sys.version_info.major}.{sys.version_info.minor}"
shutil.copytree(
    stdlib,
    target_stdlib,
    ignore=shutil.ignore_patterns(
        "site-packages", "__pycache__", "_socket*.so", "_ssl*.so", "_ctypes*.so"
    ),
)
python = root / "usr/local/bin/python"
python.parent.mkdir(parents=True, exist_ok=True)
shutil.copy2(Path(sys.executable).resolve(), python)
for library in Path(sys.base_prefix).joinpath('lib').glob('libpython*.so*'):
    if library.is_file():
        shutil.copy2(library.resolve(), root / 'usr/local/lib' / library.name)
# These extensions are deliberately absent from the submitted-controller
# chroot.  They are excluded by copytree above, and must also be excluded from
# the dependency walk below; otherwise ldd would copy them back into the root
# and make the isolation check fail deterministically.
for binary in [
    Path(sys.executable).resolve(),
    *stdlib.joinpath("lib-dynload").glob("*.so"),
]:
    if binary.name.startswith(("_socket", "_ssl", "_ctypes")):
        continue
    result = subprocess.run(
        ["ldd", str(binary)], capture_output=True, text=True, check=False
    )
    for lib in re.findall(r"(/[A-Za-z0-9_./+\-]+)", result.stdout):
        source = Path(lib)
        if source.is_file():
            destination = root / source.relative_to("/")
            destination.parent.mkdir(parents=True, exist_ok=True)
            if not destination.exists():
                shutil.copy2(source, destination)
            if source.is_relative_to(Path(sys.base_prefix) / 'lib'):
                relocated = root / 'usr/local/lib' / source.relative_to(Path(sys.base_prefix) / 'lib')
                relocated.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, relocated)
for name in ("tmp", "work"):
    path = root / name
    path.mkdir(exist_ok=True)
    path.chmod(0o1777)
# Python needs a kernel-provided random source before it can start.  Keep the
# controller chroot self-contained with only the standard character devices;
# do not copy or bind-mount the host's /dev tree.
dev = root / "dev"
dev.mkdir(exist_ok=True)
for name, major, minor in (("null", 1, 3), ("zero", 1, 5), ("random", 1, 8), ("urandom", 1, 9)):
    path = dev / name
    if not path.exists():
        os.mknod(path, stat.S_IFCHR | 0o666, os.makedev(major, minor))
for name in ("passwd", "group", "ld.so.cache"):
    source = Path("/etc") / name
    target = root / "etc" / name
    target.parent.mkdir(exist_ok=True)
    if source.exists():
        shutil.copy2(source, target)
subprocess.run([shutil.which('chroot') or '/usr/sbin/chroot', '--userspec=65534:65534', str(root), '/usr/local/bin/python', '-I', '-c',
                'import importlib.util; specs={name:importlib.util.find_spec(name) for name in ("socket","_socket","_ctypes","qiqcbench")}; assert specs["socket"] is not None; assert all(specs[name] is None or specs[name].origin == "built-in" for name in ("_socket","_ctypes")); assert specs["qiqcbench"] is None'], check=True, capture_output=True)
print("controller-python-root-created")
