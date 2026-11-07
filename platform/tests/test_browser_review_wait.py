"""Actual SQLite queue state, simulated clock; no browser/cloud/provider calls."""
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / 'scripts'))
from cloud_browser_check import wait_for_advisory
from catalog_browser_suite import parse_args


def database(tmp_path, pending):
    path = tmp_path / 'platform.db'
    with sqlite3.connect(path) as db:
        db.executescript('CREATE TABLE versions(id,status); CREATE TABLE review_advisories(version_id,state,submission_sha,result,created);')
        db.execute("INSERT INTO versions VALUES('target','pending_review')")
        for n in range(pending):
            db.execute('INSERT INTO review_advisories VALUES(?,?,?,?,?)',
                       ('target' if n == 0 else str(n), 'queued', 'bound-sha', '{}', n))
    return path


def run_wait(path, complete_at=None, terminal='complete'):
    now = [0]
    telemetry = {}

    def advance(ms):
        now[0] += ms / 1000
        if complete_at is not None and now[0] == complete_at:
            with sqlite3.connect(path) as db:
                db.execute('UPDATE review_advisories SET state=? WHERE version_id=?', (terminal, 'target'))
    result = wait_for_advisory(path, 'target', telemetry, advance, clock=lambda: now[0])
    return result, telemetry


def test_three_serial_reviews_can_finish_after_old_150_second_limit(tmp_path):
    path = database(tmp_path, 3)
    result, telemetry = run_wait(path, complete_at=350)
    assert result == ('complete', 'bound-sha', '{}')
    assert telemetry['budget_seconds'] == 390
    assert telemetry['maximum_pending_advisories'] == 3
    assert telemetry['elapsed_seconds'] == 350
    assert telemetry['last_state'] == 'complete'
    assert not telemetry['timed_out']
    assert [x['state'] for x in telemetry['state_changes']] == ['queued', 'complete']


def test_large_queue_is_bounded_and_never_fabricates_advice(tmp_path):
    result, telemetry = run_wait(database(tmp_path, 20))
    assert telemetry['budget_seconds'] == 600
    assert telemetry['elapsed_seconds'] == 600
    assert telemetry['timed_out']
    assert result[0] == 'queued'


def test_single_review_keeps_old_bound_and_terminal_failure_is_not_success(tmp_path):
    result, telemetry = run_wait(database(tmp_path, 1), complete_at=20, terminal='failed')
    assert result[0] == 'failed'
    assert telemetry['budget_seconds'] == 150
    assert telemetry['elapsed_seconds'] == 20
    assert not telemetry['timed_out']


def test_advice_does_not_override_required_manual_review(tmp_path):
    path = database(tmp_path, 1)
    with sqlite3.connect(path) as db:
        db.execute("UPDATE versions SET status='approved'")
    with pytest.raises(AssertionError, match='preserve manual'):
        run_wait(path)


def test_catalog_cli_accepts_bounded_parallel_other_active(tmp_path):
    args = parse_args(['--indices', '0,1', '--output-dir', str(tmp_path), '--max-other-active', '2'])
    assert args.max_other_active == 2
    for count in (3, 48, 49):
        assert parse_args(['--indices', '0', '--output-dir', str(tmp_path),
                           '--max-other-active', str(count)]).max_other_active == count
    with pytest.raises(SystemExit):
        parse_args(['--indices', '0', '--output-dir', str(tmp_path), '--max-other-active', '50'])


def test_delayed_advisory_creation_at_122_seconds_gets_provider_window(tmp_path):
    path = database(tmp_path, 0)
    now, telemetry = [0], {}

    def advance(ms):
        now[0] += ms / 1000
        with sqlite3.connect(path) as db:
            if now[0] == 122:
                db.execute("INSERT INTO review_advisories VALUES('target','running','bound-sha','{}',1)")
            if now[0] == 240:
                db.execute("UPDATE review_advisories SET state='complete'")
    result = wait_for_advisory(path, 'target', telemetry, advance, clock=lambda: now[0])
    assert result == ('complete', 'bound-sha', '{}')
    assert telemetry['first_running_elapsed_seconds'] == 122
    assert telemetry['budget_seconds'] == 272
    assert telemetry['elapsed_seconds'] == 240
    assert not telemetry['timed_out']


def test_missing_advisory_and_late_running_never_extend_absolute_cap(tmp_path):
    path = database(tmp_path, 0)
    now, telemetry = [0], {}

    def advance(ms):
        now[0] += ms / 1000
        if now[0] == 590:
            with sqlite3.connect(path) as db:
                db.execute("INSERT INTO review_advisories VALUES('target','running','bound-sha','{}',1)")
    result = wait_for_advisory(path, 'target', telemetry, advance, clock=lambda: now[0])
    assert result[0] == 'running'
    assert telemetry['first_running_elapsed_seconds'] == 590
    assert telemetry['budget_seconds'] == 600
    assert telemetry['elapsed_seconds'] == 600
    assert telemetry['timed_out']


def test_running_state_does_not_refresh_deadline_every_poll(tmp_path):
    path = database(tmp_path, 1)
    with sqlite3.connect(path) as db:
        db.execute("UPDATE review_advisories SET state='running'")
    result, telemetry = run_wait(path)
    assert result[0] == 'running'
    assert telemetry['elapsed_seconds'] == 150
    assert telemetry['first_running_elapsed_seconds'] == 0
    assert telemetry['timed_out']
