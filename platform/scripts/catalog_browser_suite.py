#!/usr/bin/env python3
"""Sequential bounded cloud acceptance. Explicit invocation only; resume retains failed attempt history.

Every attempt uses a new account and real browser upload. Successful prior
reports are retained. Failure stops the suite for diagnosis; no quota refunds,
direct database writes, fixture substitutions or hidden rollout output.
"""
import argparse
import datetime
import hashlib
import fcntl
import os
import json
import sqlite3
import subprocess
import sys
import time
from pathlib import Path


SCRIPTS = Path(__file__).resolve().parent


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    review=parser.add_mutually_exclusive_group()
    review.add_argument('--organizer-template-manual-review',action='store_true',help='explicit fixed organizer-template manual review with preserved AI outcome evidence')
    review.add_argument('--allow-invalid-advice-manual-review', action='store_true', help='explicit exact organizer-template invalid-advice manual review; default requires real valid advice')
    parser.add_argument('--resume', action='store_true', help='validate this exact suite and skip only its latest strictly passing attempts')
    parser.add_argument('--candidate',type=Path,help='independent native candidate config')
    parser.add_argument('--indices', required=True, help='comma-separated zero-based task ordinals')
    parser.add_argument('--output-dir', type=Path, required=True)
    parser.add_argument('--credentials', type=Path, help='private admin seed; candidate attempts always use independent credential records')
    parser.add_argument('--allow-one-other-active', action='store_true', help='second bounded lane; deployed worker must have two slots')
    parser.add_argument('--max-other-active', type=int, choices=range(50), default=None,
                        help='bounded functional acceptance lanes only; not a capacity test')
    parser.add_argument('--pause-file', type=Path,
                        help='stop at a completed task boundary when this local file exists')
    return parser.parse_args(argv)


