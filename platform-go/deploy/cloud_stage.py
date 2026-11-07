#!/usr/bin/env python3
"""Upgrade only local qiqc-stage services; failures after stop stay stopped.

Build a complete candidate, refuse in-flight work, stop services, take a local
consistent recovery snapshot, then replace assets and start one coherent release.
This is not a production deployment or an off-site disaster-recovery backup.
"""
import datetime
import grp
import hashlib
import json
import os
from pathlib import Path
import pwd
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
import urllib.request
from urllib.parse import urlparse

REPO = Path(__file__).resolve().parents[1]
WORKSPACE = REPO.parents[1]
RELEASE = Path('/opt/qiqc-staging/current')
SCIENCE = Path('/opt/qiqc-staging/science')
CONF = Path('/etc/qiqc-staging')
DATA = Path('/var/lib/qiqc-staging/data')
BACKUPS = Path('/var/backups/qiqc-staging')
UNIT_DIR = Path('/etc/systemd/system')
GROUP = 'qiqc-stage-data'
WORKER_KINDS = ('worker-key2', 'worker-key1')
KINDS = ('api', *WORKER_KINDS, 'gateway', 'review', 'scanner')
LEGACY_WORKER = 'qiqc-stage-worker.service'
ACCEPTANCE_SLOTS = int(os.environ.get('QIQC_ACCEPTANCE_TASK_SLOTS', '1'))
if ACCEPTANCE_SLOTS not in (1, 2, 4):
    raise ValueError('Local bounded acceptance supports only one, two or four task slots')
SERVICES = tuple('qiqc-stage-' + kind + '.service' for kind in KINDS)
SCIENCE_DIRS = (
    'competition-platform/platform/scripts/science', 'benchmark-assets/adapters/science-probe',
    'benchmark-assets/qiqcbench-runtime', 'benchmark-assets/benchmark-releases',
)


def command(args, **kwargs):
    return subprocess.run(args, check=True, **kwargs)


def envfile(path):
    result = {}
    for line in path.read_text().splitlines():
        line = line.strip().removeprefix('export ')
        if not line or line.startswith('#') or '=' not in line:
            continue
        key, value = line.split('=', 1)
        result[key.strip()] = value.strip().strip('\"\'')
    return result


def validate_secret(value):
    if not value or len(value) > 1024 or any(char in value for char in '\n\r\x00'):
        raise ValueError('Missing or malformed private configuration')
    return value


def validate_model(value):
    if value not in {'tencent/hy4', 'tencent/hy4-preview'}:
        raise ValueError('Unsupported frozen Hy4 model')
    return value


def validate_model_url(value):
    parsed = urlparse(value)
    if (parsed.scheme, parsed.netloc, parsed.path, parsed.query, parsed.fragment) != ('https', 'openrouter.ai', '/api/v1/chat/completions', '', ''):
        raise ValueError('Unsupported frozen model endpoint')
    return value


def private_payload(name, user, payload):
    target = CONF / name
    descriptor, temporary = tempfile.mkstemp(prefix='.' + name + '-', dir=CONF)
    temp = Path(temporary)
    try:
        with os.fdopen(descriptor, 'w') as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.chown(temp, pwd.getpwnam(user).pw_uid, grp.getgrnam(GROUP).gr_gid)
        temp.chmod(0o600)
        temp.replace(target)
    finally:
        temp.unlink(missing_ok=True)


def private_config(name, user, entries):
    private_payload(name, user, ''.join(key + '=' + validate_secret(value) + '\n'
                                       for key, value in entries.items()))


def copy_tree(source, destination):
    """Copy only regular organizer assets, rejecting links before following any."""
    if not source.is_dir() or source.is_symlink():
        raise RuntimeError('Required release directory is absent or a symlink: ' + str(source))
    for path in source.rglob('*'):
        if path.is_symlink() or not (path.is_file() or path.is_dir()):
            raise RuntimeError('Release directory contains a non-regular asset')
    shutil.copytree(source, destination, ignore=shutil.ignore_patterns(
        '__pycache__', '*.pyc', '.env', 'test-results', '*.log'))


