"""Native candidate contract tests. No Docker, E2B or scientific execution."""
import asyncio
import hashlib
import io
import json
import zipfile
from pathlib import Path
from types import SimpleNamespace
import pytest
from native_compose_contract import validate_spec,submission_manifest,validate_main_container
from native_compose_template import archive_bytes
from native_compose_runner import make_trial_config,read_scoped_token,trial_result_paths,main_oom_after_completion_allowed


def spec(tmp_path):
    return {'schema':'qiqc-native-compose-run/v1','source_root':'/opt/qiqcbench',
            'task_id':'tunable_coupler_cz_netzero','run_id':'native_test','attempt':1,
            'submission_zip':str(tmp_path/'harness.zip'),'submission_sha256':'a'*64,
            'output_dir':str(tmp_path/'out'),'agent_budget_seconds':300,
            'model_gateway_url':'http://host.docker.internal:8181/v1',
            'scoped_token_file':str(tmp_path/'token'),'compose_overlays':[],
            'candidate_identity':{'source_files_sha256':'b'*64}}


def test_original_harbor_config_keeps_task_verifier_and_mcp(tmp_path):
    value=validate_spec(spec(tmp_path));cfg=make_trial_config(value,tmp_path)
    assert cfg.task.path==Path('/opt/qiqcbench/harbor_tasks/tunable_coupler_cz_netzero')
    assert cfg.agent.import_path=='native_compose_agent:NativeSubmittedHarness'
    assert cfg.agent.override_timeout_sec==330
    assert cfg.verifier.override_timeout_sec is None
    assert cfg.extra_instructions==[]
    assert 'token' not in cfg.model_dump_json()


@pytest.mark.parametrize('change',[
 lambda s:s.update(run_id='../../host'), lambda s:s.update(agent_budget_seconds=299),
 lambda s:s.update(model_gateway_url='http://evil.invalid:8181/v1'),
 lambda s:s.update(model_gateway_url='http://token@host.docker.internal:8181/v1'),
 lambda s:s.update(source_root='/opt/../root'),lambda s:s.update(unknown=True),
])
def test_spec_rejects_host_and_endpoint_confusion(tmp_path,change):
    value=spec(tmp_path);change(value)
    with pytest.raises(ValueError):validate_spec(value)


def test_native_zip_is_deterministic_and_required(tmp_path):
    data=archive_bytes();assert data==archive_bytes()
    from qiqc.archive import inspect_archive
    assert inspect_archive(data)['manifest']['entrypoint']=='adapter.sh'
    path=tmp_path/'zip';path.write_bytes(data)
    manifest=submission_manifest(path,hashlib.sha256(data).hexdigest())
    assert manifest['task_transport']=='native-http/v1'
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        script=z.read('adapter.sh').decode()
        assert 'codex exec ' in script and 'qsim_call' not in script
    with pytest.raises(ValueError,match='digest'):submission_manifest(path,'f'*64)
    out=io.BytesIO()
    with zipfile.ZipFile(out,'w') as z:
        z.writestr('harness.json',json.dumps({**manifest,'runtime_kind':'http_bridge'}))
        z.writestr('adapter.sh','echo legacy')
    path.write_bytes(out.getvalue())
    with pytest.raises(ValueError,match='native_submission'):submission_manifest(path,hashlib.sha256(out.getvalue()).hexdigest())


def test_sustained_submission_has_distinct_reviewable_identity(tmp_path):
    data=archive_bytes(sustained=True)
    assert data==archive_bytes(sustained=True) and data!=archive_bytes()
    from qiqc.archive import inspect_archive
    parsed=inspect_archive(data)['manifest']
    path=tmp_path/'sustained.zip';path.write_bytes(data)
    accepted=submission_manifest(path,hashlib.sha256(data).hexdigest())
    assert all(accepted[key]==value for key,value in parsed.items())
    assert accepted['version']=='native-compose-sustained-acceptance/2'


def test_custom_adapter_manifest_preserves_uploaded_nested_entrypoint(tmp_path):
    manifest={'schema':'qiqc-harness/v1-demo','name':'custom scientific harness',
              'adapter':'custom-chat','entrypoint':'scripts/start.sh',
              'runtime_kind':'native_cli','task_transport':'native-http/v1',
              'model_interfaces':['chat/completions']}
    buffer=io.BytesIO()
    with zipfile.ZipFile(buffer,'w') as archive:
        archive.writestr('harness.json',json.dumps(manifest))
        archive.writestr('scripts/start.sh','#!/bin/sh\nexit 0\n')
    path=tmp_path/'custom.zip';path.write_bytes(buffer.getvalue())
    assert submission_manifest(path,hashlib.sha256(buffer.getvalue()).hexdigest())==manifest


