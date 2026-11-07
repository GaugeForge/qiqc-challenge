#!/usr/bin/env python3
"""Build and install isolated non-root QIQC staging units; never touches Demo units."""
import grp
import json
import os
from pathlib import Path
import pwd
import shutil
import subprocess
import sys

REPO = Path(__file__).resolve().parents[1]
WORKSPACE = REPO.parents[1]
RELEASE = Path('/opt/qiqc-staging/current')
DATA = Path('/var/lib/qiqc-staging/data')
CONF = Path('/etc/qiqc-staging')
GROUP = 'qiqc-stage-data'
USERS = ('qiqc-stage-api', 'qiqc-stage-worker', 'qiqc-stage-gateway')


def command(args, **kwargs):
    return subprocess.run(args, check=True, **kwargs)


def main():
    if Path('/etc/qiqc-staging/cloud-enabled').exists():
        raise SystemExit('Cloud staging is enabled; use cloud_stage.py to preserve the configured science services')
    if os.geteuid() != 0:
        raise SystemExit('Local staging installer requires root')
    try:
        grp.getgrnam(GROUP)
    except KeyError:
        command(['groupadd', '--system', GROUP])
    for user in USERS:
        try:
            pwd.getpwnam(user)
        except KeyError:
            command(['useradd', '--system', '--no-create-home', '--gid', GROUP, '--shell', '/usr/sbin/nologin', user])
        if GROUP not in subprocess.check_output(['id', '-nG', user], text=True).split():
            raise SystemExit('Existing staging identity has unexpected group; inspect manually')
        if 'docker' in subprocess.check_output(['id', '-nG', user], text=True).split():
            raise SystemExit('Staging identities must not have Docker group access')
    for path in [RELEASE, CONF]:
        path.mkdir(parents=True, exist_ok=True)
    CONF.chmod(0o755)
    DATA.mkdir(parents=True, exist_ok=True)
    os.chown(DATA, pwd.getpwnam(USERS[0]).pw_uid, grp.getgrnam(GROUP).gr_gid)
    DATA.chmod(0o2770)
    for name in ['uploads', 'quarantine', 'objects', 'cache', 'jobs']:
        path = DATA / name
        path.mkdir(exist_ok=True)
        os.chown(path, pwd.getpwnam(USERS[0]).pw_uid, grp.getgrnam(GROUP).gr_gid)
        path.chmod(0o2770)
    # Build to new files and atomically replace; never truncate a running binary.
    for name, package in [('api', '.'), ('worker', './cmd/qiqc-worker'), ('gateway', './cmd/qiqc-model-gateway')]:
        command(['/usr/bin/go', 'build', '-o', str(RELEASE / (name + '.next')), package], cwd=REPO)
        (RELEASE / (name + '.next')).replace(RELEASE / name)
    for source, name in [(REPO.parent / 'dist', 'site'), (REPO.parent / 'platform/web', 'web'), (REPO.parent / 'adapters', 'adapters')]:
        shutil.copytree(source, RELEASE / name, dirs_exist_ok=True)
    shutil.copyfile(REPO.parent / 'platform/qiqc/archive.py', RELEASE / 'archive.py')
    shutil.copyfile(Path(__file__).with_name('scanner_helper.py'), RELEASE / 'scanner_helper.py')
    shutil.copyfile(WORKSPACE / 'benchmark-assets/baseline-mock.json', RELEASE / 'baseline-mock.json')
    for path in RELEASE.rglob('*'):
        if path.is_dir():
            path.chmod(0o755)
        else:
            path.chmod(0o755 if path.name in {'api', 'worker', 'gateway'} else 0o644)
    # Only fake provider credentials exist in staging. No real secret is copied.
    provider = CONF / 'provider.env'
    if not provider.exists():
        provider.write_text('HY_URL=https://invalid.example/v1\nHY_API=staging-placeholder-not-a-real-key\n')
    os.chown(provider, pwd.getpwnam(USERS[2]).pw_uid, grp.getgrnam(GROUP).gr_gid)
    provider.chmod(0o600)
    image = subprocess.check_output(['/usr/bin/docker', 'image', 'inspect', 'python:3.13-slim', '--format', '{{.Id}}'], text=True).strip()
    parser = str(RELEASE / 'archive.py')
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
    units = {}
    common = f'''Environment=QIQC_SHARED_DATA=1
Environment=QIQC_R2_ENABLED=0
Environment=QIQC_MODEL=tencent/hy4-preview
Environment=QIQC_BASELINE={RELEASE}/baseline-mock.json
WorkingDirectory={RELEASE}
ReadWritePaths={DATA}
InaccessiblePaths=-/run/docker.sock -/var/run/docker.sock
'''
    commands = {
        'api': f'{RELEASE}/api -standalone -listen 127.0.0.1:18890 -data {DATA} -site {RELEASE}/site -web {RELEASE}/web -adapters {RELEASE}/adapters -reference {RELEASE}/adapters/reference.zip',
        'worker': f'{RELEASE}/worker -data {DATA} -slots 2',
        'gateway': f'{RELEASE}/gateway -data {DATA} -provider-env {provider} -listen 127.0.0.1:18889',
    }
    for kind, launch in commands.items():
        unit = f'''[Unit]
Description=QIQC isolated staging {kind}
After=network.target qiqc-stage-scanner.service

[Service]
Type=simple
User=qiqc-stage-{kind}
Group={GROUP}
{common}Environment=QIQC_SCANNER_SOCKET=/run/qiqc-stage-scanner/scanner.sock
ExecStart={launch}
{hardening}
[Install]
WantedBy=multi-user.target
'''
        units[f'qiqc-stage-{kind}.service'] = unit
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
ExecStart=/usr/bin/python3 {RELEASE}/scanner_helper.py --socket /run/qiqc-stage-scanner/scanner.sock --quarantine {DATA}/quarantine --spool /var/lib/qiqc-stage-scanner --parser {parser} --image {image} --api-user qiqc-stage-api
ReadWritePaths=/run/qiqc-stage-scanner /var/lib/qiqc-stage-scanner
{hardening}
[Install]
WantedBy=multi-user.target
'''
    generated = REPO / 'deploy/generated'
    generated.mkdir(exist_ok=True)
    for name, unit in units.items():
        (generated / name).write_text(unit)
        Path('/etc/systemd/system', name).write_text(unit)
    command(['systemctl', 'daemon-reload'])
    import sqlite3
    with sqlite3.connect(DATA / 'platform.db') as db:
        db.execute("CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY,value TEXT)")
        db.execute("INSERT OR REPLACE INTO settings VALUES('global_task_slots','2')")
    os.chown(DATA / 'platform.db',pwd.getpwnam(USERS[0]).pw_uid,grp.getgrnam(GROUP).gr_gid)
    (DATA / 'platform.db').chmod(0o660)
    # Distinct names only; do not restart qiqc-demo-* or qiqc-go-edge.
    command(['systemctl', 'restart', 'qiqc-stage-scanner.service'])
    command(['systemctl', 'restart', 'qiqc-stage-api.service'])
    command(['systemctl', 'restart', 'qiqc-stage-worker.service', 'qiqc-stage-gateway.service'])
    print(json.dumps({'status':'staging-installed', 'ports':[18890,18889], 'live_demo_changed':False,
                      'users':list(USERS),'units':list(units),'real_credentials_copied':False}))


if __name__ == '__main__':
    main()
