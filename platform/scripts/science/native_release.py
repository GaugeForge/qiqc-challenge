#!/usr/bin/env python3
"""Create a new frozen runtime record after all 49 real browser/R2 acceptances.

Existing evidence and candidate identities are never rewritten. prepare-activation
writes a reviewable switch plan only; it does not activate services or touch DBs.
"""
from __future__ import annotations
import argparse
import datetime
import hashlib
import json
import os
import shutil
import tempfile
from pathlib import Path

from native_candidate import load_candidate
from native_controller import verify as verify_controller
from native_freeze_gate import assess
from native_release_record import ACCEPTANCE_FIELDS, BINDINGS, SCHEMA, canonical, load_release, read_regular, validate_binding


def _digest(data):
    return hashlib.sha256(data).hexdigest()


def release_inputs(config,audit,evidence_root,plan_path,receipt_path):
    """Validate immutable inputs and return record payload plus exact file bytes."""
    controller=verify_controller(config)
    files={
        'source-audit.json':read_regular(config['source_audit']),
        'controller-manifest.json':read_regular(config['controller_manifest']),
        'build-plan.json':read_regular(plan_path),'build-receipt.json':read_regular(receipt_path)}
    for name,key in [('source-audit.json','source_audit_sha256'),('build-plan.json','build_plan_sha256'),('build-receipt.json','build_receipt_sha256')]:
        if _digest(files[name])!=config.get(key):raise ValueError('freeze_input_digest_mismatch:'+key)
    plan=json.loads(files['build-plan.json']);receipt=json.loads(files['build-receipt.json'])
    roster=sorted(audit['competition_task_ids'])
    if len(roster)!=49 or len(set(roster))!=49 or set(plan['tasks'])!=set(roster) or set(receipt['task_receipts'])!=set(roster):
        raise ValueError('freeze_roster_mismatch')
    for value in (plan,receipt):
        if any(value.get(k)!=config[k] for k in ('source_commit','source_files_sha256')):
            raise ValueError('freeze_source_binding_mismatch')
    if receipt.get('task_main_images_prebuilt') is not True:
        raise ValueError('freeze_requires_prebuilt_all49')
    root=Path(evidence_root).resolve();gate=assess(config,root)
    if gate['passed_tasks']!=49 or gate['status']!='eligible_for_explicit_freeze':
        raise ValueError('freeze_requires_all49_browser_r2_acceptance')
    checker='competition-platform/platform/scripts/science/verify_native_candidate_evidence.py'
    expected_checker=controller['host_files'][checker]
    tasks={};run_ids=set();archive_ids=set()
    for ordinal,task in enumerate(roster):
        selected=gate['rows'][ordinal]['selected'];evidence_path=root/selected['evidence']
        if not evidence_path.resolve().is_relative_to(root):raise ValueError('freeze_evidence_path_escape')
        raw=read_regular(evidence_path)
        if _digest(raw)!=selected['evidence_sha256']:raise ValueError('freeze_evidence_changed_during_check')
        evidence=json.loads(raw)
        if evidence.get('checker_sha256')!=expected_checker or evidence.get('build_input_digests')!={k:config[k] for k in ('build_plan_sha256','build_receipt_sha256')}:
            raise ValueError('freeze_checker_or_build_identity_mismatch')
        run_id=evidence.get('run_id');archive_id=evidence.get('archive_sha256')
        if not run_id or run_id in run_ids or archive_id in archive_ids:
            raise ValueError('freeze_duplicate_run_or_archive')
        run_ids.add(run_id);archive_ids.add(archive_id)
        browser_path=evidence_path.with_name(evidence_path.name[:-len('-r2.json')]+'.json')
        browser_bytes=read_regular(browser_path)
        if _digest(browser_bytes)!=evidence.get('browser_report_sha256'):
            raise ValueError('freeze_browser_changed_during_check')
        files[f'evidence/{ordinal:02d}-r2.json']=raw
        files[f'evidence/{ordinal:02d}.json']=browser_bytes
        row=receipt['task_receipts'][task];images=row['image_ids']
        if set(images)!={'agent','main','qsim','verifier'} or any(row.get(k)!=config[k] for k in ('source_commit','source_files_sha256')):
            raise ValueError('freeze_task_build_binding_missing')
        for image_id in images.values():
            if receipt['image_inspects_by_id'].get(image_id,{}).get('Id')!=image_id:
                raise ValueError('freeze_actual_image_inspection_missing')
        verifier=plan['verifiers'][plan['tasks'][task]['verifier_image']]
        if row.get('verifier_context_digest')!=verifier['context']['content_digest']:
            raise ValueError('freeze_verifier_context_mismatch')
        tasks[task]={**{key:evidence.get(key) for key in ACCEPTANCE_FIELDS},'task_index':ordinal,'image_ids':images,'verifier_context_digest':row['verifier_context_digest'],
                     'batch_id':selected['batch_id'],'run_id':run_id,'archive_sha256':archive_id,
                     'submission_sha256':evidence['submission_sha256'],
                     'source_runtime_software_verified':evidence.get('source_runtime_software_verified',evidence.get('source_runtime_parity_verified')),
                     'original_container_limits_verified':evidence.get('original_container_limits_verified',evidence.get('source_runtime_parity_verified')),
                     'full_host_capacity_verified':evidence.get('full_host_capacity_verified',evidence.get('source_runtime_parity_verified')),
                     'source_runtime_parity_verified':evidence['source_runtime_parity_verified']}
    files['freeze-gate.json']=canonical(gate)+b'\n'
    payload={**{key:config.get(key, "" if key == "diagnostic_model" else None) for key in BINDINGS},'acceptance_scope':'49-browser-bounded-five-minute',
             'agent_max_seconds':300,'early_stop_allowed':False,
             'concurrency_evidence':gate['concurrency_evidence'],
             'full_host_capacity_verified':all(row['full_host_capacity_verified'] for row in tasks.values()),
             'full_duration_performance_capacity_accepted':False,
             'competition_production_ready':False,'created_at':datetime.datetime.now(datetime.timezone.utc).isoformat(),
             'tasks':tasks,'files':{name:_digest(data) for name,data in sorted(files.items())}}
    return payload,files


