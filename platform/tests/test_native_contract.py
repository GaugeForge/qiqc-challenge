"""Original source metadata and hostile observed-evidence regression checks."""
import copy
import importlib.util
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0,str(Path(__file__).parents[1]/'scripts/science'))
import native_contract as nc
ROOT=Path(__file__).resolve().parents[3]
SOURCE=ROOT/'benchmark-assets/qiqcbench-restored'


@pytest.fixture(scope='module')
def plan():
    return nc.build_plan(SOURCE)


def test_original49_native_plan(plan):
    assert len(plan['tasks'])==49
    assert len(plan['verifiers'])==48
    assert len(plan['agent']['direct_packages'])==13
    assert len(plan['agent']['client_sha256'])==9
    assert plan['status']=='candidate-not-runtime-accepted-not-frozen'
    assert 'qiqcbench-runtime' not in json.dumps(plan)
    assert plan['qsim']['build_argv'][-1]=='.'


def test_published49_contracts_use_explicit_paper_roster():
    # The default fixture above intentionally retains the historical restoration
    # roster. Current candidate execution always supplies its paper source audit.
    published = nc.build_plan(SOURCE, ROOT / 'benchmark-assets/benchmark-releases/paper-source-audit-20261021.json')
    assert len(published['tasks']) == 49
    assert len(published['verifiers']) == 49
    assert 'time_budgeted_shadow_surrogate_60q' in published['tasks']
    for task in published['tasks'].values():
        assert not nc.validate_runtime(published, task['task_id'], observation(published, task),
                                       acceptance_scope='competition-native')


def test_all_verifier_context_hashes_match_original_builder(plan):
    # The original trusted build helper only defines static metadata functions
    # on import; no benchmark science is imported or executed by this test.
    spec=importlib.util.spec_from_file_location('upstream_build_metadata',SOURCE/'tools/build_qsim_images.py')
    module=importlib.util.module_from_spec(spec)
    exec(compile((SOURCE/'tools/build_qsim_images.py').read_bytes(),str(SOURCE/'tools/build_qsim_images.py'),'exec'),module.__dict__)
    for image,entry in plan['verifiers'].items():
        context=SOURCE/entry['context']['source_directory']
        assert module.verifier_content_digest(context,root=SOURCE)==entry['context']['content_digest']
    standalone=plan['verifiers'][plan['tasks']['time_budgeted_hamlearn_10q']['verifier_image']]
    assert standalone['context']['mode']=='manifest_allowlist'
    assert not standalone['derived_from_qsim']
    private=[f for f in standalone['context']['files'] if f['target'].startswith('private/')]
    assert len(private)==1 and private[0]['mode']=='0400'


def observation(plan, task):
    images={}
    for n,role in enumerate(['agent','main','qsim','verifier'],1):
        images[role]={'Id':'sha256:'+str(n)*64,'RootFS':{'Layers':['agent'] if role in ['agent','main'] else ['qsim']},'Config':{'Labels':{'org.opencontainers.image.revision':plan['source_commit']}}}
    containers={role:{'Image':images[role]['Id'],'HostConfig':{'Privileged':False},'Mounts':[]} for role in ['main','qsim','verifier']}
    for role,limits in task['role_resources'].items():
        containers[role]['HostConfig'].update(Memory=int((limits['memory_mb'] or 0)*1024*1024),
                                             NanoCpus=int((limits['cpus'] or 0)*1000000000))
    compose=nc.yaml.safe_load((SOURCE/task['compose_file']).read_text())
    for role in ['main','qsim']:
        env=nc._default_environment(compose['services'][role].get('environment'))
        containers[role]['Config']={'Env':[key+'='+value for key,value in env.items()]}
        containers[role]['Mounts']=[{'Type':m['type'],'Destination':m['target'],'RW':not m['read_only'],
             **({'Name':'trial_'+m['source']} if m['type']=='volume' else {'Source':'/source/'+m['source']})} for m in task[role+'_mounts']]
    return {'build_receipt':{'source_commit':plan['source_commit'],'source_files_sha256':plan['source_files_sha256'],
              'verifier_context_digest':plan['verifiers'][task['verifier_image']]['context']['content_digest'],
              'image_ids':{r:v['Id'] for r,v in images.items()}},'image_inspects':images,'container_inspects':containers,
            'main_probe':{'packages':plan['agent']['direct_packages'],'agent_lib':plan['agent']['client_sha256']},
            'compose_project':'trial','source_root':'/source','negative_reads':{p:1 for p in task['private_read_negative_paths']},
            'readonly_write_exit':1,'qsim_http_status':200,'resources':{'cpus':16,'memory_mb':32768}}


