"""Release creation gates use synthetic metadata, never science/cloud resources."""
import hashlib
import json
import sys
from pathlib import Path
import pytest
sys.path.insert(0,str(Path(__file__).parents[1]/'scripts/science'))
from native_release import create_release, prepare_activation
from native_release_record import load_release,validate_binding
from test_native_candidate import config,write_pair


def sha(data):return hashlib.sha256(data).hexdigest()


@pytest.fixture
def fixture(tmp_path):
    cfg,_=config(tmp_path)
    roster=[f'task_{index:02d}' for index in range(48)]+['tunable_coupler_cz_netzero']
    audit=json.loads(Path(cfg['source_audit']).read_text());audit['competition_task_ids']=roster
    Path(cfg['source_audit']).write_text(json.dumps(audit));cfg['source_audit_sha256']=sha(Path(cfg['source_audit']).read_bytes())
    image_ids={role:'sha256:'+sha(role.encode()) for role in ('agent','main','qsim','verifier')}
    plan={key:cfg[key] for key in ('source_commit','source_files_sha256')}
    plan.update(tasks={task:{'verifier_image':'verifier:fixture'} for task in roster},
                verifiers={'verifier:fixture':{'context':{'content_digest':'d'*64}}})
    receipt={key:cfg[key] for key in ('source_commit','source_files_sha256')}
    receipt.update(task_main_images_prebuilt=True,task_receipts={task:{**receipt,'image_ids':image_ids,'verifier_context_digest':'d'*64} for task in roster},
                   image_inspects_by_id={image_id:{'Id':image_id} for image_id in image_ids.values()})
    paths={}
    for name,value in [('build_plan',plan),('build_receipt',receipt)]:
        paths[name]=tmp_path/(name+'.json');paths[name].write_text(json.dumps(value));cfg[name+'_sha256']=sha(paths[name].read_bytes())
    evidence=tmp_path/'evidence';evidence.mkdir()
    checker=json.loads(Path(cfg['controller_manifest']).read_text())['host_files']['competition-platform/platform/scripts/science/verify_native_candidate_evidence.py']
    for i in range(49):
        browser_path,r2_path=write_pair(evidence,cfg,i)
        browser=json.loads(browser_path.read_text());browser['r2_archive_sha256']=sha(f'archive-{i}'.encode())
        browser_path.write_text(json.dumps(browser))
        r2=json.loads(r2_path.read_text());r2.update(archive_sha256=browser['r2_archive_sha256'],
           browser_report_sha256=sha(browser_path.read_bytes()),run_id=f'run-{i}',checker_sha256=checker,
           build_input_digests={k:cfg[k] for k in ('build_plan_sha256','build_receipt_sha256')})
        r2_path.write_text(json.dumps(r2))
    return cfg,audit,evidence,paths,tmp_path/'releases'


def freeze(fixture):
    cfg,audit,evidence,paths,root=fixture
    return create_release(cfg,audit,evidence,paths['build_plan'],paths['build_receipt'],root)


def test_all49_creates_genuine_frozen_record_without_changing_candidate(fixture):
    cfg,_,evidence,_,_=fixture
    before={p.name:sha(p.read_bytes()) for p in evidence.iterdir()}
    path=freeze(fixture);release=load_release(path)
    assert release['frozen'] is True
    assert release['release_id'].startswith('native-release-')
    assert len(release['payload']['tasks'])==49
    assert release['payload']['competition_production_ready'] is False
    assert cfg['frozen'] is False
    assert {p.name:sha(p.read_bytes()) for p in evidence.iterdir()}==before
    assert len(release['payload']['files'])==103


@pytest.mark.parametrize('mutation',['missing','mixed_controller','different_build','wrong_checker','duplicate_archive','build_changed'])
def test_no_release_from_incomplete_mixed_or_mutated_evidence(fixture,mutation):
    _,_,evidence,paths,root=fixture
    path=evidence/'task-07-r2.json';value=json.loads(path.read_text())
    if mutation=='missing':path.unlink()
    elif mutation=='build_changed':paths['build_receipt'].write_text('{}')
    else:
        if mutation=='mixed_controller':value['controller_sha256']='0'*64
        if mutation=='different_build':value['build_input_digests']['build_receipt_sha256']='0'*64
        if mutation=='wrong_checker':value['checker_sha256']='0'*64
        if mutation=='duplicate_archive':
            value['archive_sha256']=sha(b'archive-0')
            bp=evidence/'task-07.json';b=json.loads(bp.read_text());b['r2_archive_sha256']=value['archive_sha256'];bp.write_text(json.dumps(b))
            value['browser_report_sha256']=sha(bp.read_bytes())
        path.write_text(json.dumps(value))
    with pytest.raises(ValueError):freeze(fixture)
    assert not root.exists()


