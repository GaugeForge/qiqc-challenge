import hashlib
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
sys.path.insert(0,str(Path(__file__).parents[1]/'scripts/science'))
import native_build_receipt as receipts
from native_contract import build_plan, NativeContractError


@pytest.fixture(scope='module')
def plan():
    return build_plan(Path(__file__).resolve().parents[3]/'benchmark-assets/qiqcbench-restored')


def fixture_records(plan,tmp_path):
    ids={name:'sha256:'+hashlib.sha256(name.encode()).hexdigest() for name in ['agent','qsim','main']}
    inspected={}
    for name,image_id in ids.items():
        inspected[image_id]={'Id':image_id,'RootFS':{'Layers':['agent'] if name in ['agent','main'] else ['qsim']},
                            'Config':{'Labels':{'org.opencontainers.image.revision':plan['source_commit']}}}
    records={}
    for task,contract in plan['tasks'].items():
        image=contract['verifier_image'];image_id='sha256:'+hashlib.sha256(image.encode()).hexdigest()
        v=plan['verifiers'][image]
        inspected[image_id]={'Id':image_id,'RootFS':{'Layers':['qsim','verifier'] if v['derived_from_qsim'] else ['standalone']},
             'Config':{'Labels':{'org.opencontainers.image.revision':plan['source_commit'],
                                'qiqcbench.verifier.source_revision':plan['source_commit'],'qiqcbench.verifier.content_digest':v['context']['content_digest']}}}
        records[task]={'status':'recorded','image_ref':image,'image_id':image_id}
    (tmp_path/'agent_image.json').write_text(json.dumps({'status':'recorded','tag_stable':True,'image_id':ids['agent'],
                'packages':plan['agent']['direct_packages'],'agent_lib':plan['agent']['client_sha256']}))
    (tmp_path/'runtime_images.json').write_text(json.dumps({'qsim_status':'recorded','qsim_image_id':ids['qsim'],'verifier_images':records}))
    (tmp_path/'main_build_checks.json').write_text(json.dumps({'agent_image_id_before':ids['agent'],'agent_image_id_after':ids['agent']}))
    def run(argv,**kwargs):
        assert argv[:3]==['docker','image','inspect']
        return SimpleNamespace(returncode=0,stdout=json.dumps([inspected[argv[-1]]]))
    return ids,inspected,run


def test_captured_49_receipt_and_task_main_binding(plan,tmp_path):
    ids,_,run=fixture_records(plan,tmp_path)
    receipt=receipts.capture(plan,tmp_path,run)
    assert len(receipt['task_receipts'])==49
    assert not receipt['runtime_accepted']
    task='tunable_coupler_cz_netzero'
    seed=receipts.task_runtime_seed(receipt,plan,task,{'Image':ids['main']},run)
    assert seed['build_receipt']['image_ids']['main']==ids['main']
    assert set(seed['image_inspects'])=={'agent','main','qsim','verifier'}


def test_stale_verifier_base_is_rejected(plan,tmp_path):
    _,inspected,run=fixture_records(plan,tmp_path)
    target=plan['tasks']['tunable_coupler_cz_netzero']['verifier_image']
    inspected['sha256:'+hashlib.sha256(target.encode()).hexdigest()]['RootFS']['Layers']=['stale']
    with pytest.raises(NativeContractError,match='base_mismatch'):
        receipts.capture(plan,tmp_path,run)


def test_wrong_agent_main_base_rejected(plan,tmp_path):
    ids,inspected,run=fixture_records(plan,tmp_path)
    receipt=receipts.capture(plan,tmp_path,run)
    inspected[ids['main']]['RootFS']['Layers']=['wrong']
    with pytest.raises(NativeContractError,match='main_agent_base'):
        receipts.task_runtime_seed(receipt,plan,'tunable_coupler_cz_netzero',{'Image':ids['main']},run)


def test_missing_original_record_never_fabricates_images(plan,tmp_path):
    _,_,run=fixture_records(plan,tmp_path)
    value=json.loads((tmp_path/'runtime_images.json').read_text());value['verifier_images']={}
    (tmp_path/'runtime_images.json').write_text(json.dumps(value))
    # Agent capture must be complete before any Docker inspection is useful.
    agent=json.loads((tmp_path/'agent_image.json').read_text());agent['status']='unavailable'
    (tmp_path/'agent_image.json').write_text(json.dumps(agent))
    with pytest.raises(NativeContractError,match='record_incomplete'):
        receipts.capture(plan,tmp_path,run)


def test_prebuilt_main_manifest_binds_all49_images_and_source(plan,tmp_path):
    ids,_,run=fixture_records(plan,tmp_path)
    (tmp_path/'main_images.json').write_text(json.dumps({task:ids['main'] for task in plan['tasks']}))
    receipt=receipts.capture(plan,tmp_path,run)
    assert receipt['task_main_images_prebuilt']
    assert 'main_images.json' in receipt['upstream_records_sha256']
    for task,value in receipt['task_receipts'].items():
        assert value['image_ids']['main']==ids['main']
        assert value['main_build_context']['dockerfile_sha256']==plan['source_file_checks'][plan['tasks'][task]['main_dockerfile']]['sha256']
        assert 'AGENT_IMAGE=qiqcbench-agent:latest' in value['main_build_context']['build_argv']
    task=next(iter(plan['tasks']))
    assert receipts.task_runtime_seed(receipt,plan,task,{'Image':ids['main']},run)['build_receipt']['image_ids']['main']==ids['main']
    with pytest.raises(NativeContractError,match='differs_from_prebuilt'):
        receipts.task_runtime_seed(receipt,plan,task,{'Image':ids['agent']},run)


@pytest.mark.parametrize('mode',['missing_task','unknown_task','wrong_base'])
def test_prebuilt_main_rejects_partial_or_wrong_build(plan,tmp_path,mode):
    ids,inspected,run=fixture_records(plan,tmp_path)
    mapping={task:ids['main'] for task in plan['tasks']}
    if mode=='missing_task':mapping.pop(next(iter(mapping)))
    if mode=='unknown_task':mapping['unknown']=ids['main']
    if mode=='wrong_base':inspected[ids['main']]['RootFS']['Layers']=['wrong']
    (tmp_path/'main_images.json').write_text(json.dumps(mapping))
    with pytest.raises(NativeContractError,match='roster_invalid|base_mismatch'):
        receipts.capture(plan,tmp_path,run)


def test_prebuilt_main_rejects_mutated_agent_tag(plan,tmp_path):
    ids,_,run=fixture_records(plan,tmp_path)
    (tmp_path/'main_images.json').write_text(json.dumps({task:ids['main'] for task in plan['tasks']}))
    (tmp_path/'main_build_checks.json').write_text(json.dumps({'agent_image_id_before':ids['agent'],'agent_image_id_after':ids['qsim']}))
    with pytest.raises(NativeContractError,match='agent_tag_changed'):
        receipts.capture(plan,tmp_path,run)
