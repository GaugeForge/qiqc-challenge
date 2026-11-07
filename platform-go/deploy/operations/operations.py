#!/usr/bin/env python3
"""Operator-only durable jobs and controlled reboot evidence. Never runs submitted Harnesses."""
from __future__ import annotations
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import socket
import sqlite3
import stat
import subprocess
import sys
import time
import urllib.request
import uuid

STATE = Path('/var/lib/qiqc-ops')
INSTALLED = Path('/usr/local/lib/qiqc-ops/operations.py')
CORE = [f'qiqc-native-{x}.service' for x in ('api', 'worker', 'gateway', 'review', 'scanner')]
PUBLIC_REBOOT_RECEIPT = Path('/root/QQ-Challenge/docs/operations/CONTROLLED-REBOOT-RESULT.json')
STABLE_TABLES = ('teams', 'users', 'versions', 'batches', 'runs', 'attempts', 'ledger', 'batch_policies', 'run_artifacts', 'batch_scientific_task_sets', 'run_scientific_targets')

def boot_id():
    return Path('/proc/sys/kernel/random/boot_id').read_text().strip()

def atomic_json(path, obj):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(obj, stream, ensure_ascii=False, sort_keys=True, indent=2)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)

def private_file(path):
    path = Path(path)
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o077:
        raise ValueError('operator file must be root-owned regular file, mode 0600 or stricter')
    return path

def validate_manifest(obj):
    if not isinstance(obj, dict) or set(obj) - {'label', 'argv', 'cwd', 'timeout_seconds', 'environment_file'}:
        raise ValueError('unsupported manifest fields')
    argv = obj.get('argv')
    if not isinstance(argv, list) or not argv or not all(isinstance(x, str) and '\0' not in x for x in argv):
        raise ValueError('argv must be a nonempty list of strings without NUL')
    if not Path(argv[0]).is_absolute():
        raise ValueError('executable must be absolute')
    cwd = obj.get('cwd')
    if not isinstance(cwd, str) or not Path(cwd).is_absolute() or not Path(cwd).is_dir():
        raise ValueError('cwd must be an existing absolute directory')
    timeout = obj.get('timeout_seconds', 7200)
    if type(timeout) is not int or not 1 <= timeout <= 604800:
        raise ValueError('timeout_seconds must be between 1 and 604800')
    label = obj.get('label', 'operator-job')
    if not isinstance(label, str) or not re.fullmatch(r'[A-Za-z0-9_. -]{1,100}', label):
        raise ValueError('label must be a short safe display name')
    envfile = obj.get('environment_file')
    if envfile is not None:
        if not isinstance(envfile, str) or not Path(envfile).is_absolute():
            raise ValueError('environment_file must be absolute')
        private_file(envfile)
    return {**obj, 'label': label, 'timeout_seconds': timeout}

def job_dir(job_id):
    if not re.fullmatch(r'[0-9a-f]{32}', job_id):
        raise ValueError('invalid job ID')
    return STATE / 'jobs' / job_id

def load_environment(path):
    env = {'PATH': '/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin', 'HOME': '/root', 'LANG': 'C.UTF-8'}
    if path:
        # Deliberately data-only KEY=value format; no shell evaluation or interpolation.
        for line in private_file(path).read_text().splitlines():
            if not line.strip() or line.lstrip().startswith('#'):
                continue
            key, sep, value = line.partition('=')
            if not sep or not re.fullmatch('[A-Za-z_][A-Za-z0-9_]*', key):
                raise ValueError('invalid environment file syntax')
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            env[key] = value
    return env

def submit(manifest_path, parent_id=None):
    manifest = validate_manifest(json.loads(private_file(manifest_path).read_text()))
    identifier = uuid.uuid4().hex
    folder = job_dir(identifier)
    folder.mkdir(parents=True, mode=0o700)
    atomic_json(folder / 'manifest.json', manifest)
    receipt = {'job_id': identifier, 'label': manifest['label'], 'state': 'queued', 'created_at': time.time(), 'boot_id': boot_id(), 'parent_job_id': parent_id}
    atomic_json(folder / 'status.json', receipt)
    result = subprocess.run(['systemctl', 'start', '--no-block', f'qiqc-ops-job@{identifier}.service'], capture_output=True)
    if result.returncode:
        receipt.update(state='launch_failed', finished_at=time.time())
        atomic_json(folder / 'status.json', receipt)
        raise RuntimeError('systemd launch failed; inspect system journal locally')
    return receipt

