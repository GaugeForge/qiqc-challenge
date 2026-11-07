#!/usr/bin/env python3
"""Build the unchanged restored benchmark inside an E2B Docker host only.

This is build evidence, never a release freeze or task acceptance result.
The private source archive is transferred in memory; no credentials enter it.
"""
import argparse
import hashlib
import io
import json
import shlex
import tarfile
import time
from pathlib import Path

from e2b import Sandbox
from e2b_runtime import load_key
from verify_restored_benchmark import verify


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--source', type=Path, required=True)
    p.add_argument('--audit', type=Path, required=True)
    p.add_argument('--template', required=True)
    p.add_argument('--report', type=Path, required=True)
    p.add_argument('--resume-report', type=Path)
    args = p.parse_args()
    if args.report.exists():
        p.error('report exists')
    key = load_key()
    audit = json.loads(args.audit.read_text())
    verify(args.source, audit)
    tasks = audit['competition_task_ids']
    report = {'scope': 'original-native-image-build', 'template': args.template,
              'source_audit_sha256': hashlib.sha256(args.audit.read_bytes()).hexdigest(),
              'started_at': time.time(), 'phases': [], 'passed': False,
              'competition_release_frozen': False, 'all49_e2e_accepted': False}
    previous = json.loads(args.resume_report.read_text()) if args.resume_report else None
    if previous and (previous.get('source_audit_sha256') != report['source_audit_sha256']
                     or previous.get('template') != args.template or previous.get('passed')):
        p.error('resume must refer to a failed build of this exact source and template')
    completed = {r['name'] for r in previous['phases'] if r['passed']} if previous else set()
    if previous:
        report['resumed_from'] = str(args.resume_report)
        report['phases'] = [r for r in previous['phases'] if r['passed']]
    args.report.parent.mkdir(parents=True, exist_ok=True)

    def save():
        temp = args.report.with_suffix('.tmp')
        temp.write_text(json.dumps(report, indent=2, default=str) + '\n')
        temp.replace(args.report)

    sandbox = None
    def run(name, command, timeout=1200):
        if name in completed:
            return ''
        row = {'name': name, 'started_at': time.time(), 'passed': False}
        report['phases'].append(row)
        save()
        print(json.dumps({'phase': name, 'state': 'started'}), flush=True)
        try:
            result = sandbox.commands.run(command, user='root', timeout=timeout)
        except Exception as exc:
            row.update(finished_at=time.time(), error_type=type(exc).__name__)
            save()
            raise
        row.update(passed=result.exit_code == 0, exit_code=result.exit_code,
                   finished_at=time.time())
        save()
        if result.exit_code:
            raise RuntimeError('build_phase_failed:' + name)
        print(json.dumps({'phase': name, 'state': 'passed'}), flush=True)
        return result.stdout

    try:
        sandbox = Sandbox.connect(previous['sandbox_id'], api_key=key) if previous else Sandbox.create(template=args.template, timeout=3600, api_key=key,
                                 network={'allow_public_traffic': False},
                                 metadata={'purpose': 'qiqc-original-image-build'})
        report['sandbox_id'] = sandbox.sandbox_id
        info = sandbox.get_info()
        report['resources'] = {'cpu_count': info.cpu_count, 'memory_mb': info.memory_mb}
        save()
        run('docker_ready', 'docker info >/dev/null && mkdir -p /opt/qiqcbench /opt/qiqc-build-evidence')
        if 'restore_source' not in completed:
            archive = io.BytesIO()
            with tarfile.open(fileobj=archive, mode='w:gz', dereference=False) as tar:
                tar.add(args.source, arcname='qiqcbench')
            payload = archive.getvalue()
            report['source_transfer_sha256'] = hashlib.sha256(payload).hexdigest()
            sandbox.files.write('/tmp/qiqc-source.tar.gz', payload, user='root')
        else:
            report['source_transfer_sha256'] = previous['source_transfer_sha256']
        run('restore_source', 'tar xzf /tmp/qiqc-source.tar.gz -C /opt && rm /tmp/qiqc-source.tar.gz && '
            'git -C /opt/qiqcbench fsck --full --no-reflogs >/opt/qiqc-build-evidence/git-fsck.log 2>&1 && '
            'test -z "$(git -C /opt/qiqcbench status --porcelain)"')
        # Trusted Harbor controller is outside all original scientific image layers.
        run('install_harbor_controller',
            'set -eu; exec >/opt/qiqc-build-evidence/harbor-install.log 2>&1; '
            'python3 -m venv /opt/uv; /opt/uv/bin/pip install uv==0.9.5; '
            '/opt/uv/bin/uv venv --clear --python 3.12 /opt/harbor; '
            '/opt/uv/bin/uv pip install --python /opt/harbor/bin/python harbor==0.23.0', 600)
        run('build_original_main', 'cd /opt/qiqcbench && docker build --progress=plain '
            '-t qiqcbench-agent:latest -f docker/agent/Dockerfile . '
            '>/opt/qiqc-build-evidence/main-build.log 2>&1', 1800)
        run('build_original_qsim_and_verifiers', 'cd /opt/qiqcbench && python3 tools/build_qsim_images.py --tasks ' +
            shlex.quote(','.join(tasks)) + ' >/opt/qiqc-build-evidence/qsim-verifier-build.log 2>&1', 1800)
        run('capture_original_image_evidence', 'cd /opt/qiqcbench && python3 tools/record_agent_image.py '
            '/opt/qiqc-build-evidence --tasks ' + shlex.quote(','.join(tasks)) +
            ' >/opt/qiqc-build-evidence/capture.log 2>&1', 300)
        run('controller_identity', '/opt/uv/bin/uv pip freeze --python /opt/harbor/bin/python >/opt/qiqc-build-evidence/harbor-packages.txt && '
            'docker version --format "{{.Server.Version}}" >/opt/qiqc-build-evidence/docker-version.txt && '
            'git -C /opt/qiqcbench rev-parse HEAD >/opt/qiqc-build-evidence/source-commit.txt')
        # No scientific rollout exists in this build sandbox; metadata only is retained locally.
        for name in ('agent_image.json', 'runtime_images.json', 'harbor-packages.txt', 'docker-version.txt', 'source-commit.txt'):
            try:
                content = sandbox.files.read('/opt/qiqc-build-evidence/' + name, user='root')
                target = args.report.parent / (args.report.stem + '-' + name)
                target.write_text(content)
                report.setdefault('build_evidence', {})[name] = str(target)
            except Exception as exc:
                report.setdefault('evidence_errors', {})[name] = type(exc).__name__
                raise
        run('pre_snapshot_source_clean', 'test -z "$(git -C /opt/qiqcbench status --porcelain)" && '
            'docker ps -q | xargs -r docker stop && sync')
        snapshot = sandbox.create_snapshot(name='qiqc-original-images-20261020', mode='filesystem')
        report['snapshot'] = vars(snapshot)
        report['passed'] = True
    except Exception as exc:
        report['error_type'] = type(exc).__name__
        report['error'] = str(exc).replace(key, '[REDACTED]')[:1500]
        # Build logs contain no contestant input or runtime credentials.
        if sandbox:
            try:
                text = sandbox.commands.run('tail -c 5000 /opt/qiqc-build-evidence/harbor-install.log '
                    '/opt/qiqc-build-evidence/main-build.log /opt/qiqc-build-evidence/qsim-verifier-build.log '
                    '/opt/qiqc-build-evidence/capture.log 2>/dev/null || true', user='root', timeout=20).stdout
                report['build_diagnostic'] = text.replace(key, '[REDACTED]')[-15000:]
            except Exception:
                report['build_diagnostic_unavailable'] = True
    finally:
        report['finished_at'] = time.time()
        # A failed build is kept briefly for repair; its bounded TTL prevents leaks.
        if sandbox and report['passed']:
            sandbox.kill()
            report['build_sandbox_destroyed'] = True
        save()
    print(json.dumps({'passed': report['passed'], 'report': str(args.report)}), flush=True)
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