def test_valid_observation_and_missing_evidence_fail_closed(plan):
    task=plan['tasks']['tunable_coupler_cz_netzero']
    assert not nc.validate_runtime(plan,task['task_id'],observation(plan,task))
    assert nc.validate_runtime(plan,task['task_id'],{})


@pytest.mark.parametrize('key', ['QIQCBENCH_HIDDEN_COMMITMENT_SECRET', 'QIQCBENCH_EXECUTION_CONTEXT_ID',
                               'QIQCBENCH_SHADOW_EXPECTED_EXECUTION_CONTEXT_ID', 'QIQCBENCH_EVAL_VERIFIER_CONTEXT'])
def test_agent_cannot_receive_private_execution_binding(plan, key):
    task = plan['tasks']['tunable_coupler_cz_netzero']
    observed = observation(plan, task)
    observed['container_inspects']['main']['Config']['Env'].append(key + '=' + 'a' * 64)
    assert 'main_private_execution_binding_exposed' in nc.validate_runtime(plan, task['task_id'], observed)


@pytest.mark.parametrize('scope', ['practice-native', 'competition-native'])
def test_native_session_reaches_verifier_with_original_resource_checks(plan, scope):
    task = plan['tasks']['tunable_coupler_cz_netzero']
    observed = observation(plan, task)
    assert not nc.validate_runtime(plan, task['task_id'], observed, acceptance_scope=scope)
    observed['resources']['memory_mb'] = 1
    assert 'outer_sandbox_below_original_task_resources' in nc.validate_runtime(
        plan, task['task_id'], observed, acceptance_scope=scope)


@pytest.mark.parametrize('task_id', ['femoco_mps_qpe_resources', 'fermionic_laughlin_topo_entropy'])
def test_e2b_memory_overhead_tolerance_for_8gb_tasks(plan, task_id):
    # The same 8192 MB role limits remain exact; only the outer E2B capacity
    # check allows the host-reported overhead (7956 MB in production).
    task = plan['tasks'][task_id]
    value = observation(plan, task)
    value['resources'] = {'cpus': 4, 'memory_mb': 7956}
    errors = nc.validate_runtime(plan, task_id, value)
    assert 'outer_sandbox_below_original_task_resources' not in errors
    assert 'outer_sandbox_below_main_original_memory' not in errors
    assert 'outer_sandbox_below_verifier_original_memory' not in errors
    value['resources']['memory_mb'] = 7900
    assert 'outer_sandbox_below_original_task_resources' in nc.validate_runtime(plan, task_id, value)


@pytest.mark.parametrize('mutation,expected',[
    ('image','main_running_image_mismatch'),('mount','main_unapproved_mount'),
    ('private','main_private_read_negative_check_missing_or_failed'),
    ('resource','outer_sandbox_below_original_task_resources'),
    ('credential','main_backend_credential_exposed'),('package','main_scientific_dependencies_mismatch'),
    ('qsim_env','qsim_original_environment_mismatch'),('main_env','main_original_environment_mismatch'),
])
def test_hostile_or_insufficient_runtime_evidence(plan,mutation,expected):
    task=plan['tasks']['tunable_coupler_cz_netzero'];value=copy.deepcopy(observation(plan,task))
    if mutation=='image':value['container_inspects']['main']['Image']='sha256:'+'f'*64
    if mutation=='mount':value['container_inspects']['main']['Mounts'].append({'Destination':'/var/run/docker.sock'})
    if mutation=='private':value['negative_reads']['/app/configs']=0
    if mutation=='resource':value['resources']['memory_mb']=512
    if mutation=='credential':value['container_inspects']['main']['Config']={'Env':['E2B_API_KEY=fake-test-only']}
    if mutation=='package':value['main_probe']['packages']['numpy']='0.0.0'
    if mutation=='qsim_env':value['container_inspects']['qsim']['Config']['Env']=['QIQCBENCH_TASK_ID=another-task']
    if mutation=='main_env':value['container_inspects']['main']['Config']['Env']=[]
    assert expected in nc.validate_runtime(plan,task['task_id'],value)


def test_all49_original_environment_contracts(plan):
    for task in plan['tasks'].values():
        value=observation(plan,task)
        errors=nc.validate_runtime(plan,task['task_id'],value)
        assert 'qsim_original_environment_mismatch' not in errors
        assert 'main_original_environment_mismatch' not in errors


