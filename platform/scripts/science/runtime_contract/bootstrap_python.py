"""Trusted E2B-only interpreter preparation; never import scientific code here."""
import hashlib
import io
import json
import os
import platform
import subprocess
import urllib.request
import zipfile
from pathlib import Path

PYTHON_VERSION = "3.11.14"
UV_VERSION = "0.12.24"
UV_URL = "https://files.pythonhosted.org/packages/89/f8/2ef356d9d066686c98854be9e3e41d0ba9d69f7b2495512b6394115fa40c/uv-0.12.24-py3-none-manylinux_2_17_x86_64.manylinux2014_x86_64.whl"
UV_SHA256 = "e47005957dca320272ad68960b591b854b6bf0c3288368efb096d9ccd06b8404"
UV = "/opt/qiqc-bootstrap/uv"
PYTHON = "/opt/qiqc-python/bin/python"


def main():
    if not Path('/opt/runtime').is_dir() or Path('/root/QQ-Challenge').exists():
        raise RuntimeError('Interpreter preparation is restricted to E2B')
    if platform.system() != 'Linux' or platform.machine() != 'x86_64':
        raise RuntimeError('Runtime contract requires Linux x86_64')
    with urllib.request.urlopen(UV_URL, timeout=60) as response:
        wheel = response.read(32 * 1024 * 1024 + 1)
    if len(wheel) > 32 * 1024 * 1024 or hashlib.sha256(wheel).hexdigest() != UV_SHA256:
        raise RuntimeError('Interpreter bootstrap checksum mismatch')
    with zipfile.ZipFile(io.BytesIO(wheel)) as archive:
        binary = archive.read(f'uv-{UV_VERSION}.data/scripts/uv')
    target = Path(UV)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(binary)
    target.chmod(0o755)
    env = dict(os.environ, UV_PYTHON_INSTALL_DIR='/opt/qiqc-managed-python',
               UV_PYTHON_DOWNLOADS='automatic', UV_DEFAULT_INDEX='https://pypi.org/simple')
    subprocess.run([UV, 'python', 'install', PYTHON_VERSION], env=env, check=True)
    subprocess.run([UV, 'venv', '/opt/qiqc-python', '--python', PYTHON_VERSION,
                    '--managed-python', '--seed'], env=env, check=True)
    version = subprocess.check_output([PYTHON, '-c', 'import platform;print(platform.python_version())'], text=True).strip()
    if version != PYTHON_VERSION:
        raise RuntimeError('Interpreter version mismatch')
    Path('/opt/qiqc-bootstrap/python-origin.json').write_text(json.dumps({
        'python_version': version, 'python_binary_sha256': hashlib.sha256(Path(PYTHON).resolve().read_bytes()).hexdigest(),
        'python_source': 'uv managed python-build-standalone; uv verifies release download checksum',
        'uv_version': UV_VERSION, 'uv_wheel_url': UV_URL, 'uv_wheel_sha256': UV_SHA256,
        'system': platform.platform(), 'architecture': platform.machine(),
    }, sort_keys=True))


if __name__ == '__main__':
    main()
