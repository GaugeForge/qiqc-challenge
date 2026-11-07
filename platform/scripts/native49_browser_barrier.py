#!/usr/bin/env python3
"""Explicit 49-task browser queue barrier. Never fabricates accepted submissions."""
import argparse
import concurrent.futures
import hashlib
import json
import sqlite3
import subprocess
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
from browser_candidate_runtime import bind_candidate_imports, snapshot_driver, candidate_environment


def identity(*args, **kwargs):
    from native_candidate import identity as implementation
    return implementation(*args, **kwargs)


def agent_budget_seconds(config):
    budget = config.get('agent_budget_seconds', 300)
    if type(budget) is not int or budget not in (180, 300):
        raise ValueError('invalid_candidate_agent_budget')
    return budget


def load_candidate(path):
    bind_candidate_imports(path)
    from native_candidate import load_candidate as implementation
    return implementation(path)


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def selected_prepared(directory, index):
    paths = sorted(directory.glob(f"task-{index:02d}-attempt-*-prepared.json"))
    matches = [path for path in paths if json.loads(path.read_text()).get("status") == "browser_queued_waiting_barrier"]
    if len(matches) != 1:
        raise ValueError("expected_one_successful_prepared_request")
    return matches[0]


def validate_organizer_review(config, value, db):
    """Recheck existing browser/manual proof against trusted DB and fixed ZIP.

    Approval is already complete here. Do not fabricate pending-review or quota
    snapshots to call the browser's pre-approval validator a second time.
    """
    review = value.get('ai_review', {})
    sha = value.get('submission_sha256')
    manifest = json.loads(Path(config['adapter_manifest']).read_text())
    if (value.get('adapter') != 'codex'
            or manifest.get('codex', {}).get('sha256') != sha
            or digest(Path(config['adapters'])/'codex/harness.zip') != sha
            or value.get('upload_freeze_review_sha256') != sha
            or review.get('manual_fallback_template_sha256') != sha
            or review.get('manual_review_policy') != 'organizer-template'
            or review.get('manual_fallback_verified') is not True
            or review.get('manual_approval_preserved') is not True
            or review.get('quota_unchanged_before_manual_approval') is not True
            or review.get('ai_quality_gate_passed') is not False):
        raise ValueError('prepared_organizer_manual_proof_invalid')
    # This import is only needed for the explicitly selected browser policy.
    from cloud_browser_check import expected_advisory_provider
    advice = db.execute('SELECT state,submission_sha,result FROM review_advisories WHERE version_id=? ORDER BY created DESC LIMIT 1', (value['version_id'],)).fetchone()
    if not advice or advice[:2] != ('complete', sha):
        raise ValueError('prepared_organizer_advice_not_complete')
    result = json.loads(advice[2])
    if (result.get('status') != 'manual_required'
            or result.get('policy_version') != 'qiqc-readonly-advisory/v3'
            or result.get('provider_version') != expected_advisory_provider(config)):
        raise ValueError('prepared_organizer_advice_identity_invalid')
    for field in ('outcome', 'failure_code', 'policy_version', 'provider_version', 'manual_followup_required'):
        if review.get(field) != result.get(field):
            raise ValueError('prepared_organizer_advice_report_mismatch')
    if review.get('diagnostics') != result.get('provider_diagnostics'):
        raise ValueError('prepared_organizer_advice_report_mismatch')
    outcome = result.get('outcome')
    allowed = outcome in ('advice_ready', 'invalid_response', 'provider_timeout')
    if outcome == 'provider_error':
        diagnostic = result.get('provider_diagnostics') or {}
        allowed = (result.get('failure_code') == 'completion_token_limit'
                   and diagnostic.get('finish_reason') == 'length'
                   and all(type(diagnostic.get(k)) is int for k in ('prompt_tokens', 'completion_tokens', 'reasoning_tokens', 'max_tokens'))
                   and diagnostic['prompt_tokens'] > 0
                   and diagnostic['max_tokens'] == diagnostic['completion_tokens'] == diagnostic['reasoning_tokens'] == 8192)
    if (not allowed or review.get('real_advice_available') is not (outcome == 'advice_ready')
            or (outcome == 'advice_ready' and (result.get('manual_followup_required') is not True
                or review.get('manual_followup_visible_in_admin') is not True))
            or (outcome != 'advice_ready' and review.get('manual_fallback_notice_visible_in_admin') is not True)):
        raise ValueError('prepared_organizer_advice_outcome_invalid')
    reason = review.get('manual_approval_reason')
    expected_reason = ('AI ' + outcome + '/' + str(result.get('failure_code') or 'none')
                       + '; manual decision based on pre-reviewed organizer manifest SHA ' + sha)
    audit = db.execute("SELECT before_json,after_json,reason FROM audit WHERE action='review' AND object_id=? ORDER BY created DESC LIMIT 1", (value['version_id'],)).fetchone()
    version = db.execute('SELECT status,reason FROM versions WHERE id=?', (value['version_id'],)).fetchone()
    if (reason != expected_reason or not audit
            or json.loads(audit[0]) != 'pending_review' or json.loads(audit[1]) != 'approved'
            or audit[2] != reason or version != ('approved', reason)):
        raise ValueError('prepared_organizer_approval_audit_invalid')