def main():
    args = parse_args()
    candidate=None
    if args.candidate:
        sys.path.insert(0,str(SCRIPTS/'science'))
        from native_candidate import load_candidate
        candidate,_=load_candidate(args.candidate)
        args.credentials=args.credentials or Path(candidate['credentials'])
    args.credentials=args.credentials or Path('/etc/qiqc-staging/preview-credentials.json')
    database=Path(candidate['data'])/'platform.db' if candidate else Path('/var/lib/qiqc-staging/data/platform.db')
    other_active = args.max_other_active if args.max_other_active is not None else (1 if args.allow_one_other_active else 0)
    indices = [int(v) for v in args.indices.split(',')]
    if not indices or len(indices) != len(set(indices)) or any(not 0 <= i < 49 for i in indices):
        raise SystemExit('Distinct task ordinals 0..48 required')
    args.output_dir.mkdir(parents=True, exist_ok=True)
    if args.output_dir.is_symlink():raise ValueError('suite_directory_symlink_forbidden')
    suite_lock=os.open(args.output_dir/'.suite.lock',os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600)
    try:fcntl.flock(suite_lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    except BlockingIOError:raise ValueError('suite_already_running')
    from catalog_suite_resume import binding,validate_resume
    rows = [];skip=set();previous_duration=0;policy_transitions=[]
    resume_binding=binding(candidate,args.output_dir,args.allow_invalid_advice_manual_review,args.organizer_template_manual_review) if candidate else None
    suite_path=args.output_dir/'suite.json'
    if args.resume:
        if candidate is None:raise ValueError('resume_requires_native_candidate')
        controller=json.loads(Path(candidate['controller_manifest']).read_bytes())
        checker_sha=controller['host_files']['competition-platform/platform/scripts/science/verify_native_candidate_evidence.py']
        with sqlite3.connect(database.resolve().as_uri()+'?mode=ro',uri=True) as db:
            previous,rows,skip,resume_binding=validate_resume(args.output_dir,indices,candidate,db,checker_sha,args.allow_invalid_advice_manual_review,args.organizer_template_manual_review)
        previous_duration=previous.get('duration_seconds',0)
        policy_transitions=previous.get('policy_transitions',[])
    elif suite_path.exists() or any(args.output_dir.glob('task-*-attempt-*.json')):
        raise ValueError('existing_suite_requires_explicit_resume')
    started = time.monotonic()
    def checkpoint(status):
        value = {'schema': 'qiqc-catalog-browser-suite/v1', 'status': status,
                 'updated_at': datetime.datetime.now(datetime.timezone.utc).isoformat(),
                 'selected_indices': indices, 'attempts': rows,
                 'duration_seconds': round(previous_duration+time.monotonic() - started, 3),
                 'resume_binding':resume_binding,'policy_transitions':policy_transitions,
                 'scientific_agent_budget_seconds': 300, 'mock_science': False,
                 'formal_capacity_acceptance': False}
        target = args.output_dir / 'suite.json';temp = target.with_suffix('.tmp')
        temp.write_text(json.dumps(value, indent=2) + '\n');temp.replace(target)
    def pause_on_failure(index, reason):
        if args.pause_file is None:
            return
        args.pause_file.parent.mkdir(parents=True, exist_ok=True)
        try:
            with args.pause_file.open('x') as output:
                json.dump({'catalog_index': index, 'reason': reason,
                           'suite': str(args.output_dir)}, output)
                output.write('\n')
        except FileExistsError:
            pass  # Keep the first failure; in-flight tasks finish and archive.
    checkpoint('resume_validated' if args.resume else 'initialized')
    for index in indices:
        if index in skip:
            print(json.dumps({'event':'verified_latest_attempt_skipped','catalog_index':index}),flush=True)
            continue
        if args.pause_file is not None and args.pause_file.exists():
            checkpoint('paused_before_next_task')
            print(json.dumps({'event': 'paused_before_next_task', 'catalog_index': index}), flush=True)
            return 0
        with sqlite3.connect(database.resolve().as_uri()+'?mode=ro', uri=True) as db:
            active = db.execute("SELECT count(*) FROM runs WHERE state IN ('queued','running','uploading') OR (state='attention' AND error='worker_lost:reconciliation_required')").fetchone()[0]
        if active > other_active:
            pause_on_failure(index, 'active_work_requires_inspection')
            checkpoint('paused_active_work_requires_inspection')
            return 2
        number = max([r['attempt'] for r in rows if r['catalog_index']==index],default=0)+1
        while (args.output_dir / f'task-{index:02d}-attempt-{number:02d}.json').exists():
            number += 1
        report = args.output_dir / f'task-{index:02d}-attempt-{number:02d}.json'
        r2 = report.with_name(report.stem + '-r2.json')
        row = {'catalog_index': index, 'attempt': number, 'browser_report': report.name, 'r2_report': r2.name, 'state': 'browser_running'}
        rows.append(row);checkpoint('running')
        print(json.dumps({'event': 'starting_browser', 'catalog_index': index, 'attempt': number}), flush=True)
        command = [sys.executable, str(SCRIPTS / 'cloud_browser_check.py'), '--adapter', 'codex', '--catalog-index', str(index), ('--organizer-template-manual-review' if args.organizer_template_manual_review else '--allow-invalid-advice-manual-review' if args.allow_invalid_advice_manual_review else '--require-ai-advice'), '--credentials', str(args.credentials), '--output', str(report)]
        if candidate:command.extend(['--candidate',str(args.candidate)])
        with report.with_suffix('.log').open('x') as log:
            process = subprocess.run(command, stdout=log, stderr=log, check=False)
        if report.exists():
            row['browser_sha256']=hashlib.sha256(report.read_bytes()).hexdigest()
            try:
                failed_report=json.loads(report.read_bytes())
                if failed_report.get('batch_id'):row['batch_id']=failed_report['batch_id']
            except (ValueError,TypeError):pass
        if process.returncode or not report.exists():
            pause_on_failure(index, 'browser_failed')
            row['state'] = 'browser_failed';checkpoint('failed_requires_diagnosis')
            print(json.dumps({'event': 'browser_failed', 'catalog_index': index}), flush=True)
            return 1
        browser = json.loads(report.read_text())
        row['browser_sha256'] = hashlib.sha256(report.read_bytes()).hexdigest()
        row['batch_id'] = browser['batch_id'];row['state'] = 'r2_verifying';checkpoint('running')
        with r2.with_suffix('.log').open('x') as log:
            # Use the deployed checker's immutable candidate copy. Development
            # edits must not silently change verification of an in-flight run.
            checker=(Path(candidate['project_root'])/'competition-platform/platform/scripts/science/verify_native_candidate_evidence.py'
                     if candidate else SCRIPTS/'science'/'verify_catalog_evidence.py')
            command=[sys.executable,str(checker),'--report',str(report),'--output',str(r2)]
            if candidate:command.extend(['--candidate',str(args.candidate)])
            process=subprocess.run(command,stdout=log,stderr=log,check=False)
        if r2.exists():row['r2_sha256']=hashlib.sha256(r2.read_bytes()).hexdigest()
        if process.returncode:
            pause_on_failure(index, 'r2_evidence_failed')
            row['state'] = 'r2_evidence_failed';checkpoint('failed_requires_diagnosis')
            print(json.dumps({'event': 'r2_evidence_failed', 'catalog_index': index}), flush=True)
            return 1
        evidence = json.loads(r2.read_text())
        row.update(state='passed', r2_sha256=hashlib.sha256(r2.read_bytes()).hexdigest(),
                   budget_exhausted=evidence['budget_exhausted'], score=browser['score'],
                   model_successful_calls=browser['model_usage_rows'],
                   browser_duration_seconds=browser['duration_seconds'])
        checkpoint('running')
        print(json.dumps({'event': 'task_passed', **row}), flush=True)
    checkpoint('passed')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
