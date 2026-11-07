"""Bind browser imports to deployed science and freeze local driver dependencies."""
from __future__ import annotations
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile

DRIVER_FILES = ('cloud_browser_check.py', 'browser_credentials.py', 'browser_upload.py', 'browser_candidate_runtime.py')


def candidate_science(config_path):
    config = json.loads(Path(config_path).read_text())
    root, project = Path(config['candidate_root']), Path(config['project_root'])
    if (not root.is_absolute() or root.is_symlink() or not project.is_absolute()
            or project.is_symlink() or not project.resolve().is_relative_to(root.resolve())):
        raise ValueError('browser_candidate_project_invalid')
    science = project / 'competition-platform/platform/scripts/science'
    for name in ('native_candidate.py', 'native_controller.py', 'verify_native_candidate_evidence.py'):
        target = science / name
        if target.is_symlink() or not target.is_file():
            raise ValueError('browser_deployed_science_missing')
    return science


def bind_candidate_imports(config_path):
    science = candidate_science(config_path)
    # Refuse a mixed interpreter rather than silently reusing a workspace module.
    for name in ('native_candidate', 'native_controller', 'native_contract'):
        module = sys.modules.get(name)
        if module is not None and Path(module.__file__).resolve().parent != science.resolve():
            raise ValueError('browser_science_module_already_loaded_from_other_tree')
    sys.path.insert(0, str(science.parent))
    sys.path.insert(0, str(science))
    return science


def verify_driver_snapshot(driver):
    driver = Path(driver)
    manifest_path = driver.parent / 'browser-driver-manifest.json'
    if not manifest_path.exists():
        return None
    manifest = json.loads(manifest_path.read_text())
    files = manifest.get('files', {})
    if set(files) != set(DRIVER_FILES):
        raise ValueError('browser_driver_manifest_invalid')
    digest = hashlib.sha256(json.dumps(files, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    if driver.parent.name != digest:
        raise ValueError('browser_driver_manifest_identity_mismatch')
    for name, expected in files.items():
        path = driver.parent / name
        if path.is_symlink() or not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            raise ValueError('browser_driver_dependency_changed')
    return files


def snapshot_driver(source, directory):
    source, directory = Path(source), Path(directory)
    payload = {}
    for name in DRIVER_FILES:
        path = source / name
        if path.is_symlink() or not path.is_file():
            raise ValueError('browser_driver_dependency_missing')
        payload[name] = path.read_bytes()
    hashes = {name: hashlib.sha256(data).hexdigest() for name, data in payload.items()}
    digest = hashlib.sha256(json.dumps(hashes, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    base = directory / '.browser-driver'
    base.mkdir(parents=True, exist_ok=True)
    target = base / digest
    if not target.exists():
        temporary = Path(tempfile.mkdtemp(prefix='.preparing-', dir=base))
        try:
            for name, data in payload.items():
                (temporary / name).write_bytes(data)
                (temporary / name).chmod(0o444)
            (temporary / 'browser-driver-manifest.json').write_text(json.dumps({'schema': 'qiqc-browser-driver/v1', 'files': hashes}, indent=2)+'\n')
            (temporary / 'browser-driver-manifest.json').chmod(0o444)
            try:
                temporary.rename(target)
            except OSError:
                if not target.is_dir():
                    raise
        finally:
            if temporary.exists():
                shutil.rmtree(temporary)
    driver = target / 'cloud_browser_check.py'
    verify_driver_snapshot(driver)
    return driver


def candidate_environment(config):
    env = dict(os.environ)
    env.pop('PYTHONPATH', None)
    env['PYTHONDONTWRITEBYTECODE'] = '1'
    env['QIQC_PROJECT_ROOT'] = config['project_root']
    return env
