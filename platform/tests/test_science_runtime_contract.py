"""Offline upstream-to-E2B dependency/client contract tests; no science import."""
import hashlib
import importlib.util
import json
import re
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / 'scripts/science'))
import runtime_contract as rc
from e2b_runtime import ROOT, RUNTIME
from task_release import load_release


def test_main_pins_preserved_and_no_hidden_inputs_or_science_source():
    requirements, clients, meta = rc.contract(ROOT, RUNTIME, 'main', 'tunable_coupler_cz_netzero')
    assert set(meta['original_direct_pins']) == set(rc.MAIN_PACKAGES)
    assert len(meta['original_direct_pins']) == 13
    assert len(clients) == 9
    assert '--hash=sha256:' in requirements.decode()
    assert all(path.startswith('/opt/qiqcbench-agent/') for path in [item['path'] for item in meta['clients']])
    assert not any('hidden' in path or '/qsim/' in path or '/configs/' in path for path in clients)
    assert all(path.endswith('.py') for path in clients)
    assert 'qiqc_qiskit/provider.py' in clients
    assert 'qiqc_qcodes/transmon.py' in clients


@pytest.mark.parametrize('role', ['qsim', 'verifier', 'catalog'])
def test_scientific_role_uses_exact_frozen_hash_lock(role):
    requirements, clients, meta = rc.contract(ROOT, RUNTIME, role, 'tunable_coupler_cz_netzero')
    assert requirements == (RUNTIME / 'docker/qsim/requirements.txt').read_bytes()
    assert len(re.findall(rb'^[A-Za-z0-9_.-]+==', requirements, re.M)) == 73
    assert not clients
    assert meta['requirements_sha256'] == hashlib.sha256(requirements).hexdigest()


def test_hamlearn_uses_its_own_minimal_image_contract():
    requirements, clients, meta = rc.contract(ROOT, RUNTIME, 'verifier', 'time_budgeted_hamlearn_10q')
    assert requirements == (RUNTIME / 'harbor_tasks/time_budgeted_hamlearn_10q/tests/requirements.txt').read_bytes()
    assert meta['source_dockerfile'] == 'harbor_tasks/time_budgeted_hamlearn_10q/tests/Dockerfile'
    assert not clients


def test_independent_original_repo_hashes_when_checkout_available():
    upstream = ROOT / 'benchmark-assets/qiqcbench/QIQCBench'
    if not upstream.is_dir():
        pytest.skip('Original private checkout removed; retained release remains hash verified')
    _, clients, _ = rc.contract(ROOT, RUNTIME, 'main')
    for relative in ['docker/agent/Dockerfile', 'docker/qsim/Dockerfile', 'docker/qsim/requirements.txt']:
        assert (upstream / relative).read_bytes() == (RUNTIME / relative).read_bytes()
    for relative, payload in clients.items():
        assert (upstream / 'docker/agent/lib' / relative).read_bytes() == payload


def test_reject_main_lock_changing_upstream_pin(tmp_path, monkeypatch):
    bad = (rc.ASSETS / 'main-requirements.txt').read_text().replace('numpy==2.4.6', 'numpy==2.4.4')
    (tmp_path / 'main-requirements.txt').write_text(bad)
    monkeypatch.setattr(rc, 'ASSETS', tmp_path)
    with pytest.raises(ValueError, match='changed upstream direct'):
        rc.contract(ROOT, RUNTIME, 'main')


@pytest.mark.parametrize('role', ['main', 'qsim', 'verifier'])
def test_install_prepares_interpreter_before_hash_install_and_keeps_clients_main_only(role):
    commands, writes = [], {}
    sb = SimpleNamespace(commands=SimpleNamespace(run=lambda command, **kw: commands.append((command,kw))),
                         files=SimpleNamespace(write=lambda name,data,**kw:writes.update({name:data})))
    rc.install(sb, ROOT, RUNTIME, role=role, task_id='tunable_coupler_cz_netzero')
    assert commands[0][0] == 'python3 /root/qiqc-bootstrap-python.py'
    installer = next(command for command,_ in commands if ' pip install ' in command)
    assert '--require-hashes' in installer and '--no-build' in installer and '--no-deps' in installer
    assert '--python /opt/qiqc-python/bin/python' in installer
    assert any(' pip check ' in command for command,_ in commands)
    assert all(kw['user'] == 'root' for _,kw in commands)
    assert ('/opt/qiqcbench-agent/qiqc_qiskit/provider.py' in writes) == (role=='main')
    assert not any('/hidden/' in name or '/configs/' in name for name in writes)
    assert '/opt/runtime-environment.json' in writes['/root/qiqc-verify-runtime.py']


def test_bootstrap_is_pinned_and_cannot_run_on_workspace():
    spec = importlib.util.spec_from_file_location('qiqc_bootstrap_test', rc.ASSETS / 'bootstrap_python.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.PYTHON_VERSION == '3.11.14'
    assert module.UV_VERSION == '0.12.24'
    assert re.fullmatch('[a-f0-9]{64}', module.UV_SHA256)
    with pytest.raises(RuntimeError, match='restricted to E2B'):
        module.main()


def test_no_network_ffi_in_controller_and_normalized_python_layout():
    script = (rc.ASSETS.parent / 'build_controller_root.py').read_text()
    assert 'usr/local/lib/python' in script
    assert 'libpython*.so*' in script
    assert all(name in script for name in ['_socket*.so', '_ssl*.so', '_ctypes*.so'])
    assert "'--userspec=65534:65534'" in script
    femoco = (rc.ASSETS.parent / 'build_femoco_root.py').read_text()
    assert "packages = target_stdlib / 'site-packages'" in femoco
