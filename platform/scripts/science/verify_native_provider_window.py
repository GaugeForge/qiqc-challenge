#!/usr/bin/env python3
"""Check actual E2B timeout admission without running tasks or calling a model.

The requested lifetime covers the longest original task plus preparation,
verifier and archival budgets. The sandbox is destroyed immediately after the
provider confirms its deadline. This is not a sustained-load or duration test.
"""

import argparse
import importlib.metadata
import json
from pathlib import Path
import time

from native_contract import build_plan, execution_budget


def required_window(config, catalog):
    plan = build_plan(Path(config['source_root']), Path(config['source_audit']))
    budgets = [execution_budget(plan, task['task_id'], continuous_limit_seconds=86400,
        acceptance_scope='competition-native', agent_budget_seconds=task['limits']['agent_seconds'],
        native_limits=task['limits']) for task in catalog['tasks']]
    if len(budgets) != 49 or not all(row['account_window_supported'] for row in budgets):
        raise ValueError('native_full_window_not_supported')
    return max(row['sandbox_ttl_seconds'] for row in budgets)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--candidate', type=Path, required=True)
    parser.add_argument('--key-file', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--execute-cloud-check', action='store_true')
    args = parser.parse_args()
    if not args.execute_cloud_check or args.output.exists():
        parser.error('Explicit cloud opt-in and a new report path are required')
    from e2b import Sandbox
    from qiqc.storage import _load_env_file
    config = json.loads(args.candidate.read_text())
    catalog = json.loads(Path(config['science_task_set_path']).read_text())
    window = int(required_window(config, catalog))
    key = _load_env_file(args.key_file)['E2B_API_KEY']
    report = {'scope': 'provider-timeout-admission-only', 'passed': False,
              'sdk_version': importlib.metadata.version('e2b'),
              'required_window_seconds': window, 'science_executed': False,
              'model_called': False, 'sustained_duration_verified': False}
    sandbox = None
    started = time.monotonic()
    try:
        sandbox = Sandbox.create(template=config['template_id'], api_key=key,
            timeout=window, request_timeout=60, http_version='1.1',
            network={'allow_public_traffic': False, 'deny_out': ['0.0.0.0/0']},
            metadata={'purpose': 'qiqc-provider-window-check'})
        report['sandbox_id'] = sandbox.sandbox_id
        info = sandbox.get_info(request_timeout=20)
        granted = (info.end_at - info.started_at).total_seconds()
        report['provider_window_seconds'] = granted
        if granted < window - 5:
            raise ValueError('provider_window_shorter_than_native_budget')
        report['timeout_admitted'] = True
    except Exception as error:
        report['error_class'] = type(error).__name__
    finally:
        if sandbox is not None:
            try:
                result = sandbox.kill(request_timeout=30)
                report['sandbox_destroyed'] = type(result) is bool
            except Exception as error:
                report['cleanup_error_class'] = type(error).__name__
        report['elapsed_seconds'] = round(time.monotonic() - started, 3)
        report['passed'] = bool(report.get('timeout_admitted') and report.get('sandbox_destroyed'))
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report))
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
