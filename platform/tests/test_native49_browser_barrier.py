"""No cloud: a start barrier must bind every original browser request exactly."""
import importlib.util
import hashlib
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

spec = importlib.util.spec_from_file_location('barrier', Path(__file__).parents[1]/'scripts/native49_browser_barrier.py')
barrier = importlib.util.module_from_spec(spec)
spec.loader.exec_module(barrier)


def fixture(tmp_path, monkeypatch):
    monkeypatch.setattr(barrier, 'identity', lambda config, index: {'task_index': index, 'seal': 'a'*64})
    config = {'data': str(tmp_path), 'candidate_id': 'candidate'}
    with sqlite3.connect(tmp_path/'platform.db') as db:
        db.executescript('CREATE TABLE batches(id,status,version_id); CREATE TABLE versions(id,sha256,status,reason); CREATE TABLE batch_scientific_targets(batch_id,task_index,release_id,agent_budget_seconds); CREATE TABLE runs(batch_id,state,attempt); CREATE TABLE review_advisories(version_id,state,submission_sha,result,created); CREATE TABLE audit(action,object_id,before_json,after_json,reason,created);')
        for index in range(49):
            batch, version = f'b{index}', f'v{index}'
            db.execute('INSERT INTO batches VALUES(?,?,?)', (batch,'queued',version))
            db.execute('INSERT INTO versions VALUES(?,?,?,?)', (version,'s'*64,'approved','default fixture approval'))
            db.execute('INSERT INTO batch_scientific_targets VALUES(?,?,?,?)', (batch,index,'candidate',300))
            db.execute('INSERT INTO runs VALUES(?,?,?)', (batch,'queued',0))
            (tmp_path/f'task-{index:02d}-attempt-01-prepared.json').write_text(json.dumps({'status':'browser_queued_waiting_barrier','benchmark_release':barrier.identity(config,index),'catalog_index':index,'admin_target_bound_via_browser':True,'ai_review':{'real_advice_available':True},'batch_id':batch,'version_id':version,'submission_sha256':'s'*64}))
    return config


def test_complete_browser_queue_and_source_identity_are_required(tmp_path, monkeypatch):
    config = fixture(tmp_path, monkeypatch)
    assert len(barrier.validate_prepared(config,tmp_path)) == 49
    with sqlite3.connect(tmp_path/'platform.db') as db:
        db.execute("UPDATE versions SET sha256='changed' WHERE id='v7'")
    with pytest.raises(ValueError, match='exactly_queued'):
        barrier.validate_prepared(config,tmp_path)


def test_start_rejects_already_running_or_unrelated_work(tmp_path, monkeypatch):
    config = fixture(tmp_path, monkeypatch)
    with sqlite3.connect(tmp_path/'platform.db') as db:
        db.execute("INSERT INTO runs VALUES('unrelated','running',1)")
    with pytest.raises(ValueError, match='unrelated_active'):
        barrier.validate_prepared(config,tmp_path)
    with sqlite3.connect(tmp_path/'platform.db') as db:
        db.execute("DELETE FROM runs WHERE batch_id='unrelated'")
        db.execute("UPDATE runs SET state='running',attempt=1 WHERE batch_id='b3'")
    with pytest.raises(ValueError, match='exactly_queued'):
        barrier.validate_prepared(config,tmp_path)


def organizer_fixture(tmp_path, monkeypatch):
    config = fixture(tmp_path, monkeypatch)
    archive = b'fixed organizer template, never executed'
    sha = hashlib.sha256(archive).hexdigest()
    adapters = tmp_path/'adapters'
    (adapters/'codex').mkdir(parents=True)
    (adapters/'codex/harness.zip').write_bytes(archive)
    manifest = tmp_path/'adapter-manifest.json'
    manifest.write_text(json.dumps({'codex': {'sha256': sha}}))
    config.update(adapters=str(adapters), adapter_manifest=str(manifest))
    result = {'status': 'manual_required', 'outcome': 'invalid_response',
              'failure_code': 'explanation_bounds', 'policy_version': 'qiqc-readonly-advisory/v3',
              'provider_version': 'openrouter-hy4-chat-v3'}
    reason = 'AI invalid_response/explanation_bounds; manual decision based on pre-reviewed organizer manifest SHA ' + sha
    with sqlite3.connect(tmp_path/'platform.db') as db:
        for index in range(49):
            vid = f'v{index}'
            db.execute('UPDATE versions SET sha256=?,reason=? WHERE id=?', (sha,reason,vid))
            db.execute('INSERT INTO review_advisories VALUES(?,?,?,?,?)', (vid,'complete',sha,json.dumps(result),1))
            db.execute('INSERT INTO audit VALUES(?,?,?,?,?,?)', ('review',vid,json.dumps('pending_review'),json.dumps('approved'),reason,2))
            path = tmp_path/f'task-{index:02d}-attempt-01-prepared.json'
            report = json.loads(path.read_text())
            report.update(adapter='codex',submission_sha256=sha,upload_freeze_review_sha256=sha,
                ai_review={**result, 'real_advice_available': False, 'manual_approval_preserved': True,
                    'manual_fallback_verified': True, 'manual_review_policy': 'organizer-template',
                    'manual_fallback_template_sha256': sha, 'quota_unchanged_before_manual_approval': True,
                    'ai_quality_gate_passed': False, 'manual_fallback_notice_visible_in_admin': True,
                    'manual_approval_reason': reason})
            path.write_text(json.dumps(report))
    return config


