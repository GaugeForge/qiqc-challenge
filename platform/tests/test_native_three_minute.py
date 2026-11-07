"""Three-minute observations cannot satisfy legacy five-minute contracts.

Only trusted orchestration and fixed local fixtures are exercised here.
"""
import copy
import pytest

from native_compose_contract import agent_budget_from_identity, validate_spec
from native_compose_runner import make_trial_config
from native_contract import execution_budget
from native_model_http import budget_stop_marker
from native_worker_bridge import validate_receipt
from test_native_compose import spec


IDENTITY = {'acceptance_scope': 'bounded-three-minute', 'agent_budget_seconds': 180}


def test_three_minute_spec_changes_agent_deadline_only(tmp_path):
    value = {**spec(tmp_path), 'candidate_identity': IDENTITY, 'agent_budget_seconds': 180}
    cfg = make_trial_config(validate_spec(value), tmp_path)
    assert cfg.agent.override_timeout_sec == 210
    assert cfg.verifier.override_timeout_sec is None
    for identity, seconds in [({}, 180), (IDENTITY, 300),
                              ({**IDENTITY, 'acceptance_scope': 'bounded-five-minute'}, 180)]:
        with pytest.raises(ValueError):
            validate_spec({**value, 'candidate_identity': identity or {'fixture': True},
                           'agent_budget_seconds': seconds})


@pytest.mark.parametrize('budget', [True, 180.0, '180', 179, 301, None])
def test_invalid_budget_never_silently_becomes_180(budget):
    with pytest.raises(ValueError):
        agent_budget_from_identity({**IDENTITY, 'agent_budget_seconds': budget})


def receipt():
    return {'run_id': 'public-fixture', 'submission_sha256': 'a'*64,
            'candidate_identity': dict(IDENTITY), 'agent_budget_seconds': 180,
            'infrastructure_ok': True, 'standard_harbor_rollout': True,
            'reward': 0, 'verifier_exit': 0, 'agent_exit': 124,
            'budget_exhausted': True, 'agent_stop_reason': 'budget_exhausted',
            'runtime_seconds': 180.2, 'observation_policy': 'observe-180s-no-readiness-stop/v1',
            'execution_duration_class': 'budget_180s_observed'}


REQUEST = {'run_id': 'public-fixture', 'sha256': 'a'*64,
           'agent_budget_seconds': 180, 'lease': {'attempt': 1}}


def test_three_minute_receipt_is_strict_and_not_five_minute():
    good = receipt()
    validate_receipt(good, REQUEST)
    for change in ({'runtime_seconds': 30}, {'runtime_seconds': 300},
                   {'runtime_seconds': float('nan')}, {'agent_budget_seconds': 300},
                   {'observation_policy': 'observe-300s-no-readiness-stop/v1'},
                   {'execution_duration_class': 'budget_300s_observed'},
                   {'acceptance_early_stop_verified': True}, {'agent_exit': 1},
                   {'candidate_identity': {'acceptance_scope': 'bounded-five-minute'}}):
        with pytest.raises(ValueError):
            validate_receipt({**good, **change}, REQUEST)
    with pytest.raises(ValueError):
        validate_receipt(good, {**REQUEST, 'agent_budget_seconds': 300})


def test_three_minute_natural_completion_remains_distinct():
    good = {**receipt(), 'runtime_seconds': 20, 'agent_exit': 0,
            'budget_exhausted': False, 'agent_stop_reason': 'completed',
            'execution_duration_class': 'natural_completion_before_budget'}
    validate_receipt(good, REQUEST, allow_natural_completion=True)
    with pytest.raises(ValueError, match='three_minute'):
        validate_receipt(good, REQUEST)


def test_model_cancel_requires_matching_duration_and_stopped_main():
    marker = {**receipt(), 'schema': 'qiqc-native-agent-stop/v1', 'attempt': 1,
              'elapsed_seconds': 180.2, 'completed_at': 1000,
              'main_stopped': True, 'main_container_id': 'b'*64}
    assert budget_stop_marker(marker, REQUEST, IDENTITY)
    for change in ({'elapsed_seconds': 30}, {'main_stopped': False},
                   {'observation_policy': 'observe-300s-no-readiness-stop/v1'},
                   {'execution_duration_class': 'budget_300s_observed'},
                   {'agent_budget_seconds': 300}):
        assert not budget_stop_marker({**marker, **change}, REQUEST, IDENTITY)
    assert not budget_stop_marker(marker, {**REQUEST, 'agent_budget_seconds': 300}, IDENTITY)


def test_budget_retains_original_verifier_and_cleanup(monkeypatch):
    import native_contract
    task = {'timeouts': {'verifier': 1200}, 'role_resources': {'main': {'memory_mb': 4096, 'cpus': 2}}}
    monkeypatch.setattr(native_contract, 'task_contract', lambda *args: copy.deepcopy(task))
    old = execution_budget({}, 'fixture', acceptance_scope='bounded-five-minute')
    new = execution_budget({}, 'fixture', acceptance_scope='bounded-three-minute', agent_budget_seconds=180)
    assert old['agent_seconds'] == 300 and new['agent_seconds'] == 180
    assert new['runner_deadline_seconds'] == old['runner_deadline_seconds']-120
    assert new['sandbox_ttl_seconds'] == old['sandbox_ttl_seconds']-120
    for key in ('original_verifier_seconds', 'provisioning_setup_seconds', 'collection_archive_cleanup_seconds'):
        assert new[key] == old[key]
    with pytest.raises(ValueError):
        execution_budget({}, 'fixture', acceptance_scope='bounded-five-minute', agent_budget_seconds=180)


def test_planned_model_cancel_remains_valid_at_180_and_rejects_false_300():
    from test_model_response_contract import planned_event
    from model_response_contract import _native_planned_budget_cancelled
    event, expected = planned_event()
    candidate = {**expected['candidate_identity'], **IDENTITY}
    expected = {**expected, 'candidate_identity': candidate}
    event['planned_cancellation'].update(candidate_identity=candidate,
        agent_budget_seconds=180, elapsed_seconds=180.2,
        observation_policy='observe-180s-no-readiness-stop/v1',
        execution_duration_class='budget_180s_observed')
    assert _native_planned_budget_cancelled(event, expected)
    for change in ({'agent_budget_seconds': 300}, {'elapsed_seconds': 30},
                   {'observation_policy': 'observe-300s-no-readiness-stop/v1'},
                   {'execution_duration_class': 'budget_300s_observed'}):
        changed = copy.deepcopy(event)
        changed['planned_cancellation'].update(change)
        assert not _native_planned_budget_cancelled(changed, expected)
