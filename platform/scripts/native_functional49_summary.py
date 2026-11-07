#!/usr/bin/env python3
"""Read-only three-lane functional coverage; never a 49-concurrent freeze gate.

Selects the latest numbered attempt per task from the exact supplied lanes.
A newer failure/pending attempt cannot be hidden by an older passing result.
No cloud, database mutation, source execution, deployment or release switching.
"""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path
import re
import sys
sys.path.insert(0,str(Path(__file__).parent/'science'))
from native_candidate import identity,load_candidate
from native_controller import verify as verify_controller
from native_freeze_gate import runtime_evidence_errors
from native_release_record import acceptance_evidence_errors,read_regular

BROWSER_FLAGS=('admin_target_bound_via_browser','invitation_login_password_change','result_visible_in_browser',
 'own_hidden_trace_denied','other_identity_trace_denied','r2_index_written','all_execution_sandboxes_destroyed',
 'local_rollout_cache_removed','nonranking')
RUNTIME_FLAGS=('snapshot_build_inputs_verified','actual_uploaded_zip_executed','native_compose_execution_verified',
 'private_network_verified','standard_harbor_rollout')
PATTERN=re.compile(r'task-(\d{2})-attempt-(\d+)\.json')


def digest(raw):return hashlib.sha256(raw).hexdigest()


def pair_errors(config,index,browser,evidence,browser_sha,checker_sha):
    errors=[]
    for value in (browser,evidence):
        if value.get('status')!='passed':errors.append('attempt_not_passed')
        if value.get('catalog_index')!=index:errors.append('task_index_mismatch')
        if value.get('benchmark_release')!=identity(config,index):errors.append('candidate_source_controller_profile_mismatch')
    if evidence.get('schema')!='qiqc-native-candidate-evidence/v1':errors.append('strict_native_checker_report_required')
    if evidence.get('acceptance_scope')!='bounded-five-minute':errors.append('five_minute_scope_required')
    errors.extend(runtime_evidence_errors(evidence));errors.extend(acceptance_evidence_errors(evidence))
    for key in RUNTIME_FLAGS:
        if evidence.get(key) is not True:errors.append(key+'_missing')
    for key in BROWSER_FLAGS:
        if browser.get(key) is not True:errors.append('browser_'+key+'_missing')
    if evidence.get('browser_report_sha256')!=browser_sha:errors.append('browser_digest_changed')
    if evidence.get('checker_sha256')!=checker_sha:errors.append('strict_checker_digest_mismatch')
    if evidence.get('controller_sha256')!=config['controller_sha256']:errors.append('controller_digest_mismatch')
    if evidence.get('source_files_sha256')!=config['source_files_sha256']:errors.append('source_digest_mismatch')
    if evidence.get('build_input_digests')!={key:config[key] for key in ('build_plan_sha256','build_receipt_sha256')}:
        errors.append('source_build_input_digest_mismatch')
    for left,right in [('batch_id','batch_id'),('r2_archive_sha256','archive_sha256'),('submission_sha256','submission_sha256')]:
        if not browser.get(left) or browser.get(left)!=evidence.get(right):errors.append('browser_archive_identity_mismatch')
    for key in ('archive_sha256','submission_sha256'):
        if not re.fullmatch('[a-f0-9]{64}',str(evidence.get(key,''))):errors.append(key+'_invalid')
    recovery=evidence.get('model_response_evidence') or {}
    if (recovery.get('all_failures_recovered') is not True or recovery.get('unrecovered_failed_responses')!=0
        or recovery.get('invalid_request_evidence')!=0 or recovery.get('invalid_planned_cancellations')!=0
        or type(recovery.get('successful_responses')) is not int or recovery['successful_responses']<1
        or recovery.get('complete_vs_incomplete_distinction_verified') is not True
        or recovery.get('gateway_request_link_verified') is not True):errors.append('model_completion_or_recovery_unverified')
    if evidence.get('model_id')!=identity(config,index)['model_id']:errors.append('actual_model_mismatch')
    if not evidence.get('run_id') or not evidence.get('batch_id'):errors.append('run_batch_identity_missing')
    return sorted(set(errors))


