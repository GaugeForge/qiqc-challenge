"""Offline safety guards only. These tests NEVER touch staging, cloud or services."""
import importlib.util
import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace

import pytest

SOURCE = Path(__file__).resolve().parents[1] / 'scripts/scientific_crash_check.py'
spec = importlib.util.spec_from_file_location('scientific_crash_check', SOURCE)
driver = importlib.util.module_from_spec(spec)
spec.loader.exec_module(driver)


def receipt(**updates):
    return {'run_id': 'owned', 'attempt': 1, 'mode': 'scientific-smoke',
            'resources': {'main123': 'created', 'qsim123': 'created'}, **updates}


@pytest.mark.parametrize('updates', [
    {'run_id': 'unrelated'}, {'attempt': 2}, {'mode': 'simulated'},
    {'resources': {'../foreign': 'created'}}, {'resources': {'密钥': 'created'}},
    {'resources': {'a' * 101: 'created'}}, {'resources': {}},
    {'resources': {'a': 'created', 'b': 'created', 'c': 'created', 'd': 'created'}},
])
def test_resource_scope_refuses_invalid_identity(updates):
    with pytest.raises(driver.GuardError):
        driver.validate_receipt(receipt(**updates), 'owned', 1)


def test_resource_scope_accepts_exact_owned_pair_only():
    expected = {'main123', 'qsim123'}
    assert driver.validate_receipt(receipt(), 'owned', 1, expected, exactly_created=True) == expected
    with pytest.raises(driver.GuardError, match='resource_set_changed'):
        driver.validate_receipt(receipt(resources={'main123': 'created', 'foreign': 'created'}), 'owned', 1, expected)
    with pytest.raises(driver.GuardError, match='not_two_created'):
        driver.validate_receipt(receipt(resources={'main123': 'destroyed', 'qsim123': 'created'}), 'owned', 1, expected, exactly_created=True)


def test_empty_receipt_can_be_waited_on_but_never_cleanup_evidence():
    value = receipt(resources={})
    assert driver.validate_receipt(value, 'owned', 1, allow_empty=True) == set()
    with pytest.raises(driver.GuardError):
        driver.validate_receipt(value, 'owned', 1)


def test_idle_guard_excludes_only_reconciled_history(tmp_path, monkeypatch):
    monkeypatch.setattr(driver, 'DATA', tmp_path)
    db = sqlite3.connect(tmp_path / 'platform.db')
    db.execute('CREATE TABLE runs(id TEXT,state TEXT,error TEXT)')
    db.executemany('INSERT INTO runs VALUES(?,?,?)', [
        ('old', 'attention', 'platform:reconciled'), ('unresolved', 'attention', driver.LOST),
        ('running', 'running', None), ('queued', 'queued', None), ('done', 'cancelled', None)])
    db.commit()
    db.close()
    drill = object.__new__(driver.Drill)
    assert drill.active() == {'unresolved', 'running', 'queued'}
    with pytest.raises(sqlite3.OperationalError, match='readonly'):
        drill.rows("UPDATE runs SET state='cancelled'")


def test_private_report_never_overwrites_existing_evidence(tmp_path):
    output = tmp_path / 'report.json'
    driver.private_json(output, {'status': 'not-run'})
    assert output.stat().st_mode & 0o077 == 0
    with pytest.raises(driver.GuardError, match='output_already_exists'):
        driver.private_json(output, {'status': 'passed'})
    assert json.loads(output.read_text()) == {'status': 'not-run'}


def test_no_execute_flag_cannot_begin_drill(tmp_path, monkeypatch):
    binary = tmp_path / 'reviewed-reconcile'
    binary.write_text('offline fixture')
    monkeypatch.setattr('sys.argv', ['driver', '--reconcile', str(binary)])
    monkeypatch.setattr(driver.Drill, 'execute', lambda _: pytest.fail('unexpected execution'))
    with pytest.raises(driver.GuardError, match='explicit_execution'):
        driver.main()


def test_inventory_and_execute_cannot_be_combined(tmp_path, monkeypatch):
    binary = tmp_path / 'reviewed-reconcile'
    binary.write_text('offline fixture')
    monkeypatch.setattr('sys.argv', ['driver', '--reconcile', str(binary), '--inventory', '--execute'])
    with pytest.raises(driver.GuardError, match='exclusive'):
        driver.main()