@pytest.mark.parametrize('interfaces', [[], 'responses', ['unapproved-endpoint']])
def test_custom_adapter_cannot_declare_invalid_model_interface(tmp_path,interfaces):
    manifest={'schema':'qiqc-harness/v1-demo','adapter':'custom-chat','entrypoint':'run.sh',
              'runtime_kind':'native_cli','task_transport':'native-http/v1','model_interfaces':interfaces}
    buffer=io.BytesIO()
    with zipfile.ZipFile(buffer,'w') as archive:
        archive.writestr('harness.json',json.dumps(manifest));archive.writestr('run.sh','exit 0')
    path=tmp_path/'bad.zip';path.write_bytes(buffer.getvalue())
    with pytest.raises(ValueError,match='unsupported_model_interface'):
        submission_manifest(path,hashlib.sha256(buffer.getvalue()).hexdigest())


@pytest.mark.parametrize('member',['../escape','/root/secret','x\\evil'])
def test_archive_paths_never_extract_on_controller(tmp_path,member):
    out=io.BytesIO()
    with zipfile.ZipFile(out,'w') as z:z.writestr(member,'x')
    path=tmp_path/'zip';path.write_bytes(out.getvalue())
    with pytest.raises(ValueError,match='unsafe'):submission_manifest(path,hashlib.sha256(out.getvalue()).hexdigest())


@pytest.mark.parametrize('mutation',[
 lambda x:x['HostConfig'].update(Privileged=True),
 lambda x:x['HostConfig'].update(NetworkMode='host'),
 lambda x:x['HostConfig'].update(CapAdd=['SYS_ADMIN']),
 lambda x:x['Mounts'].append({'Source':'/var/run/docker.sock','Destination':'/socket'}),
 lambda x:x['Mounts'].append({'Source':'/a','Destination':'/qsim_logs'}),
 lambda x:x['Mounts'].append({'Source':'/a','Destination':'/qsim_logs/public_job_results','RW':True}),
])
def test_docker_structure_does_not_imply_permission_to_run(mutation):
    item={'HostConfig':{},'Mounts':[]};mutation(item)
    with pytest.raises(ValueError):validate_main_container(item)


def test_scoped_token_requires_private_regular_file(tmp_path):
    path=tmp_path/'token';path.write_text('qr_'+'a'*32);path.chmod(0o600)
    assert read_scoped_token(path).startswith('qr_')
    path.chmod(0o644)
    with pytest.raises(ValueError):read_scoped_token(path)


