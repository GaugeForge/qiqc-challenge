#!/usr/bin/env python3
"""Read-only local service boundary checks; never evaluates tasks or reads secrets.

Run after cloud_stage.py succeeds. Missing review identity/key is a failure,
not an optional skip. This verifier does not create users or start services.
"""
import argparse
import datetime
import json
from pathlib import Path
import pwd
import stat
import subprocess

REPO = Path(__file__).resolve().parents[1]
USERS = ['qiqc-stage-api', 'qiqc-stage-worker', 'qiqc-stage-gateway', 'qiqc-stage-review']
FILES = {'provider.env': 'qiqc-stage-gateway', 'worker-e2b.env': 'qiqc-stage-worker',
         'api-r2.env': 'qiqc-stage-api', 'worker-key2-r2.env': 'qiqc-stage-worker',
         'worker-key1-r2.env': 'qiqc-stage-worker',
         'review.key': 'qiqc-stage-review'}


def run(args):
    return subprocess.run(args, capture_output=True, text=True, timeout=20, check=False)


def verify():
    checks = []
    for user in USERS:
        identity = pwd.getpwnam(user)
        assert identity.pw_uid != 0, user
        groups_result = run(['id', '-nG', user])
        assert groups_result.returncode == 0, user
        groups = set(groups_result.stdout.split())
        assert 'qiqc-stage-data' in groups and 'docker' not in groups, user
        assert ('qiqc-stage-science' in groups) == (user == 'qiqc-stage-worker'), user
        if user == 'qiqc-stage-review':
            assert groups == {'qiqc-stage-data'}, user
        checks.append({'user': user, 'check': 'nonroot_groups', 'passed': True})
        for filename, owner in FILES.items():
            path = Path('/etc/qiqc-staging', filename)
            info = path.lstat()
            assert stat.S_ISREG(info.st_mode) and not path.is_symlink(), filename
            assert stat.S_IMODE(info.st_mode) == 0o600, filename
            assert info.st_uid == pwd.getpwnam(owner).pw_uid and info.st_nlink == 1, filename
            if filename == 'review.key':
                assert 0 < info.st_size <= 1024, filename
            # Opening/closing tests permissions without reading secret bytes.
            probe = run(['runuser', '-u', user, '--', 'python3', '-c',
                         'import sys;open(sys.argv[1],"rb").close()', str(path)])
            allowed = probe.returncode == 0
            assert allowed == (owner == user), (user, filename)
            checks.append({'user': user, 'file': filename, 'read_allowed': allowed})
        science = run(['runuser', '-u', user, '--', 'python3', '-c',
            "open('/opt/qiqc-staging/science/qq/adapters/science-probe/executor.py','rb').close()"])
        assert (science.returncode == 0) == (user == 'qiqc-stage-worker'), user
        checks.append({'user': user, 'check': 'private_science_release', 'read_allowed': science.returncode == 0})
        for name, code in [
            ('root_secret_denied', "open('/root/QQ-Challenge/.env','rb').close()"),
            ('release_write_denied', "open('/opt/qiqc-staging/current/api','ab').close()"),
            ('docker_socket_denied', "import socket;s=socket.socket(socket.AF_UNIX);s.connect('/run/docker.sock')"),
            ('upgrade_backup_denied', "import os;os.listdir('/var/backups/qiqc-staging')"),
        ]:
            assert run(['runuser', '-u', user, '--', 'python3', '-c', code]).returncode != 0
            checks.append({'user': user, 'check': name, 'denied': True})
    services = {}
    for service in ['qiqc-stage-api', 'qiqc-stage-worker-key2', 'qiqc-stage-worker-key1',
                    'qiqc-stage-gateway', 'qiqc-stage-review', 'qiqc-stage-scanner']:
        result = run(['systemctl', 'show', service,
            '--property=User,Group,MainPID,ActiveState,NoNewPrivileges,ProtectHome,ProtectSystem,PrivateTmp,TimeoutStopUSec,Restart,CapabilityBoundingSet,SupplementaryGroups,ExecStart,InaccessiblePaths'])
        assert result.returncode == 0, service
        props = dict(line.split('=', 1) for line in result.stdout.splitlines())
        assert props['ActiveState'] == 'active', service
        assert props['NoNewPrivileges'] == 'yes' and props['ProtectSystem'] == 'strict', service
        assert props['ProtectHome'] == 'yes' and props['PrivateTmp'] == 'yes', service
        assert props['Restart'] == 'on-failure' and props['CapabilityBoundingSet'] == '', service
        if service in USERS:
            assert props['User'] == service and props['Group'] == 'qiqc-stage-data', service
        elif service.startswith('qiqc-stage-worker-key'):
            assert props['User'] == 'qiqc-stage-worker' and props['Group'] == 'qiqc-stage-data', service
        if service == 'qiqc-stage-review':
            assert 'qiqc-stage-science' not in props['SupplementaryGroups'], service
            assert '--provider https' in props['ExecStart'] and '--timeout 120s' in props['ExecStart'], service
            assert '--key-file /etc/qiqc-staging/review.key' in props['ExecStart'], service
            assert '/opt/qiqc-staging/science' in props['InaccessiblePaths'], service
            assert '/run/docker.sock' in props['InaccessiblePaths'], service
            assert '/var/lib/qiqc-staging/data/uploads' in props['InaccessiblePaths'], service
        parent = next(line for line in Path('/proc', props['MainPID'], 'status').read_text().splitlines() if line.startswith('PPid:')).split()[1]
        assert parent == '1', service
        props['survives_installer_shell'] = True
        props['boot_enabled'] = run(['systemctl', 'is-enabled', service]).stdout.strip() == 'enabled'
        assert props['boot_enabled'], service
        services[service] = props
    return {'generated_at': datetime.datetime.now(datetime.timezone.utc).isoformat(),
            'status': 'passed', 'type': 'read-only-local-cloud-staging-permissions',
            'cloud_called': False, 'service_restarted': False, 'secret_values_read': False,
            'permission_checks': checks, 'services': services,
            'limitations': ['shared trusted DB group is not database row isolation',
                'review key file is separate; existing Hy4 provider key scope is unchanged',
                'R2 file identities split; existing cloud key privileges unchanged',
                'scanner helper is privileged and requires its own review',
                'systemd parent check proves logout survival, not host reboot recovery',
                'local upgrade snapshot is not an off-site disaster-recovery backup',
                'no production HTTPS, email, alerts or off-site backup acceptance']}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path,
                        default=REPO.parent / 'platform/test-results/cloud-stage-permissions-20261020.json')
    args = parser.parse_args()
    report = verify()
    args.output.write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps({'status': 'passed', 'report': str(args.output), 'checks': len(report['permission_checks'])}))


if __name__ == '__main__':
    main()
