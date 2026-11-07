#!/usr/bin/env python3
"""Read-only normal scientific R2 proof, separate from the actual browser report.

Loads the deployed strict archive inspector. SQL-derived target binding is clearly
identified as such; it never invents browser observations or manual target rows.
Fetches each private R2 archive into memory, never writes rollout contents to disk.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import sys


def require(condition, code):
    if not condition:
        raise ValueError(code)


def lookup_normal_runs(database, browser, config, audit):
    kind = browser.get("kind", "infra")
    require(kind in ("infra", "public"), "ordinary_invalid_kind")
    current_catalog = json.loads(Path(config['science_task_set_path']).read_text())
    native = current_catalog.get('schema') == 'qiqc-scientific-task-set/v2'
    expected = 5 if kind == "public" or native else 1
    if native:
        from task_catalog import upgrade_catalog
        require(upgrade_catalog(current_catalog, config['source_root']) == current_catalog,
                'ordinary_native_limits_source_mismatch')
    proofs = browser.get("runs", [browser])
    require(len(proofs) == expected and len({p.get("run_id") for p in proofs}) == expected, "ordinary_browser_run_count")
    proofs = {p["run_id"]: p for p in proofs}
    results = []
    require(browser.get('schema') == 'qiqc-ordinary-browser/v1' and browser.get('status') == 'passed', 'ordinary_browser_not_passed')
    require(browser.get('candidate_id') == config['candidate_id'] and browser.get('automatic_review') is True
            and browser.get('admin_after_upload_requests') == 0, 'ordinary_browser_identity_mismatch')
    require(browser.get('public_trace_readback') is True and browser.get('other_team_trace_denied') is True, 'ordinary_browser_permissions_missing')
    with sqlite3.connect(Path(database).resolve().as_uri() + '?mode=ro', uri=True) as db:
        db.row_factory = sqlite3.Row
        db.execute('BEGIN')
        rows = db.execute('''SELECT r.id AS run_id,r.batch_id,r.ordinal,r.public,r.attempt,r.state,r.reward,r.object_key,r.object_sha,
            b.status AS batch_status,b.mode,b.kind,b.team_id,b.version_id,v.team_id AS version_team,v.sha256,
            t.task_index,t.task_id,t.public AS target_public,t.release_id,t.agent_budget_seconds,
            t.catalog_sha256,t.submission_sha256,c.document,bc.catalog_sha256 AS batch_catalog
            FROM runs r JOIN batches b ON b.id=r.batch_id JOIN versions v ON v.id=b.version_id
            JOIN run_scientific_targets t ON t.run_id=r.id
            JOIN scientific_task_sets c ON c.sha256=t.catalog_sha256
            JOIN batch_scientific_task_sets bc ON bc.batch_id=b.id WHERE b.id=? ORDER BY r.ordinal''', (browser.get('batch_id'),)).fetchall()
        require(len(rows) == expected, 'ordinary_expected_run_count')
        for ordinal, item in enumerate(rows):
            row = dict(item)
            proof = proofs.get(row["run_id"], {})
            require(row['mode'] == 'scientific' and row['kind'] == kind and row['state'] == row['batch_status'] == 'completed', 'ordinary_run_not_complete')
            require(row['team_id'] == row['version_team'] and row['public'] == row['target_public'] == 1 and row['ordinal'] == ordinal, 'ordinary_run_ownership_or_visibility')
            for name in ('run_id', 'batch_id', 'team_id', 'version_id'):
                require(isinstance(row[name], str) and re.fullmatch(r'[a-zA-Z0-9_-]{1,80}', row[name]), 'ordinary_invalid_identity')
            require(type(row['attempt']) is int and row['attempt'] > 0, 'ordinary_invalid_attempt')
            require(row['run_id'] == proof.get('run_id') and row['attempt'] == proof.get('attempt') and row['version_id'] == browser.get('version_id'), 'ordinary_browser_run_mismatch')
            require(row['sha256'] == row['submission_sha256'] == browser.get('submission_sha256')
                    and isinstance(row['sha256'], str) and re.fullmatch(r'[a-f0-9]{64}', row['sha256']), 'ordinary_submission_mismatch')
            require(row['object_sha'] == proof.get('r2_archive_sha256') and isinstance(row['object_sha'], str)
                    and re.fullmatch(r'[a-f0-9]{64}', row['object_sha']), 'ordinary_archive_identity_mismatch')
            require(row['object_key'] == f"{row['team_id']}/{row['version_id']}/{row['batch_id']}/{row['run_id']}/{row['attempt']}.tar.gz", 'ordinary_archive_key_mismatch')
            require(row['catalog_sha256'] == row['batch_catalog'] == hashlib.sha256(row['document'].encode()).hexdigest(), 'ordinary_catalog_digest_mismatch')
            catalog = json.loads(row['document'])
            require(catalog == current_catalog, 'ordinary_current_catalog_mismatch')
            tasks = catalog.get('tasks', [])
            require(catalog.get('schema') in ('qiqc-scientific-task-set/v1', 'qiqc-scientific-task-set/v2') and len(tasks) == 49
                    and [t.get('task_index') for t in tasks] == list(range(49))
                    and [t.get('task_id') for t in tasks] == sorted(audit['competition_task_ids'])
                    and all(type(t.get('public')) is bool for t in tasks), 'ordinary_catalog_source_mismatch')
            require(type(row['task_index']) is int and 0 <= row['task_index'] < 49, 'ordinary_target_index')
            selected = [catalog.get('infra_task_index')] if kind == 'infra' and not native else [t['task_index'] for t in tasks if t['public']]
            task = tasks[row['task_index']]
            require(len(selected) == expected and selected[ordinal] == row['task_index'] and
                    {k: task[k] for k in ('task_index', 'task_id', 'public')} ==
                    dict(task_index=row['task_index'], task_id=row['task_id'], public=True), 'ordinary_infra_selection_mismatch')
            expected_budget = task['limits']['agent_seconds'] if native and task.get('evaluation_role') != 'infrastructure' else (min(catalog.get('agent_budget_seconds', 300), task['limits']['agent_seconds']) if native else catalog.get('agent_budget_seconds', 300))
            require(row['agent_budget_seconds'] == expected_budget, 'ordinary_budget_mismatch')
            require(row['release_id'] == catalog.get('release_id'), 'ordinary_release_mismatch')
            usage = [dict(r) for r in db.execute('SELECT request_id,request_correlation,upstream_attempt,status,error_class,model,input_tokens,output_tokens,cached_tokens FROM usage WHERE run_id=? AND attempt=? ORDER BY rowid', (row['run_id'], row['attempt']))]
            index = dict(batch_id=row['batch_id'], run_id=row['run_id'], attempt=row['attempt'], key=row['object_key'], sha256=row['object_sha'], submission_sha256=row['sha256'], reward=row['reward'])
            binding = dict(catalog_index=row['task_index'], release_id=row['release_id'], catalog_sha256=row['catalog_sha256'], agent_budget_seconds=row['agent_budget_seconds'])
            if native:
                binding['native_limits'] = task['limits']
                binding['acceptance_scope'] = 'practice-native'
            results.append((index, binding, usage))
    return results


def lookup_normal(database, browser, config, audit):
    """Compatibility entrypoint for the original single infra run inspector."""
    results = lookup_normal_runs(database, browser, config, audit)
    require(len(results) == 1, 'ordinary_expected_one_run')
    return results[0]


def readback_imports(config_path, workspace=False):
    from browser_candidate_runtime import bind_candidate_imports
    if not workspace:
        return bind_candidate_imports(config_path)
    # Explicit local acceptance mode; never import from an arbitrary path in
    # a candidate JSON, and never label workspace code as deployed code.
    root = Path(__file__).resolve().parents[3]
    config = json.loads(Path(config_path).read_text())
    project, candidate = Path(config['project_root']), Path(config['candidate_root'])
    require(not project.is_symlink() and project.resolve() == root and
            not candidate.is_symlink() and candidate.resolve().is_relative_to(root / 'artifacts'),
            'readback_workspace_candidate_invalid')
    science = Path(__file__).parent / 'science'
    sys.path.insert(0, str(science))
    return science


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--candidate', type=Path, required=True)
    ap.add_argument('--browser-report', type=Path, required=True)
    ap.add_argument('--output', type=Path, required=True)
    ap.add_argument('--recorded-controller', action='store_true',
                    help='Inspect an already completed run against its immutable execution manifest, not current workspace source')
    ap.add_argument('--workspace-candidate', action='store_true',
                    help='Use this repository inspector for an isolated candidate under repository artifacts/')
    args = ap.parse_args()
    require(not args.output.exists(), 'evidence_already_exists')
    report = {'schema': 'qiqc-ordinary-scientific-readback/v1', 'status': 'failed',
              'caller_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
              'browser_report_sha256': hashlib.sha256(args.browser_report.read_bytes()).hexdigest(),
              'binding_origin': 'read-only-immutable-run-target-and-catalog', 'browser_report_modified': False}
    try:
        science = readback_imports(args.candidate, args.workspace_candidate)
        from native_candidate import load_candidate, identity, admission_release_id
        import verify_native_candidate_evidence as checker
        from verify_cloud_relay_evidence import fetch_archive, read_r2_config, EvidenceError
        import ordinary_account_browser_check as browser_checker
        require(Path(checker.__file__).resolve().parent == science.resolve(), 'checker_not_deployed')
        config, audit = load_candidate(args.candidate, verify_controller=not args.recorded_controller)
        browser = json.loads(args.browser_report.read_text())
        database = Path(config['data']) / 'platform.db'
        runs = lookup_normal_runs(database, browser, config, audit)
        proof = browser_checker.check_database(database, browser['version_id'], browser['batch_id'], browser['submission_sha256'], browser.get('kind', 'infra'))
        require({p['run_id'] for p in proof['runs']} == {index['run_id'] for index, _, _ in runs}, 'ordinary_audit_run_mismatch')
        report['automatic_review_verified'] = True
        report['browser_audit_checker_sha256'] = hashlib.sha256(Path(browser_checker.__file__).read_bytes()).hexdigest()
        report['checker_sha256'] = hashlib.sha256(Path(checker.__file__).read_bytes()).hexdigest()
        report['kind'] = browser.get('kind', 'infra')
        report['current_workspace_execution_verified'] = not args.recorded_controller
        report['inspector_location'] = 'workspace' if args.workspace_candidate else 'deployment'
        report['runs'] = []
        r2 = read_r2_config(Path(config['r2_env']))
        for index, binding, usage in runs:
            require(binding['release_id'] == admission_release_id(config), 'ordinary_inactive_release')
            expected = identity(config, binding['catalog_index'], binding['release_id'])
            if binding.get('native_limits'):
                expected.update(native_limits=binding['native_limits'],
                                agent_budget_seconds=binding['agent_budget_seconds'],
                                acceptance_scope=binding['acceptance_scope'])
            derived = {'catalog_index': binding['catalog_index'], 'benchmark_release': expected, 'execution_topology': 'e2b-native-compose'}
            archive = fetch_archive(r2, index)
            inspected = checker.inspect(archive, index, derived, config, audit, usage,
                                        recorded_controller=args.recorded_controller)
            require(inspected.get('status') == 'passed', 'strict_archive_inspection_failed')
            report['runs'].append(dict(run_id=index['run_id'], derived_archive_inspection_binding=derived,
                                       catalog_sha256=binding['catalog_sha256'], strict_archive_evidence=inspected))
            del archive
        report.update(status='passed', batch_id=browser['batch_id'], verified_run_count=len(runs))
        if len(runs) == 1:
            report.update(report['runs'][0])
    except Exception as exc:
        # Only bounded fixed labels are written; raw provider/config exceptions stay private.
        text = str(exc)
        report['failure_code'] = text if isinstance(exc, (ValueError, RuntimeError)) and re.fullmatch(r'[a-z][a-z0-9_]{0,120}', text) else type(exc).__name__
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open('x') as stream:
        json.dump(report, stream, indent=2)
        stream.write('\n')
    print(json.dumps({'status': report['status'], 'report': str(args.output), 'failure_code': report.get('failure_code')}))
    return 0 if report['status'] == 'passed' else 1


if __name__ == '__main__':
    raise SystemExit(main())
