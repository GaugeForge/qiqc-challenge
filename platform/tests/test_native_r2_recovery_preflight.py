import sys
from pathlib import Path
import pytest
sys.path.insert(0,str(Path(__file__).parents[1]/'scripts'))
from native_r2_recovery_preflight import validate_pending_native


def fixture():
    row={'id':'r','batch_id':'b','team_id':'t','version_id':'v','submission_sha256':'a'*64,'mode':'scientific-smoke','kind':'infra','owner':'worker','state':'uploading','attempt':1,'public':0,'ordinal':0}
    lease={k:row[k] for k in ('batch_id','team_id','version_id','mode','kind','owner','attempt','public','ordinal')};lease.update(run_id='r',sha256='a'*64,attempt_id='attempt')
    pending={'lease':lease,'reward':0,'seconds':'300.100000','provenance':'scientific-smoke','bundle_sha256':'b'*64}
    resource={'run_id':'r','attempt':1,'mode':'scientific-smoke','phase':'uploading_rollout','resources':{'vm':'destroyed'}}
    target={'batch_id':'b','submission_sha256':'a'*64,'task_index':48,'release_id':'native-release-'+'c'*64}
    return row,pending,resource,target,'b'*64,[{'id':'attempt','number':1,'state':'running'}],['r']


def test_native_single_destroyed_vm_pending_is_eligible_for_503_observation():
    assert validate_pending_native(*fixture())==[]


@pytest.mark.parametrize('mutation',['legacy_three','active_vm','other_active','candidate_only','staged','other_sha','wrong_attempt','foreign_owner'])
def test_no_kill_for_wrong_resource_identity_or_incomplete_publication(mutation):
    args=list(fixture());row,pending,resource,target=args[:4]
    if mutation=='legacy_three':resource['resources'].update(vm2='destroyed',vm3='destroyed')
    if mutation=='active_vm':resource['resources']['vm']='created'
    if mutation=='other_active':args[-1].append('another')
    if mutation=='candidate_only':target['release_id']='native-candidate-'+'c'*64
    if mutation=='staged':row['state']='running'
    if mutation=='other_sha':pending['bundle_sha256']='d'*64
    if mutation=='wrong_attempt':args[-2][0]['number']=2
    if mutation=='foreign_owner':pending['lease']['owner']='other'
    assert validate_pending_native(*args)
