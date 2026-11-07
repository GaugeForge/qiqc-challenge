"""Offline resume proofs; never starts a browser, service or cloud operation."""
import copy
import json
import sqlite3
import pytest
from test_native_functional49_summary import cohort
from catalog_suite_resume import binding,validate_resume,sha
from catalog_browser_suite import parse_args
from native_functional49_summary import assess


@pytest.fixture
def setup(cohort):
    cfg,lanes,checker=cohort
    db=sqlite3.connect(':memory:')
    db.executescript('CREATE TABLE runs(batch_id TEXT,state TEXT,error TEXT); CREATE TABLE batch_scientific_targets(batch_id TEXT,task_index INTEGER);')
    lane=lanes[0];suite=json.loads((lane/'suite.json').read_bytes())
    return cfg,lanes,checker,db,suite


def validate(setup):
    cfg,lanes,checker,db,suite=setup
    return validate_resume(lanes[0],suite['selected_indices'],cfg,db,checker)


def save(setup):
    (setup[1][0]/'suite.json').write_text(json.dumps(setup[4]))


def test_passed_are_verified_and_history_preserved(setup):
    old,rows,skip,bound=validate(setup)
    assert len(rows)==len(skip)==17
    assert rows==setup[4]['attempts']
    assert bound==binding(setup[0],setup[1][0],False)


def test_explicit_resume_flag():
    args=parse_args(['--indices','0','--output-dir','x','--resume'])
    assert args.resume and not args.allow_invalid_advice_manual_review


@pytest.mark.parametrize('field',['browser','r2'])
def test_report_tampering_rejected(setup,field):
    path=setup[1][0]/('task-00-attempt-01'+('-r2' if field=='r2' else '')+'.json')
    path.write_bytes(path.read_bytes()+b' ')
    with pytest.raises(ValueError,match='digest_changed'):validate(setup)


def test_profile_changed_rejected_even_legacy(setup):
    from test_native_provider_profile import fixture
    setup[0]['provider_profile']=fixture()['identity']
    with pytest.raises(ValueError,match='identity_changed'):validate(setup)


def test_bound_config_or_review_policy_changes_rejected(setup):
    setup[4]['resume_binding']=binding(setup[0],setup[1][0],False);save(setup)
    setup[0]['url']='http://127.0.0.1:9999'
    with pytest.raises(ValueError,match='configuration_changed'):validate(setup)


@pytest.mark.parametrize('state',['browser_running','r2_verifying'])
def test_unfinished_local_attempt_rejected(setup,state):
    setup[4]['attempts'][0]['state']=state;save(setup)
    with pytest.raises(ValueError,match='unfinished_attempt'):validate(setup)


@pytest.mark.parametrize('state,error',[('queued',''),('running',''),('uploading',''),('attention','worker_lost:reconciliation_required')])
def test_related_database_active_rejected_without_browser_batch(setup,state,error):
    setup[3].execute('INSERT INTO runs VALUES(?,?,?)',('unknown-batch',state,error))
    setup[3].execute('INSERT INTO batch_scientific_targets VALUES(?,?)',('unknown-batch',0))
    with pytest.raises(ValueError,match='related_run_active'):validate(setup)


def test_other_lane_active_allowed(setup):
    setup[3].execute('INSERT INTO runs VALUES(?,?,?)',('other','running',''))
    setup[3].execute('INSERT INTO batch_scientific_targets VALUES(?,?)',('other',1))
    assert 0 in validate(setup)[2]


def test_older_pass_newer_fail_not_skipped_and_history_retained(setup):
    cfg,lanes,checker,db,suite=setup
    path=lanes[0]/'task-00-attempt-02.json'
    b=json.loads((lanes[0]/'task-00-attempt-01.json').read_bytes());b['status']='failed'
    path.write_text(json.dumps(b))
    newer={'catalog_index':0,'attempt':2,'state':'browser_failed','browser_sha256':sha(path.read_bytes())}
    suite['attempts'].append(newer);save(setup)
    _,rows,skip,_=validate(setup)
    assert 0 not in skip and len(rows)==18 and rows[0]['state']=='passed' and rows[-1]==newer
    result=assess(cfg,lanes,checker)
    assert result['rows'][0]['attempt']==2 and not result['rows'][0]['passed']


def test_original_indices_order_required(setup):
    cfg,lanes,checker,db,suite=setup
    with pytest.raises(ValueError,match='original_indices'):
        validate_resume(lanes[0],list(reversed(suite['selected_indices'])),cfg,db,checker)


def test_report_symlink_rejected(setup):
    path=setup[1][0]/'task-00-attempt-01.json';target=path.with_suffix('.original');path.rename(target);path.symlink_to(target)
    with pytest.raises(ValueError,match='not_regular'):validate(setup)


def test_unattempted_retried_without_losing_plan(setup):
    suite=setup[4];suite['attempts']=[r for r in suite['attempts'] if r['catalog_index']!=0]
    for path in setup[1][0].glob('task-00-attempt-01*.json'):path.unlink()
    save(setup)
    _,rows,skip,_=validate(setup)
    assert 0 not in skip and 0 in suite['selected_indices']


