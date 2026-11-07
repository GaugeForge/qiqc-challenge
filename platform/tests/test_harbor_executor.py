"""Fail-closed controller tests; no provider requests are made."""

import importlib.util
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    importlib.util.find_spec("harbor") is None, reason="Harbor interpreter required"
)
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts/science"))


def accepted(monkeypatch):
    import harbor_executor

    monkeypatch.setattr(harbor_executor, "validate_submission", lambda *_: "codex")
    return harbor_executor


def args():
    return {
        "submission_zip": b"accepted-official-fixture",
        "expected_sha256": "0" * 64,
        "run_id": "unit-run",
        "model_call": lambda *_: None,
        "upload_rollout": lambda *_: None,
    }


@pytest.mark.parametrize("run_id", ["../escape", "x/y", "x y", "a" * 81])
def test_run_id_rejected_before_cloud(monkeypatch, run_id):
    module = accepted(monkeypatch)
    with pytest.raises(module.ProbeError):
        module.run_harbor_scientific_probe(**{**args(), "run_id": run_id})


def test_unknown_task_rejected(monkeypatch):
    module = accepted(monkeypatch)
    with pytest.raises(module.ProbeError):
        module.run_harbor_scientific_probe(**args(), task_id="hidden-task")


def test_cancelled_before_sandbox_creation(monkeypatch):
    module = accepted(monkeypatch)
    with pytest.raises(module.Cancelled):
        module.run_harbor_scientific_probe(**args(), cancelled=lambda: True)


def test_weak_model_token_rejected(monkeypatch):
    module = accepted(monkeypatch)
    with pytest.raises(module.ProbeError, match="token"):
        module.run_harbor_scientific_probe(**args(), model_token="short")


def test_frozen_catalog_task_and_budget_forward_to_trial(monkeypatch):
    module = accepted(monkeypatch)
    task = 'adaptive_clustered_clbcs_h2o'
    recorded = {}
    monkeypatch.setattr(module,'_run',lambda **kwargs:recorded.update(kwargs) or {'ok':True})
    assert module.run_harbor_scientific_probe(**args(),task_id=task,generic_task=True,agent_budget_seconds=300) == {'ok':True}
    assert recorded['task_id'] == task and recorded['generic_task'] is True
    assert recorded['agent_budget_seconds'] == 300


@pytest.mark.parametrize('budget',[0,29,301,True,'300'])
def test_diagnostic_budget_rejects_unbounded_or_invalid_value(monkeypatch,budget):
    module = accepted(monkeypatch)
    monkeypatch.setattr(module,'_run',lambda **kwargs:pytest.fail('no execution'))
    with pytest.raises(module.ProbeError,match='budget'):
        module.run_harbor_scientific_probe(**args(),generic_task=True,agent_budget_seconds=budget)


def test_catalog_requires_explicit_admin_diagnostic_mode(monkeypatch):
    module = accepted(monkeypatch)
    with pytest.raises(module.ProbeError,match='unapproved'):
        module.run_harbor_scientific_probe(**args(),task_id='adaptive_clustered_clbcs_h2o')


@pytest.mark.parametrize('exit_code,reward,report,log,accepted',[(0,0,None,b'',True),(1,0,{'passed':False},b'',True),(1,0,None,b'',False),(1,1,{'passed':True},b'',False),(2,0,{'passed':False},b'',False),(1,0,{'passed':False},b'ImportError: missing',False),(1,0,{},b'',False)])
def test_real_verifier_scientific_rejection_requires_complete_fresh_evidence(tmp_path,exit_code,reward,report,log,accepted):
    import harbor_executor,json
    (tmp_path/'verifier').mkdir()
    (tmp_path/'verifier/test-stdout.txt').write_bytes(log)
    if report is not None:(tmp_path/'verifier/score_report.json').write_text(json.dumps(report))
    result=harbor_executor.classify_verifier(tmp_path,exit_code,reward)
    assert result['infrastructure_ok'] is accepted
    assert result['verifier_scientific_rejection'] is (accepted and exit_code==1)
