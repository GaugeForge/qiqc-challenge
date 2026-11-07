#!/usr/bin/env python3
"""Read-only preparation for a future native R2 outage/SIGKILL drill.

There is deliberately no execution/fault-injection entrypoint. This tool never
changes configuration, kills processes, restarts services, or calls a provider.
"""
import argparse
import hashlib
import json
import re
import sqlite3
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).parent/'science'))
from native_controller import verify as verify_controller
from native_candidate import active_release
from native_release_record import read_regular

ACTIVE="state IN ('queued','running','uploading') OR (state='attention' AND error='worker_lost:reconciliation_required')"


def validate_pending_native(row,pending,resource_receipt,target,archive_sha,attempt_rows,active_runs):
    errors=[]
    if row.get('state')!='uploading' or row.get('attempt')!=1 or row.get('public')!=0 or row.get('ordinal')!=0:
        errors.append('native_first_catalog_upload_not_ready')
    if row.get('mode')!='scientific-smoke' or row.get('kind')!='infra':errors.append('native_diagnostic_mode_required')
    lease=pending.get('lease',{})
    expected={k:row.get(k) for k in ('batch_id','team_id','owner','attempt','ordinal','public','version_id','mode','kind')}
    expected.update(run_id=row.get('id'),sha256=row.get('submission_sha256'))
    if any(lease.get(k)!=v for k,v in expected.items()):errors.append('native_pending_identity_mismatch')
    if pending.get('provenance')!='scientific-smoke' or type(pending.get('reward')) is not int or pending['reward'] not in (0,1):errors.append('native_pending_score_invalid')
    if not isinstance(pending.get('seconds'),str) or not re.fullmatch(r'[0-9]+\.[0-9]{6}',pending['seconds']):errors.append('native_pending_runtime_invalid')
    if not re.fullmatch('[a-f0-9]{64}',archive_sha or '') or pending.get('bundle_sha256')!=archive_sha:errors.append('native_pending_digest_mismatch')
    if resource_receipt.get('run_id')!=row.get('id') or resource_receipt.get('attempt')!=1 or resource_receipt.get('mode')!='scientific-smoke':errors.append('native_resource_identity_mismatch')
    resources=resource_receipt.get('resources',{})
    if not isinstance(resources,dict) or len(resources)!=1 or set(resources.values())!={'destroyed'}:errors.append('native_one_vm_cleanup_not_confirmed')
    if resource_receipt.get('phase')!='uploading_rollout' or resource_receipt.get('controller_error_class'):errors.append('native_science_not_finished_cleanly')
    if target.get('batch_id')!=row.get('batch_id') or target.get('submission_sha256')!=row.get('submission_sha256') or target.get('task_index')!=48:
        errors.append('native_dedicated_cz_binding_missing')
    if not str(target.get('release_id','')).startswith('native-release-'):errors.append('native_frozen_release_required')
    if attempt_rows!=[{'id':lease.get('attempt_id'),'number':1,'state':'running'}]:errors.append('native_original_attempt_changed')
    if set(active_runs)!={row.get('id')}:errors.append('native_exclusive_batch_required')
    return sorted(set(errors))


def prepare(candidate,run_id=None):
    config=json.loads(read_regular(candidate));verify_controller(config)
    release=active_release(config)
    db=Path(config['data'])/'platform.db'
    with sqlite3.connect(db.resolve().as_uri()+'?mode=ro',uri=True) as connection:
        connection.row_factory=sqlite3.Row
        active=[r[0] for r in connection.execute('SELECT id FROM runs WHERE '+ACTIVE)]
        report={'schema':'qiqc-native-r2-recovery-preflight/v1','read_only':True,'fault_injected':False,'worker_killed':False,
                'controller_sha256':config['controller_sha256'],'active_run_count':len(active),
                'formal_release_verified':release is not None,'native_expected_sandbox_count':1,
                'ready_for_future_exclusive_setup':not active and release is not None,
                'operator_start_authorization_required':True,
                'trigger_contract':'one-bound-native-CZ-attempt-pending-archive-and-destroyed-VM-plus-observed-full-body-503',
                'not_verified':['actual fault injection','worker SIGKILL restart','same-attempt real R2 recovery','new browser result after recovery']}
        if run_id:
            if not re.fullmatch('[a-f0-9]{32}',run_id):raise ValueError('invalid_run_id')
            row=connection.execute('SELECT r.*,b.team_id,b.version_id,b.mode,b.kind,v.sha256 submission_sha256 FROM runs r JOIN batches b ON b.id=r.batch_id JOIN versions v ON v.id=b.version_id WHERE r.id=?',(run_id,)).fetchone()
            if row is None:raise ValueError('unknown_run')
            row=dict(row);target=connection.execute('SELECT * FROM batch_scientific_targets WHERE batch_id=?',(row['batch_id'],)).fetchone()
            attempts=[dict(r) for r in connection.execute('SELECT id,number,state FROM attempts WHERE run_id=? ORDER BY number',(run_id,))]
            cache=Path(config['data'])/'cache'/run_id/str(row['attempt'])
            if not (cache/'pending.json').exists():report['pending_errors']=['pending_not_promoted_no_kill']
            else:
                pending=json.loads(read_regular(cache/'pending.json',65536))
                resource=json.loads(read_regular(Path(config['data'])/'scientific'/f'{run_id}_{row["attempt"]}.json',65536))
                archive_sha=hashlib.sha256(read_regular(cache/'rollout.tar.gz',64*1024*1024)).hexdigest()
                report['pending_errors']=validate_pending_native(row,pending,resource,dict(target or {}),archive_sha,attempts,active)
                report['archive_sha256']=archive_sha
            # This preflight cannot prove that a fault server observed the upload.
            report['sigkill_authorized']=False
            report['full_body_503_observed']=False
    return report


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--candidate',type=Path,required=True);parser.add_argument('--run-id');args=parser.parse_args()
    print(json.dumps(prepare(args.candidate,args.run_id),indent=2))

if __name__=='__main__':main()
