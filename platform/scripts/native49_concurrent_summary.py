#!/usr/bin/env python3
"""Read-only 49-task / 180-second browser evidence and actual Agent overlap.

No cloud calls, workload execution, database writes, freezing or activation.
Missing Harbor timestamps are never replaced by queue or VM creation times.
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import json
from pathlib import Path
import re
import sys

SCRIPTS = Path(__file__).resolve().parent


def science_dependency_root(argv):
    """Choose trusted deployed verifier code before importing any inventory.

    The running candidate's inventory must not inherit newer workspace edits.
    This initial JSON read selects code only; load_candidate/verify_controller
    still perform the actual integrity verification before assessment.
    """
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument('--candidate', type=Path)
    args, _ = parser.parse_known_args(argv)
    if args.candidate is None:
        return SCRIPTS/'science'  # --help has no candidate and executes no audit.
    value = json.loads(args.candidate.read_text())
    root = Path(value['project_root'])/'competition-platform/platform/scripts/science'
    for name in ('native_candidate.py','native_controller.py','native_release_record.py','native_freeze_gate.py'):
        if not (root/name).is_file():raise ValueError('deployed_science_dependency_missing')
    return root.resolve()


SCIENCE = science_dependency_root(sys.argv[1:]) if __name__=='__main__' else SCRIPTS/'science'
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(SCIENCE))
from native_candidate import agent_budget_seconds, identity, load_candidate
from native_controller import verify as verify_controller
from native_release_record import agent_window, concurrency_evidence, read_regular
import native_freeze_gate  # Bind the deployed acceptance rules before helper imports.
from native_functional49_summary import pair_errors
# The unbound report-layout helper prepends its workspace science directory.
# Restore deployment precedence for any lazy imports during candidate checks.
sys.path.insert(0, str(SCIENCE))
if __name__=='__main__':
    for name in ('native_candidate','native_controller','native_release_record','native_freeze_gate'):
        if Path(sys.modules[name].__file__).resolve().parent != SCIENCE.resolve():
            raise ValueError('mixed_science_dependency_roots')

NAME = re.compile(r'task-(\d{2})-attempt-(\d+)(-prepared|-r2)?\.json')
CHECKER = 'competition-platform/platform/scripts/science/verify_native_candidate_evidence.py'


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def failure_category(value):
    """Finite classifications only; never echo untrusted error text."""
    code = str(value.get('failure_code', ''))
    if 'qsim' in code:return 'qsim_evidence'
    if 'model' in code:return 'model_evidence'
    if 'verifier' in code:return 'verifier_evidence'
    if any(s in code for s in ('archive', 'rollout', 'r2', 'transfer')):return 'archive_or_transfer'
    if any(s in code for s in ('timing', 'duration', 'budget')):return 'execution_window'
    if value.get('stage') in ('ai-advisory', 'admin-review'):return 'submission_review'
    return 'other_or_unclassified'


def discover(directory):
    attempts = {}
    for path in [*directory.glob('task-*-attempt-*.json'), *directory.glob('task-*/task-*-attempt-*.json')]:
        if path.parent.is_symlink() or not path.resolve().is_relative_to(directory.resolve()):
            raise ValueError('report_path_escapes_directory')
        match = NAME.fullmatch(path.name)
        if not match:continue
        index, attempt = map(int, match.groups()[:2])
        if not 0 <= index < 49 or attempt < 1:
            raise ValueError('invalid_attempt_filename')
        if path.parent != directory and path.parent.name != f'task-{index:02d}':
            raise ValueError('task_directory_index_mismatch')
        key = (index, attempt)
        stem = path.with_name(f'task-{index:02d}-attempt-{attempt:02d}')
        if key in attempts and attempts[key] != stem:
            raise ValueError('duplicate_attempt_locations')
        attempts[key] = stem
    return attempts


def bound_identity_errors(config, index, browser, evidence, browser_sha, checker_sha):
    errors = []
    for value in (browser, evidence):
        if value.get('catalog_index') != index:errors.append('task_index_mismatch')
        if value.get('benchmark_release') != identity(config,index):errors.append('candidate_identity_mismatch')
    for key, expected in [('controller_sha256',config['controller_sha256']),
                          ('source_files_sha256',config['source_files_sha256']),
                          ('browser_report_sha256',browser_sha),('checker_sha256',checker_sha),
                          ('acceptance_scope','bounded-three-minute'),('agent_budget_seconds',180),
                          ('schema','qiqc-native-candidate-evidence/v1')]:
        if evidence.get(key) != expected:errors.append(key+'_mismatch')
    if type(evidence.get('agent_budget_seconds')) is not int:
        errors.append('agent_budget_seconds_mismatch')
    if (not isinstance(evidence.get('run_id'),str) or not evidence.get('run_id')
            or not isinstance(evidence.get('batch_id'),str) or not evidence.get('batch_id')
            or evidence.get('batch_id') != browser.get('batch_id')):
        errors.append('run_batch_identity_missing_or_mismatch')
    if evidence.get('build_input_digests') != {key:config[key] for key in ('build_plan_sha256','build_receipt_sha256')}:
        errors.append('build_input_digests_mismatch')
    for left,right in [('r2_archive_sha256','archive_sha256'),('submission_sha256','submission_sha256')]:
        if not re.fullmatch('[a-f0-9]{64}',str(evidence.get(right,''))) or browser.get(left) != evidence.get(right):
            errors.append('archive_or_submission_digest_mismatch')
    return sorted(set(errors))


def assess(config, directory, checker_sha):
    directory = Path(directory)
    if directory.is_symlink() or not directory.is_dir():raise ValueError('evidence_directory_invalid')
    if agent_budget_seconds(config) != 180:raise ValueError('explicit_180_second_candidate_required')
    attempts = discover(directory)
    rows = []; windows = {}; evidence_by_index = {}
    for index in range(49):
        selected = max((n for i,n in attempts if i == index), default=None)
        row = {'task_index':index,'attempt':selected,'passed':False,'errors':[],
               'failure_category':None,'timing_verified':False}
        rows.append(row)
        if selected is None:
            row['errors'] = ['not_started'];continue
        stem = attempts[index,selected]
        bp = stem.with_suffix('.json');ep = stem.with_name(stem.name+'-r2.json')
        row.update(browser_report=str(bp.relative_to(directory)),r2_report=str(ep.relative_to(directory)))
        if not bp.exists():row['errors']=['latest_browser_report_missing'];continue
        row['browser_report_present'] = True
        try:
            raw = read_regular(bp,4*1024*1024);browser = json.loads(raw)
            row['browser_sha256'] = digest(raw)
            if not isinstance(browser,dict):raise ValueError('browser_not_object')
            if browser.get('status') != 'passed':row['failure_category']=failure_category(browser)
            if not ep.exists():row['errors']=['latest_r2_report_missing'];continue
            eraw = read_regular(ep,4*1024*1024);evidence = json.loads(eraw)
            row['r2_sha256'] = digest(eraw)
            if not isinstance(evidence,dict):raise ValueError('r2_not_object')
            if evidence.get('status') != 'passed':row['failure_category']=failure_category(evidence)
            errors = bound_identity_errors(config,index,browser,evidence,digest(raw),checker_sha)
            row.update(run_id=evidence.get('run_id'),batch_id=evidence.get('batch_id'))
            if not errors:
                try:
                    window = agent_window(evidence)
                    windows[index] = window;evidence_by_index[index] = evidence
                    row.update(timing_verified=True,agent_execution_started_at=window[0].isoformat(),
                               agent_execution_finished_at=window[1].isoformat(),agent_elapsed_seconds=evidence['agent_elapsed_seconds'])
                except ValueError:
                    errors.append('harbor_agent_window_invalid_or_missing')
            if browser.get('status') == evidence.get('status') == 'passed':
                # Reuse the full existing browser/runtime/archive checks. Its
                # sole 300s-only scope label is replaced by the explicit 180s
                # binding above; shared acceptance checks already support 180s.
                errors.extend(e for e in pair_errors(config,index,browser,evidence,digest(raw),checker_sha)
                              if e != 'five_minute_scope_required')
            else:
                errors.append('attempt_not_passed')
            row['errors'] = sorted(set(errors));row['passed'] = not row['errors']
        except (ValueError,TypeError,KeyError,AttributeError,OSError):
            row['errors'] = ['report_invalid'];row['failure_category']='evidence_integrity'
            row['passed']=False;row['timing_verified']=False
            windows.pop(index,None);evidence_by_index.pop(index,None)
    # Reusing one accepted run under several task names never proves concurrency.
    for field in ('run_id','batch_id'):
        groups = {}
        for row in rows:
            if isinstance(row.get(field),str) and row[field]:groups.setdefault(row[field],[]).append(row)
        for group in groups.values():
            if len(group) < 2:continue
            for row in group:
                row['errors'].append('duplicate_'+field);row['passed']=False;row['timing_verified']=False
                windows.pop(row['task_index'],None);evidence_by_index.pop(row['task_index'],None)
    events = sorted((stamp,delta) for start,finish in windows.values() for stamp,delta in [(start,1),(finish,-1)])
    active = peak = 0
    for _,delta in events:
        active += delta;peak = max(peak,active)
    overlap = None
    if len(windows) == 49:
        try:overlap = concurrency_evidence([evidence_by_index[i] for i in range(49)])
        except ValueError:pass
    span = (max(w[0] for w in windows.values())-min(w[0] for w in windows.values())).total_seconds() if windows else None
    passed = [r['task_index'] for r in rows if r['passed']]
    missing = [r['task_index'] for r in rows if not r['timing_verified']]
    return {'schema':'qiqc-native49-concurrent-180s-summary/v1','candidate_id':config['candidate_id'],
        'controller_sha256':config['controller_sha256'],'source_files_sha256':config['source_files_sha256'],
        'agent_budget_seconds':180,'required_tasks':49,'passed_indices':passed,
        'failed_indices':[r['task_index'] for r in rows if r.get('browser_report_present') and not r['passed']],
        'pending_indices':[r['task_index'] for r in rows if not r.get('browser_report_present')],
        'timing_missing_indices':missing,'timing_verified_count':len(windows),
        'agent_start_span_seconds':span,'observed_peak_agent_concurrency':peak,
        'peak_is_lower_bound':bool(missing),'all49_common_agent_window':overlap,
        'all49_agent_overlap_verified':overlap is not None,
        'all49_end_to_end_passed':len(passed)==49,
        'status':'passed' if len(passed)==49 and overlap is not None else 'incomplete',
        'assessment_only_no_cloud_calls':True,'frozen':False,'platform_switched':False,
        'competition_production_ready':False,'rows':rows}


def markdown(report):
    lines=['# 49题同时运行180秒：实际Agent窗口核对','',
        f"严格端到端通过：**{len(report['passed_indices'])} / 49**。",
        f"已有可信运行时间：**{report['timing_verified_count']} / 49**；观测峰值并发：**{report['observed_peak_agent_concurrency']}**。",
        f"Agent启动时间跨度（秒）：`{report['agent_start_span_seconds']}`。",
        f"49题共有运行窗口：`{json.dumps(report['all49_common_agent_window'],ensure_ascii=False)}`。",'',
        '时间缺失时峰值只是已有证据的下界，不代表其余任务未运行。队列接受、VM创建和调度启动数量不替代Agent实际窗口。',
        '本报告不冻结、不切换平台，也不代表正式容量已经验收。','',
        '| 索引 | 最新attempt | 端到端通过 | 时间已验证 | 问题 |','|---:|---:|---|---|---|']
    for row in report['rows']:
        lines.append(f"| {row['task_index']} | {row['attempt']} | {row['passed']} | {row['timing_verified']} | {', '.join(row['errors']) or '—'} |")
    if report.get('readback_correction'):
        lines.extend(['', '归档复核采用明确记录的检查器修正：向验证函数传入原配置中的 180 秒预算。原部署检查器未通过，运行环境及 controller 摘要保持不变，没有重跑科学任务。', '', '修正检查器 SHA256：`'+report['readback_correction']['corrected_checker_sha256']+'`。'])
    return '\n'.join(lines)+'\n'


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--candidate',type=Path,required=True)
    parser.add_argument('--output-dir',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--markdown',type=Path)
    parser.add_argument('--readback-correction',type=Path,help='strict attestation for the omitted trusted budget argument only')
    args=parser.parse_args(argv)
    config,_=load_candidate(args.candidate);verify_controller(config)
    manifest=json.loads(read_regular(config['controller_manifest']))
    checker_sha=manifest['host_files'][CHECKER]
    correction = None
    if args.readback_correction:
        from native_readback_correction import verify as verify_correction
        correction = verify_correction(config, args.readback_correction)
        checker_sha = correction['corrected_checker_sha256']
    for target in [args.output,*([args.markdown] if args.markdown else [])]:
        if target.is_symlink() or target.resolve().is_relative_to(args.output_dir.resolve()):
            raise ValueError('summary_must_not_overwrite_evidence')
    report=assess(config,args.output_dir,checker_sha)
    if correction is not None:
        report['readback_correction'] = correction
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(report,indent=2)+'\n')
    if args.markdown:
        args.markdown.parent.mkdir(parents=True,exist_ok=True);args.markdown.write_text(markdown(report))
    print(json.dumps({key:report[key] for key in ('status','passed_indices','timing_missing_indices','observed_peak_agent_concurrency','all49_agent_overlap_verified')}))


if __name__=='__main__':main()