def test_release_readback_rejects_changed_evidence_or_record(fixture):
    path=freeze(fixture)
    target=path.parent/'evidence/00-r2.json';target.chmod(0o600);target.write_text('{}')
    with pytest.raises(ValueError,match='evidence_changed'):load_release(path)


def test_activation_is_only_prepared_and_rechecks_bindings(fixture,tmp_path):
    cfg,*_=fixture;path=freeze(fixture);pointer=tmp_path/'active-release.json'
    pointer.write_text('{"old":"unchanged"}')
    plan=prepare_activation(cfg,path,pointer)
    assert plan['platform_switched'] is False
    assert plan['expected_previous_pointer_sha256']==sha(pointer.read_bytes())
    assert pointer.read_text()=='{"old":"unchanged"}'
    bad={**cfg,'template_id':'another-template'}
    with pytest.raises(ValueError,match='runtime_binding'):prepare_activation(bad,path,pointer)


def test_bounded_release_records_real_capacity_gap_without_claiming_parity(fixture):
    cfg,_,evidence,_,_=fixture
    report=evidence/'task-16-r2.json';value=json.loads(report.read_text())
    value.update(acceptance_scope='bounded-five-minute',source_runtime_software_verified=True,
        original_container_limits_verified=True,full_host_capacity_verified=False,
        source_runtime_parity_verified=False,bounded_functional_errors=[])
    report.write_text(json.dumps(value))
    path=freeze(fixture);release=load_release(path)
    payload=release['payload'];row=payload['tasks']['task_16']
    assert payload['acceptance_scope']=='49-browser-bounded-five-minute'
    assert payload['full_host_capacity_verified'] is False
    assert payload['full_duration_performance_capacity_accepted'] is False
    assert row['source_runtime_parity_verified'] is False
    assert row['source_runtime_software_verified'] is True


@pytest.mark.parametrize('change',[
    {'source_runtime_software_verified':False},{'original_container_limits_verified':False},
    {'source_runtime_parity_verified':True},{'bounded_functional_errors':['qsim_oom_killed']},
    {'full_host_capacity_verified':None},
])
def test_bounded_freeze_does_not_excuse_other_runtime_errors(fixture,change):
    _,_,evidence,_,_=fixture;report=evidence/'task-16-r2.json';value=json.loads(report.read_text())
    value.update(acceptance_scope='bounded-five-minute',source_runtime_software_verified=True,
        original_container_limits_verified=True,full_host_capacity_verified=False,
        source_runtime_parity_verified=False,bounded_functional_errors=[])
    value.update(change);report.write_text(json.dumps(value))
    with pytest.raises(ValueError,match='freeze_requires_all49'):freeze(fixture)


@pytest.mark.parametrize('change',[
    {'qsim_successful_interaction_verified':False},{'qsim_progress_sha256':None},
    {'reward':True},{'reward':0.0},{'reward':2},{'agent_stop_reason':'infra_error'},
    {'agent_stop_reason':'acceptance_early_stop','acceptance_early_stop_verified':False},
    {'agent_stop_reason':'acceptance_early_stop','acceptance_early_stop_verified':True,'acceptance_readiness_sha256':None},
    {'budget_exhausted':False},{'acceptance_readiness_sha256':'f'*64},
])
def test_release_gate_requires_qsim_binary_reward_and_consistent_stop(fixture,change):
    _,_,root,_,_=fixture;path=root/'task-07-r2.json'
    value=json.loads(path.read_text());value.update(change);path.write_text(json.dumps(value))
    with pytest.raises(ValueError,match='freeze_requires_all49'):freeze(fixture)