def test_manual_mode_needs_explicit_opt_in_and_real_approval_proof(tmp_path, monkeypatch):
    config = organizer_fixture(tmp_path, monkeypatch)
    with pytest.raises(ValueError, match='identity_or_review'):
        barrier.validate_prepared(config,tmp_path)
    assert len(barrier.validate_prepared(config,tmp_path,True)) == 49
    with sqlite3.connect(tmp_path/'platform.db') as db:
        db.execute("DELETE FROM audit WHERE object_id='v5'")
    with pytest.raises(ValueError, match='approval_audit'):
        barrier.validate_prepared(config,tmp_path,True)


@pytest.mark.parametrize('field,value', [
    ('manual_fallback_verified', False), ('manual_approval_preserved', False),
    ('manual_review_policy', 'invalid-advice-only'), ('manual_fallback_template_sha256', 'b'*64),
    ('quota_unchanged_before_manual_approval', False), ('ai_quality_gate_passed', True),
    ('manual_fallback_notice_visible_in_admin', False), ('real_advice_available', True),
    ('manual_approval_reason', 'fabricated'), ('provider_version', 'unapproved'),
    ('outcome', 'advice_ready'), ('failure_code', 'different'),
])
def test_manual_report_tampering_does_not_release_barrier(tmp_path, monkeypatch, field, value):
    config = organizer_fixture(tmp_path, monkeypatch)
    path = tmp_path/'task-00-attempt-01-prepared.json'
    report = json.loads(path.read_text());report['ai_review'][field] = value
    path.write_text(json.dumps(report))
    with pytest.raises(ValueError, match='prepared_organizer'):
        barrier.validate_prepared(config,tmp_path,True)


@pytest.mark.parametrize('sql', [
    "UPDATE review_advisories SET state='running' WHERE version_id='v0'",
    "UPDATE review_advisories SET submission_sha='wrong' WHERE version_id='v0'",
    "UPDATE versions SET status='rejected' WHERE id='v0'",
    "UPDATE audit SET after_json='\"rejected\"' WHERE object_id='v0'",
    "UPDATE audit SET reason='different' WHERE object_id='v0'",
])
def test_manual_database_must_still_prove_real_review(tmp_path, monkeypatch, sql):
    config = organizer_fixture(tmp_path, monkeypatch)
    with sqlite3.connect(tmp_path/'platform.db') as db:db.execute(sql)
    with pytest.raises(ValueError, match='prepared_organizer'):
        barrier.validate_prepared(config,tmp_path,True)


def test_changed_organizer_zip_cannot_use_old_manual_proof(tmp_path, monkeypatch):
    config = organizer_fixture(tmp_path, monkeypatch)
    (Path(config['adapters'])/'codex/harness.zip').write_bytes(b'changed')
    with pytest.raises(ValueError, match='manual_proof'):
        barrier.validate_prepared(config,tmp_path,True)


@pytest.mark.parametrize('outcome,failure,accepted', [
    ('advice_ready',None,True), ('provider_timeout','deadline',True),
    ('provider_error','completion_token_limit',True),
    ('provider_error','transport_error',False), ('provider_error','quota_error',False),
    ('provider_unavailable','unknown',False),
])
def test_organizer_policy_preserves_known_outcome_boundary(tmp_path,monkeypatch,outcome,failure,accepted):
    config = organizer_fixture(tmp_path,monkeypatch)
    path = tmp_path/'task-00-attempt-01-prepared.json';report = json.loads(path.read_text())
    with sqlite3.connect(tmp_path/'platform.db') as db:
        result = json.loads(db.execute("SELECT result FROM review_advisories WHERE version_id='v0'").fetchone()[0])
        diagnostic = {'finish_reason':'length','prompt_tokens':1152,'max_tokens':8192,'completion_tokens':8192,'reasoning_tokens':8192} if outcome=='provider_error' else None
        result.update(outcome=outcome,failure_code=failure,manual_followup_required=True,provider_diagnostics=diagnostic)
        reason = 'AI '+outcome+'/'+str(failure or 'none')+'; manual decision based on pre-reviewed organizer manifest SHA '+report['submission_sha256']
        report['ai_review'].update(outcome=outcome,failure_code=failure,manual_followup_required=True,
            diagnostics=diagnostic,real_advice_available=outcome=='advice_ready',manual_followup_visible_in_admin=outcome=='advice_ready',manual_approval_reason=reason)
        db.execute("UPDATE review_advisories SET result=? WHERE version_id='v0'",(json.dumps(result),))
        db.execute("UPDATE versions SET reason=? WHERE id='v0'",(reason,))
        db.execute("UPDATE audit SET reason=? WHERE object_id='v0'",(reason,))
        if accepted:barrier.validate_organizer_review(config,report,db)
        else:
            with pytest.raises(ValueError,match='outcome_invalid'):barrier.validate_organizer_review(config,report,db)