@pytest.mark.parametrize('selected_model',['tencent/hy4-preview','minimax/minimax-m3.1-flash:free','approved-provider/scientific-model'])
@pytest.mark.parametrize('exit_code,elapsed,expected',[(0,1,'completed'),(124,300,'budget_exhausted'),(1,1,'adapter_failure'),(124,1,'adapter_failure')])
def test_agent_runs_uploaded_entry_and_real_cutoff(tmp_path,monkeypatch,exit_code,elapsed,expected,selected_model):
    import native_compose_agent as agent
    from harbor.models.task.config import MCPServerConfig
    path=tmp_path/'harness.zip';data=archive_bytes();path.write_bytes(data)
    run={**spec(tmp_path),'control_dir':tmp_path,'submission_sha256':hashlib.sha256(data).hexdigest(),'scoped_token':'qr_'+'x'*32}
    run['candidate_identity'].update(model_id=selected_model,diagnostic_model='' if selected_model=='tencent/hy4-preview' else selected_model)
    if selected_model=='approved-provider/scientific-model':
        run['candidate_identity'].update(diagnostic_model='',provider_profile=approved_profile(selected_model))
    agent.RUNS['native_test']=run
    calls=[];uploads={}
    class FakeEnvironment:
        native_security_ready=True
        native_security_receipt={'verified':'fixture'}
        async def upload_file(self,source,target):uploads[target]=Path(source).read_bytes()
        async def exec(self,command,**kwargs):
            calls.append((command,kwargs))
            return SimpleNamespace(return_code=exit_code if command.startswith('timeout ') else 0,
                                   stdout='codex-cli 0.144.4\n' if command=='codex --version' else '')
    harness=agent.NativeSubmittedHarness(logs_dir=tmp_path,run_handle='native_test',
            mcp_servers=[MCPServerConfig(name='qsim',transport='streamable-http',url='http://qsim:8123/mcp')])
    ticks=iter([10,10+elapsed])
    # Keep the asyncio event loop's own clock intact.
    monkeypatch.setattr(agent,'time',SimpleNamespace(monotonic=lambda:next(ticks)))
    context=SimpleNamespace(metadata={})
    async def run():
        await harness.setup(FakeEnvironment())
        await harness.run('ORIGINAL TASK',FakeEnvironment(),context)
    try:
        if expected=='adapter_failure':
            with pytest.raises(RuntimeError,match='adapter_failed'):asyncio.run(run())
        else:asyncio.run(run())
        assert context.metadata['agent_stop_reason']==expected
        assert uploads['/tmp/qiqc-native-task.txt']==b'ORIGINAL TASK'
        assert uploads['/tmp/qiqc-native-harness.zip']==data
        assert b'http://qsim:8123/mcp' in uploads['/tmp/qiqc-native-codex/config.toml']
        import tomllib
        config=tomllib.loads(uploads['/tmp/qiqc-native-codex/config.toml'].decode())
        provider=config['model_providers']['qiqc']
        assert provider['base_url']=='http://host.docker.internal:8181/v1'
        assert provider['env_key']=='QIQC_MODEL_TOKEN' and provider['wire_api']=='responses'
        assert provider['namespace_tools'] is False
        assert config['web_search']=='disabled'
        assert config['model']==selected_model
        assert calls[-1][1]['env']['QIQC_MODEL']==selected_model
        assert context.metadata['model_id']==selected_model
        assert agent.RUNS['native_test']['scoped_token'] not in uploads['/tmp/qiqc-native-codex/config.toml'].decode()
        assert calls[-1][1]['env']['CODEX_HOME']=='/tmp/qiqc-native-codex'
        assert calls[-1][1]['env']['QIQC_MODEL_TOKEN']==agent.RUNS['native_test']['scoped_token']
        assert not any(word in command for command,_ in calls for word in ('npm ', 'pip ', 'curl ', 'wget '))
        assert 'sh /opt/qiqc-submission/adapter.sh' in calls[-1][0]
        assert 'qsim_call' not in calls[-1][0]
    finally:
        agent.RUNS.pop('native_test',None)
        from native_compose_environment import EVIDENCE
        EVIDENCE.pop('native_test',None)


def test_main_oom_after_completed_verified_run_is_not_infra_failure():
    metadata={'agent_exit':0,'agent_stop_reason':'completed'}
    classification={'infrastructure_ok':True,'verifier_exit':0}
    evidence={'main_stopped_before_verifier':True}
    assert main_oom_after_completion_allowed(
        ['main_terminal_oom_or_missing'], metadata, classification, 1, evidence, True)
    assert not main_oom_after_completion_allowed(
        ['main_terminal_oom_or_missing'], {**metadata,'agent_exit':137}, classification, 1, evidence, True)
    assert not main_oom_after_completion_allowed(
        ['main_terminal_oom_or_missing','verifier_terminal_oom_or_missing'],
        metadata, classification, 1, evidence, True)
    assert not main_oom_after_completion_allowed(
        ['main_terminal_oom_or_missing'], metadata,
        {**classification,'infrastructure_ok':False}, 1, evidence, True)
    assert not main_oom_after_completion_allowed(
        ['main_terminal_oom_or_missing'], metadata, classification, 1,
        {'main_stopped_before_verifier':False}, True)


def test_uploaded_harness_extraction_root_is_traversable_by_unprivileged_worker():
    import native_compose_agent as agent
    # The submitted Python worker drops to UID 65534. Its adapter directory
    # must remain traversable; a 0700 extraction root makes the worker reject
    # its own public path ancestor before any task work starts.
    assert "root.mkdir(mode=0o755,exist_ok=True)" in agent.EXTRACT
    assert "root.chmod(0o755)" in agent.EXTRACT