def validate_prepared(config, directory, organizer_template_manual_review=False):
    rows = []
    with sqlite3.connect(Path(config['data']).joinpath('platform.db').as_uri()+'?mode=ro', uri=True) as db:
        for index in range(49):
            path = selected_prepared(directory, index)
            value = json.loads(path.read_text())
            if (value.get('status') != 'browser_queued_waiting_barrier'
                    or value.get('benchmark_release') != identity(config, index)
                    or value.get('catalog_index') != index
                    or value.get('admin_target_bound_via_browser') is not True):
                raise ValueError('prepared_browser_identity_or_review_invalid')
            if organizer_template_manual_review:
                validate_organizer_review(config, value, db)
            elif value.get('ai_review', {}).get('real_advice_available') is not True:
                raise ValueError('prepared_browser_identity_or_review_invalid')
            row = db.execute('SELECT b.status,b.version_id,v.sha256,t.task_index,t.release_id,r.state,r.attempt,t.agent_budget_seconds FROM batches b JOIN versions v ON v.id=b.version_id JOIN batch_scientific_targets t ON t.batch_id=b.id JOIN runs r ON r.batch_id=b.id WHERE b.id=?', (value['batch_id'],)).fetchall()
            expected = ('queued', value['version_id'], value['submission_sha256'], index, config['candidate_id'], 'queued', 0, agent_budget_seconds(config))
            if row != [expected]:
                raise ValueError('prepared_batch_not_exactly_queued')
            rows.append({'task_index': index, 'batch_id': value['batch_id'], 'prepared_sha256': digest(path), 'prepared_file': path.name})
        if len({r['batch_id'] for r in rows}) != 49:
            raise ValueError('duplicate_prepared_batch')
        accepted = db.execute("SELECT COUNT(*) FROM runs WHERE state IN ('queued','running','uploading')").fetchone()[0]
        if accepted != 49:
            raise ValueError('unrelated_active_work_at_barrier')
    return rows


def worker_inactive():
    return all(subprocess.run(['systemctl', 'is-active', '--quiet', unit], check=False).returncode != 0
               for unit in ('qiqc-native-worker-key2.service', 'qiqc-native-worker-key1.service'))