@pytest.mark.parametrize('budget', [180,300])
def test_batch_budget_must_equal_explicit_candidate_budget(tmp_path, monkeypatch, budget):
    config = fixture(tmp_path, monkeypatch);config['agent_budget_seconds'] = budget
    with sqlite3.connect(tmp_path/'platform.db') as db:db.execute('UPDATE batch_scientific_targets SET agent_budget_seconds=?',(budget,))
    assert len(barrier.validate_prepared(config,tmp_path)) == 49
    with sqlite3.connect(tmp_path/'platform.db') as db:db.execute('UPDATE batch_scientific_targets SET agent_budget_seconds=? WHERE task_index=48',(300 if budget==180 else 180,))
    with pytest.raises(ValueError, match='exactly_queued'):
        barrier.validate_prepared(config,tmp_path)


def test_collect_uses_deployed_checker_and_explicit_review_flag(tmp_path, monkeypatch):
    config = fixture(tmp_path, monkeypatch);config['project_root'] = str(tmp_path/'deployed')
    calls = []
    monkeypatch.setattr(barrier.subprocess,'run',lambda command,**kwargs:(calls.append(command) or SimpleNamespace(returncode=0)))
    result = barrier.browser(tmp_path/'candidate.json',tmp_path,0,'collect',True,config)
    assert result['state'] == 'passed'
    assert '--organizer-template-manual-review' in calls[0]
    assert '--require-ai-advice' not in calls[0]
    assert calls[1][1] == str(Path(config['project_root'])/'competition-platform/platform/scripts/science/verify_native_candidate_evidence.py')
    assert '--resume-queued-report' in calls[0]


def test_default_prepare_still_demands_ai_advice(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(barrier,'worker_inactive',lambda:True)
    monkeypatch.setattr(barrier.subprocess,'run',lambda command,**kwargs:(calls.append(command) or SimpleNamespace(returncode=0)))
    assert barrier.browser(tmp_path/'candidate.json',tmp_path,0,'prepare')['state'] == 'queued'
    assert '--require-ai-advice' in calls[0]
    assert '--organizer-template-manual-review' not in calls[0]
    assert '--queue-only' in calls[0]


@pytest.mark.parametrize('workers',[1,2,4,8])
def test_browser_concurrency_is_explicit_and_does_not_change_science_slots(tmp_path,workers):
    args = barrier.parse_args(['prepare','--candidate','candidate.json','--output-dir',str(tmp_path),'--browser-workers',str(workers)])
    assert args.browser_workers == workers
    assert not args.organizer_template_manual_review


@pytest.mark.parametrize('workers',['0','9','49','-1'])
def test_invalid_browser_parallelism_rejected(tmp_path,workers):
    with pytest.raises(SystemExit):
        barrier.parse_args(['prepare','--candidate','candidate.json','--output-dir',str(tmp_path),'--browser-workers',workers])


def test_recovery_skips_success_and_preserves_failed_log(tmp_path,monkeypatch):
    config=fixture(tmp_path,monkeypatch);config['project_root']=str(tmp_path/'deployed')
    calls=[]
    monkeypatch.setattr(barrier.subprocess,'run',lambda command,**kwargs:(calls.append(command) or SimpleNamespace(returncode=0)))
    output=tmp_path/'task-00-attempt-01.json';output.write_text('{"status":"passed"}')
    original=output.read_bytes()
    assert barrier.browser(tmp_path/'candidate.json',tmp_path,0,'recover-browser',True,config)['state']=='browser_passed_existing'
    assert not calls and output.read_bytes()==original
    log=tmp_path/'task-01-attempt-01.log';log.write_text('original failed driver evidence')
    assert barrier.browser(tmp_path/'candidate.json',tmp_path,1,'recover-browser',True,config)['state']=='browser_passed'
    assert len(calls)==1 and '--resume-queued-report' in calls[0] and '--queue-only' not in calls[0]
    assert (tmp_path/'task-01-attempt-01-driver-failed-01.log').read_text()=='original failed driver evidence'
