import hashlib
import json
import sys
from pathlib import Path
import pytest
sys.path.insert(0,str(Path(__file__).parents[1]/'scripts/science'))
from native_acceptance_stop import qsim_summary,accepted_request,write_private,validate_progress


def test_only_original_success_events_count():
    rows=[{'action':'qsim_bootstrap_context'}, {'action':'qsim_heartbeat'},
          {'action':'get_job_result','tool':'get_job_result','surface':'qiskit','status':'queued'},
          {'action':'get_job_result','tool':'get_job_result','surface':'mcp','status':'failed','error':'failure'},
          {'action':'list_devices','tool':'list_devices','surface':'mcp'},
          {'action':'get_job_result','tool':'get_job_result','surface':'qiskit','status':'complete'}]
    raw=b'\n'.join(json.dumps(r).encode() for r in rows)
    assert qsim_summary(raw)['success_count']==2
    assert qsim_summary(b'{broken\n')['success_count']==0


def proof(tmp_path):
    run={'run_id':'run','attempt':1,'submission_sha256':'a'*64,'candidate_identity':{'controller_sha256':'b'*64},'output_dir':str(tmp_path)}
    q={'schema':'qiqc-native-qsim-progress/v1',**{k:run[k] for k in ('run_id','attempt','submission_sha256','candidate_identity')},'qsim_container_id':'c'*64,'success_count':1,'event_sha256':'d'*64,'log_prefix_sha256':'e'*64,'log_bytes':100,'observed_at':1000}
    ready={'schema':'qiqc-native-acceptance-readiness/v1',**{k:run[k] for k in ('run_id','attempt','submission_sha256','candidate_identity')},'qsim':q,'qsim_progress_sha256':hashlib.sha256(json.dumps(q,sort_keys=True).encode()).hexdigest(),'model':{'gateway_request_id':'f'*32,'adapter_request_sha256':'1'*64,'response_contract':{'terminal':True,'success':True,'category':'completed'}}}
    write_private(tmp_path/'acceptance-readiness.json',ready)
    request={'schema':'qiqc-native-acceptance-stop-request/v1',**{k:run[k] for k in ('run_id','attempt','submission_sha256','candidate_identity')},'readiness_sha256':hashlib.sha256((tmp_path/'acceptance-readiness.json').read_bytes()).hexdigest()}
    write_private(tmp_path/'acceptance-stop-request.json',request)
    return run,q,ready,request


def test_complete_proof_matches_trusted_progress(tmp_path):
    run,q,ready,request=proof(tmp_path)
    assert validate_progress(q,run)==[]
    result=accepted_request(run,q)
    assert result['acceptance_early_stop_verified'] is True
    assert result['readiness']['qsim_success_count']==1


@pytest.mark.parametrize('kind',['identity','model_error','qsim_forged','digest'])
def test_reject_unproven_stop_request(tmp_path,kind):
    run,q,ready,request=proof(tmp_path)
    if kind=='identity':ready['attempt']=2
    if kind=='model_error':ready['model']['response_contract']['success']=False
    if kind=='qsim_forged':q['event_sha256']='0'*64
    if kind=='digest':ready['qsim_progress_sha256']='0'*64
    write_private(tmp_path/'acceptance-readiness.json',ready)
    request['readiness_sha256']=hashlib.sha256((tmp_path/'acceptance-readiness.json').read_bytes()).hexdigest()
    write_private(tmp_path/'acceptance-stop-request.json',request)
    with pytest.raises(RuntimeError):accepted_request(run,q)
