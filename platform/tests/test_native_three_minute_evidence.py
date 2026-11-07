"""Explicit short observation must not impersonate five-minute acceptance."""
import io
import json
import zipfile
import pytest
from test_native_candidate import config, write_pair
from native_candidate import agent_budget_seconds, commands, identity, load_candidate
from native_controller import inventory, digest
from native_release_record import acceptance_evidence_errors, concurrency_evidence
from native_freeze_gate import assess
from native_compose_template import archive_bytes
from verify_native_candidate_evidence import verified_agent_timing, EvidenceError


def short_record(tmp_path):
    cfg, _ = config(tmp_path)
    _, report = write_pair(tmp_path, cfg, 0)
    value = json.loads(report.read_text())
    value.update(acceptance_scope='bounded-three-minute', agent_budget_seconds=180,
                 observation_policy='observe-180s-no-readiness-stop/v1',
                 execution_duration_class='budget_180s_observed', agent_elapsed_seconds=180.2,
                 agent_execution_finished_at='2026-10-20T13:03:00.200000+00:00')
    return cfg, report, value


def test_budget_bound_to_identity_command_and_controller(tmp_path):
    cfg, path = config(tmp_path)
    assert agent_budget_seconds(cfg) == 300
    assert 'agent_budget_seconds' not in identity(cfg, 0)
    cfg['agent_budget_seconds'] = 180
    assert identity(cfg, 0)['acceptance_scope'] == 'bounded-three-minute'
    assert identity(cfg, 0)['agent_budget_seconds'] == 180
    api = commands(cfg)['api']
    assert api[api.index('-science-agent-budget-seconds')+1] == '180'
    path.write_text(json.dumps(cfg))
    with pytest.raises(ValueError, match='controller_changed'):
        load_candidate(path, verify_source=False)
    manifest = inventory(cfg['project_root'], cfg['binaries'], agent_budget_seconds=180)
    from pathlib import Path
    Path(cfg['controller_manifest']).write_text(json.dumps(manifest))
    cfg['controller_sha256'] = digest(manifest)
    path.write_text(json.dumps(cfg))
    load_candidate(path, verify_source=False)


@pytest.mark.parametrize('value', [True, 0, 179, 181, 301, '180'])
def test_invalid_budget_rejected(value):
    with pytest.raises(ValueError): agent_budget_seconds({'agent_budget_seconds':value})


def test_short_observation_timing_and_wrong_policy_rejection(tmp_path):
    _, _, value = short_record(tmp_path)
    assert acceptance_evidence_errors(value) == []
    assert concurrency_evidence([value]*49)['overlap_seconds'] == 180.2
    wrong = {**value, 'acceptance_scope':'bounded-five-minute'}
    assert acceptance_evidence_errors(wrong)
    for change in ({'agent_elapsed_seconds':10}, {'agent_budget_seconds':300},
                   {'execution_duration_class':'budget_300s_observed'}, {'agent_stop_reason':'completed'}):
        assert acceptance_evidence_errors({**value, **change})
    result = {'agent_execution': {'started_at':value['agent_execution_started_at'], 'finished_at':value['agent_execution_finished_at']}}
    assert verified_agent_timing(result, {'runtime_seconds':180.2,'agent_budget_seconds':180})
    with pytest.raises(EvidenceError): verified_agent_timing(result, {'runtime_seconds':180.2,'agent_budget_seconds':300})


def test_short_run_cannot_pass_existing_freeze_gate(tmp_path):
    cfg, report, value = short_record(tmp_path)
    report.write_text(json.dumps(value))
    result = assess(cfg, tmp_path)
    assert result['status'] != 'passed'
    assert 'five_minute_acceptance_scope_required' in str(result)


def test_short_harness_declares_budget_without_changing_default():
    assert archive_bytes(sustained=True) == archive_bytes(sustained=True, agent_budget_seconds=300)
    with zipfile.ZipFile(io.BytesIO(archive_bytes(sustained=True, agent_budget_seconds=180))) as z:
        assert json.loads(z.read('harness.json'))['version'].endswith('-budget180')
        assert b'180-second' in z.read('README.md')
        assert b'300-second' not in z.read('README.md')
