#!/usr/bin/env python3
"""Run bounded real public acceptance in an independent loopback database.

Reuses approved provider/E2B/R2 configuration without copying or displaying keys.
Never modifies active services, their database, the task source or production time.
Requires prebuilt candidate binaries and an explicit real-science option.
"""

import argparse
import datetime as dt
import json
import os
from pathlib import Path
import secrets
import signal
import subprocess
import sys
import time
import urllib.request

ROOT = Path(__file__).resolve().parents[3]
SCIENCE = Path(__file__).parent / 'science'
sys.path.insert(0, str(SCIENCE))
from native_candidate import commands, load_candidate, model_id
from native_controller import inventory, digest
from task_catalog import upgrade_catalog


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--source-candidate', type=Path, required=True)
    p.add_argument('--root', type=Path, required=True)
    p.add_argument('--template', required=True)
    p.add_argument('--adapters', nargs='+', choices=('codex', 'deepseek', 'pi'), default=['codex'])
    p.add_argument('--kind', choices=('infra', 'public'), default='public')
    p.add_argument('--execution-timeout', type=int, default=15000)
    p.add_argument('--execute-real-science', action='store_true')
    args = p.parse_args()
    if not args.execute_real_science:
        p.error('Explicit real-science opt-in required: five public tasks per adapter')
    # Read the prior deployment's configuration/source identity. Its controller
    # belongs to the old release; the new controller is fully rebound below.
    base, audit = load_candidate(args.source_candidate, verify_controller=False)
    e2b_key_file = Path(base['candidate_root']) / 'config/worker-e2b.env'
    if e2b_key_file.is_symlink() or not e2b_key_file.is_file():
        p.error('Existing private worker E2B configuration is required')
    root = args.root.resolve()
    root.mkdir(parents=True, exist_ok=True)
    root.chmod(0o700)
    if (root / 'data/platform.db').exists():
        p.error('Fresh independent acceptance database required; do not resubmit old batches')
    bins = root / 'bin'
    for name in ('api','worker','gateway','review','admin'):
        if not (bins / name).is_file():
            p.error('Build candidate binaries first')
    python = str(ROOT / 'competition-platform/platform/.venv/bin/python')
    config = {**base, 'candidate_root': str(root), 'data': str(root / 'data'),
              'binaries': str(bins), 'credentials': str(root / 'credentials.json'),
              'url': 'http://127.0.0.1:29890', 'gateway_url': 'http://127.0.0.1:29889',
              'project_root': str(ROOT), 'python': python, 'template_id': args.template,
              'functional_slots': 5, 'adapters': str(ROOT / 'competition-platform/adapters'),
              'controller_manifest': str(root / 'controller.json'),
              'science_task_set_path': str(root / 'task-set.json'),
              'event_calendar_path': str(root / 'calendar.json'),
              'adapter_manifest': str(root / 'science-submissions.json')}
    config.pop('formal_execution_path', None)
    starters = json.loads((ROOT / 'competition-platform/adapters/manifest.json').read_text())
    (root / 'science-submissions.json').write_text(json.dumps({
        (row.get('manifest', {}).get('adapter') or row.get('derived', {}).get('adapter')): {'sha256': row['sha256']}
        for row in starters['packages'].values()}))
    catalog = upgrade_catalog(json.loads(Path(base['science_task_set_path']).read_text()), base['source_root'])
    (root / 'task-set.json').write_text(json.dumps(catalog))
    now = dt.datetime.now(dt.timezone.utc)
    # Only the disposable acceptance database gets this practice window.
    calendar = {'mode': 'scheduled', 'version': 'isolated-real-acceptance-' + secrets.token_hex(6),
                'infra_starts_at': (now - dt.timedelta(hours=1)).isoformat(),
                'infra_ends_at': (now + dt.timedelta(hours=8)).isoformat(),
                'starts_at': '2026-11-08T08:00:00+08:00', 'ends_at': '2026-11-10T21:00:00+08:00',
                'appeal_closes_at': '2026-11-11T21:00:00+08:00'}
    (root / 'calendar.json').write_text(json.dumps(calendar))
    controller = inventory(ROOT, bins, upstream_ip_family=config.get('upstream_ip_family','auto'),
                           agent_budget_seconds=config.get('agent_budget_seconds',300),
                           model_max_concurrent=config.get('model_max_concurrent',49))
    Path(config['controller_manifest']).write_text(json.dumps(controller, indent=2))
    config['controller_sha256'] = digest(controller)
    candidate = root / 'candidate.json'
    candidate.write_text(json.dumps(config, indent=2)); candidate.chmod(0o600)
    load_candidate(candidate)
    env = dict(os.environ, QIQC_NATIVE_CANDIDATE=str(candidate), QIQC_PROJECT_ROOT=str(ROOT),
               QIQC_R2_ENABLED='1', QIQC_R2_ENV=config['r2_env'], QIQC_MODEL=model_id(config),
               QIQC_E2B_KEY_FILE=str(e2b_key_file), QIQC_COMPETITION_PORTAL='1')
    env.pop('QIQC_SCANNER_SOCKET', None)
    admin = {'email': 'acceptance-admin@qiqc.invalid', 'password': secrets.token_urlsafe(32), 'name': 'Isolated acceptance operator'}
    subprocess.run(commands(config)['bootstrap'], input=json.dumps(admin).encode(), env=env,
                   check=True, stdout=subprocess.DEVNULL)
    Path(config['credentials']).write_text(json.dumps({'admin': admin, 'teams': []})); Path(config['credentials']).chmod(0o600)
    children, logs = [], []
    report = {'scope': 'new-release-real-public-automatic-browser', 'passed': False,
              'competition_portal': env['QIQC_COMPETITION_PORTAL'] == '1',
              'candidate': str(candidate), 'production_changed': False,
              'started_at': time.time(), 'adapters': [], 'controller_sha256': config['controller_sha256']}
    def stop(*_):
        raise KeyboardInterrupt()
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        role_commands = commands(config)
        role_commands['api'] += ['-clock-health', '', '-scanner', str(ROOT / 'competition-platform/platform/qiqc/archive.py')]
        role_commands['review'] += ['-timeout', '120s']
        for role in ('api','gateway','review','worker'):
            log = (root / (role + '.log')).open('w'); logs.append(log)
            child = subprocess.Popen(role_commands[role], env=env, cwd=ROOT, stdout=log, stderr=log)
            children.append((role,child))
        for _ in range(60):
            if any(child.poll() is not None for _, child in children):
                raise RuntimeError('candidate_service_start_failed')
            try:
                with urllib.request.urlopen(config['url'] + '/healthz', timeout=2) as response:
                    if response.status == 200:
                        break
            except OSError:
                time.sleep(1)
        else:
            raise RuntimeError('candidate_health_timeout')
        for adapter in args.adapters:
            output = root / ('browser-' + adapter + '.json')
            result = subprocess.run([python, str(Path(__file__).with_name('ordinary_account_browser_check.py')),
                '--candidate', str(candidate), '--output', str(output), '--adapter', adapter,
                '--kind', args.kind, '--review-timeout', '600', '--execution-timeout', str(args.execution_timeout),
                '--execute-real-science'], cwd=ROOT, env=env, timeout=args.execution_timeout + 900,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            report['adapters'].append({'adapter': adapter, 'exit_code': result.returncode, 'report': str(output)})
            if result.returncode:
                raise RuntimeError('browser_acceptance_failed')
            readback = root / ('ordinary-r2-' + adapter + '.json')
            readback_result = subprocess.run([
                python, str(Path(__file__).with_name('ordinary_scientific_readback.py')),
                '--candidate', str(candidate), '--browser-report', str(output),
                '--output', str(readback), '--workspace-candidate',
            ], cwd=ROOT, env=env, timeout=args.execution_timeout + 900,
               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            report['adapters'][-1]['r2_readback_exit_code'] = readback_result.returncode
            report['adapters'][-1]['r2_readback'] = str(readback)
            if readback_result.returncode:
                raise RuntimeError('r2_rollout_readback_failed')
        report['passed'] = True
    except (Exception, KeyboardInterrupt) as error:
        report['error_class'] = type(error).__name__
    finally:
        # Worker receives SIGTERM first, allowing lease-aware remote cleanup.
        for role, child in reversed(children):
            if child.poll() is None:
                child.terminate()
                try:
                    child.wait(timeout=510 if role == 'worker' else 20)
                except subprocess.TimeoutExpired:
                    child.kill(); child.wait()
                    report.setdefault('forced_service_stops', []).append(role)
        for log in logs:
            log.close()
        report['finished_at'] = time.time()
        (root / 'acceptance.json').write_text(json.dumps(report, indent=2))
    print(json.dumps({'passed': report['passed'], 'report': str(root / 'acceptance.json')}))
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