def test_dsh_stream_idle_default_is_raised_for_uploaded_runtime(tmp_path,monkeypatch):
    import native_compose_agent as agent
    target=tmp_path/'node_modules/@deepseek-ai/dsh-llm-pi-ai/lib/index.js'
    target.parent.mkdir(parents=True)
    target.write_text(
        "const DEFAULT_STREAM_IDLE_TIMEOUT_MS = 3e5;\n"
        "const streamIdleTimeoutMs = source.streamIdleTimeoutMs ?? 3e5;\n"
    )
    monkeypatch.setenv('QIQC_RUNTIME_ROOT',str(tmp_path))
    monkeypatch.setenv('QIQC_DSH_STREAM_IDLE_MS',str(agent.DSH_STREAM_IDLE_TIMEOUT_MS))
    exec(agent.DSH_IDLE_PATCH,{})
    text=target.read_text()
    assert agent.DSH_STREAM_IDLE_TIMEOUT_MS == 1800000
    assert 'DEFAULT_STREAM_IDLE_TIMEOUT_MS = 1800000' in text
    assert 'streamIdleTimeoutMs ?? 1800000' in text
    assert '= 3e5' not in text
    exec(agent.DSH_IDLE_PATCH,{})
    assert target.read_text()==text


def test_native_runtime_never_builds_or_pulls(monkeypatch):
    from native_compose_environment import NativeDockerEnvironment,DockerEnvironment
    calls=[]
    async def fake(self,command,*args,**kwargs):calls.append(command);return 'fixture'
    monkeypatch.setattr(DockerEnvironment,'_run_docker_compose_command',fake)
    env=object.__new__(NativeDockerEnvironment)
    async def run():
        for command in (['build'],['pull','main']):
            with pytest.raises(RuntimeError,match='forbidden'):
                await env._run_docker_compose_command(command)
        assert await env._run_docker_compose_command(['up','--wait'])=='fixture'
        await env._run_docker_compose_command(['exec','main','true'])
        with pytest.raises(RuntimeError,match='force_build'):
            await env.start(True)
    asyncio.run(run())
    assert calls==[['up','--no-build','--pull','never','--wait'],['exec','main','true']]


@pytest.mark.parametrize('role',['agent','verifier'])
def test_native_prebuilt_binding_keeps_original_task_config(tmp_path,monkeypatch,role):
    import yaml
    import native_compose_observation as observation
    from native_compose_environment import NativeDockerEnvironment,DockerEnvironment
    from harbor.models.task.config import EnvironmentConfig
    task='fixture'; ids={name:'sha256:'+digit*64 for name,digit in [('main','1'),('qsim','2'),('verifier','3')]}
    monkeypatch.setattr(observation,'load_inputs',lambda identity:({}, {'task_receipts':{task:{'image_ids':ids}}}))
    def fake_init(self,*args,**kwargs):
        self.task_env_config=kwargs['task_env_config'];self.session_id='native_test'
        self.extra_docker_compose_paths=[]
        self._mounts=[{'target':'/logs/verifier'},{'target':'/logs/agent'}]
    monkeypatch.setattr(DockerEnvironment,'__init__',fake_init)
    original=EnvironmentConfig(docker_image='original-tag')
    agentdir=tmp_path/'harbor_tasks'/task/'environment'
    env=NativeDockerEnvironment(environment_dir=agentdir if role=='agent' else agentdir.parent/'tests',
        trial_paths=SimpleNamespace(trial_dir=tmp_path/'trial'),task_env_config=original,
        native_agent_environment_dir=agentdir,native_control_dir=tmp_path,native_run_handle='native_test',
        native_candidate_identity={},native_task_id=task)
    assert original.docker_image=='original-tag'
    assert env.task_env_config.docker_image==ids['main' if role=='agent' else 'verifier']
    overlay=yaml.safe_load(env.extra_docker_compose_paths[0].read_text())
    assert overlay['services']['main']['image']==env.task_env_config.docker_image
    assert overlay['services']['main']['pull_policy']=='never'
    if role=='agent':
        assert overlay['services']['qsim']['image']==ids['qsim']
        assert env._mounts==[{'target':'/logs/agent'}]
        assert 'build' not in overlay['services']['main']
    else:assert overlay['services']['main']['network_mode']=='none'