def test_reconcile_scope_guard_runs_before_operator_command(monkeypatch):
    drill = object.__new__(driver.Drill)
    drill.owned = {'main123', 'qsim123'}
    drill.receipt = lambda: driver.validate_receipt(receipt(resources={'foreign': 'created'}), 'owned', 1, drill.owned)
    monkeypatch.setattr(driver, 'command', lambda *a, **kw: pytest.fail('operator command must not run'))
    with pytest.raises(driver.GuardError, match='resource_set_changed'):
        drill.reconcile()


def test_operator_timeout_kills_own_process_group_not_only_parent(monkeypatch):
    killed = []
    class Process:
        pid = 123456
        returncode = None
        calls = 0
        def communicate(self, timeout=None):
            self.calls += 1
            if self.calls == 1:
                raise driver.subprocess.TimeoutExpired('fixed-command', timeout)
            return b'', None
    process = Process()
    def popen(*args, **kwargs):
        assert kwargs['start_new_session'] is True
        return process
    monkeypatch.setattr(driver.subprocess, 'Popen', popen)
    monkeypatch.setattr(driver.os, 'killpg', lambda pid, signal: killed.append((pid, signal)))
    with pytest.raises(driver.GuardError, match='operator_command_deadline'):
        driver.command(['reviewed-reconcile'], timeout=1)
    assert killed == [(123456, driver.signal.SIGKILL)]
    assert process.calls == 2


def test_failed_preflight_cannot_start_service_or_cancel_other_requests(monkeypatch):
    drill = object.__new__(driver.Drill)
    drill.killed = False
    drill.batch = drill.team = drill.team_id = drill.idem = None
    monkeypatch.setattr(driver, 'command', lambda *a, **kw: pytest.fail('must not operate services'))
    drill.cleanup()


def test_cleanup_discovers_only_own_accepted_request_after_response_timeout(monkeypatch):
    drill = object.__new__(driver.Drill)
    drill.killed = False
    drill.batch = None
    drill.team_id, drill.idem = 'owned-team', 'owned-idem'
    drill.team = object()
    drill.report = {}
    drill.args = SimpleNamespace(recovery_timeout=90)
    def rows(sql, values):
        assert 'WHERE team_id=? AND idem=?' in sql
        assert values == ('owned-team', 'owned-idem')
        return [{'id': 'owned-batch'}]
    drill.rows = rows
    calls = []
    drill.api = lambda client, method, path, **kw: calls.append((method, path))
    drill.state = lambda: {'state': 'cancelled'}
    drill.cleanup()
    assert drill.batch == 'owned-batch'
    assert calls == [('POST', '/api/batches/owned-batch/cancel')]
    assert drill.report['finally_cleanup_completed'] is True


def test_shared_data_flag_is_required_before_accounts_or_cloud(tmp_path, monkeypatch):
    drill = object.__new__(driver.Drill)
    drill.args = SimpleNamespace(output=tmp_path / 'result.json')
    monkeypatch.setattr(driver.os, 'geteuid', lambda: 0)
    monkeypatch.delenv('QIQC_SHARED_DATA', raising=False)
    monkeypatch.setattr(driver, 'inventory', lambda *args: pytest.fail('must stop before inventory/cloud'))
    with pytest.raises(driver.GuardError, match='shared_data_environment_required'):
        drill.setup()


def test_secret_sidecar_cannot_be_written_inside_repository(monkeypatch):
    drill = object.__new__(driver.Drill)
    drill.args = SimpleNamespace(output=SOURCE.parents[1] / 'test-results' / 'never-created.json')
    monkeypatch.setattr(driver.os, 'geteuid', lambda: 0)
    monkeypatch.setenv('QIQC_SHARED_DATA', '1')
    monkeypatch.setattr(driver, 'inventory', lambda *args: pytest.fail('must stop before inventory/cloud'))
    with pytest.raises(driver.GuardError, match='private_output_must_be_outside_repository'):
        drill.setup()


def test_two_sandboxes_without_successful_model_call_never_allow_kill(tmp_path, monkeypatch):
    monkeypatch.setattr(driver, 'DATA', tmp_path)
    (tmp_path / 'scientific').mkdir()
    (tmp_path / 'scientific/owned_1.json').write_text(json.dumps(receipt()))
    drill = object.__new__(driver.Drill)
    drill.args = SimpleNamespace(phase_timeout=60)
    drill.run = 'owned'
    drill.owned = None
    drill.state = lambda: {'id': 'owned', 'state': 'running', 'attempt': 1}
    drill.active = lambda: {'owned'}
    drill.rows = lambda *args: [{'n': 0}]
    def wait(seconds, predicate, failure):
        assert predicate() is False
        raise driver.GuardError(failure)
    drill.wait = wait
    monkeypatch.setattr(driver, 'command', lambda *a, **kw: pytest.fail('must not kill worker'))
    with pytest.raises(driver.GuardError, match='phase_deadline_no_kill'):
        drill.crash()