def test_source_read_rejects_changed_bytes(tmp_path):
    root=tmp_path/'source';root.mkdir();(root/'a').write_text('changed')
    audit=tmp_path/'audit.json';audit.write_text(json.dumps({'schema':'qiqc-upstream-restoration-audit/v1','source_commit':'a'*40,
        'files':{'a':{'git_mode':'100644','bytes':7,'sha256':'0'*64}}}))
    with pytest.raises(nc.NativeContractError,match='source_input_changed'):
        nc.Source(root,audit).read('a')


def test_original_large_verifier_and_qsim_are_not_hidden_by_main(plan):
    task=plan['tasks'][sorted(plan['tasks'])[16]]
    value=observation(plan,task);value['resources']['memory_mb']=7956
    errors=nc.validate_runtime(plan,task['task_id'],value)
    assert 'outer_sandbox_below_verifier_original_memory' in errors
    assert 'outer_sandbox_below_qsim_original_memory' in errors
    value['resources']['memory_mb']=15600
    assert not nc.validate_runtime(plan,task['task_id'],value)
    value['container_inspects']['verifier']['HostConfig']['Memory']=4096*1024*1024
    assert 'verifier_original_memory_limit_mismatch' in nc.validate_runtime(plan,task['task_id'],value)


def test_account_window_preserves_original_timeout_without_truncation(plan):
    budgets={i:nc.execution_budget(plan,key) for i,key in enumerate(sorted(plan['tasks']))}
    assert budgets[13]['original_verifier_seconds']==4200
    assert budgets[13]['sandbox_ttl_seconds']==5400
    assert budgets[13]['account_window_supported'] is False
    assert budgets[1]['sandbox_ttl_seconds']==3000
    assert budgets[1]['account_window_supported'] is True
    blocked=[i for i,b in budgets.items() if b['minimum_role_memory_mb']>7956 or not b['account_window_supported']]
    assert blocked==[5,13,14,15,16]


def test_old_frozen_plan_resource_enrichment_is_source_bound(plan):
    old=copy.deepcopy(plan)
    for task in old['tasks'].values():
        task.pop('role_resources')
    actual=nc.enrich_role_resources(old,SOURCE)
    assert {key:row['role_resources'] for key,row in actual['tasks'].items()} == {
        key:row['role_resources'] for key,row in plan['tasks'].items()}
    altered=copy.deepcopy(plan)
    task=next(iter(altered['tasks']))
    altered['source_file_checks'][f'harbor_tasks/{task}/task.toml']['sha256']='0'*64
    with pytest.raises(nc.NativeContractError,match='resource_contract_source_mismatch'):
        nc.enrich_role_resources(altered,SOURCE)


def test_bounded_scope_keeps_original_limits_and_failures_but_reports_capacity_separately(plan):
    task=plan['tasks']['ft_bell_513_neutral_atom'];value=observation(plan,task)
    value['resources']['memory_mb']=7956
    assert nc.validate_runtime(plan,task['task_id'],value)
    assert not nc.validate_runtime(plan,task['task_id'],value,acceptance_scope='bounded-functional')
    claims=nc.runtime_verification(plan,task['task_id'],value)
    assert claims['source_runtime_software_verified'] and claims['original_container_limits_verified']
    assert not claims['full_host_capacity_verified'] and not claims['source_runtime_parity_verified']
    value['container_inspects']['verifier']['HostConfig']['Memory']=4096*1024*1024
    assert 'verifier_original_memory_limit_mismatch' in nc.validate_runtime(plan,task['task_id'],value,acceptance_scope='bounded-functional')
    value['container_inspects']['qsim']['State']={'OOMKilled':True}
    assert 'qsim_oom_killed' in nc.validate_runtime(plan,task['task_id'],value,acceptance_scope='bounded-functional')
    assert not nc.runtime_verification(plan,task['task_id'],value)['source_runtime_software_verified']


def test_bounded_window_retains_original_verifier_declaration(plan):
    value=nc.execution_budget(plan,'femoco_mps_qpe_resources',acceptance_scope='bounded-functional')
    assert value['original_verifier_seconds']==4200
    assert value['sandbox_ttl_seconds']==3600 and value['runner_deadline_seconds']==3300
    assert value['account_window_supported'] and not value['full_verifier_window_supported']


def test_bounded_scope_still_requires_actual_host_resource_measurement(plan):
    task=plan['tasks']['tunable_coupler_cz_netzero'];value=observation(plan,task)
    for resources in ({},None,{'cpus':4,'memory_mb':float('nan')}):
        value['resources']=resources
        assert 'actual_host_resources_missing_or_invalid' in nc.validate_runtime(plan,task['task_id'],value,acceptance_scope='bounded-functional')
