import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
from unittest import mock
import pytest

spec = importlib.util.spec_from_file_location('qiqc_operator_operations', Path(__file__).with_name('operations.py'))
ops = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ops)

@pytest.fixture
def state(tmp_path, monkeypatch):
    monkeypatch.setattr(ops, 'STATE', tmp_path)
    return tmp_path

def manifest(tmp_path, **extra):
    return {'label': 'test', 'argv': ['/usr/bin/python3', '-c', 'print("ok")'], 'cwd': str(tmp_path), 'timeout_seconds': 3, **extra}

def queued(state, **extra):
    identifier = 'a' * 32
    folder = ops.job_dir(identifier)
    ops.atomic_json(folder / 'manifest.json', manifest(state, **extra))
    ops.atomic_json(folder / 'status.json', {'job_id': identifier, 'state': 'queued', 'boot_id': ops.boot_id()})
    return identifier, folder

@pytest.mark.parametrize('change', [
    {'argv': 'touch /tmp/no'}, {'argv': []}, {'argv': ['python3']}, {'argv': ['/bin/echo', '\0']},
    {'cwd': 'relative'}, {'timeout_seconds': 0}, {'timeout_seconds': True}, {'label': 'bad\nlabel'},
    {'unexpected': 'field'}, {'environment_file': 'relative'},
])
def test_reject_manifest(state, change):
    with pytest.raises(ValueError): ops.validate_manifest(manifest(state, **change))

def test_private_file_permissions_and_symlink(state):
    source = state / 'secret'
    source.write_text('not-for-output')
    source.chmod(0o644)
    with pytest.raises(ValueError): ops.private_file(source)
    source.chmod(0o600)
    assert ops.private_file(source) == source
    (state / 'link').symlink_to(source)
    with pytest.raises(ValueError): ops.private_file(state / 'link')

def test_atomic_receipt_private(state):
    p = state / 'jobs' / 'result.json'
    ops.atomic_json(p, {'x': 1})
    assert p.stat().st_mode & 0o777 == 0o600
    assert p.parent.stat().st_mode & 0o777 == 0o700
    ops.atomic_json(p, {'x': 2})
    assert json.loads(p.read_text()) == {'x': 2}

def test_env_no_shell_evaluation(state):
    p = state / 'env'; p.write_text("TOKEN='$(touch /tmp/should-not-exist)'\nOTHER=hello\n"); p.chmod(0o600)
    env = ops.load_environment(str(p))
    assert env['TOKEN'] == '$(touch /tmp/should-not-exist)'
    assert env['OTHER'] == 'hello'

def test_success_and_no_reexecution(state):
    identifier, folder = queued(state)
    result = ops.run_job(identifier)
    assert result['state'] == 'succeeded'
    assert result['exit_code'] == 0
    assert (folder / 'output.log').read_text() == 'ok\n'
    assert (folder / 'output.log').stat().st_mode & 0o777 == 0o600
    with pytest.raises(RuntimeError): ops.run_job(identifier)

def test_failure(state):
    identifier, _ = queued(state, argv=['/usr/bin/python3', '-c', 'raise SystemExit(7)'])
    result = ops.run_job(identifier)
    assert result['state'] == 'failed' and result['exit_code'] == 7

def test_timeout(state):
    identifier, _ = queued(state, argv=['/usr/bin/python3', '-c', 'import time; time.sleep(20)'], timeout_seconds=1)
    assert ops.run_job(identifier)['state'] == 'timed_out'

def test_old_boot_job_never_runs(state):
    identifier, folder = queued(state)
    ops.atomic_json(folder / 'status.json', {'job_id': identifier, 'state': 'queued', 'boot_id': 'old-boot'})
    with pytest.raises(RuntimeError): ops.run_job(identifier)
    with mock.patch.object(ops.subprocess, 'run', side_effect=AssertionError('must not restart')):
        assert ops.reconcile_jobs() == {'interrupted_jobs': [identifier], 'automatic_reruns': 0}
    assert json.loads((folder / 'status.json').read_text())['state'] == 'interrupted'

def test_current_boot_job_not_interrupted(state):
    identifier, folder = queued(state)
    assert ops.reconcile_jobs()['interrupted_jobs'] == []
    assert json.loads((folder / 'status.json').read_text())['state'] == 'queued'

def test_bad_job_ids(state):
    for value in ('../secret', '; reboot', '123', 'A' * 32):
        with pytest.raises(ValueError): ops.job_dir(value)

def database(state):
    path = state / 'db'
    with sqlite3.connect(path) as db:
        db.executescript("CREATE TABLE runs(id TEXT,state TEXT);CREATE TABLE versions(id TEXT,status TEXT);CREATE TABLE batches(id TEXT,status TEXT);CREATE TABLE run_tokens(revoked INTEGER,expires REAL);")
        for name in ops.STABLE_TABLES:
            if name not in ('runs', 'versions', 'batches'): db.execute(f'CREATE TABLE {name}(id TEXT,value TEXT)')
        db.execute("INSERT INTO runs VALUES('run1','completed')")
    return path