def assess(config,lanes,checker_sha):
    if len(lanes)!=3 or len({Path(p).resolve() for p in lanes})!=3:raise ValueError('three_distinct_lane_directories_required')
    rows={};lane_info=[];global_errors=[]
    for lane_index,path in enumerate(lanes):
        path=Path(path)
        if path.is_symlink() or not path.is_dir():raise ValueError('lane_directory_invalid')
        suite_path=path/'suite.json'
        if not suite_path.exists():
            lane_info.append({'lane':lane_index,'directory':str(path),'status':'not_started'})
            continue
        raw=read_regular(suite_path,1024*1024);suite=json.loads(raw)
        if suite.get('schema')!='qiqc-catalog-browser-suite/v1':raise ValueError('suite_schema_invalid')
        planned=suite.get('selected_indices')
        if not isinstance(planned,list) or len(planned)!=len(set(planned)) or any(type(i) is not int or not 0<=i<49 for i in planned):
            raise ValueError('suite_task_plan_invalid')
        lane_info.append({'lane':lane_index,'directory':str(path),'status':suite.get('status'),'suite_sha256':digest(raw),'planned_indices':planned})
        files={}
        for file in path.glob('task-*-attempt-*.json'):
            match=PATTERN.fullmatch(file.name)
            if match:
                i,n=map(int,match.groups())
                if not 0<=i<49 or n<1:raise ValueError('attempt_filename_invalid')
                files.setdefault(i,{})[n]=file
        attempts={};attempt_keys=set()
        for row in suite.get('attempts',[]):
            i,n=row.get('catalog_index'),row.get('attempt')
            if type(i) is not int or i not in planned or type(n) is not int or n<1:raise ValueError('suite_attempt_identity_invalid')
            if (i,n) in attempt_keys:raise ValueError('duplicate_suite_attempt')
            attempt_keys.add((i,n))
            if i not in attempts or n>attempts[i]['attempt']:attempts[i]=row
        for index in sorted(set(planned)|set(files)):
            if index in rows:
                global_errors.append('task_present_in_multiple_lanes:'+str(index));rows[index]['errors'].append('task_present_in_multiple_lanes');rows[index]['passed']=False;continue
            manifest=attempts.get(index)
            numbers=list(files.get(index,{})) + ([manifest['attempt']] if manifest else [])
            selected=max(numbers) if numbers else None
            row={'task_index':index,'lane':lane_index,'attempt':selected,'passed':False,'errors':[],
                 'earlier_attempts_retained':sorted(n for n in numbers if n!=selected)}
            rows[index]=row
            if index not in planned:row['errors'].append('task_not_in_current_suite_plan')
            if manifest is None or selected!=manifest['attempt']:row['errors'].append('attempt_not_in_current_suite_manifest')
            if selected is None:row['errors']=['task_not_started'];continue
            browser_path=path/f'task-{index:02d}-attempt-{selected:02d}.json'
            r2_path=browser_path.with_name(browser_path.stem+'-r2.json')
            row.update(browser_report=str(browser_path),r2_report=str(r2_path))
            if manifest and selected==manifest['attempt'] and manifest.get('state')!='passed':row['errors'].append('latest_suite_attempt_not_passed')
            if not browser_path.exists() or not r2_path.exists():row['errors'].append('latest_attempt_evidence_incomplete');continue
            try:
                bb=read_regular(browser_path,4*1024*1024);eb=read_regular(r2_path,4*1024*1024)
                browser=json.loads(bb);evidence=json.loads(eb)
                row.update(browser_sha256=digest(bb),r2_sha256=digest(eb),run_id=evidence.get('run_id'),batch_id=evidence.get('batch_id'),
                    archive_sha256=evidence.get('archive_sha256'),agent_elapsed_seconds=evidence.get('agent_elapsed_seconds'),
                    recovered_model_failures=(evidence.get('model_response_evidence') or {}).get('recovered_failed_responses'))
                row['errors'].extend(pair_errors(config,index,browser,evidence,digest(bb),checker_sha))
                if manifest and selected==manifest['attempt']:
                    if manifest.get('browser_sha256')!=digest(bb) or manifest.get('r2_sha256')!=digest(eb):row['errors'].append('suite_report_digest_mismatch')
            except (ValueError,TypeError,OSError,KeyError):row['errors'].append('latest_report_invalid')
            row['errors']=sorted(set(row['errors']));row['passed']=not row['errors']
    for index in range(49):rows.setdefault(index,{'task_index':index,'passed':False,'errors':['task_not_planned_or_started']})
    for field in ('run_id','batch_id'):
        seen={}
        for index,row in rows.items():
            value=row.get(field)
            if value:
                if value in seen:
                    for duplicate in (row,rows[seen[value]]):duplicate['errors'].append('duplicate_'+field);duplicate['passed']=False
                seen[value]=index
    passed=sum(row['passed'] for row in rows.values());complete=passed==49 and not global_errors
    return {'schema':'qiqc-native-functional-all49-summary/v1','status':'passed' if complete else 'incomplete',
        'functional_all49':complete,'concurrent_all49':False,'concurrent_all49_verified':False,
        'concurrency_reason':'three sequential lanes do not prove 49 simultaneous task execution; existing concurrency freeze gate unchanged',
        'competition_production_ready':False,'official_model_equivalence_verified':False,'frozen':False,'platform_switched':False,
        'candidate_id':config['candidate_id'],'controller_sha256':config['controller_sha256'],
        'source_files_sha256':config['source_files_sha256'],'provider_profile':config.get('provider_profile'),
        'model_id':identity(config,0)['model_id'],'passed_tasks':passed,'required_tasks':49,'lanes':lane_info,
        'errors':global_errors,'rows':[rows[i] for i in range(49)],'assessment_only_no_cloud_calls':True}