def check_idle():
    """Read-only: never pause, cancel, change a lease or rewrite a quota."""
    database = DATA / 'platform.db'
    if database.is_symlink() or not database.is_file():
        raise RuntimeError('Existing regular staging database required')
    with sqlite3.connect(database.as_uri() + '?mode=ro', uri=True, timeout=10) as db:
        active = db.execute("""SELECT COUNT(*) FROM runs WHERE
            state IN ('queued','running','uploading') OR
            (state='attention' AND error='worker_lost:reconciliation_required')""").fetchone()[0]
        unsettled = db.execute("SELECT COUNT(*) FROM batches WHERE status IN ('queued','testing','cancelling')").fetchone()[0]
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        reviewing = 0
        if 'review_advisories' in tables:
            reviewing = db.execute("SELECT COUNT(*) FROM review_advisories WHERE state='running' AND lease_until>?", (time.time(),)).fetchone()[0]
    if active or unsettled or reviewing:
        raise RuntimeError('Staging is not idle; accepted work, unresolved resources or review lease remains')
    for relative in ('cache', 'failure-evidence'):
        if any((DATA / relative).rglob('pending.json')) or any((DATA / relative).rglob('staged.json')):
            raise RuntimeError('Durable evidence still requires recovery before upgrading')
    if any((DATA / 'quarantine').glob('incoming-*.zip')):
        raise RuntimeError('An upload inspection is still in progress or requires cleanup')


def stop_services():
    installed = [service for service in SERVICES if (UNIT_DIR / service).exists()]
    if (UNIT_DIR / LEGACY_WORKER).exists():
        installed.append(LEGACY_WORKER)
    if installed:
        command(['systemctl', 'stop', *installed], timeout=510)
    for service in installed:
        result = subprocess.run(['systemctl', 'is-active', service], capture_output=True,
                                text=True, check=False, timeout=10)
        if result.stdout.strip() in {'active', 'activating', 'deactivating', 'reloading'}:
            raise RuntimeError('Staging service did not fully stop')


def prepare_candidate(directory):
    release, science = directory / 'release', directory / 'science'
    release.mkdir()
    science.mkdir()
    for relative in SCIENCE_DIRS:
        copy_tree(WORKSPACE / relative, science / relative)
    subprocess.run([sys.executable, str(science / 'competition-platform/platform/scripts/science/task_release.py'),
                    '--project-root', str(science)], check=True, capture_output=True, text=True)
    for source in (WORKSPACE / 'benchmark-assets/adapters').glob('*-runtime.zip'):
        shutil.copyfile(source, science / 'benchmark-assets/adapters' / source.name)
    for source, name in ((REPO.parent / 'dist', 'site'),
                         (REPO.parent / 'platform/web', 'web'),
                         (REPO.parent / 'adapters', 'adapters')):
        copy_tree(source, release / name)
    for source, name in ((REPO.parent / 'platform/qiqc/archive.py', 'archive.py'),
                         (Path(__file__).with_name('scanner_helper.py'), 'scanner_helper.py'),
                         (WORKSPACE / 'docs/baselines/gpt-6-astra-xhigh-normal-39-final/baseline.json', 'baseline-astra-39.json'),
                         (science / 'benchmark-assets/adapters/science-probe/allowed-submissions.json', 'science-submissions.json')):
        if source.is_symlink() or not source.is_file():
            raise RuntimeError('Required regular release file missing')
        shutil.copyfile(source, release / name)
    manifest = json.loads((release / 'science-submissions.json').read_text())
    for adapter, public in (('codex', 'codex'), ('pi-agent', 'pi'), ('deepseek-harness', 'deepseek')):
        digest = manifest[adapter]['sha256']
        for archive in (science / 'benchmark-assets/adapters' / manifest[adapter]['archive'],
                        release / 'adapters' / public / 'harness.zip'):
            if hashlib.sha256(archive.read_bytes()).hexdigest() != digest:
                raise RuntimeError('Public and executable adapter bytes do not match')
    if 'qiqc-archive/2' not in (release / 'archive.py').read_text():
        raise RuntimeError('The release needs scanner ruleset 2 with provenance')
    for name, package in (('api', '.'), ('worker', './cmd/qiqc-worker'),
                          ('gateway', './cmd/qiqc-model-gateway'), ('admin', './cmd/qiqc-admin'),
                          ('review-worker', './cmd/review-worker')):
        command(['/usr/bin/go', 'build', '-o', str(release / name), package], cwd=REPO)
    return release, science


