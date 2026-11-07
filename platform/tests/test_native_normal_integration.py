"""Ordinary approved harnesses need not behave like sustained diagnostics."""
import json
import pytest

import native_worker_bridge as bridge
from bounded_files import RemoteFileError


def collect(monkeypatch, payload, *, normal=True):
    calls = []
    def read(*args, **kwargs):
        calls.append(kwargs)
        if isinstance(payload, Exception):
            raise payload
        return payload
    monkeypatch.setattr(bridge, 'read_remote_bytes', read)
    result = bridge.collect_qsim_progress(object(), {}, normal_science=normal,
                                         request_timeout=4, deadline=99)
    assert calls[0]['missing_ok'] is normal
    assert calls[0]['request_timeout'] == 4 and calls[0]['deadline'] == 99
    return result


def test_natural_short_normal_harness_does_not_require_qsim_receipt(monkeypatch):
    value = collect(monkeypatch, None)
    assert value['observed'] is False
    assert value['reason'] == 'no_progress_receipt'
    assert 'success_count' not in value


def test_existing_normal_qsim_receipt_retained_without_fabricating_success(monkeypatch):
    value = {'schema': 'qiqc-native-qsim-progress/v1', 'success_count': 0}
    assert collect(monkeypatch, json.dumps(value).encode()) == value


@pytest.mark.parametrize('payload', [None, b'{}', b'{"success_count":0}'])
def test_diagnostic_missing_or_unproven_interaction_still_fails(monkeypatch, payload):
    with pytest.raises(ValueError, match='interaction_not_verified'):
        collect(monkeypatch, payload, normal=False)


@pytest.mark.parametrize('payload', [b'[]', b'not-json', RemoteFileError('Remote file transfer failed')])
def test_normal_mode_does_not_hide_corruption_or_transport_errors(monkeypatch, payload):
    with pytest.raises((ValueError, RemoteFileError)):
        collect(monkeypatch, payload)


def test_diagnostic_positive_bound_progress_is_preserved(monkeypatch):
    expected = {'run_id': 'fixture', 'attempt': 1, 'submission_sha256': 'a'*64, 'candidate_identity': {'fixture': True}}
    value = {**expected, 'schema':'qiqc-native-qsim-progress/v1', 'success_count':1,
             'qsim_container_id':'b'*64, 'event_sha256':'c'*64, 'log_prefix_sha256':'d'*64,
             'log_bytes':100, 'observed_at':1000}
    monkeypatch.setattr(bridge, 'read_remote_bytes', lambda *a, **kw: json.dumps(value).encode())
    assert bridge.collect_qsim_progress(object(), expected, normal_science=False,
                                        request_timeout=4, deadline=99) == value