def run_job(identifier):
    folder = job_dir(identifier)
    with open(folder / 'lock', 'a') as lock:
        os.chmod(folder / 'lock', 0o600)
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        receipt = json.loads((folder / 'status.json').read_text())
        if receipt['state'] != 'queued' or receipt['boot_id'] != boot_id():
            raise RuntimeError('job is not queued in this boot; explicit rerun required')
        manifest = validate_manifest(json.loads(private_file(folder / 'manifest.json').read_text()))
        receipt.update(state='running', started_at=time.time())
        atomic_json(folder / 'status.json', receipt)
        child = None
        interrupted = False
        def stop(signum, frame):
            nonlocal interrupted
            interrupted = True
            receipt.update(state='interrupted', finished_at=time.time(), reason='service_stop_no_automatic_rerun')
            atomic_json(folder / 'status.json', receipt)
            if child and child.poll() is None:
                child.terminate()
        previous = {s: signal.signal(s, stop) for s in (signal.SIGTERM, signal.SIGINT)}
        try:
            log_fd = os.open(folder / 'output.log', os.O_CREAT | os.O_WRONLY | os.O_APPEND, 0o600)
            with os.fdopen(log_fd, 'wb', buffering=0) as output:
                child = subprocess.Popen(manifest['argv'], cwd=manifest['cwd'], env=load_environment(manifest.get('environment_file')), stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.STDOUT)
                try:
                    code = child.wait(timeout=manifest['timeout_seconds'])
                    state = 'interrupted' if interrupted else ('succeeded' if code == 0 else 'failed')
                except subprocess.TimeoutExpired:
                    child.terminate()
                    try:
                        child.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        child.kill()
                        child.wait()
                    state, code = 'timed_out', child.returncode
                receipt.update(state=state, exit_code=code, finished_at=time.time())
        except Exception as error:
            receipt.update(state='failed', error_type=type(error).__name__, finished_at=time.time())
        finally:
            for sig, old in previous.items():
                signal.signal(sig, old)
            atomic_json(folder / 'status.json', receipt)
        return receipt

def reconcile_jobs():
    changed = []
    for path in sorted((STATE / 'jobs').glob('*/status.json')):
        receipt = json.loads(path.read_text())
        if receipt['state'] in ('queued', 'running') and receipt['boot_id'] != boot_id():
            receipt.update(state='interrupted', finished_at=time.time(), reason='host_reboot_no_automatic_rerun')
            atomic_json(path, receipt)
            changed.append(receipt['job_id'])
    return {'interrupted_jobs': changed, 'automatic_reruns': 0}

def file_inventory(folder):
    result = {}
    for path in sorted(Path(folder).rglob('*')):
        if path.is_symlink():
            raise ValueError('symlink in upload inventory')
        if path.is_file():
            digest = hashlib.sha256()
            with path.open('rb') as stream:
                for block in iter(lambda: stream.read(1024 * 1024), b''):
                    digest.update(block)
            result[str(path.relative_to(folder))] = {'size': path.stat().st_size, 'sha256': digest.hexdigest()}
    return result

def db_snapshot(database):
    with sqlite3.connect(Path(database).absolute().as_uri() + '?mode=ro', uri=True, timeout=30) as conn:
        conn.execute('BEGIN')
        check = [r[0] for r in conn.execute('PRAGMA quick_check')]
        if check != ['ok']:
            raise RuntimeError('database quick_check failed')
        active = conn.execute("SELECT COUNT(*) FROM runs WHERE state IN ('queued','running','uploading')").fetchone()[0]
        pending = conn.execute("SELECT COUNT(*) FROM versions WHERE status='pending_review'").fetchone()[0]
        active_batches = conn.execute("SELECT COUNT(*) FROM batches WHERE status IN ('queued','testing','cancelling')").fetchone()[0]
        active_tokens = conn.execute("SELECT COUNT(*) FROM run_tokens WHERE revoked=0 AND expires>?", (time.time(),)).fetchone()[0]
        tables = {}
        for table in STABLE_TABLES:
            # Fixed identifiers only. Sorted row encodings make digest independent of query order.
            rows = sorted(json.dumps(tuple(row), ensure_ascii=False, separators=(',', ':'), default=lambda x: {'bytes_hex': x.hex()}) for row in conn.execute(f'SELECT * FROM "{table}"'))
            digest = hashlib.sha256(('\n'.join(rows) + '\n').encode()).hexdigest()
            tables[table] = {'rows': len(rows), 'sha256': digest}
        return {'quick_check': 'ok', 'active_runs': active, 'pending_reviews': pending, 'active_batches': active_batches, 'active_tokens': active_tokens, 'tables': tables}

def service_states(units):
    result = {}
    for unit in units:
        result[unit] = {'active': subprocess.run(['systemctl', 'is-active', unit], capture_output=True, text=True).stdout.strip(), 'enabled': subprocess.run(['systemctl', 'is-enabled', unit], capture_output=True, text=True).stdout.strip()}
    return result