def test_shadow_bootstrap_binding_is_private_per_run_and_role():
    from native_compose_environment import bind_private_execution, EXECUTION_BINDINGS
    from native_security import compose_overlay, verifier_overlay
    task = 'time_budgeted_shadow_surrogate_60q'
    main, verifier, other = compose_overlay('binding_a'), verifier_overlay(), compose_overlay('binding_b')
    try:
        bind_private_execution(main, task, 'agent', 'binding_a')
        bind_private_execution(verifier, task, 'verifier', 'binding_a')
        bind_private_execution(other, task, 'agent', 'binding_b')
        qsim = main['services']['qsim']['environment']
        grader = verifier['services']['main']['environment']
        assert qsim['QIQCBENCH_EXECUTION_CONTEXT_ID'] == grader['QIQCBENCH_SHADOW_EXPECTED_EXECUTION_CONTEXT_ID']
        assert qsim['QIQCBENCH_HIDDEN_COMMITMENT_SECRET'] == grader['QIQCBENCH_HIDDEN_COMMITMENT_SECRET']
        assert len(bytes.fromhex(qsim['QIQCBENCH_HIDDEN_COMMITMENT_SECRET'])) == 32
        assert other['services']['qsim']['environment'] != qsim
        assert not any('CONTEXT' in key or 'COMMITMENT' in key for key in main['services']['main'].get('environment', {}))
        untouched = compose_overlay('unbound')
        before = json.dumps(untouched, sort_keys=True)
        bind_private_execution(untouched, 'other_task', 'agent', 'unbound')
        assert json.dumps(untouched, sort_keys=True) == before
        assert 'unbound' not in EXECUTION_BINDINGS
    finally:
        EXECUTION_BINDINGS.pop('binding_a', None)
        EXECUTION_BINDINGS.pop('binding_b', None)


def test_verifier_refuses_to_start_before_main_stops():
    from native_compose_environment import NativeDockerEnvironment
    env=object.__new__(NativeDockerEnvironment)
    env.native_role='verifier';env.native_run_handle='absent'
    with pytest.raises(RuntimeError,match='not_stopped'):
        asyncio.run(env.start(False))


@pytest.mark.parametrize('exit_code,reward,report,expected',[(0,0,False,True),(1,0,False,False),
    (1,0,True,True),(1,1,True,False),(2,0,True,False)])
def test_original_verifier_score_report_classification(tmp_path,exit_code,reward,report,expected):
    from native_compose_runner import classify_verifier
    verifier=tmp_path/'verifier';verifier.mkdir()
    (verifier/'test-stdout.txt').write_text('scientific verdict produced\n')
    if report:
        target=verifier/'artifacts';target.mkdir()
        (target/'score_report.json').write_text('{"reward":0}')
    assert classify_verifier(tmp_path,exit_code,reward)['infrastructure_ok']==expected


@pytest.mark.parametrize('running',[True,False])
def test_stop_marker_requires_real_container_stop(tmp_path,monkeypatch,running):
    import native_compose_environment as module
    import native_compose_observation as observation
    env=object.__new__(module.NativeDockerEnvironment)
    env.native_role='agent';env.native_run_handle='stop-test';env.native_trial_root=str(tmp_path/'trial')
    execution={'run_id':'stop-test','attempt':1,'submission_sha256':'a'*64,'candidate_identity':{'controller_sha256':'b'*64},
               'agent_budget_seconds':300,'agent_exit':124,'elapsed_seconds':300.2,'budget_exhausted':True,'agent_stop_reason':'budget_exhausted'}
    module.EVIDENCE['stop-test']={'agent_execution_stop':execution,'runtime_observation':{'container_inspects':{'main':{'Id':'c'*64}}}}
    async def stop(self,service):pass
    monkeypatch.setattr(module.DockerEnvironment,'stop_service',stop)
    monkeypatch.setattr(observation,'container_inspect',lambda cid:{'Id':cid,'State':{'Running':running}})
    try:
        if running:
            with pytest.raises(RuntimeError,match='stop_not_confirmed'):asyncio.run(env.stop_service('main'))
            assert not (tmp_path/'agent-stop.json').exists()
        else:
            asyncio.run(env.stop_service('main'))
            marker=json.loads((tmp_path/'agent-stop.json').read_text())
            assert marker['schema']=='qiqc-native-agent-stop/v1' and marker['main_stopped'] is True
            assert marker['attempt']==1 and marker['candidate_identity']==execution['candidate_identity']
            assert (tmp_path/'agent-stop.json').stat().st_mode&0o077==0
    finally:module.EVIDENCE.pop('stop-test',None)