def test_orphan_attempt_rejected(setup):
    (setup[1][0]/'task-00-attempt-02.json').write_text('{}')
    with pytest.raises(ValueError,match='orphan_attempt'):validate(setup)


def test_duplicate_attempt_rejected(setup):
    setup[4]['attempts'].append(copy.deepcopy(setup[4]['attempts'][0]));save(setup)
    with pytest.raises(ValueError,match='history_invalid'):validate(setup)


def test_copied_directory_binding_rejected(setup):
    setup[4]['resume_binding']=binding(setup[0],setup[1][1],False);save(setup)
    with pytest.raises(ValueError,match='configuration_changed'):validate(setup)


def test_legacy_failed_digest_binding_is_explicit_and_second_tamper_fails(setup):
    cfg,lanes,checker,db,suite=setup
    row=suite['attempts'][0];row['state']='browser_failed';row.pop('browser_sha256')
    path=lanes[0]/'task-00-attempt-01.json';b=json.loads(path.read_bytes());b['status']='failed';path.write_text(json.dumps(b))
    save(setup)
    _,rows,skip,bound=validate(setup)
    assert 0 not in skip and rows[0]['browser_digest_bound_at_resume']
    suite.update(attempts=rows,resume_binding=bound);save(setup)
    path.write_bytes(path.read_bytes()+b' ')
    with pytest.raises(ValueError,match='digest_changed'):validate(setup)


def test_entirely_completed_suite_resume_launches_no_browser(setup,monkeypatch):
    import sys
    import catalog_browser_suite as driver
    import native_candidate
    cfg,lanes,checker,db,suite=setup
    # Main uses real readonly SQLite, preserving data; cloud/subprocess forbidden.
    data=lanes[0]/'database';data.mkdir();filedb=sqlite3.connect(data/'platform.db');db.backup(filedb);filedb.close()
    cfg['data']=str(data);cfg['credentials']=str(lanes[0]/'unused-private-credentials')
    manifest=lanes[0]/'controller.json';manifest.write_text(json.dumps({'host_files':{'competition-platform/platform/scripts/science/verify_native_candidate_evidence.py':checker}}));cfg['controller_manifest']=str(manifest)
    monkeypatch.setattr(native_candidate,'load_candidate',lambda path:(cfg,None))
    monkeypatch.setattr(driver.subprocess,'run',lambda *a,**k:pytest.fail('completed tasks must not launch subprocess'))
    monkeypatch.setattr(sys,'argv',['suite','--candidate','unused','--indices',','.join(map(str,suite['selected_indices'])),'--output-dir',str(lanes[0]),'--resume'])
    assert driver.main()==0
    result=json.loads((lanes[0]/'suite.json').read_bytes())
    assert result['status']=='passed' and result['attempts']==suite['attempts']
    assert result['selected_indices']==suite['selected_indices'] and result['resume_binding']==binding(cfg,lanes[0],False)


def test_manual_policy_upgrade_records_exact_transition_without_losing_passes(setup):
    cfg,lanes,checker,db,suite=setup
    old=binding(cfg,lanes[0],True);suite['resume_binding']=old;save(setup)
    before_sha=sha((lanes[0]/'suite.json').read_bytes())
    result,rows,skip,bound=validate_resume(lanes[0],suite['selected_indices'],cfg,db,checker,False,True)
    assert len(skip)==17 and rows==suite['attempts']
    transition=result['policy_transitions'][-1]
    assert transition['from']==old and transition['to']==bound
    assert transition['previous_suite_sha256']==before_sha
    assert bound['organizer_template_manual_review'] is True
    result['resume_binding']=bound;(lanes[0]/'suite.json').write_text(json.dumps(result))
    again=validate_resume(lanes[0],suite['selected_indices'],cfg,db,checker,False,True)
    assert len(again[0]['policy_transitions'])==1


@pytest.mark.parametrize('tamper',['config','directory','report'])
def test_explicit_policy_upgrade_cannot_hide_other_change(setup,tamper):
    cfg,lanes,checker,db,suite=setup
    suite['resume_binding']=binding(cfg,lanes[0],True);save(setup)
    if tamper=='config':cfg['url']='http://127.0.0.1:9999'
    elif tamper=='directory':suite['resume_binding']['evidence_directory']=str(lanes[1]);save(setup)
    else:
        path=lanes[0]/'task-00-attempt-01.json';path.write_bytes(path.read_bytes()+b' ')
    with pytest.raises(ValueError):validate_resume(lanes[0],suite['selected_indices'],cfg,db,checker,False,True)


def test_review_modes_are_explicit_and_mutually_exclusive():
    args=parse_args(['--indices','0','--output-dir','x','--organizer-template-manual-review'])
    assert args.organizer_template_manual_review and not args.allow_invalid_advice_manual_review
    with pytest.raises(SystemExit):parse_args(['--indices','0','--output-dir','x','--organizer-template-manual-review','--allow-invalid-advice-manual-review'])
