#!/usr/bin/env python3
"""Bounded E2B Docker/Compose capability check, with synthetic data only.

No task, contestant code, model credential or benchmark private input is uploaded.
All installation and container execution happen in one disposable E2B sandbox.
"""
from __future__ import annotations

import argparse
import json
import shlex
import time
from pathlib import Path

from e2b_runtime import create

COMPOSE = '''version: '2.4'
services:
  qsim:
    image: busybox:1.37.0
    command: sh -c 'mkdir -p /private /www; echo qsim-only > /private/truth; echo public-ok > /www/index.html; echo shared-ok > /public/result; httpd -f -p 8123 -h /www'
    volumes:
      - public:/public
    networks: [task]
  main:
    image: busybox:1.37.0
    command: sleep 600
    volumes:
      - public:/public:ro
    networks: [task]
  verifier:
    image: busybox:1.37.0
    command: sleep 600
    network_mode: none
volumes:
  public: {}
networks:
  task:
    internal: true
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live', action='store_true', required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error('preserve existing evidence; use a new report path')
    report = {'scope': 'synthetic-e2b-compose-capability-only',
              'started_at': time.time(), 'scientific_task_tested': False,
              'production_security_accepted': False, 'steps': [], 'destroyed': False}
    sandbox = None
    phase = 'create'

    def run(name, command, timeout=60):
        nonlocal phase
        phase = name
        started = time.monotonic()
        print(json.dumps({'phase': name, 'state': 'started'}), flush=True)
        result = sandbox.commands.run(command, user='root', timeout=timeout)
        row = {'name': name, 'exit_code': result.exit_code,
               'seconds': round(time.monotonic()-started, 3)}
        report['steps'].append(row)
        if result.exit_code:
            raise RuntimeError('probe_command_failed')
        print(json.dumps({'phase': name, 'state': 'passed'}), flush=True)
        return result.stdout.strip()

    try:
        sandbox = create(timeout=900)
        info = sandbox.get_info()
        report['resources'] = {'cpu_count': info.cpu_count, 'memory_mb': info.memory_mb}
        report['sandbox_id'] = sandbox.sandbox_id
        report['kernel'] = run('inspect_kernel', 'uname -r')
        run('install_docker_in_e2b',
            'command -v docker >/dev/null && (command -v docker-compose >/dev/null || docker compose version >/dev/null 2>&1) || '
            '(export DEBIAN_FRONTEND=noninteractive; apt-get update -qq >/tmp/probe-install.log 2>&1 && '
            'apt-get install -y -qq docker.io docker-compose >>/tmp/probe-install.log 2>&1)', 300)
        run('start_docker',
            'if ! docker info >/dev/null 2>&1; then nohup dockerd --host=unix:///var/run/docker.sock '
            '>/tmp/probe-dockerd.log 2>&1 </dev/null & fi; '
            'for n in $(seq 1 45); do docker info >/dev/null 2>&1 && exit 0; sleep 1; done; exit 1', 60)
        report['docker_version'] = run('docker_version', "docker version --format '{{.Server.Version}}'")
        compose = run('compose_version',
                      'if docker compose version >/dev/null 2>&1; then echo "docker compose"; else docker-compose version >/dev/null && echo docker-compose; fi')
        if compose not in ('docker compose', 'docker-compose'):
            raise RuntimeError('unexpected_compose_command')
        run('pull_fixture_image', 'docker pull busybox:1.37.0 >/tmp/probe-image.log 2>&1', 120)
        report['image_digest'] = run('image_identity', "docker image inspect busybox:1.37.0 --format '{{index .RepoDigests 0}}'")
        sandbox.files.write('/tmp/qiqc-compose-probe.yaml', COMPOSE, user='root')
        prefix = compose + ' -p qiqcprobe -f /tmp/qiqc-compose-probe.yaml '
        run('compose_three_roles', prefix + 'up -d', 60)
        checks = {
            'service_dns_and_http': "wget -qO- http://qsim:8123 | grep -qx public-ok",
            'shared_result_read': "grep -qx shared-ok /public/result",
            'readonly_result_write_denied': "! (echo changed >/public/result) && grep -qx shared-ok /public/result",
            'private_role_file_absent': "test ! -e /private/truth",
            'no_docker_socket_in_main': "test ! -S /var/run/docker.sock",
        }
        for name, command in checks.items():
            run(name, prefix + 'exec -T main sh -c ' + shlex.quote(command))
        run('verifier_separate_filesystem', prefix + 'exec -T verifier sh -c ' + shlex.quote('test ! -e /private/truth && test ! -e /public/result'))
        run('compose_cleanup', prefix + 'down -v', 30)
        report['capability_passed'] = True
    except Exception as exc:
        report.update(capability_passed=False, failed_phase=phase, error_type=type(exc).__name__)
        # This fresh sandbox contains synthetic data only; keep diagnostics bounded.
        if sandbox is not None:
            try:
                result = sandbox.commands.run('tail -c 6000 /tmp/probe-dockerd.log /tmp/probe-install.log /tmp/probe-image.log 2>/dev/null || true', user='root', timeout=10)
                report['synthetic_setup_diagnostic'] = result.stdout[-8000:]
            except Exception:
                report['setup_diagnostic_unavailable'] = True
    finally:
        if sandbox is not None:
            try:
                sandbox.kill()
                report['destroyed'] = True
            except Exception as exc:
                report['cleanup_error_type'] = type(exc).__name__
        report['finished_at'] = time.time()
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open('x') as stream:
            json.dump(report, stream, indent=2)
            stream.write('\n')
    print(json.dumps({key: report.get(key) for key in ('capability_passed', 'failed_phase', 'destroyed', 'resources')}), flush=True)
    return 0 if report.get('capability_passed') and report['destroyed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