@pytest.mark.parametrize('agent_budget',[180,300])
@pytest.mark.parametrize('diagnostic_model',['','minimax/minimax-m3.1-flash:free'])
@pytest.mark.parametrize('exit_code,elapsed,duration',[(124,300.2,'budget_300s_observed'),(0,24.7,'natural_completion_before_budget')])
def test_readiness_cannot_stop_actual_observation(tmp_path,monkeypatch,exit_code,elapsed,duration,diagnostic_model,agent_budget):
    import native_compose_agent as agent
    import native_acceptance_stop as acceptance
    from native_compose_environment import EVIDENCE
    run={**spec(tmp_path),'control_dir':tmp_path,'scoped_token':'qr_'+'x'*32}
    from native_candidate import identity
    config={'candidate_id':'native-candidate-'+('b'*64),'source_files_sha256':'b'*64,
            'source_audit_sha256':'c'*64,'source_commit':'d'*40,'template_id':'fixture-native',
            'controller_sha256':'e'*64,'build_plan_sha256':'f'*64,'build_receipt_sha256':'1'*64}
    config.update(diagnostic_model=diagnostic_model,diagnostic_rpm=20,diagnostic_daily_cap=50,agent_budget_seconds=agent_budget)
    run['agent_budget_seconds']=agent_budget
    # This test bypasses setup, but run must still validate real uploaded bytes.
    uploaded = archive_bytes(agent_budget_seconds=agent_budget)
    Path(run['submission_zip']).write_bytes(uploaded)
    run['submission_sha256'] = hashlib.sha256(uploaded).hexdigest()
    if exit_code==124:
        elapsed=agent_budget+0.2
        duration=f'budget_{agent_budget}s_observed'
    run['candidate_identity']=identity(config,48)
    assert run['candidate_identity']['acceptance_scope']==('bounded-three-minute' if agent_budget==180 else 'bounded-five-minute')
    validated=validate_spec({key:value for key,value in run.items() if key not in ('control_dir','scoped_token')})
    cfg=make_trial_config(validated,tmp_path)
    assert cfg.environment.kwargs['native_candidate_identity']==run['candidate_identity']
    assert cfg.agent.model_name==(diagnostic_model or 'tencent/hy4-preview')
    agent.RUNS['native_test']=run
    stopped=asyncio.Event();calls=[]
    class Environment:
        native_security_receipt={}
        async def upload_file(self,*args):pass
        async def exec(self,command,**kwargs):
            if command.startswith('timeout '):
                assert command.startswith(f'timeout --signal=TERM --kill-after=5s {agent_budget} ' )
                assert kwargs['timeout_sec']==agent_budget+20
                calls.append('uploaded_harness_started')
                await stopped.wait();calls.append('actual_exit_received')
                return SimpleNamespace(return_code=exit_code)
            return SimpleNamespace(return_code=0)
        async def stop_service(self,service):raise AssertionError('readiness must not stop main')
    def observe(*args):
        calls.append('positive_qsim_observed')
        return {'success_count':1}
    def forbidden(*args):raise AssertionError('do not read proactive stop request')
    monkeypatch.setattr(acceptance,'observe_qsim',observe)
    monkeypatch.setattr(acceptance,'accepted_request',forbidden)
    ticks=iter([10,10+elapsed]);monkeypatch.setattr(agent,'time',SimpleNamespace(monotonic=lambda:next(ticks)))
    harness=agent.NativeSubmittedHarness(logs_dir=tmp_path,run_handle='native_test',mcp_servers=[])
    harness.runtime_env = {}  # Codex setup state; this fixture deliberately skips setup.
    context=SimpleNamespace(metadata={})
    async def scenario():
        task=asyncio.create_task(harness.run('UNCHANGED TASK',Environment(),context))
        await asyncio.sleep(2.2)
        assert not task.done()
        stopped.set()
        await task
    try:
        asyncio.run(scenario())
        assert context.metadata['agent_exit']==exit_code
        assert context.metadata['execution_duration_class']==duration
        assert context.metadata['budget_exhausted'] is (exit_code==124)
        assert context.metadata['observation_policy']==f'observe-{agent_budget}s-no-readiness-stop/v1'
        assert 'acceptance_early_stop_verified' not in context.metadata
        assert calls==['uploaded_harness_started','positive_qsim_observed','actual_exit_received']
        assert EVIDENCE['native_test']['agent_execution_stop']['execution_duration_class']==duration
    finally:
        agent.RUNS.pop('native_test',None);EVIDENCE.pop('native_test',None)