def create_release(config,audit,evidence_root,plan_path,receipt_path,releases_root):
    payload,files=release_inputs(config,audit,evidence_root,plan_path,receipt_path)
    value={'schema':SCHEMA,'release_id':'native-release-'+_digest(canonical(payload)),
           'frozen':True,'payload':payload}
    root=Path(releases_root)
    if root.is_symlink():raise ValueError('release_root_symlink_forbidden')
    root.mkdir(parents=True,exist_ok=True,mode=0o700)
    target=root/value['release_id']
    if target.exists():raise ValueError('release_already_exists')
    temporary=Path(tempfile.mkdtemp(prefix='.freeze-',dir=root))
    try:
        for relative,data in {**files,'release.json':canonical(value)+b'\n'}.items():
            path=temporary/relative;path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
            with path.open('xb') as stream:stream.write(data);stream.flush();os.fsync(stream.fileno())
            path.chmod(0o440)
        load_release(temporary/'release.json')
        temporary.rename(target)
        fd=os.open(root,os.O_RDONLY)
        try:os.fsync(fd)
        finally:os.close(fd)
    finally:
        if temporary.exists():shutil.rmtree(temporary)
    return target/'release.json'


def prepare_activation(config,release_path,active_pointer):
    release=validate_binding(config,load_release(release_path))
    pointer=Path(active_pointer)
    previous=_digest(read_regular(pointer)) if pointer.exists() else None
    return {'schema':'qiqc-native-activation-plan/v1','status':'prepared_not_activated',
            'platform_switched':False,'release_id':release['release_id'],
            'release_file':str(Path(release_path).resolve()),'release_sha256':_digest(read_regular(release_path)),
            'candidate_id':config['candidate_id'],'controller_sha256':config['controller_sha256'],
            'data':config['data'],'active_pointer':str(pointer.absolute()),
            'expected_previous_pointer_sha256':previous,
            'preserve_existing_candidate_batches':True,'new_evaluations_only':True,
            'evaluation_scope':'organizer-only bounded functional native diagnostic, 300-second observed agent budget without readiness stop; no full-ranking or capacity acceptance',
            'required_atomic_activation_checks':[
                'Revalidate release files, candidate source, controller, template and exact build digests',
                'Create and verify one short-lived database recovery file for this switch transaction; do not accumulate timestamped historical backups',
                'Compare current active pointer with expected previous digest',
                'Bind only newly accepted evaluations to the new release; preserve existing run identities',
                'Record operator action and old/new pointer in the database transaction',
                'Verify API and worker agree on the active release before admitting new requests',
                'Retain release records and required evidence; keep the previous pointer only as short-lived switch recovery data',
                'After successful post-switch verification, delete this transaction recovery file and previous-pointer copy; preserve DB run, score and operation audit history'],
            'execution':'Use the platform activation implementation; this tool never mutates DB or services.'}


def main():
    parser=argparse.ArgumentParser(description=__doc__);sub=parser.add_subparsers(dest='action',required=True)
    freeze=sub.add_parser('freeze')
    for key in ('candidate','evidence-root','build-plan','build-receipt','releases-root'):freeze.add_argument('--'+key,type=Path,required=True)
    prepare=sub.add_parser('prepare-activation')
    for key in ('candidate','release','active-pointer','output'):prepare.add_argument('--'+key,type=Path,required=True)
    args=parser.parse_args();config,audit=load_candidate(args.candidate)
    if args.action=='freeze':
        path=create_release(config,audit,args.evidence_root,args.build_plan,args.build_receipt,args.releases_root)
        print(json.dumps({'release':str(path),'frozen':True,'platform_switched':False}))
    else:
        value=prepare_activation(config,args.release,args.active_pointer)
        with args.output.open('x') as stream:json.dump(value,stream,indent=2);stream.write('\n')
        print(json.dumps({'activation_plan':str(args.output),'platform_switched':False}))


if __name__=='__main__':main()
