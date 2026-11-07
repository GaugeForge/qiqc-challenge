#!/usr/bin/env python3
"""Read-only build/deployment inventory. No credentials or provider calls."""
from pathlib import Path
import argparse
import hashlib
import json
import shutil
import subprocess
import sys
from datetime import datetime, timezone

PLATFORM = Path(__file__).resolve().parents[1]
SITE = PLATFORM.parent
ROOT = SITE.parent
COMMANDS = {'go': ['go', 'version'], 'node': ['node', '--version'],
            'docker': ['docker', '--version'], 'pandoc': ['pandoc', '--version'],
            'xelatex': ['xelatex', '--version'], 'pdfinfo': ['pdfinfo', '-v']}


def source_identity(site=SITE):
    """Hash only maintained source/lockfiles; never hash private runtime files."""
    files = set()
    for relative, extensions in [('platform-go/internal', {'.go', '.sql'}),
                                 ('platform-go/cmd', {'.go'}),
                                 ('platform-go/deploy', {'.py', '.lock'}),
                                 ('platform/qiqc', {'.py'}), ('platform/web', {'.js', '.css', '.html'}),
                                 ('platform/scripts', {'.py'})]:
        for p in (site / relative).rglob('*'):
            if p.is_file() and not p.is_symlink() and p.suffix in extensions:
                files.add(p)
    for name in ['platform-go/go.mod', 'platform-go/go.sum', 'platform/requirements.lock']:
        if (site/name).is_file(): files.add(site/name)
    files.update(p for p in (site/'platform-go').glob('*.go') if not p.is_symlink())
    digest = hashlib.sha256()
    for p in sorted(files):
        digest.update(str(p.relative_to(site)).encode()+b'\0'+hashlib.sha256(p.read_bytes()).digest())
    return {'algorithm': 'sha256-path-and-file-digests/v1', 'sha256': digest.hexdigest(), 'files': len(files)}


def probe(command):
    try:
        p = subprocess.run(command, capture_output=True, timeout=10, text=True)
        return p.returncode == 0, (p.stdout or p.stderr).splitlines()[0][:180] if (p.stdout or p.stderr) else ''
    except (OSError, subprocess.TimeoutExpired):
        return False, 'unavailable'


def check():
    versions = {name: probe(cmd) if shutil.which(cmd[0]) else (False, 'not installed') for name, cmd in COMMANDS.items()}
    private = {}
    for label, path in [('project_env', ROOT/'.env'), ('r2_env', ROOT/'runtime/legacy-platform-data/r2.env'),
                        ('preview_credentials', Path('/etc/qiqc-staging/preview-credentials.json'))]:
        exists = path.is_file() and not path.is_symlink()
        private[label] = {'exists': exists, 'private_file_mode': bool(exists and path.stat().st_mode & 0o077 == 0)}
    services = {}
    for name in ('qiqc-stage-api', 'qiqc-stage-worker', 'qiqc-stage-gateway', 'qiqc-stage-scanner', 'qiqc-stage-review'):
        services[name] = probe(['systemctl', 'is-active', name])[0] if shutil.which('systemctl') else False
    return {'schema': 'qiqc-acceptance/v1', 'module': 'M00', 'checked_at': datetime.now(timezone.utc).isoformat(),
            'tools': {k: v[0] for k, v in versions.items()}, 'tool_versions': {k: v[1] for k, v in versions.items()},
            'python': sys.version.split()[0], 'source': source_identity(), 'private_paths': private,
            'services': services, 'dependency_ready': all(v[0] for v in versions.values()),
            'production_ready': False, 'secret_values_read': False, 'cloud_called': False,
            'source_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            'notes': ['Tool availability is separate from private deployment readiness.',
                      'No configuration values or hidden tasks read. Service checks refer to native Go staging.',
                      'Production sign-off and real provider acceptance require separate reports.']}


if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('--output', type=Path, default=PLATFORM/'test-results/preflight.json')
    args = ap.parse_args()
    result = check()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2)+'\n')
    print(json.dumps(result))
    sys.exit(0 if result['dependency_ready'] else 1)