def test_other_active_request_prevents_kill(monkeypatch):
    drill = object.__new__(driver.Drill)
    drill.args = SimpleNamespace(phase_timeout=60)
    drill.run = 'owned'
    drill.state = lambda: {'id': 'owned', 'state': 'running', 'attempt': 1}
    drill.active = lambda: {'owned', 'other'}
    drill.wait = lambda seconds, predicate, failure: predicate()
    monkeypatch.setattr(driver, 'command', lambda *a, **kw: pytest.fail('must not kill worker'))
    with pytest.raises(driver.GuardError, match='other_work_appeared'):
        drill.crash()


def kill_events(pid=123, auxiliary=True):
    messages = [f'{driver.UNIT}: Sent signal SIGKILL to main process {pid} (worker) on client request.',
                f'{driver.UNIT}: Main process exited, code=killed, status=9/KILL']
    if auxiliary:
        messages.append(f'{driver.UNIT}: Failed to send signal SIGKILL to auxiliary processes on client request: Invalid argument')
    return [{'message': message} for message in messages]


def test_nonzero_kill_requires_specific_observed_systemd_error_and_matching_pid():
    assert driver.classify_kill(1, kill_events(), 123) == 'auxiliary_einval_with_independent_process_evidence'
    for code, events, pid in [(1, kill_events(auxiliary=False), 123), (1, kill_events(), 999),
                              (None, kill_events(), 123), (-9, kill_events(), 123), (1, [], 123)]:
        with pytest.raises(driver.GuardError):
            driver.classify_kill(code, events, pid)


def test_even_zero_kill_requires_actual_sigkill_journal_evidence():
    assert driver.classify_kill(0, kill_events(auxiliary=False), 123) == 'command_success_and_process_evidence'
    with pytest.raises(driver.GuardError):
        driver.classify_kill(0, [], 123)
    with pytest.raises(driver.GuardError):
        driver.classify_kill(0, kill_events()[:1], 123)


def test_restart_wait_observes_exec_transition_before_acceptance(monkeypatch):
    unit = {'ActiveState': 'active', 'SubState': 'running', 'MainPID': '987654', 'NRestarts': '2'}
    monkeypatch.setattr(driver, 'unit_info', lambda: unit)
    values = iter(['launching-systemd-sha', 'frozen-worker-sha'])
    monkeypatch.setattr(driver, 'digest', lambda _: next(values))
    names = iter(['/usr/lib/systemd/systemd', '/opt/qiqc-staging/current/worker'])
    monkeypatch.setattr(driver.os, 'readlink', lambda _: next(names))
    observations = []
    assert driver.restarted_worker(123, 1, [], 'frozen-worker-sha', observations) is False
    assert driver.restarted_worker(123, 1, [], 'frozen-worker-sha', observations) == unit
    assert [row['matches_expected'] for row in observations] == [False, True]
    assert [row['executable_basename'] for row in observations] == ['systemd', 'worker']


def test_persistently_wrong_executable_remains_not_ready(monkeypatch):
    monkeypatch.setattr(driver, 'unit_info', lambda: {'ActiveState': 'active', 'SubState': 'running', 'MainPID': '987654', 'NRestarts': '2'})
    monkeypatch.setattr(driver, 'digest', lambda _: 'wrong-sha')
    monkeypatch.setattr(driver.os, 'readlink', lambda _: '/untrusted/wrong-worker')
    observations = []
    for _ in range(35):
        assert driver.restarted_worker(123, 1, [], 'frozen-worker-sha', observations) is False
    assert observations == [{'pid': 987654, 'executable_basename': 'wrong-worker', 'sha256': 'wrong-sha', 'matches_expected': False}]
    # Production wait must convert persistent not-ready into a bounded failure.
    clock = iter([0, 0, 31])
    monkeypatch.setattr(driver.time, 'monotonic', lambda: next(clock))
    monkeypatch.setattr(driver.time, 'sleep', lambda _: None)
    drill = object.__new__(driver.Drill)
    with pytest.raises(driver.GuardError, match='restart_deadline'):
        drill.wait(30, lambda: driver.restarted_worker(123, 1, [], 'frozen-worker-sha', observations), 'restart_deadline')