def prepare_reboot(database, uploads, https_url, extra_units=None, disabled_units=None):
    if not https_url.startswith('https://'):
        raise ValueError('HTTPS URL required')
    snapshot = db_snapshot(database)
    if any(snapshot[k] for k in ('active_runs', 'pending_reviews', 'active_batches', 'active_tokens')):
        raise RuntimeError('reboot refused: pending or active work exists')
    jobs = []
    for p in (STATE / 'jobs').glob('*/status.json'):
        r = json.loads(p.read_text())
        if r['state'] in ('queued', 'running'):
            jobs.append(r['job_id'])
    if jobs:
        raise RuntimeError('reboot refused: operator jobs still active')
    units = CORE + ['nginx.service'] + (extra_units or [])
    services = service_states(units)
    if any(s != {'active': 'active', 'enabled': 'enabled'} for s in services.values()):
        raise RuntimeError('required services must be active and enabled before reboot')
    disabled_services = service_states(disabled_units or [])
    if any(s != {'active': 'inactive', 'enabled': 'disabled'} for s in disabled_services.values()):
        raise RuntimeError('retired services must be inactive and disabled')
    previous = STATE / 'reboot' / 'before.json'
    if previous.exists():
        previous_data = json.loads(previous.read_text())
        atomic_json(STATE / 'reboot' / f"before-{previous_data['prepared_at_ns']}.json", previous_data)
    receipt = {'schema': 'qiqc-controlled-reboot/v1', 'prepared_at_ns': time.time_ns(), 'boot_id': boot_id(), 'database': str(Path(database).resolve()), 'uploads': str(Path(uploads).resolve()), 'https_url': https_url, 'units': units, 'disabled_units': disabled_units or [], 'database_snapshot': snapshot, 'upload_inventory': file_inventory(uploads), 'services': services}
    atomic_json(previous, receipt)
    return {'prepared': True, 'boot_id': receipt['boot_id'], 'stable_tables': {k: v['rows'] for k, v in snapshot['tables'].items()}, 'upload_files': len(receipt['upload_inventory']), 'reboot_triggered': False}

def postcheck_once(before):
    checks = {'boot_id_changed': before['boot_id'] != boot_id()}
    services = service_states(before['units'])
    disabled_services = service_states(before.get('disabled_units', []))
    checks['retired_services_disabled'] = all(s == {'active': 'inactive', 'enabled': 'disabled'} for s in disabled_services.values())
    checks['services_active_enabled'] = all(s == {'active': 'active', 'enabled': 'enabled'} for s in services.values())
    for port in (19890, 443):
        try:
            with socket.create_connection(('127.0.0.1', port), timeout=3):
                checks[f'port_{port}'] = True
        except OSError:
            checks[f'port_{port}'] = False
    try:
        # TLS validation is required; no insecure/self-signed bypass.
        with urllib.request.urlopen(before['https_url'], timeout=10) as response:
            checks['https_reachable'] = response.status == 200
    except Exception:
        checks['https_reachable'] = False
    snapshot = db_snapshot(before['database'])
    checks['database_quick_check'] = snapshot['quick_check'] == 'ok'
    checks['stable_database_rows_unchanged'] = snapshot['tables'] == before['database_snapshot']['tables']
    checks['queue_empty'] = all(snapshot[k] == 0 for k in ('active_runs', 'pending_reviews', 'active_batches', 'active_tokens'))
    checks['uploads_unchanged'] = file_inventory(before['uploads']) == before['upload_inventory']
    return {'schema': 'qiqc-controlled-reboot-result/v1', 'checked_at': time.time(), 'before_boot_id': before['boot_id'], 'after_boot_id': boot_id(), 'checks': checks, 'passed': all(checks.values()), 'services': services, 'retired_services': disabled_services, 'database_snapshot': snapshot}

def postcheck(wait_seconds):
    if not 0 <= wait_seconds <= 240:
        raise ValueError('wait_seconds must be between 0 and 240')
    path = STATE / 'reboot' / 'before.json'
    if not path.exists():
        return {'skipped': True, 'reason': 'no_prepared_reboot'}
    before = json.loads(path.read_text())
    deadline = time.monotonic() + wait_seconds
    while True:
        try:
            result = postcheck_once(before)
        except Exception as error:
            result = {'passed': False, 'error_type': type(error).__name__, 'checked_at': time.time(), 'after_boot_id': boot_id()}
        atomic_json(STATE / 'reboot' / 'after.json', result)
        if result['passed'] or time.monotonic() >= deadline or before['boot_id'] == boot_id():
            atomic_json(STATE / 'reboot' / f"after-{before['prepared_at_ns']}.json", result)
            public = {k: v for k, v in result.items() if k != 'database_snapshot'}
            public['private_receipt'] = str(STATE / 'reboot' / 'after.json')
            atomic_json(PUBLIC_REBOOT_RECEIPT, public)
            return result
        time.sleep(5)