@pytest.mark.parametrize('scope', ['full-runtime', 'practice-native', 'competition-native'])
def test_custom_chat_adapter_executes_through_mock_environment(tmp_path, monkeypatch, scope):
    import asyncio
    from types import SimpleNamespace
    import native_compose_agent as agent
    from native_compose_environment import EVIDENCE
    from native_compose_runner import make_trial_config
    from native_compose_contract import validate_spec
    from test_native_compose import spec, approved_profile
    value=spec(tmp_path)
    if scope != 'full-runtime':
        value['candidate_identity'].update(acceptance_scope=scope, agent_budget_seconds=300,
            native_limits={'agent_seconds':300,'setup_seconds':30,'build_seconds':60,
                           'verifier_seconds':30,'source_sha256':'d'*64})
    profile=approved_profile(endpoints=['chat/completions'])
    value['candidate_identity'].update(model_id=profile['model'], diagnostic_model='', provider_profile=profile)
    assert make_trial_config(validate_spec(value), tmp_path).agent.model_name == profile['model']
    manifest={'adapter':'custom-chat','entrypoint':'custom.sh','model_interfaces':['chat/completions']}
    agent.RUNS['native_test']={**value,'control_dir':tmp_path,'scoped_token':'qr_public_fixture_only', 'submission_manifest':manifest}
    calls=[]
    observations=[]
    import native_acceptance_stop
    monkeypatch.setattr(native_acceptance_stop,'observe_qsim',lambda *a:observations.append('observed') or {'success_count':0})
    class Environment:
        native_security_receipt={}
        async def upload_file(self,*args): pass
        async def exec(self,command,**kwargs):
            calls.append((command,kwargs))
            return SimpleNamespace(return_code=0)
    try:
        harness=agent.NativeSubmittedHarness(logs_dir=tmp_path,run_handle='native_test',mcp_servers=[])
        harness.runtime_env = {}  # Setup is replaced by this isolated environment fixture.
        context=SimpleNamespace(metadata={})
        asyncio.run(harness.run('PUBLIC FIXTURE ONLY',Environment(),context))
        assert context.metadata['model_endpoint']=='chat/completions'
        command,kwargs=next(item for item in calls if item[0].startswith('timeout '))
        assert 'sh /opt/qiqc-submission/custom.sh' in command
        assert kwargs['env']['QIQC_MODEL']==profile['model']
        assert (tmp_path/'instruction.txt').read_text()=='PUBLIC FIXTURE ONLY'
        assert bool(observations) == (scope != 'full-runtime')
    finally:
        agent.RUNS.pop('native_test',None)
        EVIDENCE.pop('native_test',None)


def test_normal_planned_cutoff_is_not_falsely_counted_as_model_success():
    from test_model_response_contract import planned_event
    event, expected = planned_event()
    summary = bridge.validate_model_observation([event], expected, normal_science=True)
    assert summary['planned_cancelled_responses'] == 1
    assert summary['successful_responses'] == 0
    assert summary['all_failures_recovered'] is False
    with pytest.raises(ValueError, match='native_model_unrecovered'):
        bridge.validate_model_observation([event], expected)
    assert bridge.validate_model_observation([], expected, normal_science=True)['successful_responses'] == 0
    with pytest.raises(ValueError):
        bridge.validate_model_observation([], expected)


@pytest.mark.parametrize('change', [
    lambda e: e.pop('planned_cancellation'),
    lambda e: e['planned_cancellation'].update(main_stopped=False),
    lambda e: e['planned_cancellation'].update(run_id='other'),
    lambda e: e['planned_cancellation'].update(elapsed_seconds=2),
    lambda e: e.pop('response_contract'),
    lambda e: e.update(response_contract='malformed'),
    lambda e: e.pop('adapter_request_sha256'),
    lambda e: e.update(forwarded_request_sha256='f'*64),
    lambda e: e.update(status=502),
])
def test_normal_cutoff_does_not_accept_missing_or_forged_evidence(change):
    from test_model_response_contract import planned_event
    event, expected = planned_event()
    change(event)
    with pytest.raises(ValueError):
        bridge.validate_model_observation([event], expected, normal_science=True)


def test_normal_duplicate_cancellation_and_missing_expected_identity_rejected():
    from test_model_response_contract import planned_event
    event, expected = planned_event()
    for events, identity in (([event,event], expected), ([event], None), ([None], expected),
                             ([{}], expected), (None, expected)):
        with pytest.raises(ValueError):
            bridge.validate_model_observation(events, identity, normal_science=True)


def test_normal_planned_cancel_does_not_recover_an_actual_prior_model_failure():
    from test_model_response_contract import planned_event
    event, expected = planned_event()
    failed = {**event, 'status':502, 'request_correlation':'e'*32,
              'response_contract': {'schema':'qiqc-model-response/v2','success':False,'terminal':False,'category':'incomplete'}}
    failed.pop('planned_cancellation')
    with pytest.raises(ValueError, match='native_model_unrecovered'):
        bridge.validate_model_observation([failed,event], expected, normal_science=True)