def browser(config_path, directory, index, mode, organizer_template_manual_review=False, config=None, driver=None):
    if mode == 'prepare':
        number = 1
        while (directory / f'task-{index:02d}-attempt-{number:02d}-prepared.json').exists():
            number += 1
        prepared = directory / f'task-{index:02d}-attempt-{number:02d}-prepared.json'
    else:
        prepared = selected_prepared(directory, index)
    output = prepared if mode == 'prepare' else prepared.with_name(prepared.name.replace('-prepared.json', '.json'))
    if mode == 'recover-browser':
        if output.exists() and json.loads(output.read_text()).get('status') == 'passed':
            return {'task_index': index, 'state': 'browser_passed_existing', 'report': output.name}
        # Recovery is only a fresh observation of the original queued request.
        # Retain failed driver evidence under an explicitly non-attempt name.
        for old in (output, output.with_suffix('.log')):
            if old.exists():
                number = 1
                while old.with_name(old.stem + f'-driver-failed-{number:02d}' + old.suffix).exists():
                    number += 1
                old.rename(old.with_name(old.stem + f'-driver-failed-{number:02d}' + old.suffix))
    if output.exists():
        raise ValueError('refusing_to_overwrite_browser_evidence')
    review_flag = '--organizer-template-manual-review' if organizer_template_manual_review else '--require-ai-advice'
    driver = Path(driver) if driver is not None else snapshot_driver(SCRIPTS, directory)
    command = [sys.executable, str(driver), '--candidate', str(config_path), '--catalog-index', str(index), review_flag, '--output', str(output)]
    if mode == 'prepare':
        if not worker_inactive():
            raise ValueError('worker_must_be_stopped_before_queue_preparation')
        command.append('--queue-only')
    else:
        command.extend(['--resume-queued-report', str(prepared)])
    with output.with_suffix('.log').open('x') as log:
        code = subprocess.run(command, stdout=log, stderr=log, check=False, **({'env': candidate_environment(config)} if config else {})).returncode
    if code:
        return {'task_index': index, 'state': 'browser_failed', 'report': output.name}
    if mode == 'prepare':
        return {'task_index': index, 'state': 'queued', 'report': output.name}
    if mode == 'recover-browser':
        return {'task_index': index, 'state': 'browser_passed', 'report': output.name}
    r2 = output.with_name(output.stem+'-r2.json')
    if config is None:
        config, _ = load_candidate(config_path)
    checker = Path(config['project_root'])/'competition-platform/platform/scripts/science/verify_native_candidate_evidence.py'
    with r2.with_suffix('.log').open('x') as log:
        code = subprocess.run([sys.executable, str(checker), '--candidate', str(config_path), '--report', str(output), '--output', str(r2)], stdout=log, stderr=log, check=False, env=candidate_environment(config)).returncode
    return {'task_index': index, 'state': 'passed' if code == 0 else 'r2_failed', 'report': output.name, 'r2_report': r2.name}


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=['prepare', 'start', 'collect', 'recover-browser'])
    parser.add_argument('--candidate', type=Path, required=True)
    parser.add_argument('--output-dir', type=Path, required=True)
    parser.add_argument('--indices', default=','.join(map(str, range(49))))
    parser.add_argument('--organizer-template-manual-review', action='store_true', help='explicit fixed organizer ZIP manual-review proof; default requires valid AI advice')
    parser.add_argument('--browser-workers', type=int, choices=range(1, 9), default=2, help='parallel browser preparation/collection only; science retains the 49-task start barrier')
    return parser.parse_args(argv)


def main():
    args = parse_args()
    config, _ = load_candidate(args.candidate)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    if args.mode == 'start':
        if not worker_inactive() or config.get('functional_slots') != 49:
            raise ValueError('start_requires_stopped_worker_with_49_explicit_slots')
        rows = validate_prepared(config, args.output_dir, args.organizer_template_manual_review)
        target = args.output_dir/'barrier-release.json'
        with target.open('x') as stream:
            json.dump({'schema': 'qiqc-native49-browser-barrier/v1', 'controller_sha256': config['controller_sha256'], 'queued': rows, 'actual_agent_overlap_verified': False}, stream, indent=2)
        subprocess.run(['systemctl', 'start', 'qiqc-native-worker-key1.service'], check=True)
        print(json.dumps({'started': True, 'queued': 49, 'worker_pool': 'key1', 'actual_overlap_not_yet_verified': True}), flush=True)
        return 0
    indices = [int(v) for v in args.indices.split(',')]
    if len(set(indices)) != len(indices) or any(not 0 <= i < 49 for i in indices):
        raise ValueError('invalid_indices')
    if args.mode == 'prepare' and not worker_inactive():
        raise ValueError('worker_must_be_stopped_before_queue_preparation')
    driver = snapshot_driver(SCRIPTS, args.output_dir)
    rows = []
    # Browser throughput does not change the separately verified science slots.
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.browser_workers) as pool:
        futures = {pool.submit(browser, args.candidate, args.output_dir, index, args.mode, args.organizer_template_manual_review, config, driver): index for index in indices}
        for future in concurrent.futures.as_completed(futures):
            try:
                row = future.result()
            except Exception as exc:  # noqa: BLE001 — public diagnostics never expose provider secrets.
                row = {'task_index': futures[future], 'state': 'driver_failed', 'error_type': type(exc).__name__}
            rows.append(row)
            (args.output_dir/(args.mode+'-progress.json')).write_text(json.dumps({'mode': args.mode, 'attempts': sorted(rows,key=lambda r:r['task_index'])}, indent=2)+'\n')
            print(json.dumps(row), flush=True)
    expected = {'queued'} if args.mode == 'prepare' else ({'browser_passed', 'browser_passed_existing'} if args.mode == 'recover-browser' else {'passed'})
    return 0 if all(row['state'] in expected for row in rows) else 1


if __name__ == '__main__':
    raise SystemExit(main())
