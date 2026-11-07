"""Trusted E2B host readiness evidence; never imports benchmark science."""
from __future__ import annotations
import hashlib
import inspect
import json
import math
import re
import subprocess
import time
from pathlib import Path

IDENTITY_KEYS=('run_id','attempt','submission_sha256','candidate_identity')
SHA=re.compile(r'[0-9a-f]{64}')


def validate_progress(value,expected):
    errors=[]
    if not isinstance(value,dict):return ['qsim_progress_not_object']
    if value.get('schema')!='qiqc-native-qsim-progress/v1':errors.append('qsim_progress_schema_invalid')
    if any(value.get(k)!=expected.get(k) for k in IDENTITY_KEYS):errors.append('qsim_progress_identity_mismatch')
    for key in ('qsim_container_id','event_sha256','log_prefix_sha256'):
        if not SHA.fullmatch(str(value.get(key,''))):errors.append('qsim_progress_'+key+'_invalid')
    if type(value.get('success_count')) is not int or value['success_count']<1:errors.append('qsim_progress_no_success')
    if type(value.get('log_bytes')) is not int or not 0<value['log_bytes']<=16*1024*1024:errors.append('qsim_progress_bytes_invalid')
    observed=value.get('observed_at')
    if type(observed) not in (int,float) or not math.isfinite(observed) or observed<=0:errors.append('qsim_progress_time_invalid')
    return errors


def qsim_summary(raw):
    """Original common.py logs accepted metadata calls and completed job polls.

    Bootstrap, heartbeat, queued work and failed tools are not positive evidence.
    Returns only counts and digests; original event payload remains in qsim.
    """
    successes=[]
    for line in raw.splitlines():
        try:event=json.loads(line)
        except (ValueError,TypeError):continue
        if not isinstance(event,dict) or event.get('error') or event.get('failure_kind'):continue
        action=event.get('action')
        metadata=action in ('list_devices','get_device_spec','get_lab_notebook')
        completed=action=='get_job_result' and event.get('status')=='complete'
        if (metadata or completed) and event.get('tool')==action and isinstance(event.get('surface'),str) and event['surface']:
            successes.append(hashlib.sha256(line).hexdigest())
    return {'success_count':len(successes),'event_sha256':successes[0] if successes else None,
            'log_prefix_sha256':hashlib.sha256(raw).hexdigest(),'log_bytes':len(raw)}


def write_private(path,value):
    temporary=path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value,sort_keys=True));temporary.chmod(0o600);temporary.replace(path)


def observe_qsim(run,environment):
    from native_compose_environment import EVIDENCE
    target=Path(run['output_dir'])/'qsim-progress.json'
    if target.is_file():
        previous=json.loads(target.read_bytes())
        if previous.get('success_count',0)>0 and all(previous.get(k)==run[k] for k in IDENTITY_KEYS):
            return previous
    observed=EVIDENCE[run['run_id']]['runtime_observation']
    cid=observed['container_inspects']['qsim']['Id']
    if not SHA.fullmatch(cid):raise RuntimeError('invalid_qsim_container_id')
    # This metadata-only reader executes in qsim, not on the VPS. A bounded read
    # only limits readiness observation, never task input or tool responses.
    script='import json,hashlib,pathlib\n'+inspect.getsource(qsim_summary)+"\np=pathlib.Path('/qsim_logs/experiment_log.jsonl')\nraw=p.open('rb').read(16*1024*1024) if p.is_file() else b''\nprint(json.dumps(qsim_summary(raw)))\n"
    result=subprocess.run(['docker','exec',cid,'python3','-c',script],check=True,capture_output=True,text=True,timeout=10)
    summary=json.loads(result.stdout)
    value={'schema':'qiqc-native-qsim-progress/v1',**{k:run[k] for k in IDENTITY_KEYS},
           'qsim_container_id':cid,**summary,'observed_at':time.time()}
    write_private(target,value)
    return value


def accepted_request(run,progress):
    root=Path(run['output_dir']);request_path=root/'acceptance-stop-request.json'
    if not request_path.exists():return None
    request=json.loads(request_path.read_bytes())
    if request.get('schema')!='qiqc-native-acceptance-stop-request/v1':raise RuntimeError('acceptance_stop_schema_invalid')
    raw=(root/'acceptance-readiness.json').read_bytes();readiness=json.loads(raw)
    digest=hashlib.sha256(raw).hexdigest()
    if request.get('readiness_sha256')!=digest:raise RuntimeError('acceptance_readiness_digest_mismatch')
    for value in (request,readiness):
        if any(value.get(k)!=run[k] for k in IDENTITY_KEYS):raise RuntimeError('acceptance_stop_identity_mismatch')
    if readiness.get('schema')!='qiqc-native-acceptance-readiness/v1':raise RuntimeError('acceptance_readiness_schema_invalid')
    model=readiness.get('model',{});contract=model.get('response_contract',{})
    if contract.get('success') is not True or contract.get('terminal') is not True or contract.get('category')!='completed':
        raise RuntimeError('acceptance_model_not_completed')
    if not re.fullmatch(r'[a-f0-9]{32}',str(model.get('gateway_request_id',''))) or not SHA.fullmatch(str(model.get('adapter_request_sha256',''))):
        raise RuntimeError('acceptance_model_identity_invalid')
    qsim=readiness.get('qsim',{})
    if validate_progress(qsim,run):raise RuntimeError('acceptance_qsim_progress_invalid')
    if any(qsim.get(k)!=run[k] for k in IDENTITY_KEYS) or qsim.get('qsim_container_id')!=progress.get('qsim_container_id'):
        raise RuntimeError('acceptance_qsim_identity_invalid')
    if type(qsim.get('success_count')) is not int or not 1<=qsim['success_count']<=progress.get('success_count',0) or qsim.get('event_sha256')!=progress.get('event_sha256'):
        raise RuntimeError('acceptance_qsim_positive_evidence_missing')
    # The exact observed file may precede a later progress refresh; its bytes are
    # bound by the readiness request, and event hash/count are checked above.
    if hashlib.sha256(json.dumps(qsim,sort_keys=True).encode()).hexdigest()!=readiness.get('qsim_progress_sha256'):raise RuntimeError('acceptance_qsim_digest_invalid')
    return {'acceptance_readiness_sha256':digest,'acceptance_early_stop_verified':True,
            'readiness':{'model_gateway_request_id':model['gateway_request_id'],
                         'model_adapter_request_sha256':model['adapter_request_sha256'],
                         'qsim_progress_sha256':readiness['qsim_progress_sha256'],
                         'qsim_success_count':qsim['success_count']}}
