#!/usr/bin/env python3
"""Extend an existing E2B image snapshot for the paper roster; no VPS builds.

Reuses and independently inspects the 48 shared task images. Only the added
Shadow task is built. The result is a candidate snapshot, never a scored run.
"""
import argparse
import hashlib
import json
import shlex
import time
from pathlib import Path

from e2b import Sandbox
from e2b_runtime import load_key


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--template', required=True)
    parser.add_argument('--snapshot-name', required=True)
    parser.add_argument('--plan', type=Path, required=True)
    parser.add_argument('--previous-receipt', type=Path, required=True)
    parser.add_argument('--output-dir', type=Path, required=True)
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    report_path = args.output_dir / 'delta-build.json'
    if report_path.exists():
        parser.error('output already exists')
    plan = json.loads(args.plan.read_bytes())
    previous = json.loads(args.previous_receipt.read_bytes())
    added = set(plan['tasks']) - set(previous['task_receipts'])
    shared = set(plan['tasks']) & set(previous['task_receipts'])
    if added != {'time_budgeted_shadow_surrogate_60q'} or len(shared) != 48:
        parser.error('expected one added Shadow task and 48 shared tasks')
    if any(plan[k] != previous[k] for k in ('source_commit', 'source_files_sha256')):
        parser.error('source identity changed')
    report = {'status': 'running', 'scope': 'paper49-delta-image-build',
              'started_at': time.time(), 'template': args.template, 'phases': [],
              'build_plan_sha256': hashlib.sha256(args.plan.read_bytes()).hexdigest(),
              'scientific_execution_accepted': False, 'competition_release_frozen': False}
    key = load_key()
    sandbox = None

    def save():
        report_path.write_text(json.dumps(report, indent=2) + '\n')

    def run(name, command, timeout=120):
        row = {'name': name, 'started_at': time.time(), 'passed': False}
        report['phases'].append(row)
        save()
        print(json.dumps({'phase': name, 'state': 'started'}), flush=True)
        result = sandbox.commands.run(command, user='root', timeout=timeout)
        row.update(passed=result.exit_code == 0, finished_at=time.time())
        save()
        if result.exit_code:
            raise RuntimeError('build_phase_failed:' + name)
        print(json.dumps({'phase': name, 'state': 'passed'}), flush=True)

    try:
        sandbox = Sandbox.create(template=args.template, api_key=key, timeout=1800,
                                 network={'allow_public_traffic': False},
                                 metadata={'purpose': 'qiqc-paper49-shadow-build'})
        report['sandbox_id'] = sandbox.sandbox_id
        save()
        run('docker_ready', 'for i in $(seq 1 45); do docker info >/dev/null 2>&1 && exit 0; sleep 1; done; exit 1')
        run('source_clean', 'test -z "$(git -C /opt/qiqcbench status --porcelain)" && '
            'test "$(git -C /opt/qiqcbench rev-parse HEAD)" = ' + shlex.quote(plan['source_commit']) +
            ' && mkdir -p /opt/qiqc-paper-evidence /opt/qiqc-native-tools')
        sandbox.files.write('/opt/qiqc-native-build-plan.json', args.plan.read_bytes(), user='root')
        for name in ('native_build_receipt.py', 'native_contract.py'):
            sandbox.files.write('/opt/qiqc-native-tools/' + name, (Path(__file__).parent / name).read_bytes(), user='root')
        task = plan['tasks'][next(iter(added))]
        verifier = plan['verifiers'][task['verifier_image']]
        if verifier['context']['mode'] != 'original_tests_directory':
            raise RuntimeError('unsupported_delta_verifier_context')
        run('build_shadow_verifier', 'cd /opt/qiqcbench && ' + shlex.join(verifier['build_argv_template']) +
            ' >/opt/qiqc-paper-evidence/verifier-build.log 2>&1', 600)
        run('build_shadow_main', 'cd /opt/qiqcbench && ' + shlex.join(task['main_build_argv_template']) +
            ' >/opt/qiqc-paper-evidence/main-build.log 2>&1', 600)
        remote = '''import json,pathlib,subprocess
plan=json.load(open('/opt/qiqc-native-build-plan.json'))
def image(tag):return json.loads(subprocess.check_output(['docker','image','inspect',tag]))[0]['Id']
agent=image('qiqcbench-agent:latest')
main={task:image('qiqc-native-main-'+task+':candidate') for task in plan['tasks']}
root=pathlib.Path('/opt/qiqc-paper-evidence')
(root/'main_images.json').write_text(json.dumps(main))
(root/'main_build_checks.json').write_text(json.dumps({'agent_image_id_before':agent,'agent_image_id_after':image('qiqcbench-agent:latest')}))
'''
        sandbox.files.write('/opt/qiqc-native-tools/paper_main_inventory.py', remote, user='root')
        run('inspect_all49_main_images', '/opt/harbor/bin/python /opt/qiqc-native-tools/paper_main_inventory.py')
        run('original_image_recorder', 'cd /opt/qiqcbench && python3 tools/record_agent_image.py '
            '/opt/qiqc-paper-evidence --tasks ' + shlex.quote(','.join(plan['tasks'])) +
            ' >/opt/qiqc-paper-evidence/record.log 2>&1', 300)
        run('bind_all49_receipt', '/opt/harbor/bin/python /opt/qiqc-native-tools/native_build_receipt.py '
            '--plan /opt/qiqc-native-build-plan.json --evidence-dir /opt/qiqc-paper-evidence '
            '--output /opt/qiqc-paper-evidence/receipt.json >/opt/qiqc-paper-evidence/receipt.log 2>&1', 300)
        raw = sandbox.files.read('/opt/qiqc-paper-evidence/receipt.json', user='root')
        receipt = json.loads(raw)
        if set(receipt['task_receipts']) != set(plan['tasks']):
            raise RuntimeError('receipt_roster_mismatch')
        for task_id in shared:
            if receipt['task_receipts'][task_id]['image_ids'] != previous['task_receipts'][task_id]['image_ids']:
                raise RuntimeError('shared_task_image_changed')
        report['shared_task_image_ids_unchanged'] = len(shared)
        report['task_count'] = len(receipt['task_receipts'])
        (args.output_dir / 'build-receipt.json').write_text(raw)
        report['build_receipt_sha256'] = hashlib.sha256(raw.encode()).hexdigest()
        sandbox.files.write('/opt/qiqc-native-build-receipt.json', raw, user='root')
        run('source_clean_before_snapshot', 'test -z "$(git -C /opt/qiqcbench status --porcelain)" && sync')
        snapshot = sandbox.create_snapshot(name=args.snapshot_name, mode='filesystem')
        report['snapshot'] = vars(snapshot)
        report['status'] = 'passed'
    except Exception as exc:
        report.update(status='failed', error_type=type(exc).__name__,
                      error=str(exc).replace(key, '[REDACTED]')[:1000])
    finally:
        if sandbox:
            try:
                sandbox.kill()
                report['build_sandbox_destroyed'] = True
            except Exception:
                report['build_sandbox_destroyed'] = False
        report['finished_at'] = time.time()
        save()
    print(json.dumps({'status': report['status'], 'report': str(report_path)}), flush=True)
    return 0 if report['status'] == 'passed' and report.get('build_sandbox_destroyed') else 1


if __name__ == '__main__':
    raise SystemExit(main())