def install():
    STATE.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(STATE, 0o700)
    (STATE / 'jobs').mkdir(exist_ok=True, mode=0o700)
    INSTALLED.parent.mkdir(parents=True, exist_ok=True)
    if Path(__file__).resolve() != INSTALLED:
        shutil.copyfile(__file__, INSTALLED)
    os.chmod(INSTALLED, 0o755)
    units = {
        'qiqc-ops-job@.service': '''[Unit]\nDescription=QIQC operator job %i\nAfter=network-online.target\nWants=network-online.target\n[Service]\nType=exec\nUser=root\nUMask=0077\nExecStart=/usr/bin/python3 /usr/local/lib/qiqc-ops/operations.py run %i\nRestart=no\nKillMode=control-group\nTimeoutStopSec=20\nStandardOutput=null\nStandardError=journal\nNoNewPrivileges=yes\n''',
        'qiqc-ops-job-reconcile.service': '''[Unit]\nDescription=Mark interrupted QIQC operator jobs without restarting cloud jobs\nAfter=local-fs.target\n[Service]\nType=oneshot\nUser=root\nUMask=0077\nExecStart=/usr/bin/python3 /usr/local/lib/qiqc-ops/operations.py reconcile\nRemainAfterExit=yes\n[Install]\nWantedBy=multi-user.target\n''',
        'qiqc-ops-reboot-check.service': '''[Unit]\nDescription=Validate QIQC controlled reboot recovery\nAfter=network-online.target nginx.service qiqc-native-api.service qiqc-native-worker-key2.service qiqc-native-worker-key1.service qiqc-native-gateway.service qiqc-native-review.service qiqc-native-scanner.service\nWants=network-online.target\n[Service]\nType=oneshot\nUser=root\nUMask=0077\nExecStart=/usr/bin/python3 /usr/local/lib/qiqc-ops/operations.py reboot-postcheck --wait-seconds 240\nTimeoutStartSec=300\nRemainAfterExit=yes\n[Install]\nWantedBy=multi-user.target\n'''}
    for name, body in units.items():
        path = Path('/etc/systemd/system') / name
        path.write_text(body)
        path.chmod(0o644)
    subprocess.run(['systemctl', 'daemon-reload'], check=True)
    subprocess.run(['systemctl', 'enable', 'qiqc-ops-job-reconcile.service', 'qiqc-ops-reboot-check.service'], check=True, capture_output=True)
    return {'installed': sorted(units), 'jobs_auto_restart': False, 'reboot_triggered': False}

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('install')
    p = sub.add_parser('submit'); p.add_argument('--manifest', required=True)
    for name in ('run', 'status', 'rerun'):
        p = sub.add_parser(name); p.add_argument('job_id')
    sub.add_parser('reconcile')
    p = sub.add_parser('reboot-prepare')
    p.add_argument('--database', default='/opt/qiqc-native-candidate/data/platform.db')
    p.add_argument('--uploads', default='/opt/qiqc-native-candidate/data/uploads')
    p.add_argument('--https-url', required=True)
    p.add_argument('--extra-unit', action='append', default=[])
    p.add_argument('--disabled-unit', action='append', default=[])
    p = sub.add_parser('reboot-postcheck'); p.add_argument('--wait-seconds', type=int, default=0)
    args = parser.parse_args()
    if os.geteuid() != 0:
        parser.error('operator root access required')
    if args.command == 'install': result = install()
    elif args.command == 'submit': result = submit(args.manifest)
    elif args.command == 'run': result = run_job(args.job_id)
    elif args.command == 'status': result = json.loads((job_dir(args.job_id) / 'status.json').read_text())
    elif args.command == 'rerun':
        folder = job_dir(args.job_id)
        old = json.loads((folder / 'status.json').read_text())
        if old['state'] not in ('interrupted', 'timed_out', 'failed', 'launch_failed'):
            raise ValueError('only failed or interrupted jobs can be explicitly rerun')
        result = submit(folder / 'manifest.json', parent_id=args.job_id)
    elif args.command == 'reconcile': result = reconcile_jobs()
    elif args.command == 'reboot-prepare': result = prepare_reboot(args.database, args.uploads, args.https_url, args.extra_unit, args.disabled_unit)
    else: result = postcheck(args.wait_seconds)
    print(json.dumps(result, sort_keys=True, ensure_ascii=False))
    if result.get('passed') is False or result.get('state') in ('failed', 'timed_out', 'interrupted'):
        return 1
    return 0

if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception as exc:
        print(json.dumps({'error_type': type(exc).__name__, 'message': 'Operation failed; inspect private operator state. No command or secret values are printed.'}), file=sys.stderr)
        sys.exit(1)
