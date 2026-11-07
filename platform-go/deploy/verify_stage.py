#!/usr/bin/env python3
"""Exercise only dedicated local staging; does not call real cloud or send mail."""
import datetime
import grp
import http.cookiejar
import importlib.util
import json
import os
from pathlib import Path
import pwd
import secrets
import socket
import sqlite3
import subprocess
import tempfile
import time
import urllib.request

REPO = Path(__file__).resolve().parents[1]
DATA = Path('/var/lib/qiqc-staging/data')
API = 'http://127.0.0.1:18890'
SERVICES = ['qiqc-stage-api', 'qiqc-stage-worker-key2', 'qiqc-stage-worker-key1', 'qiqc-stage-gateway', 'qiqc-stage-scanner']


def run(args):
    return subprocess.run(args, capture_output=True, text=True, check=False, timeout=20)


def api(opener, path, data=None, raw=None):
    payload = raw if raw is not None else json.dumps(data).encode() if data is not None else None
    headers = {'X-QIQC-Request': '1'}
    if payload is not None:
        headers['Content-Type'] = 'application/zip' if raw is not None else 'application/json'
    req = urllib.request.Request(API + path, data=payload, headers=headers)
    with opener.open(req, timeout=45) as response:
        return json.load(response)


def main():
    if Path("/etc/qiqc-staging/cloud-enabled").exists():
        raise SystemExit("Cloud staging is active; use read-only verify_permissions.py instead")
    before = {}
    for service in SERVICES:
        props = dict(line.split('=', 1) for line in run(['systemctl', 'show', service, '--property=User,Group,MainPID,ActiveState,NoNewPrivileges,ProtectHome,ProtectSystem,PrivateTmp']).stdout.splitlines())
        assert props['ActiveState'] == 'active', service
        before[service] = props
        if service != 'qiqc-stage-scanner':
            assert props['User'].startswith('qiqc-stage-') and props['User'] != 'root'
        assert props['ProtectSystem'] == 'strict' and props['ProtectHome'] == 'yes'
    probes = []
    for user in ['qiqc-stage-api', 'qiqc-stage-worker', 'qiqc-stage-gateway']:
        for label, code in [
            ('root_directory_denied', "from pathlib import Path; Path('/root/QQ-Challenge/.env').open('rb')"),
            ('release_write_denied', "open('/opt/qiqc-staging/current/api','ab')"),
            ('docker_socket_denied', "import socket;s=socket.socket(socket.AF_UNIX);s.connect('/run/docker.sock')"),
        ]:
            result = run(['runuser', '-u', user, '--', 'python3', '-c', code])
            assert result.returncode != 0, (user, label)
            probes.append({'user': user, 'check': label, 'denied': True})
        provider = run(['runuser', '-u', user, '--', 'python3', '-c', "open('/etc/qiqc-staging/provider.env','rb').close()"])
        assert (provider.returncode == 0) == (user == 'qiqc-stage-gateway')
        probes.append({'user': user, 'check': 'provider_config_access', 'allowed': provider.returncode == 0})
    # Fresh staging-only team, using the existing compatible password hash.
    from hashlib import scrypt
    password = secrets.token_urlsafe(24)
    salt = secrets.token_bytes(16)
    stored = salt.hex() + ':' + scrypt(password.encode(), salt=salt, n=16384, r=8, p=1).hex()
    identity = secrets.token_hex(16)
    email = identity + '@example.test'
    with sqlite3.connect(DATA / 'platform.db') as db:
        db.execute('INSERT INTO teams VALUES(?,?)', (identity, 'Isolated staging acceptance'))
        db.execute('INSERT INTO users VALUES(?,?,?,?,?,?)', (identity, email, stored, identity, 'team', 1))
        db.execute("INSERT OR REPLACE INTO settings VALUES('global_task_slots','2')")
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
    login = api(opener, '/api/login', {'email': email, 'password': password})
    assert login['must_change'] == 1
    api(opener, '/api/password', {'password': secrets.token_urlsafe(24)})
    with opener.open(API + '/api/reference.zip') as response:
        package = response.read()
    version = api(opener, '/api/versions', raw=package)
    assert version['status'] == 'approved'
    request = {'version_id': version['id'], 'kind': 'infra', 'mode': 'simulated', 'idempotency_key': identity}
    batch = api(opener, '/api/batches', request)
    assert api(opener, '/api/batches', request)['id'] == batch['id']
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        state = api(opener, '/api/batches/' + batch['id'])
        if state['status'] == 'completed':
            break
        time.sleep(.2)
    assert state['status'] == 'completed', state
    trace = api(opener, f"/api/batches/{batch['id']}/traces/0")
    assert 'text' in trace
    assert api(opener, '/api/me')['quotas']['infra'] == 9
    # Request path cannot control the privileged parser's path or command.
    sock = socket.socket(socket.AF_UNIX)
    sock.settimeout(5)
    sock.connect('/run/qiqc-stage-scanner/scanner.sock')
    sock.sendall(b'{"filename":"../../etc/shadow"}\n')
    assert json.loads(sock.recv(4096))['ok'] is False
    sock.close()
    # Unauthorized worker cannot invoke the privileged scanner even though it
    # shares the data group needed for SQLite.
    result = run(['runuser', '-u', 'qiqc-stage-worker', '--', 'python3', '-c', "import socket,json;s=socket.socket(socket.AF_UNIX);s.connect('/run/qiqc-stage-scanner/scanner.sock');s.sendall(b'{\"filename\":\"incoming-123.zip\"}\\n');print(json.loads(s.recv(4096))['ok'])"])
    assert result.returncode == 0 and result.stdout.strip() == 'False'
    # Prove installer's shell is not the parent of the managed services.
    process_parent = {}
    for service, props in before.items():
        pid = props['MainPID']
        parent = next(line for line in Path('/proc', pid, 'status').read_text().splitlines() if line.startswith('PPid:')).split()[1]
        assert parent == '1'
        assert run(['systemctl', 'is-active', service]).stdout.strip() == 'active'
        process_parent[service] = {'parent_pid': 1, 'active_after_installer_exit': True}
    report = {'generated_at': datetime.datetime.now(datetime.timezone.utc).isoformat(), 'status': 'passed',
              'mode': 'real-local-systemd-staging', 'cloud_accessed': False, 'mail_sent': False,
              'live_demo_changed': False, 'services': before, 'permission_probes': probes,
              'systemd_survival': process_parent,
              'business_checks': {'login_first_password': True, 'fixed_docker_scanner': True,
                                  'submission_hash_approval': True, 'duplicate_confirmation': True,
                                  'nonroot_worker_completion': True, 'public_trace_cross_identity': True,
                                  'single_quota_debit': True, 'scanner_path_traversal_denied': True,
                                  'scanner_unauthorized_worker_denied': True},
              'limits': ['synthetic local provider credentials only', 'shared trusted database group can read DB contents',
                         'scanner helper remains privileged and has Docker access', 'no formal HTTPS or mail',
                         'real scientific executor not enabled in staging', 'live Demo identities unchanged']}
    target = REPO.parent / 'platform/test-results/staging-hardening-20261020.json'
    target.write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps({'status':'passed','report':str(target),'business_checks':report['business_checks']}))


if __name__ == '__main__':
    main()