def markdown(report):
    text=['# 49题分批端到端验收进展','',f"当前严格通过：**{report['passed_tasks']} / 49**。",'',
          f"- 全49题功能闭环：`{str(report['functional_all49']).lower()}`。",
          '- 全49题同时并发验收：`false`，本报告不提供该证明，也不修改已有冻结门禁。',
          '- 本报告只读取指定三个lane中的最新attempt，旧pass不能覆盖新失败或新一轮缺失。',
          '- 所有入选记录必须来自同一source、controller、profile及300秒验收规则。',
          f"- Controller：`{report['controller_sha256']}`。",'',
          '| Task索引 | 最新attempt | 状态 | 未通过原因 |','|---:|---:|---|---|']
    for row in report['rows']:
        text.append(f"| {row['task_index']} | {row.get('attempt','—')} | {'通过' if row['passed'] else '待完成'} | {', '.join(row['errors']) or '—'} |")
    return '\n'.join(text)+'\n'


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--candidate',type=Path,required=True)
    p.add_argument('--lane',type=Path,action='append',required=True);p.add_argument('--output',type=Path,required=True)
    p.add_argument('--markdown',type=Path);args=p.parse_args()
    config,_=load_candidate(args.candidate);verify_controller(config)
    manifest=json.loads(read_regular(config['controller_manifest']))
    checker=manifest['host_files']['competition-platform/platform/scripts/science/verify_native_candidate_evidence.py']
    for target in [args.output,*([args.markdown] if args.markdown else [])]:
        if any(target.resolve().is_relative_to(lane.resolve()) for lane in args.lane):raise ValueError('summary_must_not_overwrite_evidence')
        if target.is_symlink():raise ValueError('summary_symlink_forbidden')
    report=assess(config,args.lane,checker)
    args.output.parent.mkdir(parents=True,exist_ok=True);args.output.write_text(json.dumps(report,indent=2)+'\n')
    if args.markdown:args.markdown.parent.mkdir(parents=True,exist_ok=True);args.markdown.write_text(markdown(report))
    print(json.dumps({'status':report['status'],'functional_all49':report['functional_all49'],'concurrent_all49':False,'passed_tasks':report['passed_tasks']}))

if __name__=='__main__':main()