def test_snapshot_readonly_preserves_and_detects_change(state):
    db = database(state)
    first = ops.db_snapshot(db)
    assert first['active_runs'] == 0
    assert first['quick_check'] == 'ok'
    assert ops.db_snapshot(db) == first
    with sqlite3.connect(db) as conn: conn.execute("UPDATE runs SET state='attention'")
    assert ops.db_snapshot(db)['tables'] != first['tables']

def test_reboot_refuses_active_work(state):
    db = database(state)
    with sqlite3.connect(db) as conn: conn.execute("UPDATE runs SET state='running'")
    with pytest.raises(RuntimeError, match='pending or active'):
        ops.prepare_reboot(db, state, 'https://example.com/')
    assert not (state / 'reboot' / 'before.json').exists()

def test_reboot_refuses_active_operator_job(state):
    db = database(state); queued(state)
    with pytest.raises(RuntimeError, match='operator jobs'):
        ops.prepare_reboot(db, state, 'https://example.com/')

def test_reboot_snapshot_no_trigger_and_detect_changed_upload(state):
    db = database(state)
    uploads = state / 'uploads'; uploads.mkdir(); (uploads / 'archive.zip').write_bytes(b'example')
    states = {n: {'active':'active','enabled':'enabled'} for n in ops.CORE + ['nginx.service']}
    with mock.patch.object(ops, 'service_states', side_effect=lambda units: {u: states[u] for u in units}):
        result = ops.prepare_reboot(db, uploads, 'https://example.com/')
    assert result['reboot_triggered'] is False
    before = json.loads((state / 'reboot' / 'before.json').read_text())
    response = mock.MagicMock(); response.__enter__.return_value.status = 200
    with mock.patch.object(ops, 'boot_id', return_value='new-boot'), mock.patch.object(ops, 'service_states', side_effect=lambda units: {u: states[u] for u in units}), mock.patch.object(ops.socket, 'create_connection', return_value=mock.MagicMock()), mock.patch.object(ops.urllib.request, 'urlopen', return_value=response):
        assert ops.postcheck_once(before)['passed']
        (uploads / 'archive.zip').write_bytes(b'tampered')
        assert not ops.postcheck_once(before)['checks']['uploads_unchanged']

def test_https_failure_is_not_success(state):
    db = database(state); uploads = state / 'uploads'; uploads.mkdir()
    states = {n: {'active':'active','enabled':'enabled'} for n in ops.CORE + ['nginx.service']}
    with mock.patch.object(ops, 'service_states', side_effect=lambda units: {u: states[u] for u in units}): ops.prepare_reboot(db, uploads, 'https://example.com/')
    before = json.loads((state / 'reboot' / 'before.json').read_text())
    with mock.patch.object(ops, 'boot_id', return_value='new-boot'), mock.patch.object(ops, 'service_states', side_effect=lambda units: {u: states[u] for u in units}), mock.patch.object(ops.socket, 'create_connection', return_value=mock.MagicMock()), mock.patch.object(ops.urllib.request, 'urlopen', side_effect=OSError()):
        result = ops.postcheck_once(before)
    assert not result['passed'] and not result['checks']['https_reachable']

def test_inventory_reject_symlinks(state):
    uploads = state / 'uploads'; uploads.mkdir(); (uploads / 'outside').symlink_to('/etc/passwd')
    with pytest.raises(ValueError): ops.file_inventory(uploads)

def test_retired_service_must_be_disabled(state):
    db = database(state); uploads = state / 'uploads'; uploads.mkdir()
    def states(units):
        return {u: {'active': 'active', 'enabled': 'enabled'} for u in units}
    with mock.patch.object(ops, 'service_states', side_effect=states):
        with pytest.raises(RuntimeError, match='retired services'):
            ops.prepare_reboot(db, uploads, 'https://example.com/', disabled_units=['old.service'])

def test_timer_and_retired_unit_survive_reboot_requirements(state):
    db = database(state); uploads = state / 'uploads'; uploads.mkdir()
    def states(units):
        return {u: {'active': 'inactive', 'enabled': 'disabled'} if u == 'old.service' else {'active': 'active', 'enabled': 'enabled'} for u in units}
    with mock.patch.object(ops, 'service_states', side_effect=states):
        ops.prepare_reboot(db, uploads, 'https://example.com/', extra_units=['backup.timer'], disabled_units=['old.service'])
    before = json.loads((state / 'reboot' / 'before.json').read_text())
    assert 'backup.timer' in before['units'] and before['disabled_units'] == ['old.service']
    response = mock.MagicMock(); response.__enter__.return_value.status = 200
    with mock.patch.object(ops, 'boot_id', return_value='new-boot'), mock.patch.object(ops, 'service_states', side_effect=states), mock.patch.object(ops.socket, 'create_connection', return_value=mock.MagicMock()), mock.patch.object(ops.urllib.request, 'urlopen', return_value=response):
        assert ops.postcheck_once(before)['passed']
