#!/usr/bin/env python3
"""Explicit local staging R2 outage + SIGKILL acceptance; NEVER executes by default.

First capture a read-only inventory; review it, then use --execute. Requires the
same reviewed --reconcile binary as scientific_crash_check.py. The driver creates
one dedicated account/public Codex CZ run, substitutes ONLY the worker's R2
config with a loopback HTTPS 503 sink, and kills that worker ONCE after complete
pending evidence and SDK sandbox cleanup. Restoring the original config invokes
Go's existing same-attempt upload recovery. No DB writes, clock edits, retries,
quota refunds, remote bucket configuration, or participant code execution here.

A temporary runtime systemd drop-in adds a private test CA to existing system CAs;
TLS verification remains enabled. The sink receives dummy credentials only.
Original config bytes/uid/gid/mode are backed up privately beside --output and
restored in finally. Interrupted operators can use that private backup plus the
reported fixed drop-in path; SIGKILL of THIS driver cannot execute Python finally.
Reports never contain credentials, model payloads, or scientific trajectories.
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import http.server
import json
import os
import re
from pathlib import Path
import shutil
import signal
import ssl
import stat
import subprocess
import tempfile
import threading
import time
from urllib.parse import urlsplit

import scientific_crash_check as base

require = base.require
GuardError = base.GuardError
CONFIG = Path('/etc/qiqc-staging/worker-r2.env')
DROPIN = Path('/run/systemd/system/qiqc-stage-worker.service.d/90-qiqc-r2-drill.conf')
MAX_ARCHIVE = 64 * 1024 * 1024


def sha_file(path, limit=MAX_ARCHIVE):
    info = path.lstat()
    require(stat.S_ISREG(info.st_mode) and 0 < info.st_size <= limit, 'invalid_bounded_regular_file')
    h = hashlib.sha256()
    total = 0
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, 'rb') as stream:
        require(os.fstat(stream.fileno()).st_ino == info.st_ino, 'file_changed_before_read')
        while chunk := stream.read(65536):
            total += len(chunk)
            require(total <= limit, 'file_exceeded_read_bound')
            h.update(chunk)
    require(total == info.st_size, 'file_changed_during_read')
    return h.hexdigest(), total


def bounded_json(path):
    sha_file(path, 65536)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, 'rb') as stream:
        raw = stream.read(65537)
    require(len(raw) <= 65536, 'json_exceeded_read_bound')
    return json.loads(raw)


def atomic_private(path, data, mode, uid, gid):
    fd, name = tempfile.mkstemp(prefix='.r2-drill-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            os.fchmod(stream.fileno(), mode)
            os.fchown(stream.fileno(), uid, gid)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
        directory = os.open(path.parent, os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(name):
            os.unlink(name)


class SavedConfig:
    """Exact local rollback. Retain backup if rollback cannot be verified."""
    def __init__(self, path, backup):
        self.path, self.backup = path, backup
        info = path.lstat()
        require(stat.S_ISREG(info.st_mode) and info.st_size <= 65536
                and stat.S_IMODE(info.st_mode) & 0o077 == 0, 'config_not_private_regular_file')
        self.mode, self.uid, self.gid = stat.S_IMODE(info.st_mode), info.st_uid, info.st_gid
        self.original = path.read_bytes()
        self.replacement = None
        fd = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'wb') as stream:
            stream.write(self.original)
            stream.flush()
            os.fsync(stream.fileno())
        base.private_json(backup.with_suffix('.metadata.json'),
                          {'path': str(path), 'uid': self.uid, 'gid': self.gid, 'mode': self.mode,
                           'sha256': hashlib.sha256(self.original).hexdigest()})

    def replace(self, value):
        require(self.path.read_bytes() == self.original, 'config_changed_before_injection')
        # Set before mutation: rollback also covers a rename/fsync exception.
        self.replacement = value
        atomic_private(self.path, value, self.mode, self.uid, self.gid)

    def restore(self):
        info = self.path.lstat()
        require(stat.S_ISREG(info.st_mode), 'config_replaced_by_nonregular_file')
        require(self.path.read_bytes() in (self.original, self.replacement), 'config_changed_by_other_operator')
        atomic_private(self.path, self.original, self.mode, self.uid, self.gid)
        info = self.path.stat()
        require(self.path.read_bytes() == self.original and stat.S_IMODE(info.st_mode) == self.mode
                and info.st_uid == self.uid and info.st_gid == self.gid, 'config_restore_mismatch')
        self.backup.unlink()
        self.backup.with_suffix('.metadata.json').unlink()


class FaultServer(http.server.ThreadingHTTPServer):
    daemon_threads = True
    block_on_close = False
    request_queue_size = 4

    def __init__(self, cert, key):
        super().__init__(('127.0.0.1', 0), FaultHandler)
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(cert, key)
        self.tls_context = ctx
        self.events, self.event_lock = [], threading.Lock()
        self.slots = threading.BoundedSemaphore(4)

    def get_request(self):
        raw, address = self.socket.accept()
        raw.settimeout(5)
        try:
            return self.tls_context.wrap_socket(raw, server_side=True), address
        except Exception:
            raw.close()
            raise

    def process_request(self, request, address):
        if not self.slots.acquire(blocking=False):
            request.close()
            return
        super().process_request(request, address)

    def process_request_thread(self, request, address):
        try:
            super().process_request_thread(request, address)
        finally:
            self.slots.release()

    def handle_error(self, request, client_address):
        pass  # Never dump a client request or Authorization to stderr.

    def matching(self, key, sha, size):
        expected = hashlib.sha256(('/drill-only/' + key).encode()).hexdigest()
        with self.event_lock:
            return sum(e['path_sha256'] == expected and e['body_sha256'] == sha
                       and e['bytes'] == size and e['status'] == 503 for e in self.events)


def canonical_put_path(target):
    # AWS SDK Go S3 v1.76 adds exactly ?x-id=PutObject. Do not normalize
    # arbitrary query keys, encoded IDs, absolute URLs, or dot/path segments.
    require(isinstance(target, str) and target.isascii() and len(target) <= 1024
            and target.startswith('/') and '#' not in target, 'invalid_fault_target')
    parsed = urlsplit(target)
    require(not parsed.scheme and not parsed.netloc and not parsed.fragment
            and parsed.query in ('', 'x-id=PutObject'), 'invalid_fault_operation_query')
    require(re.fullmatch(r'/drill-only/(?:[a-zA-Z0-9_-]{1,80}/){4}[1-9][0-9]{0,8}\.tar\.gz', parsed.path)
            is not None, 'invalid_fault_object_path')
    return parsed.path


class FaultHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def log_message(self, *_):
        pass

    def do_PUT(self):
        self.connection.settimeout(5)
        try:
            object_path = canonical_put_path(self.path)
            size = int(self.headers.get('Content-Length', '-1'))
            require(0 < size <= MAX_ARCHIVE and not self.headers.get('Transfer-Encoding'),
                    'unsupported_fault_request_framing')
            remain, h, started = size, hashlib.sha256(), time.monotonic()
            while remain:
                require(time.monotonic() - started < 30, 'fault_body_deadline')
                chunk = self.rfile.read(min(remain, 65536))
                require(bool(chunk), 'fault_body_incomplete')
                remain -= len(chunk)
                h.update(chunk)
            payload = b'<Error><Code>ServiceUnavailable</Code><Message>controlled outage</Message></Error>'
            self.send_response(503)
            self.send_header('Content-Type', 'application/xml')
            self.send_header('Content-Length', str(len(payload)))
            self.send_header('Connection', 'close')
            self.end_headers()
            self.wfile.write(payload)
            self.wfile.flush()
            with self.server.event_lock:
                if len(self.server.events) < 64:
                    self.server.events.append({'path_sha256': hashlib.sha256(object_path.encode()).hexdigest(),
                                               'operation_query': 'x-id=PutObject' if '?' in self.path else 'none',
                                               'body_sha256': h.hexdigest(), 'bytes': size, 'status': 503})
        except Exception:
            pass
        finally:
            self.close_connection = True


def inventory(reconcile):
    return {'base': base.inventory(reconcile), 'driver_sha256': base.digest(Path(__file__)),
            'release_pin_sha256': base.digest(base.SCIENCE / 'competition-platform/platform/scripts/science/task-release-pin.json'),
            'worker_r2_config_sha256': sha_file(CONFIG, 65536)[0],
            'scope': 'one-public-CZ-archive-R2-503-worker-SIGKILL-upload-only'}


def readback_request(batch, sha):
    # This is a recovery/API receipt, not browser coverage. Use the same strictly
    # bound DB/object contract as the independently reviewed R2 reader.
    return {'status': 'passed', 'standard_harbor_rollout': True, 'batch_id': batch,
            'r2_archive_sha256': sha}


def validate_pending(cache, row, version_id, submission_sha, receipt):
    require(row['state'] == 'uploading' and row['attempt'] == 1, 'not_first_uploading_attempt')
    p = bounded_json(cache / 'pending.json')
    l = p.get('lease', {})
    expected = {key: row[key] for key in ('team_id', 'batch_id', 'owner', 'attempt', 'ordinal', 'public')}
    expected.update(run_id=row['id'], version_id=version_id, sha256=submission_sha,
                    mode='scientific-smoke', kind='infra')
    require(all(l.get(k) == v for k, v in expected.items()) and bool(l.get('attempt_id')),
            'pending_identity_mismatch')
    require(p.get('provenance') == 'scientific-smoke' and type(p.get('reward')) is int
            and p['reward'] in (0, 1) and isinstance(p.get('seconds'), str), 'pending_result_invalid')
    owned = base.validate_receipt(receipt, row['id'], 1)
    require(len(owned) == 3 and set(receipt['resources'].values()) == {'destroyed'}, 'sandboxes_not_all_confirmed_destroyed')
    sha, size = sha_file(cache / 'rollout.tar.gz')
    require(sha == p.get('bundle_sha256'), 'pending_archive_digest_mismatch')
    return p, sha, size, owned


class R2Drill(base.Drill):
    def __init__(self, args):
        super().__init__(args)
        self.full_expected, self.expected = self.expected, self.expected['base']
        self.fault = self.saved = self.runtime = self.thread = None
        self.dropin_owned = self.configured = self.restore_restart_needed = False
        self.pending = None
        self.report.update(evidence_level='real-cloud-scientific-R2-503-SIGKILL-upload-only',
                           r2_failure_injected=False, independent_readback=False,
                           database_or_clock_mutated=False, formal_capacity_acceptance=False)

    def api(self, client, method, path, expected=200, **kwargs):
        if method == 'POST' and path == '/api/batches' and not self.configured:
            self.install_fault()
        return super().api(client, method, path, expected, **kwargs)

    def setup(self):
        require(inventory(self.args.reconcile) == self.full_expected, 'full_inventory_mismatch')
        require(not DROPIN.exists(), 'fault_dropin_already_exists')
        super().setup()

    def install_fault(self):
        self.stage = 'install-local-TLS-fault'
        require(not self.active(), 'not_idle_before_fault')
        environment = base.command(['systemctl', 'show', base.UNIT, '--property=Environment', '--value'])
        require(not any(name + '=' in environment for name in
                        ('R2_ENDPOINT', 'R2_ACCESS_KEY_ID', 'R2_SECRET_ACCESS_KEY', 'R2_BUCKET', 'SSL_CERT_FILE')),
                'direct_environment_would_override_fault')
        self.saved = SavedConfig(CONFIG, self.args.output.with_suffix('.worker-r2.backup'))
        self.restore_restart_needed = True
        self.runtime = Path(tempfile.mkdtemp(prefix='qiqc-r2-drill-', dir='/run'))
        self.runtime.chmod(0o755)
        cert, key = self.runtime / 'cert.pem', self.runtime / 'key.pem'
        base.command(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-days', '1',
                      '-subj', '/CN=127.0.0.1', '-addext', 'subjectAltName=IP:127.0.0.1',
                      '-keyout', str(key), '-out', str(cert)], timeout=20)
        key.chmod(0o600)
        cert.chmod(0o644)
        system_ca = ssl.get_default_verify_paths().cafile
        require(system_ca and Path(system_ca).is_file(), 'system_CA_bundle_unavailable')
        ca = self.runtime / 'ca-bundle.pem'
        ca.write_bytes(Path(system_ca).read_bytes() + b'\n' + cert.read_bytes())
        ca.chmod(0o644)
        self.fault = FaultServer(cert, key)
        self.thread = threading.Thread(target=self.fault.serve_forever, daemon=True)
        self.thread.start()
        DROPIN.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(DROPIN, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
        self.dropin_owned = True
        with os.fdopen(fd, 'w') as stream:
            stream.write('[Service]\nEnvironment=SSL_CERT_FILE=' + str(ca) + '\n')
        data = (f'R2_ENDPOINT=https://127.0.0.1:{self.fault.server_port}\n'
                'R2_ACCESS_KEY_ID=qiqc-local-outage-only\nR2_SECRET_ACCESS_KEY=not-a-cloud-secret\n'
                'R2_BUCKET=drill-only\n').encode()
        self.saved.replace(data)
        base.command(['systemctl', 'daemon-reload'])
        self.restart_worker()
        self.configured = True
        self.report['r2_failure_injected'] = True

    def restart_worker(self):
        old = int(base.unit_info()['MainPID'])
        base.command(['systemctl', 'restart', '--no-block', base.UNIT])
        def ready():
            unit = base.unit_info()
            if unit['ActiveState'] == 'active' and unit['SubState'] == 'running' and int(unit['MainPID']) != old:
                try:
                    return base.process_info(unit)
                except (GuardError, FileNotFoundError):
                    return False
            return False
        self.wait(self.args.recovery_timeout, ready, 'worker_restart_deadline')

    def counts(self):
        return {name: self.rows(sql, (self.run,))[0]['n'] for name, sql in {
            'attempts': 'SELECT COUNT(*) n FROM attempts WHERE run_id=?',
            'model_calls': 'SELECT COUNT(*) n FROM usage WHERE run_id=?',
            'live_tokens': 'SELECT COUNT(*) n FROM run_tokens WHERE run_id=? AND revoked=0'}.items()}

    def crash(self):
        self.stage = 'await-complete-archive-and-observed-503'
        def ready():
            row = self.state()
            require(self.active() <= {self.run}, 'other_work_appeared')
            require(row['state'] in ('queued', 'running', 'uploading'), 'run_failed_before_archive')
            if row['state'] != 'uploading':
                return False
            self.attempt = row['attempt']
            cache = base.DATA / 'cache' / self.run / str(self.attempt)
            if not (cache / 'pending.json').exists():
                return False
            version = self.rows('SELECT version_id FROM batches WHERE id=?', (self.batch,))[0]['version_id']
            p, sha, size, owned = validate_pending(cache, row, version,
                                                  self.report['submission_sha256'], self.receipt())
            attempts = self.rows('SELECT id,state FROM attempts WHERE run_id=? AND number=1', (self.run,))
            require(attempts == [{'id': p['lease']['attempt_id'], 'state': 'running'}],
                    'pending_attempt_not_current')
            key = '/'.join([self.team_id, version, self.batch, self.run, str(self.attempt) + '.tar.gz'])
            observed = self.fault.matching(key, sha, size)
            return (p, sha, size, owned, observed, key) if observed else False
        self.pending, sha, size, self.owned, observed, key = self.wait(
            self.args.phase_timeout, ready, 'archive_or_503_not_observed_no_kill')
        self.before_counts = self.counts()
        require(self.before_counts['attempts'] == 1 and self.before_counts['model_calls'] > 0,
                'missing_real_execution_evidence')
        self.receipt_sha = sha_file(base.DATA / 'scientific' / f'{self.run}_1.json', 65536)[0]
        require(self.active() == {self.run}, 'not_exclusive_at_kill')
        require(base.inventory(self.args.reconcile) == self.expected, 'release_changed_before_kill')
        require(base.digest(base.SCIENCE / 'competition-platform/platform/scripts/science/task-release-pin.json')
                == self.full_expected['release_pin_sha256'], 'release_pin_changed_before_kill')
        unit = base.unit_info()
        processes = base.process_info(unit)
        self.report.update(run_id=self.run, attempt=1, archive_sha256=sha, archive_bytes=size,
                           observed_complete_uploads_rejected_503=observed,
                           sandboxes_destroyed_before_kill=3, counts_before_kill=self.before_counts,
                           archive_key_sha256=hashlib.sha256(key.encode()).hexdigest())
        self.stage = 'worker-single-SIGKILL'
        self.killed = True
        when = time.time()
        result = subprocess.run(['systemctl', 'kill', '--kill-whom=all', '--signal=SIGKILL', base.UNIT],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15)
        observations = []
        self.wait(30, lambda: base.restarted_worker(int(unit['MainPID']), int(unit['NRestarts']),
                  [p['pid'] for p in processes], self.expected['sha256']['worker'], observations),
                  'automatic_worker_restart_not_proven')
        events = base.kill_journal(when)
        self.report['kill_confirmation'] = base.classify_kill(result.returncode, events, int(unit['MainPID']))
        self.report['automatic_restart_verified'] = True
        # The restarted worker is still pointed at the 503 sink: prove it resumed
        # this same exact archive before restoring the real provider.
        self.stage = 'restarted-worker-retries-original-archive'
        post_restart_observed = self.fault.matching(key, sha, size)
        self.wait(90, lambda: self.fault.matching(key, sha, size) > post_restart_observed,
                  'restarted_worker_did_not_retry_pending_archive')
        require(self.state()['state'] == 'uploading' and self.state()['attempt'] == 1,
                'attempt_changed_during_outage')
        require(self.counts()['model_calls'] == self.before_counts['model_calls'], 'model_reexecuted_after_crash')
        self.restore_fault()

    def restore_fault(self):
        changed = self.saved is not None or self.dropin_owned or self.restore_restart_needed
        if self.saved is not None:
            self.saved.restore()
            self.saved = None
            self.report['config_bytes_mode_owner_restored'] = True
        if self.dropin_owned:
            DROPIN.unlink()
            self.dropin_owned = False
        if changed:
            base.command(['systemctl', 'daemon-reload'])
            self.restart_worker()
            self.report['original_R2_service_restored'] = True
            self.restore_restart_needed = False
        if self.fault:
            self.fault.shutdown()
            self.fault.server_close()
            self.thread.join(timeout=5)
            self.fault = None
        if self.runtime:
            shutil.rmtree(self.runtime)
            self.runtime = None

    def finish(self):
        self.stage = 'native-upload-only-recovery'
        def completed():
            row = self.state()
            require(row['attempt'] == 1 and row['state'] in ('uploading', 'completed'), 'unexpected_recovery_state')
            return row if row['state'] == 'completed' and row['batch_status'] == 'completed' else False
        row = self.wait(self.args.recovery_timeout, completed, 'real_R2_recovery_deadline')
        require(row['object_sha'] == self.report['archive_sha256']
                and row['reward'] == self.pending['reward'] and row['seconds'] == self.pending['seconds'],
                'recovered_result_changed')
        counts = self.counts()
        require(counts['attempts'] == 1 and counts['model_calls'] == self.before_counts['model_calls']
                and counts['live_tokens'] == 0, 'execution_repeated_or_tokens_live')
        require(self.rows('SELECT delta,action FROM ledger WHERE batch_id=?', (self.batch,))
                == [{'delta': -1, 'action': 'accepted'}], 'ledger_not_single_original_debit')
        require(sha_file(base.DATA / 'scientific' / f'{self.run}_1.json', 65536)[0] == self.receipt_sha,
                'resource_receipt_changed_after_crash')
        self.wait(30, lambda: not (base.DATA / 'cache' / self.run / '1').exists(), 'cache_not_cleaned')
        require(self.run not in self.active(), 'slot_not_released')
        batch = self.api(self.team, 'GET', '/api/batches/' + self.batch)
        require(batch['status'] == 'completed', 'participant_result_not_completed')
        board = self.api(self.team, 'GET', '/api/leaderboard')
        require(not any(t['team_id'] == self.team_id for t in board['teams']), 'diagnostic_entered_ranking')
        quota = self.api(self.team, 'GET', '/api/me')['quotas']
        require(quota == {**self.report['initial_quota'], 'infra': self.report['initial_quota']['infra'] - 1},
                'quota_changed_unexpectedly')
        # Independent read-only R2 stream/hash validation, not merely the DB row.
        import sys
        sys.path.insert(0, str(Path(__file__).parent / 'science'))
        import verify_cloud_relay_evidence as evidence
        index = evidence.lookup_run(base.DATA / 'platform.db',
                                    readback_request(self.batch, self.report['archive_sha256']))
        archive = evidence.fetch_archive(evidence.read_r2_config(Path('/etc/qiqc-staging/api-r2.env')), index)
        from task_release import identity, verify_release
        pin = base.SCIENCE / 'competition-platform/platform/scripts/science/task-release-pin.json'
        release = identity(verify_release(base.SCIENCE, pin_path=pin), 'tunable_coupler_cz_netzero')
        checked = evidence.inspect_archive(archive, index, release)
        self.report.update(status='passed', terminal_state='completed', counts_after_recovery=counts,
                           independent_readback=True, standard_harbor_rollout=True,
                           readback_archive_sha256=index['sha256'], r2_archive_sha256=index['sha256'], qsim_tools=checked['qsim_tools'],
                           benchmark_release=release, benchmark_release_verified=checked['benchmark_release_verified'],
                           quota_debits=1, cache_removed=True, resource_receipt_unchanged=True,
                           solver_reexecuted=False, final_quota=quota)

    def cleanup(self):
        # Restore local service/config even when an assertion fails. Never cancel
        # a resumable pending upload; the native worker should finish that run.
        self.restore_fault()
        if self.batch is None and self.team_id and self.idem:
            accepted = self.rows('SELECT id FROM batches WHERE team_id=? AND idem=?', (self.team_id, self.idem))
            require(len(accepted) <= 1, 'ambiguous_owned_request')
            if accepted:
                self.batch = accepted[0]['id']
        if not self.batch:
            self.report['finally_cleanup_completed'] = True
            return
        row = self.state()
        if row['state'] in ('completed', 'cancelled'):
            self.report['finally_cleanup_completed'] = True
            return
        if row['state'] == 'uploading':
            self.wait(self.args.recovery_timeout, lambda: self.state()['state'] == 'completed',
                      'owned_upload_remains_operator_attention')
            self.report['finally_cleanup_completed'] = True
            return
        # Only the independently created, exact owned batch may be cancelled and
        # reconciled if failure occurred before durable upload readiness.
        super().cleanup()

    def artifact_observation(self):
        if self.run:
            self.report['final_owned_state'] = self.state()['state']
        self.report['local_fault_config_remaining'] = self.saved is not None or self.dropin_owned or self.restore_restart_needed


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--reconcile', type=Path, required=True)
    parser.add_argument('--inventory', action='store_true')
    parser.add_argument('--execute', action='store_true')
    parser.add_argument('--expected-inventory', type=Path)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--phase-timeout', type=int, default=900)
    parser.add_argument('--recovery-timeout', type=int, default=240)
    args = parser.parse_args()
    require(args.reconcile.is_absolute() and args.reconcile.is_file(), 'absolute_reconcile_binary_required')
    require(60 <= args.phase_timeout <= 1200 and 90 <= args.recovery_timeout <= 600, 'timeout_out_of_bounds')
    if args.inventory:
        require(not args.execute, 'inventory_and_execute_exclusive')
        print(json.dumps(inventory(args.reconcile), indent=2))
        return 0
    require(args.execute and args.expected_inventory and args.output, 'explicit_execution_inventory_output_required')
    # Cooperating copies of THIS driver cannot race; other operators must retain
    # exclusive staging access. Actual active runs are checked independently.
    with open('/run/lock/qiqc-scientific-r2-drill.lock', 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        def terminate(_signum, _frame):
            raise GuardError('operator_interrupted')
        signal.signal(signal.SIGTERM, terminate)
        return R2Drill(args).execute()


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(json.dumps({'status': 'failed', 'failure': str(exc) if isinstance(exc, GuardError) else type(exc).__name__}))
        raise SystemExit(2)