def test_budget_release_keeps_exact_proof_and_readback_rejects_valid_but_different_claim(fixture):
    from native_release_record import canonical
    _,_,root,_,_=fixture;path=root/'task-07-r2.json'
    value=json.loads(path.read_text());value.update(reward=1)
    path.write_text(json.dumps(value));release_path=freeze(fixture)
    release=load_release(release_path);row=release['payload']['tasks']['task_07']
    assert row['reward']==1 and row['qsim_successful_interaction_verified'] is True
    # Simulate internally inconsistent trusted release assembly, even with a new
    # valid payload hash; attachment content hash still matches original bytes.
    row['qsim_progress_sha256']='f'*64
    release['release_id']='native-release-'+sha(canonical(release['payload']))
    release_path.chmod(0o600);release_path.write_bytes(canonical(release))
    with pytest.raises(ValueError,match='acceptance_attachment_mismatch'):load_release(release_path)


@pytest.mark.parametrize('change',[
    {'acceptance_scope':'bounded-functional'},
    {'agent_stop_reason':'completed','budget_exhausted':False,'execution_duration_class':'natural_completion_before_budget','agent_exit':0,'agent_elapsed_seconds':80},
    {'agent_stop_reason':'acceptance_early_stop','budget_exhausted':False,'acceptance_early_stop_verified':True,'acceptance_readiness_sha256':'e'*64},
    {'agent_elapsed_seconds':298.9},{'agent_elapsed_seconds':float('nan')},
    {'agent_elapsed_seconds':True},{'agent_exit':0},{'agent_budget_seconds':299},
    {'observation_policy':None},{'execution_duration_class':'natural_completion_at_budget'},
])
def test_five_minute_freeze_rejects_early_natural_or_unproven_duration(fixture,change):
    _,_,root,_,_=fixture;path=root/'task-07-r2.json'
    value=json.loads(path.read_text());value.update(change);path.write_text(json.dumps(value))
    with pytest.raises(ValueError,match='freeze_requires_all49'):freeze(fixture)


@pytest.mark.parametrize('change',[
    {'terminal_runtime_verified':False},{'terminal_runtime_sha256':None},
    {'harbor_agent_timing_verified':False},{'agent_execution_started_at':None},
    {'agent_execution_started_at':'2026-10-20T13:00:00'},
    {'agent_execution_finished_at':'2026-10-20T13:00:05+00:00'},
    {'agent_execution_finished_at':'2026-10-20T13:10:00+00:00'},
    {'agent_execution_started_at':'2026-10-20T13:00:00+08:00'},
])
def test_all49_freeze_requires_verified_real_utc_timing(fixture,change):
    _,_,root,_,_=fixture;path=root/'task-07-r2.json'
    value=json.loads(path.read_text());value.update(change);path.write_text(json.dumps(value))
    with pytest.raises(ValueError,match='freeze_requires_all49'):freeze(fixture)


def test_individually_passed_tasks_without_common_overlap_cannot_freeze(fixture):
    from native_freeze_gate import assess
    cfg,_,root,_,_=fixture;path=root/'task-07-r2.json'
    value=json.loads(path.read_text());value.update(agent_execution_started_at='2026-10-20T13:05:00.200000+00:00',
        agent_execution_finished_at='2026-10-20T13:10:00.400000+00:00')
    path.write_text(json.dumps(value));gate=assess(cfg,root)
    assert gate['passed_tasks']==49 and gate['status']=='blocked'
    assert gate['concurrency_errors']==['all49_agent_execution_did_not_overlap']
    with pytest.raises(ValueError,match='freeze_requires_all49'):freeze(fixture)


def test_release_readback_recomputes_overlap_instead_of_trusting_claim(fixture):
    from native_release_record import canonical
    path=freeze(fixture);release=load_release(path)
    assert release['payload']['concurrency_evidence']['actual_concurrent_tasks']==49
    assert release['payload']['concurrency_evidence']['overlap_seconds']==300.2
    release['payload']['concurrency_evidence']['overlap_seconds']=999
    release['release_id']='native-release-'+sha(canonical(release['payload']))
    path.chmod(0o600);path.write_bytes(canonical(release))
    with pytest.raises(ValueError,match='concurrency_claim_invalid'):load_release(path)
