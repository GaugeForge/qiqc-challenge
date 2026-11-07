#!/usr/bin/env python3
"""Explicit, bounded REAL staging SIGKILL acceptance (never runs by default).

Operator sequence AFTER other cloud acceptance is idle:
  python3 scientific_crash_check.py --reconcile /absolute/reviewed/science-reconcile --inventory > /private/inventory.json
  QIQC_SHARED_DATA=1 python3 scientific_crash_check.py --reconcile /absolute/reviewed/science-reconcile --expected-inventory /private/inventory.json --output /private/result.json --execute

Uses ONE fresh official Codex/public CZ infra request. Kills ONLY the staging
worker cgroup, once, after two persisted resource IDs and one successful model
call. Reads SQLite in mode=ro; never edits leases, scores, quotas or cloud
configuration. Existing systemd restart and science-reconcile implement recovery.
No R2 fault is injected; an absent archive is reported absent, never synthesized.
API workflow (not browser/UI coverage). Review daemon may independently consume
its configured advisory request for the new submission. Never reruns Harness.
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import os
from pathlib import Path
import secrets
import signal
import sqlite3
import subprocess
import time

URL = 'http://127.0.0.1:18890'
DATA = Path('/var/lib/qiqc-staging/data')
RELEASE = Path('/opt/qiqc-staging/current')
SCIENCE = Path('/opt/qiqc-staging/science')
UNIT = 'qiqc-stage-worker.service'
CREDENTIALS = Path('/etc/qiqc-staging/preview-credentials.json')
LOST = 'worker_lost:reconciliation_required'
ACTOR = 'bounded-scientific-crash-acceptance'
ACTIVE_SQL = "SELECT id FROM runs WHERE state IN ('queued','running','uploading') OR (state='attention' AND error=?)"
SCIENCE_FILES = [
    'benchmark-assets/adapters/science-probe/' + name for name in
    ['worker_bridge.py', 'executor.py', 'qsim_relay.py', 'qsim_call.py',
     'bounded_files.py', 'relay_sandbox.py', 'reconcile_resources.py']
] + [
    'competition-platform/platform/scripts/science/' + name for name in
    ['harbor_e2b.py', 'harbor_agent.py', 'harbor_executor.py', 'verify_public.py',
     'agent_boundary_probe.py', 'e2b_runtime.py']
]


class GuardError(RuntimeError):
    """Only fixed, credential-free reason codes may be included in reports."""


def require(value, reason):
    if not value:
        raise GuardError(reason)


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def command(argv, timeout=15):
    process = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                               start_new_session=True)
    try:
        output, _ = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        # The reconcile CLI starts Python: bound the whole operator process group.
        os.killpg(process.pid, signal.SIGKILL)
        process.communicate()
        raise GuardError('operator_command_deadline') from None
    require(process.returncode == 0, 'operator_command_failed')
    require(len(output) <= 65536, 'operator_output_oversized')
    return output.decode('utf-8')


def unit_info():
    fields = ['MainPID', 'ActiveState', 'SubState', 'Restart', 'KillMode', 'ControlGroup',
              'FragmentPath', 'TimeoutStopUSec', 'NRestarts']
    raw = command(['systemctl', 'show', UNIT, '--property=' + ','.join(fields)])
    result = dict(line.split('=', 1) for line in raw.splitlines() if '=' in line)
    require(set(fields) <= result.keys(), 'unit_properties_missing')
    return result


def inventory(reconcile):
    unit = unit_info()
    paths = {name: RELEASE / name for name in ['api', 'worker', 'gateway']}
    paths.update({name: SCIENCE / name for name in SCIENCE_FILES})
    # CLI currently invokes the workspace Python helper; hash that separate path.
    paths['reconcile_binary'] = reconcile
    paths['reconcile_source'] = Path('/root/QQ-Challenge/benchmark-assets/adapters/science-probe/reconcile_resources.py')
    paths['reconcile_runtime'] = Path('/root/QQ-Challenge/competition-platform/platform/scripts/science/e2b_runtime.py')
    paths['worker_unit'] = Path(unit['FragmentPath'])
    paths['driver'] = Path(__file__).resolve()
    return {'sha256': {name: digest(path) for name, path in paths.items()},
            'worker_restart': unit['Restart'], 'worker_kill_mode': unit['KillMode'],
            'scope': 'local-staging-one-public-codex-cz-scientific-smoke'}


def process_info(unit):
    require(unit['ActiveState'] == 'active' and unit['SubState'] == 'running', 'worker_not_running')
    require(unit['Restart'] in ('on-failure', 'always'), 'worker_restart_not_configured')
    require(unit['KillMode'] == 'control-group', 'unexpected_worker_kill_mode')
    cgroup = unit['ControlGroup']
    require(cgroup.startswith('/system.slice/') and cgroup.endswith('/' + UNIT), 'wrong_worker_cgroup')
    raw = (Path('/sys/fs/cgroup') / cgroup.lstrip('/') / 'cgroup.procs').read_text()
    pids = [int(value) for value in raw.split()]
    require(0 < len(pids) <= 12 and int(unit['MainPID']) in pids, 'unexpected_worker_process_count')
    result = []
    for pid in pids:
        proc = Path('/proc') / str(pid)
        # No command line/environment: they can contain a scoped capability.
        result.append({'pid': pid, 'comm': (proc / 'comm').read_text().strip()[:40],
                       'executable_sha256': digest(proc / 'exe')})
    require(digest(Path('/proc') / unit['MainPID'] / 'exe') == digest(RELEASE / 'worker'),
            'running_worker_binary_mismatch')
    return result


def validate_receipt(receipt, run, attempt, owned=None, exactly_created=False, allow_empty=False):
    require(receipt.get('run_id') == run and receipt.get('attempt') == attempt
            and receipt.get('mode') == 'scientific-smoke', 'receipt_identity_mismatch')
    resources = receipt.get('resources')
    require(isinstance(resources, dict) and (0 if allow_empty else 1) <= len(resources) <= 3, 'invalid_resource_count')
    require(all(isinstance(sid, str) and sid.isascii() and sid.isalnum() and len(sid) <= 100
                for sid in resources), 'invalid_resource_id')
    if owned is not None:
        require(set(resources) == set(owned), 'resource_set_changed_abort_cleanup')
    if exactly_created:
        require(len(resources) == 2 and set(resources.values()) == {'created'}, 'not_two_created_resources')
    return set(resources)




def restarted_worker(previous_pid, restart_count, original_pids, expected_sha, observations):
    current = unit_info()
    if current['ActiveState'] != 'active' or current['SubState'] != 'running':
        return False
    pid = int(current['MainPID'])
    if pid <= 0 or pid == previous_pid or int(current['NRestarts']) <= restart_count:
        return False
    if any((Path('/proc') / str(original)).exists() for original in original_pids):
        return False
    executable = Path('/proc') / str(pid) / 'exe'
    try:
        actual_sha = digest(executable)
        basename = Path(os.readlink(executable)).name[:100]
    except FileNotFoundError:
        return False
    observation = {'pid': pid, 'executable_basename': basename, 'sha256': actual_sha,
                   'matches_expected': actual_sha == expected_sha}
    if len(observations) < 32 and (not observations or observations[-1] != observation):
        observations.append(observation)
    # Type=simple may be active before exec completes. Require the exact frozen
    # executable within the bounded wait; a persistently wrong binary fails.
    if actual_sha != expected_sha:
        return False
    return current


def classify_kill(returncode, events, previous_pid):
    """Never infer successful delivery from a failed command alone."""
    messages = [event['message'] for event in events]
    prefix = f'{UNIT}: Sent signal SIGKILL to main process {previous_pid} ('
    require(any(message.startswith(prefix) and message.endswith('on client request.')
                for message in messages), 'main_sigkill_delivery_not_proven')
    require(f'{UNIT}: Main process exited, code=killed, status=9/KILL' in messages,
            'main_sigkill_exit_not_proven')
    if returncode == 0:
        return 'command_success_and_process_evidence'
    # systemd 255 on this host delivered both signals, but its subsequent
    # auxiliary-process signal operation returned EINVAL. This narrow observed
    # condition is accepted only AFTER PID/restart/identity checks by the caller.
    require(returncode is not None and returncode > 0 and
            f'{UNIT}: Failed to send signal SIGKILL to auxiliary processes on client request: Invalid argument' in messages,
            'nonzero_kill_result_not_explained')
    return 'auxiliary_einval_with_independent_process_evidence'


def kill_journal(since):
    stamp = datetime.datetime.fromtimestamp(since - 1, datetime.timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')
    raw = command(['journalctl', 'SYSLOG_IDENTIFIER=systemd', '--since', stamp,
                   '--grep=qiqc-stage-worker[.]service', '-n', '80', '-o', 'json', '--no-pager'])
    events = []
    for line in raw.splitlines():
        entry = json.loads(line)
        message = entry.get('MESSAGE', '')
        if entry.get('SYSLOG_IDENTIFIER') == 'systemd' and (message.startswith(UNIT + ':') or message.startswith('Started ' + UNIT + ' ')):
            events.append({'time_us': entry.get('__REALTIME_TIMESTAMP'), 'message': message})
    return events


def private_json(path, value):
    require(not path.exists(), 'output_already_exists')
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as stream:
        json.dump(value, stream, indent=2, ensure_ascii=False)
        stream.write('\n')
        stream.flush()
        os.fsync(stream.fileno())


class Drill:
    def __init__(self, args):
        self.args = args
        self.report = {'status': 'failed', 'evidence_level': 'real-cloud-scientific-worker-sigkill',
                       'checked_at': datetime.datetime.now(datetime.timezone.utc).isoformat(),
                       'r2_failure_injected': False, 'browser_coverage': False,
                       'automatic_retry_performed': False, 'quota_refund_performed': False}
        self.run = self.batch = self.team_id = None
        self.attempt = None
        self.idem = None
        self.owned = None
        self.killed = False
        self.team = self.admin = None
        self.stage = 'preflight'
        self.expected = json.loads(args.expected_inventory.read_text())

    def rows(self, sql, values=()):
        # Explicitly read-only even though root could open the production DB writable.
        with sqlite3.connect(f'file:{DATA / "platform.db"}?mode=ro', uri=True, timeout=5) as db:
            db.row_factory = sqlite3.Row
            return [dict(row) for row in db.execute(sql, values)]

    def active(self):
        return {row['id'] for row in self.rows(ACTIVE_SQL, (LOST,))}

    def state(self):
        rows = self.rows('SELECT r.*,b.status batch_status,b.mode,b.kind,b.team_id,b.passed,b.mean '
                         'FROM runs r JOIN batches b ON b.id=r.batch_id WHERE r.batch_id=?', (self.batch,))
        require(len(rows) == 1, 'request_does_not_have_exactly_one_run')
        row = rows[0]
        require(row['team_id'] == self.team_id and row['mode'] == 'scientific-smoke'
                and row['kind'] == 'infra', 'request_scope_mismatch')
        if self.run is None:
            self.run = row['id']
        require(row['id'] == self.run, 'run_identity_changed')
        return row

    def receipt(self, allow_empty=False):
        path = DATA / 'scientific' / f'{self.run}_{self.attempt}.json'
        require(path.stat().st_size <= 65536, 'receipt_oversized')
        value = json.loads(path.read_text())
        validate_receipt(value, self.run, self.attempt, self.owned, allow_empty=allow_empty)
        return value

    def api(self, client, method, path, expected=200, **kwargs):
        response = client.request(method, path, **kwargs)
        require(response.status_code == expected, 'unexpected_api_status')
        return response.json() if response.content else None

    def action(self, action, target, expected=200, **kwargs):
        return self.api(self.admin, 'POST', '/api/admin/actions', expected,
                        json={'action': action, 'target': target, 'actor': ACTOR,
                              'reason': 'Single owned public CZ crash acceptance; no retry or quota change', **kwargs})

    def wait(self, seconds, predicate, failure):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            value = predicate()
            if value:
                return value
            time.sleep(1)
        raise GuardError(failure)

    def setup(self):
        import httpx
        require(os.geteuid() == 0, 'root_operator_required')
        require(os.environ.get('QIQC_SHARED_DATA') == '1', 'shared_data_environment_required')
        project = Path(__file__).resolve().parents[3]
        require(not self.args.output.resolve().is_relative_to(project), 'private_output_must_be_outside_repository')
        self.args.output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        parent = self.args.output.parent.stat()
        require(parent.st_uid == 0 and parent.st_mode & 0o077 == 0, 'output_directory_must_be_root_private')
        require(not self.args.output.exists() and not self.args.output.with_suffix('.credentials.json').exists(),
                'evidence_path_exists')
        require(inventory(self.args.reconcile) == self.expected, 'release_inventory_mismatch')
        require(not self.active(), 'staging_not_idle')
        unit = unit_info()
        self.report['initial_processes'] = process_info(unit)
        self.report['inventory'] = self.expected
        require(CREDENTIALS.stat().st_mode & 0o077 == 0, 'credentials_not_private')
        credentials = json.loads(CREDENTIALS.read_text())['admin']
        headers = {'X-QIQC-Request': '1', 'Origin': URL}
        self.admin = httpx.Client(base_url=URL, headers=headers, timeout=45, trust_env=False)
        self.team = httpx.Client(base_url=URL, headers=headers, timeout=45, trust_env=False)
        self.api(self.admin, 'POST', '/api/login', json=credentials)
        admin = self.api(self.admin, 'GET', '/api/me')
        require(not admin.get('must_change_password', admin.get('must_change', 0)), 'admin_first_change_required')
        password = secrets.token_urlsafe(24)
        account = {'email': 'crash-' + secrets.token_hex(6) + '@qiqc-preview.local',
                   'password': password, 'team': 'Bounded public CZ crash ' + secrets.token_hex(3)}
        self.stage = 'account-upload-review'
        self.api(self.admin, 'POST', '/api/admin/accounts', json={**account, 'actor': ACTOR, 'reason': 'One isolated crash drill account'})
        self.api(self.team, 'POST', '/api/login', json={'email': account['email'], 'password': password})
        account['password'] = secrets.token_urlsafe(24)
        private_json(self.args.output.with_suffix('.credentials.json'), {**account, 'initial_password': password})
        self.api(self.team, 'POST', '/api/password', json={'password': account['password']})
        me = self.api(self.team, 'GET', '/api/me')
        self.team_id = me['team_id']
        self.report['initial_quota'] = me['quotas']
        archive = self.team.get('/adapters/codex/harness.zip')
        require(archive.status_code == 200 and len(archive.content) < 10 * 1024 * 1024, 'official_archive_unavailable')
        version = self.api(self.team, 'POST', '/api/versions', content=archive.content,
                           headers={'Content-Type': 'application/zip'})
        require(version['sha256'] == hashlib.sha256(archive.content).hexdigest(), 'frozen_archive_mismatch')
        self.action('review', version['id'], decision='approve')
        require(not self.active(), 'staging_became_busy_before_confirm')
        self.idem = 'crash-' + secrets.token_hex(16)
        batch = self.api(self.team, 'POST', '/api/batches', json={
            'version_id': version['id'], 'kind': 'infra', 'mode': 'scientific-smoke',
            'idempotency_key': self.idem})
        self.batch = batch['id']
        self.report.update(batch_id=self.batch, team_id=self.team_id, submission_sha256=version['sha256'])

    def crash(self):
        self.stage = 'await-two-sandboxes-and-model-call'
        def ready():
            row = self.state()
            require(self.active() <= {self.run}, 'other_work_appeared')
            require(row['state'] in ('queued', 'running'), 'run_left_execution_before_kill')
            if row['state'] != 'running':
                return False
            self.attempt = row['attempt']
            path = DATA / 'scientific' / f'{self.run}_{self.attempt}.json'
            if not path.exists():
                return False
            receipt = self.receipt(allow_empty=True)
            if len(receipt['resources']) != 2 or set(receipt['resources'].values()) != {'created'}:
                return False
            calls = self.rows('SELECT COUNT(*) n FROM usage WHERE run_id=? AND status=200', (self.run,))[0]['n']
            return (row, receipt, calls) if calls >= 1 else False
        row, receipt, calls = self.wait(self.args.phase_timeout, ready, 'phase_deadline_no_kill')
        self.owned = validate_receipt(receipt, self.run, self.attempt, exactly_created=True)
        require(self.attempt == 1, 'unexpected_attempt')
        require(inventory(self.args.reconcile) == self.expected, 'release_changed_before_kill')
        unit = unit_info()
        self.report['killed_processes'] = process_info(unit)
        require(self.active() == {self.run}, 'not_exclusive_at_kill')
        row = self.state()
        require(row['state'] == 'running' and row['attempt'] == self.attempt, 'phase_changed_before_kill')
        validate_receipt(self.receipt(), self.run, self.attempt, self.owned, exactly_created=True)
        require(0 < row['lease_until'] - time.time() <= 61, 'unexpected_native_lease_window')
        self.report.update(run_id=self.run, attempt=self.attempt, sandbox_ids=sorted(self.owned),
                           successful_model_calls_before_kill=calls, lease_until_before_kill=row['lease_until'],
                           killed_pid=int(unit['MainPID']), initial_restart_count=int(unit['NRestarts']))
        self.stage = 'kill-once'
        self.killed = True  # A command timeout is uncertain: finally must still restore service.
        self.report['kill_requested_at'] = time.time()
        # Capture this one command's outcome even if systemd reports EINVAL after
        # delivering the signals. No second signal is sent under any outcome.
        try:
            result = subprocess.run(['systemctl', 'kill', '--kill-whom=all', '--signal=SIGKILL', UNIT],
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=15, check=False)
            outcome = {'returncode': result.returncode, 'stdout': result.stdout[:65536].decode(errors='replace'),
                       'stderr': result.stderr[:65536].decode(errors='replace')}
        except subprocess.TimeoutExpired:
            outcome = {'returncode': None, 'timed_out': True}
        private_json(self.args.output.with_suffix('.kill-command.json'), outcome)
        self.report['kill_command_returncode'] = outcome['returncode']
        require(outcome['returncode'] is not None, 'kill_command_timed_out')
        self.report['restart_executable_observations'] = []
        def restarted():
            return restarted_worker(self.report['killed_pid'], self.report['initial_restart_count'],
                                    [process['pid'] for process in self.report['killed_processes']],
                                    self.expected['sha256']['worker'],
                                    self.report['restart_executable_observations'])
        confirmed = self.wait(30, restarted, 'killed_processes_or_restart_unconfirmed')
        self.report['restarted_processes'] = process_info(confirmed)
        require(self.active() == {self.run}, 'owned_slot_changed_after_kill')
        require(self.state()['attempt'] == self.attempt, 'attempt_changed_after_kill')
        self.receipt()  # Exact original resources must still describe this run.
        events = kill_journal(self.report['kill_requested_at'])
        self.report['kill_journal'] = events
        self.report['kill_delivery_confirmation'] = classify_kill(outcome['returncode'], events, self.report['killed_pid'])
        self.report['all_original_pids_gone'] = True
        self.stage = 'native-restart-and-expiry'
        def recovered():
            state = self.state()
            require(state['attempt'] == self.attempt, 'unexpected_retry')
            unit = unit_info()
            return (state, unit) if state['state'] == 'attention' and state['error'] == LOST and unit['ActiveState'] == 'active' and int(unit['MainPID']) != self.report['killed_pid'] else False
        row, restarted = self.wait(self.args.recovery_timeout, recovered, 'native_recovery_deadline')
        require(time.time() >= self.report['lease_until_before_kill'], 'recovered_before_native_expiry')
        self.report.update(native_expiry_observed_at=time.time(), restarted_pid=int(restarted['MainPID']),
                           restart_count=int(restarted['NRestarts']), restarted_active=True)
        require(int(restarted['NRestarts']) > self.report['initial_restart_count'], 'systemd_restart_not_observed')
        require(self.active() == {self.run}, 'unresolved_slot_not_reserved')
        require(self.rows('SELECT COUNT(*) n FROM run_tokens WHERE run_id=? AND revoked=0', (self.run,))[0]['n'] == 0,
                'expired_model_token_still_active')
        self.action('retry', self.batch, expected=409)
        self.api(self.team, 'POST', f'/api/batches/{self.batch}/cancel', json={})
        row = self.state()
        require(row['batch_status'] == 'cancelling' and row['state'] == 'attention'
                and self.active() == {self.run}, 'cancel_bypassed_resource_fence')
        self.report.update(retry_http_status=409, cancel_kept_slot=True, expired_tokens_revoked=True)

    def reconcile(self):
        require(self.owned is not None, 'no_owned_resources_for_cleanup')
        self.receipt()  # Exact owned IDs, run/attempt and bounded receipt immediately before CLI.
        require(inventory(self.args.reconcile) == self.expected, 'release_changed_before_reconcile')
        row = self.state()
        require(row['state'] == 'attention' and row['error'] == LOST and row['attempt'] == self.attempt,
                'run_not_reconcilable')
        command([str(self.args.reconcile), '--data', str(DATA), '--run', self.run,
                 '--attempt', str(self.attempt), '--reason', 'Owned public CZ SIGKILL acceptance'], timeout=120)
        receipt = self.receipt()
        require(receipt.get('provider_checked') is True and set(receipt['resources'].values()) == {'destroyed'},
                'provider_cleanup_not_confirmed')
        self.report['provider_confirmed_destroyed'] = sorted(self.owned)
        self.report['provider_evidence'] = 'reviewed CLI: SDK kill followed by get_info NotFound; never timeout-as-success'

    def finish(self):
        self.stage = 'real-provider-reconciliation'
        self.reconcile()
        self.stage = 'final-invariants'
        row = self.state()
        require(row['state'] == 'cancelled' and row['batch_status'] == 'cancelled', 'not_cancelled_after_cleanup')
        require(row['attempt'] == 1 and row['reward'] is None and row['passed'] is None and row['mean'] is None,
                'unexpected_attempt_or_score')
        require(self.run not in self.active(), 'slot_not_released')
        ledger = self.rows('SELECT delta,action FROM ledger WHERE batch_id=?', (self.batch,))
        require(ledger == [{'delta': -1, 'action': 'accepted'}], 'ledger_not_one_original_debit')
        require(self.rows('SELECT COUNT(*) n FROM attempts WHERE run_id=?', (self.run,))[0]['n'] == 1, 'attempt_count_changed')
        require(self.rows('SELECT COUNT(*) n FROM run_tokens WHERE run_id=? AND revoked=0', (self.run,))[0]['n'] == 0,
                'active_tokens_after_cleanup')
        batch = self.api(self.team, 'GET', '/api/batches/' + self.batch)
        require(batch['score'] is None and batch['status'] == 'cancelled', 'participant_saw_fake_score')
        board = self.api(self.team, 'GET', '/api/leaderboard')
        require(not any(team['team_id'] == self.team_id for team in board['teams']), 'diagnostic_entered_ranking')
        quota = self.api(self.team, 'GET', '/api/me')['quotas']
        expected = {**self.report['initial_quota'], 'infra': self.report['initial_quota']['infra'] - 1}
        require(quota == expected, 'quota_changed_unexpectedly')
        self.report.update(status='passed', terminal_state='cancelled', attempts=1, quota_debits=1,
                           final_quota=quota, score_published=False, active_tokens=0, slot_released=True)

    def artifact_observation(self):
        if not self.run or not self.attempt:
            return
        paths = [DATA / name / self.run / str(self.attempt) for name in ('cache', 'failure-evidence')]
        files = []
        for directory in paths:
            for name in ('staged.json', 'pending.json', 'rollout.tar.gz'):
                path = directory / name
                if path.is_file():
                    files.append({'path': str(path.relative_to(DATA)), 'size': path.stat().st_size,
                                  'sha256': digest(path)})
        artifacts = self.rows('SELECT kind,object_key,object_sha,format FROM run_artifacts WHERE run_id=? AND attempt=?',
                              (self.run, self.attempt))
        self.report['archive_observation'] = {'private_files': files, 'indexed_artifacts': artifacts,
            'absence_meaning': 'No persisted archive is expected when hard kill precedes controller finally/spooling.',
            'real_r2_outage_recovery_tested': False,
            'r2_readback_independently_checked': False}

    def cleanup(self):
        # No second kill, no retry, no direct DB edits, no arbitrary Sandbox.list/delete.
        if self.killed:
            unit = unit_info()
            if unit['ActiveState'] != 'active':
                command(['systemctl', 'start', UNIT], timeout=30)
        if self.batch is None and self.team_id and self.idem:
            # A response timeout may follow an accepted request. Discover only
            # this account's exact idempotency key; never resubmit the Harness.
            accepted = self.rows('SELECT id FROM batches WHERE team_id=? AND idem=?',
                                 (self.team_id, self.idem))
            require(len(accepted) <= 1, 'ambiguous_accepted_request')
            if accepted:
                self.batch = accepted[0]['id']
        if not self.batch or self.team is None:
            return
        self.api(self.team, 'POST', f'/api/batches/{self.batch}/cancel', json={})
        def settled():
            row = self.state()
            if row['state'] in ('cancelled', 'completed'):
                return True
            if row['state'] == 'attention' and row['error'] == LOST:
                self.attempt = row['attempt']
                # If failure occurred before kill, bind only this owned run's receipt.
                if self.owned is None:
                    self.owned = validate_receipt(self.receipt(), self.run, self.attempt)
                self.reconcile()
                return True
            return False
        self.wait(self.args.recovery_timeout, settled, 'cleanup_incomplete_operator_required')
        self.report['finally_cleanup_completed'] = True

    def execute(self):
        started = time.monotonic()
        try:
            self.setup()
            self.crash()
            self.finish()
        except Exception as exc:
            self.report.update(status='failed', failure_stage=self.stage,
                               failure_reason=str(exc) if isinstance(exc, GuardError) else type(exc).__name__)
        finally:
            try:
                self.cleanup()
            except Exception as exc:
                self.report.update(status='failed', cleanup_failed=True,
                                   cleanup_reason=str(exc) if isinstance(exc, GuardError) else type(exc).__name__)
            try:
                self.artifact_observation()
            except Exception:
                self.report.update(status='failed', artifact_observation_failed=True)
            for client in (self.team, self.admin):
                if client is not None:
                    client.close()
            self.report['duration_seconds'] = round(time.monotonic() - started, 3)
            private_json(self.args.output, self.report)
        # No raw provider output, cookies, model tokens or credentials in console.
        print(json.dumps({'status': self.report['status'], 'report': str(self.args.output)}))
        return 0 if self.report['status'] == 'passed' else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--reconcile', type=Path, required=True, help='Absolute reviewed existing science-reconcile binary')
    parser.add_argument('--inventory', action='store_true', help='Read-only deployed source/binary identity; no account/cloud changes')
    parser.add_argument('--execute', action='store_true', help='Explicitly permit one local stage worker cgroup SIGKILL and real cloud drill')
    parser.add_argument('--expected-inventory', type=Path)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--phase-timeout', type=int, default=900)
    parser.add_argument('--recovery-timeout', type=int, default=180)
    args = parser.parse_args()
    require(args.reconcile.is_absolute() and args.reconcile.is_file(), 'absolute_reconcile_binary_required')
    require(60 <= args.phase_timeout <= 1200 and 90 <= args.recovery_timeout <= 600, 'timeout_out_of_bounds')
    if args.inventory:
        require(not args.execute, 'inventory_and_execute_are_exclusive')
        print(json.dumps(inventory(args.reconcile), indent=2))
        return 0
    require(args.execute and args.expected_inventory and args.output, 'explicit_execution_inventory_and_output_required')
    return Drill(args).execute()


if __name__ == '__main__':
    raise SystemExit(main())