def test_verifier_original_no_network_constructs_without_egress_probe(tmp_path, monkeypatch):
    """Exercise actual Harbor 0.23 constructor, which failed in real tasks13/14."""
    import yaml
    import native_compose_observation as observation
    from native_compose_environment import NativeDockerEnvironment, DockerEnvironment
    from harbor.models.task.config import EnvironmentConfig, NetworkPolicy, NetworkMode
    from harbor.models.trial.paths import TrialPaths
    ids={name:'sha256:'+digit*64 for name,digit in [('main','1'),('qsim','2'),('verifier','3')]}
    monkeypatch.setattr(observation,'load_inputs',lambda identity:({}, {'task_receipts':{'fixture':{'image_ids':ids}}}))
    def forbidden_probe(*args, **kwargs):
        pytest.fail('isolated verifier must not probe/pull Harbor sidecar images')
    monkeypatch.setattr(DockerEnvironment,'_egress_control_kernel_support',forbidden_probe)
    agentdir=tmp_path/'harbor_tasks'/'fixture'/'environment'
    verifierdir=agentdir.parent/'tests';verifierdir.mkdir(parents=True)
    policy=NetworkPolicy(network_mode=NetworkMode.NO_NETWORK)
    env=NativeDockerEnvironment(environment_dir=verifierdir,environment_name='fixture',session_id='fixture-verifier',
        trial_paths=TrialPaths(trial_dir=tmp_path/'trial'),task_env_config=EnvironmentConfig(),
        network_policy=policy,phase_network_policies=[policy],native_agent_environment_dir=agentdir,
        native_control_dir=tmp_path,native_run_handle='fixture',native_candidate_identity={},native_task_id='fixture')
    assert env.network_policy==policy
    assert env._enable_egress_control is False
    assert yaml.safe_load(env.extra_docker_compose_paths[-1].read_text())['services']['main']['network_mode']=='none'
    asyncio.run(env.set_network_policy(policy))  # Harbor's real same-phase call
    with pytest.raises(ValueError):
        asyncio.run(env.set_network_policy(NetworkPolicy(network_mode=NetworkMode.PUBLIC)))


def test_verifier_does_not_claim_allowlist_or_main_no_network_support():
    from native_compose_environment import NativeDockerEnvironment
    from harbor.models.task.config import NetworkPolicy, NetworkMode
    env=object.__new__(NativeDockerEnvironment)
    env.native_role='verifier';env._is_windows_container=False;env._enable_egress_control=False
    policy=NetworkPolicy(network_mode=NetworkMode.ALLOWLIST,allowed_hosts=['example.com'])
    with pytest.raises(ValueError,match='unsupported'):
        env._requires_egress_control(startup_network_policy=policy,phase_network_policies=[])
    with pytest.raises(ValueError):env.validate_network_policy_support(policy)
    env.native_role='agent'
    with pytest.raises(ValueError):env.validate_network_policy_support(NetworkPolicy(network_mode=NetworkMode.NO_NETWORK))


def test_verifier_actual_network_readback_still_rejects_networked_container(monkeypatch):
    import native_compose_environment as module
    env=object.__new__(module.NativeDockerEnvironment)
    env.native_role='verifier';env.native_run_handle='network-readback-test'
    env.native_image_ids={'verifier':'sha256:'+'a'*64}
    module.EVIDENCE[env.native_run_handle]={'main_stopped_before_verifier':True}
    async def no_start(self,force_build):pass
    async def cid(service):return 'b'*64
    monkeypatch.setattr(module.DockerEnvironment,'start',no_start)
    env._container_id=cid
    monkeypatch.setattr(module,'validate_main_container',lambda observation:{})
    monkeypatch.setattr(module.subprocess,'run',lambda *args,**kwargs:SimpleNamespace(stdout=json.dumps([
        {'Image':env.native_image_ids['verifier'],'HostConfig':{'NetworkMode':'bridge'}}])))
    try:
        with pytest.raises(RuntimeError,match='network_not_disabled'):asyncio.run(env.start(False))
    finally:module.EVIDENCE.pop(env.native_run_handle,None)


@pytest.mark.parametrize('model',['tencent/hy4-preview','minimax/minimax-m3.1-flash:free'])
def test_explicit_model_identity_reaches_harbor_trial_without_budget_change(tmp_path,model):
    value=spec(tmp_path)
    value['candidate_identity'].update(model_id=model,diagnostic_model='' if model=='tencent/hy4-preview' else model)
    cfg=make_trial_config(validate_spec(value),tmp_path)
    assert cfg.agent.model_name==model
    assert cfg.agent.override_timeout_sec==330
    assert cfg.verifier.override_timeout_sec is None
    assert value['agent_budget_seconds']==300


