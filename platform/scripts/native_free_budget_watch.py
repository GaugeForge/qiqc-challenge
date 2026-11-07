#!/usr/bin/env python3
"""Bounded provider-account quota observer; never starts or stops scientific work."""
import argparse
import datetime as dt
import json
import os
from pathlib import Path
import sqlite3
import time
import uuid

from native_free_budget_status import private_bytes, status


def atomic_status(path, value):
    if path.is_symlink():
        raise ValueError('unsafe_status_path')
    tmp = path.with_name('.' + path.name + '.' + uuid.uuid4().hex)
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, 'w') as f:
            json.dump(value, f, sort_keys=True)
            f.write('\n')
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def pause_once(path, value):
    # Link a complete file into place; never replace an earlier failure pause.
    tmp = path.with_name('.budget-pause.' + uuid.uuid4().hex)
    atomic_status(tmp, value)
    try:
        try:
            os.link(tmp, path, follow_symlinks=False)
            return True
        except FileExistsError:
            return False
    finally:
        tmp.unlink(missing_ok=True)


def workload(root, database):
    passed, failures, lane_busy, latest = set(), [], False, {}
    for file in sorted(root.glob('lane-*/suite.json')):
        data = json.loads(file.read_text())
        if data.get('status') == 'running':
            lane_busy = True
        for row in data.get('attempts', []):
            index = row.get('catalog_index')
            if index not in latest or row.get('attempt', 0) >= latest[index].get('attempt', 0):
                latest[index] = row
    for index, row in latest.items():
        if row.get('state') == 'passed':
            passed.add(index)
        elif row.get('state') in ('browser_failed', 'r2_evidence_failed'):
            failures.append({'catalog_index': index, 'attempt': row.get('attempt'), 'state': row['state']})
    with sqlite3.connect(database.as_uri() + '?mode=ro', uri=True, timeout=5) as db:
        active = db.execute("SELECT COUNT(*) FROM runs WHERE state IN ('queued','running','uploading') OR (state='attention' AND error='worker_lost:reconciliation_required')").fetchone()[0]
    return {'passed_count': len(passed), 'active_run_count': active,
            'lane_busy': lane_busy, 'failures': failures}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--profile', type=Path, required=True)
    parser.add_argument('--database', type=Path, required=True)
    parser.add_argument('--output-dir', type=Path, required=True)
    parser.add_argument('--interval', type=int, default=180)
    parser.add_argument('--threshold', type=int, default=100)
    args = parser.parse_args()
    if not 60 <= args.interval <= 600 or args.threshold != 100:
        raise SystemExit('unsupported_monitor_bounds')
    root = args.output_dir.absolute()
    if not root.is_dir() or root.is_symlink():
        raise SystemExit('existing_output_directory_required')
    database = args.database.absolute()
    history = root / 'budget-history.jsonl'
    historyfd = os.open(history, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
    os.chmod(history, 0o600)
    next_query, seen = 0.0, set()
    print(json.dumps({'event': 'budget_watcher_started', 'pid': os.getpid(), 'interval_seconds': args.interval, 'threshold': args.threshold}), flush=True)
    with os.fdopen(historyfd, 'a') as journal:
        while True:
            try:
                current = workload(root, database)
            except Exception:
                current = {'workload_state': 'unavailable', 'active_run_count': None, 'lane_busy': True, 'failures': []}
            for failure in current['failures']:
                key = (failure['catalog_index'], failure['attempt'], failure['state'])
                if key not in seen:
                    seen.add(key)
                    print(json.dumps({'event': 'first_task_failure_observed', **failure}), flush=True)
            if time.monotonic() >= next_query:
                try:
                    profile = json.loads(private_bytes(args.profile, 16384))
                    value = status(profile, database, query_provider=True)
                except Exception:
                    value = {'schema': 'qiqc-free-budget-status/v1', 'checked_at': dt.datetime.now(dt.timezone.utc).isoformat(), 'state': 'budget_status_unavailable'}
                value['workload'] = current
                local = value.get('local_daily_remaining')
                remote = value.get('provider', {}).get('free_model_daily_requests', {}).get('remaining')
                low = any(type(v) is int and v < args.threshold for v in (local, remote))
                if low:
                    reason = {'schema': 'qiqc-budget-pause/v1', 'reason': 'remaining_budget_below_100', 'checked_at': value['checked_at'],
                              'local_remaining': local, 'provider_remaining': remote, 'inflight_action': 'finish_normally_no_kill'}
                    value['pause_created'] = pause_once(root / 'pause.json', reason)
                    print(json.dumps({'event': 'budget_pause_threshold_reached', **reason, 'pause_created': value['pause_created']}), flush=True)
                atomic_status(root / 'budget-status.json', value)
                journal.write(json.dumps(value, sort_keys=True) + '\n')
                journal.flush()
                os.fsync(journal.fileno())
                print(json.dumps({'event': 'budget_status', 'local_remaining': local, 'provider_remaining': remote,
                                  'provider_state': value.get('provider', {}).get('state'), 'active_run_count': current.get('active_run_count'),
                                  'passed_count': current.get('passed_count')}), flush=True)
                next_query = time.monotonic() + args.interval
            stopped = current.get('active_run_count') == 0 and not current.get('lane_busy')
            if stopped and (current.get('passed_count') == 49 or (root / 'pause.json').exists()):
                print(json.dumps({'event': 'budget_watcher_finished', 'reason': 'all_passed' if current.get('passed_count') == 49 else 'paused_and_drained', **current}), flush=True)
                return 0
            time.sleep(5)


if __name__ == '__main__':
    raise SystemExit(main())