def recovery_snapshot():
    """Only called with all staging services stopped and check_idle passed."""
    BACKUPS.mkdir(parents=True, exist_ok=True, mode=0o700)
    if BACKUPS.is_symlink() or BACKUPS.stat().st_uid != os.geteuid():
        raise RuntimeError('Backup root must be owned by the operator and must not be a symlink')
    BACKUPS.chmod(0o700)
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%SZ-')
    backup = Path(tempfile.mkdtemp(prefix=stamp, dir=BACKUPS))
    metadata = {}
    for source in (DATA, DATA / 'platform.db', DATA / 'uploads', RELEASE, SCIENCE, CONF):
        info = source.stat()
        metadata[str(source)] = {'uid': info.st_uid, 'gid': info.st_gid, 'mode': oct(stat.S_IMODE(info.st_mode))}
    with sqlite3.connect((DATA / 'platform.db').as_uri() + '?mode=ro', uri=True) as source:
        with sqlite3.connect(backup / 'platform.db') as target:
            source.backup(target)
            if target.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                raise RuntimeError('Recovery database integrity check failed')
    for source, name in ((DATA / 'uploads', 'uploads'), (RELEASE, 'release'),
                         (SCIENCE, 'science'), (CONF, 'config')):
        copy_tree(source, backup / name)
    # Completed/error controller receipts support later diagnosis after rollback.
    for relative in ('scientific', 'failure-evidence'):
        if (DATA / relative).exists():
            copy_tree(DATA / relative, backup / relative)
    (backup / 'units').mkdir()
    for service in SERVICES:
        source = UNIT_DIR / service
        if source.exists():
            shutil.copy2(source, backup / 'units' / service)
    ownership_paths = list(CONF.iterdir()) + list((DATA / 'uploads').rglob('*'))
    for path in ownership_paths:
        if path.is_file() or path.is_dir():
            info = path.stat()
            metadata[str(path)] = {'uid': info.st_uid, 'gid': info.st_gid, 'mode': oct(stat.S_IMODE(info.st_mode))}
    (backup / 'ownership.json').write_text(json.dumps(metadata, indent=2) + '\n')
    (backup / 'RESTORE.txt').write_text(
        'Root-only local recovery snapshot; not an off-site backup. Keep all qiqc-stage services stopped.\n'
        'Restore release/science, config and saved unit files together. Restore platform.db and uploads together; '
        'remove old platform.db-wal/platform.db-shm only after every DB user is stopped. '
        'Restore recorded ownership/modes from ownership.json; uploads and DB must keep the trusted shared group. '
        'New review identity/unit must stay stopped if absent from saved units. Run daemon-reload, start scanner first, '
        'then saved application services. Verify health, frozen ZIP hashes and permissions before reopening access.\n'
        'A failed upgrade never automatically restarts old binaries against a possibly migrated database.\n')
    for path in backup.rglob('*'):
        if path.is_file():
            with path.open('rb') as stream:
                os.fsync(stream.fileno())
    directories = sorted((path for path in backup.rglob('*') if path.is_dir()),
                         key=lambda path: len(path.parts), reverse=True)
    for directory in [*directories, backup, BACKUPS]:
        fd = os.open(directory, os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    return backup


def service_units(image, model='tencent/hy4-preview', model_url='https://openrouter.ai/api/v1/chat/completions'):
    model = validate_model(model)
    model_url = validate_model_url(model_url)
    hardening = '''NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectControlGroups=true
RestrictSUIDSGID=true
RestrictRealtime=true
LockPersonality=true
RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6
CapabilityBoundingSet=
AmbientCapabilities=
UMask=0007
Restart=on-failure
RestartSec=2
TimeoutStopSec=480
'''
    launches = {
        'api': f'{RELEASE}/api -standalone -listen 127.0.0.1:18890 -data {DATA} -site {RELEASE}/site -web {RELEASE}/web -adapters {RELEASE}/adapters -reference {RELEASE}/adapters/reference.zip -science-manifest {RELEASE}/science-submissions.json -scanner {RELEASE}/archive.py',
        'worker-key2': f'{RELEASE}/worker -data {DATA} -slots 20 -science-project {SCIENCE} -science-python /opt/qiqc-staging/venv/bin/python -model-gateway http://127.0.0.1:18889',
        'worker-key1': f'{RELEASE}/worker -data {DATA} -slots 100 -science-project {SCIENCE} -science-python /opt/qiqc-staging/venv/bin/python -model-gateway http://127.0.0.1:18889',
        'gateway': f'{RELEASE}/gateway -data {DATA} -provider-env {CONF}/provider.env -listen 127.0.0.1:18889',
        'review': f'{RELEASE}/review-worker --db {DATA}/platform.db --provider https --key-file {CONF}/review.key --model {model} --url {model_url} --timeout 120s',
    }
    units = {}
    for kind, launch in launches.items():
        extra = ''
        if kind == 'api' or kind in WORKER_KINDS:
            extra += f'Environment=QIQC_R2_ENABLED=1\nEnvironment=QIQC_R2_ENV={CONF}/{kind}-r2.env\n'
        if kind == 'api':
            extra += 'Environment=QIQC_SCANNER_SOCKET=/run/qiqc-stage-scanner/scanner.sock\n'
        if kind in WORKER_KINDS:
            pool = 'key2' if kind == 'worker-key2' else 'key1'
            extra += (f'SupplementaryGroups=qiqc-stage-science\nEnvironment=QIQC_PROJECT_ROOT={SCIENCE}\n'
                      f'Environment=QIQC_E2B_KEY_FILE={CONF}/worker-e2b.env\nEnvironment=PYTHONDONTWRITEBYTECODE=1\n'
                      f'Environment=QIQC_WORKER_POOL={pool}\n'
                      'Environment=HF_HUB_DISABLE_TELEMETRY=1\n')
        hidden = '-/run/docker.sock -/var/run/docker.sock'
        if kind == 'review':
            hidden += f' -{SCIENCE}' + ''.join(f' -{DATA}/{name}' for name in ('uploads', 'quarantine', 'cache', 'scientific', 'failure-evidence', 'objects'))
        units[f'qiqc-stage-{kind}.service'] = f'''[Unit]
Description=QIQC local staging {kind}
After=network.target qiqc-stage-scanner.service

[Service]
Type=simple
User={'qiqc-stage-worker' if kind in WORKER_KINDS else 'qiqc-stage-' + kind}
Group={GROUP}
Environment=QIQC_SHARED_DATA=1
Environment=QIQC_MODEL={model}
Environment=QIQC_BASELINE={RELEASE}/baseline-astra-39.json
WorkingDirectory={RELEASE}
ReadWritePaths={DATA}
InaccessiblePaths={hidden}
{extra}ExecStart={launch}
{hardening}
[Install]
WantedBy=multi-user.target
'''
    units['qiqc-stage-scanner.service'] = f'''[Unit]
Description=QIQC fixed archive scanner helper (staging only)
After=docker.service
Requires=docker.service

[Service]
Type=simple
User=root
Group={GROUP}
RuntimeDirectory=qiqc-stage-scanner
RuntimeDirectoryMode=0750
StateDirectory=qiqc-stage-scanner
StateDirectoryMode=0700
ExecStart=/usr/bin/python3 {RELEASE}/scanner_helper.py --socket /run/qiqc-stage-scanner/scanner.sock --quarantine {DATA}/quarantine --spool /var/lib/qiqc-stage-scanner --parser {RELEASE}/archive.py --image {image} --api-user qiqc-stage-api
ReadWritePaths=/run/qiqc-stage-scanner /var/lib/qiqc-stage-scanner
{hardening}
[Install]
WantedBy=multi-user.target
'''
    return units


def install_tree(candidate, destination, gid, private):
    displaced = destination.with_name(destination.name + '.pre-upgrade-' + str(time.time_ns()))
    destination.rename(displaced)
    candidate.rename(destination)
    for path in [destination, *destination.rglob('*')]:
        os.chown(path, 0, gid if private else 0)
        executable = path.name in {'api', 'worker', 'gateway', 'admin', 'review-worker'}
        path.chmod((0o750 if path.is_dir() else 0o640) if private else (0o755 if path.is_dir() or executable else 0o644))
    # A complete recovery copy already exists. Keep displaced assets until the
    # whole deployment succeeds; failures leave them available for an operator.
    return displaced


def start_services():
    command(['systemctl', 'daemon-reload'])
    subprocess.run(['systemctl', 'disable', LEGACY_WORKER], stdout=subprocess.DEVNULL,
                   stderr=subprocess.DEVNULL, check=False)
    command(['systemctl', 'enable', *SERVICES], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    command(['systemctl', 'start', 'qiqc-stage-scanner.service'])
    for _ in range(50):
        if Path('/run/qiqc-stage-scanner/scanner.sock').is_socket():
            break
        time.sleep(.1)
    else:
        raise RuntimeError('Scanner socket did not become ready')
    command(['systemctl', 'start', *(service for service in SERVICES if 'scanner' not in service)])
    for port in (18889, 18890):
        for _ in range(30):
            try:
                with urllib.request.urlopen(f'http://127.0.0.1:{port}/healthz', timeout=1) as response:
                    if response.status == 200:
                        break
            except OSError:
                pass
            time.sleep(.2)
        else:
            raise RuntimeError('Local staging health check failed')
    for service in SERVICES:
        command(['systemctl', 'is-active', '--quiet', service])


def main():
    if os.geteuid() != 0:
        raise SystemExit('Local staging operator privileges required')
    for path in (RELEASE, SCIENCE, CONF, DATA, UNIT_DIR):
        if not path.is_dir() or path.is_symlink():
            raise SystemExit('Existing regular local staging directories required')
    values = envfile(WORKSPACE / '.env')
    r2 = envfile(WORKSPACE / 'runtime/legacy-platform-data/r2.env')
    for key in ('E2B_API_KEY', 'E2B_API_KEY_2', 'HY_API_2', 'HY_URL_2', 'HY_MODEL'):
        validate_secret(values.get(key, ''))
    validate_model(values['HY_MODEL'])
    validate_model_url(values['HY_URL_2'])
    for key in ('R2_ENDPOINT', 'R2_ACCESS_KEY_ID', 'R2_SECRET_ACCESS_KEY', 'R2_BUCKET'):
        validate_secret(r2.get(key, ''))
    check_idle()
    image = subprocess.check_output(['/usr/bin/docker', 'image', 'inspect', 'python:3.13-slim', '--format', '{{.Id}}'], text=True).strip()
    if not image.startswith('sha256:') or len(image) != 71:
        raise SystemExit('Pinned local scanner image unavailable')
    if shutil.disk_usage(RELEASE.parent).free < 512 * 1024 * 1024:
        raise SystemExit('At least 512 MiB free staging workspace required')
    try:
        science_gid = grp.getgrnam('qiqc-stage-science').gr_gid
    except KeyError:
        command(['groupadd', '--system', 'qiqc-stage-science'])
        science_gid = grp.getgrnam('qiqc-stage-science').gr_gid
    command(['usermod', '-a', '-G', 'qiqc-stage-science', 'qiqc-stage-worker'])
    try:
        reviewer = pwd.getpwnam('qiqc-stage-review')
    except KeyError:
        command(['useradd', '--system', '--no-create-home', '--gid', GROUP, '--shell', '/usr/sbin/nologin', 'qiqc-stage-review'])
        reviewer = pwd.getpwnam('qiqc-stage-review')
    memberships = set(subprocess.check_output(['id', '-nG', 'qiqc-stage-review'], text=True).split())
    if reviewer.pw_uid == 0 or memberships != {GROUP}:
        raise SystemExit('Review identity must be non-root, data-group only, without Docker/science access')
    stopped = False
    backup = None
    with tempfile.TemporaryDirectory(prefix='.cloud-stage-candidate-', dir=RELEASE.parent) as temporary:
        candidate, science = prepare_candidate(Path(temporary))
        units = service_units(image, values['HY_MODEL'], values['HY_URL_2'])
        try:
            # Set the flag first: a partially failed systemctl stop must never
            # fall through to restarting a mixture of old and new services.
            stopped = True
            stop_services()
            check_idle()
            backup = recovery_snapshot()
            displaced = [install_tree(candidate, RELEASE, 0, False),
                         install_tree(science, SCIENCE, science_gid, True)]
            private_config('worker-e2b.env', 'qiqc-stage-worker', {'E2B_API_KEY': values['E2B_API_KEY'], 'E2B_API_KEY_2': values['E2B_API_KEY_2']})
            private_config('provider.env', 'qiqc-stage-gateway', {'HY_API': values['HY_API_2'], 'HY_URL': values['HY_URL_2'], 'HY_MODEL': values['HY_MODEL']})
            # Single key value, not an env file or shared platform config.
            # Automated review must use the same frozen production gateway
            # identity as the API/worker pool; never fall back to the legacy
            # unsuffixed development key.
            private_payload('review.key', 'qiqc-stage-review', validate_secret(values['HY_API_2']))
            for kind, owner in (('api', 'qiqc-stage-api'),
                                ('worker-key2', 'qiqc-stage-worker'),
                                ('worker-key1', 'qiqc-stage-worker')):
                private_config(kind + '-r2.env', owner,
                               {key: r2[key] for key in ('R2_ENDPOINT', 'R2_ACCESS_KEY_ID', 'R2_SECRET_ACCESS_KEY', 'R2_BUCKET')})
            generated = REPO / 'deploy/generated'
            generated.mkdir(exist_ok=True)
            for name, unit in units.items():
                (UNIT_DIR / name).write_text(unit)
                (generated / name).write_text(unit)
            with sqlite3.connect(DATA / 'platform.db') as db:
                db.execute("INSERT OR REPLACE INTO settings VALUES('global_task_slots','120')")
            (CONF / 'cloud-enabled').write_text('Local cloud-backed staging; use cloud_stage.py for coherent updates.\n')
            start_services()
            for path in displaced:
                shutil.rmtree(path)
        except BaseException:
            stop_confirmed = False
            if stopped:
                try:
                    stop_services()
                    stop_confirmed = True
                except Exception:
                    # Do not mask the original failure, and do not restart.
                    print('Stopping all staging units needs operator verification', file=sys.stderr)
                # Prevent a reboot from starting a partially upgraded release.
                try:
                    subprocess.run(['systemctl', 'disable', *SERVICES], check=False,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30)
                except Exception:
                    print('Disabling failed-upgrade boot startup needs operator verification', file=sys.stderr)
            print(json.dumps({'status': 'upgrade-failed-do-not-restart',
                              'all_staging_services_stopped': stop_confirmed,
                              'recovery_snapshot': str(backup) if backup else None,
                              'secret_values_printed': False}), file=sys.stderr)
            raise
    print(json.dumps({'status': 'cloud-staging-configured', 'project_root': str(SCIENCE),
                      'bounded_acceptance_task_slots': ACCEPTANCE_SLOTS,
                      'ports': [18890, 18889], 'recovery_snapshot': str(backup),
                      'review_provider': 'https-single-worker-120s', 'secrets_printed': False,
                      'remote_configuration_changed': False, 'legacy_demo_services_changed': False,
                      'cloud_permission_limit': 'existing R2 and Hy4 cloud key scopes unchanged'}))


if __name__ == '__main__':
    main()