@pytest.mark.parametrize('identity',[
    {'model_id':'minimax/minimax-m3.1-flash:free'},
    {'diagnostic_model':'minimax/minimax-m3.1-flash:free'},
    {'model_id':'tencent/hy4-preview','diagnostic_model':'minimax/minimax-m3.1-flash:free'},
    {'model_id':'minimax/minimax-m3.1-flash:free','diagnostic_model':''},
    {'model_id':'other/model','diagnostic_model':'other/model'},
    {'model_id':['tencent/hy4-preview'],'diagnostic_model':''},
    {'model_id':'tencent/hy4-preview','diagnostic_model':None},
])
def test_model_identity_cannot_silently_fallback(tmp_path,identity):
    value=spec(tmp_path);value['candidate_identity'].update(identity)
    with pytest.raises(ValueError,match='model'):validate_spec(value)


def test_legacy_identity_still_means_hy4(tmp_path):
    assert make_trial_config(validate_spec(spec(tmp_path)),tmp_path).agent.model_name=='tencent/hy4-preview'


def approved_profile(model='approved-provider/scientific-model',endpoints=None):
    from native_provider_profile import profile_identity
    return profile_identity({'schema':'qiqc-provider-profile/v1','id':'fixture-approved','model':model,
        'base_url':'https://provider.example/v1','endpoints':endpoints or ['responses'],'key_file':'/private/operator-key',
        'quota_group':'fixture','requests_per_minute':20,'requests_per_day':50,'allow_paid_fallback':False,
        'authentication':'bearer','parameter_policy':'passthrough/v1','billing':'paid'})


def test_operator_profile_identity_model_and_endpoint_bind_runner(tmp_path):
    from native_candidate import identity
    value=spec(tmp_path);profile=approved_profile()
    config={'candidate_id':'native-candidate-'+('b'*64),'source_files_sha256':'b'*64,
            'source_audit_sha256':'c'*64,'source_commit':'d'*40,'template_id':'fixture-native',
            'controller_sha256':'e'*64,'build_plan_sha256':'f'*64,'build_receipt_sha256':'1'*64,
            'diagnostic_model':'','provider_profile':profile}
    value['candidate_identity']=identity(config,48)
    cfg=make_trial_config(validate_spec(value),tmp_path)
    assert cfg.agent.model_name==profile['model']
    encoded=cfg.model_dump_json()
    assert '/private/operator-key' not in encoded and 'provider.example' not in encoded
    assert profile['sha256'] in encoded
    value['candidate_identity']['model_id']='unapproved/model'
    with pytest.raises(ValueError,match='profile_model_identity'):validate_spec(value)


def test_codex_does_not_invent_chat_only_protocol_support(tmp_path):
    value=spec(tmp_path);profile=approved_profile(endpoints=['chat/completions'])
    value['candidate_identity'].update(model_id=profile['model'],diagnostic_model='',provider_profile=profile)
    validate_spec(value)  # profile is valid for an adapter that supports Chat
    assert make_trial_config(value,tmp_path).agent.model_name == profile['model']
    from native_compose_contract import native_codex_endpoint
    with pytest.raises(ValueError,match='responses_endpoint_not_approved'):
        native_codex_endpoint(value['candidate_identity'])


@pytest.mark.parametrize('change',[
    lambda profile:profile.update(sha256='not-a-sha'),
    lambda profile:profile.update(base_url='https://attacker.invalid'),
    lambda profile:profile.update(key_file='/private/key'),
    lambda profile:profile.update(endpoints=['arbitrary/path']),
    lambda profile:profile.update(schema='attacker-schema'),
])
def test_unbound_or_unsafe_profile_identity_rejected(tmp_path,change):
    value=spec(tmp_path);profile=approved_profile();change(profile)
    value['candidate_identity'].update(model_id=profile['model'],diagnostic_model='',provider_profile=profile)
    with pytest.raises(ValueError,match='profile'):validate_spec(value)


def test_trial_result_selection_ignores_nested_submission_result(tmp_path):
    job = tmp_path / 'job'
    trial = job / 'trial'
    nested = trial / 'artifacts' / 'submission_artifacts' / 'public_output'
    nested.mkdir(parents=True)
    (job / 'result.json').write_text('{}')
    (trial / 'result.json').write_text('{}')
    (nested / 'result.json').write_text('{}')
    assert trial_result_paths(job) == [trial / 'result.json']


def test_trial_result_selection_rejects_multiple_trials(tmp_path):
    job = tmp_path / 'job'
    for name in ('one', 'two'):
        trial = job / name
        trial.mkdir(parents=True)
        (trial / 'result.json').write_text('{}')
    assert len(trial_result_paths(job)) == 2
