"""No-cloud SQL binding tests for ordinary R2 readback."""
import hashlib
import importlib.util
import json
from pathlib import Path
import sqlite3
import pytest

SCRIPT = Path(__file__).parents[1] / 'scripts/ordinary_scientific_readback.py'
spec = importlib.util.spec_from_file_location('ordinary_readback', SCRIPT)
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)

@pytest.fixture
def record(tmp_path):
    catalog = dict(schema='qiqc-scientific-task-set/v1', release_id='native-candidate-'+'d'*64, agent_budget_seconds=180, infra_task_index=48,
                   tasks=[dict(task_index=i, task_id=f'task_{i:02d}', public=i==48) for i in range(49)])
    raw=json.dumps(catalog,separators=(',', ':'));digest=hashlib.sha256(raw.encode()).hexdigest()
    path=tmp_path/'catalog.json';path.write_text(json.dumps(catalog,indent=2))
    config=dict(candidate_id='candidate',science_task_set_path=str(path),agent_budget_seconds=180)
    audit=dict(competition_task_ids=[t['task_id'] for t in catalog['tasks']])
    browser=dict(schema='qiqc-ordinary-browser/v1',status='passed',candidate_id='candidate',automatic_review=True,admin_after_upload_requests=0,
                 public_trace_readback=True,other_team_trace_denied=True,batch_id='b',run_id='r',attempt=1,version_id='v',
                 submission_sha256='a'*64,r2_archive_sha256='c'*64,agent_budget_seconds=180)
    database=tmp_path/'platform.db'
    with sqlite3.connect(database) as db:
        db.executescript('''CREATE TABLE runs(id,batch_id,ordinal,public,attempt,state,reward,object_key,object_sha);
          CREATE TABLE batches(id,status,mode,kind,team_id,version_id);
          CREATE TABLE versions(id,team_id,sha256);
          CREATE TABLE run_scientific_targets(run_id,task_index,task_id,public,release_id,agent_budget_seconds,catalog_sha256,submission_sha256);
          CREATE TABLE scientific_task_sets(sha256,document);
          CREATE TABLE batch_scientific_task_sets(batch_id,catalog_sha256);
          CREATE TABLE usage(run_id,attempt,request_id,request_correlation,upstream_attempt,status,error_class,model,input_tokens,output_tokens,cached_tokens);''')
        db.execute('INSERT INTO runs VALUES(?,?,?,?,?,?,?,?,?)',('r','b',0,1,1,'completed',0,'t/v/b/r/1.tar.gz','c'*64))
        db.execute('INSERT INTO batches VALUES(?,?,?,?,?,?)',('b','completed','scientific','infra','t','v'))
        db.execute('INSERT INTO versions VALUES(?,?,?)',('v','t','a'*64))
        db.execute('INSERT INTO run_scientific_targets VALUES(?,?,?,?,?,?,?,?)',('r',48,'task_48',1,catalog['release_id'],180,digest,'a'*64))
        db.execute('INSERT INTO scientific_task_sets VALUES(?,?)',(digest,raw))
        db.execute('INSERT INTO batch_scientific_task_sets VALUES(?,?)',('b',digest))
        db.execute('INSERT INTO usage VALUES(?,?,?,?,?,?,?,?,?,?,?)',('r',1,'e'*32,'f'*32,1,200,'','fixture',1,1,0))
    return database,browser,config,audit

def test_real_public_ordinal_readonly(record):
    index,binding,usage=m.lookup_normal(*record)
    assert binding['catalog_index']==48 and index['run_id']=='r' and usage[0]['status']==200
    assert 'catalog_index' not in record[1]

@pytest.mark.parametrize('sql',[
 "UPDATE batches SET mode='scientific-smoke'","UPDATE batches SET status='attention'",
 "UPDATE versions SET team_id='other'","UPDATE runs SET public=0",
 "UPDATE run_scientific_targets SET task_index=0","UPDATE run_scientific_targets SET task_id='task_00'",
 "UPDATE run_scientific_targets SET submission_sha256='wrong'","UPDATE run_scientific_targets SET agent_budget_seconds=300",
 "UPDATE run_scientific_targets SET release_id='wrong'","UPDATE batch_scientific_task_sets SET catalog_sha256='wrong'",
 "UPDATE scientific_task_sets SET document='{}'","UPDATE runs SET object_key='other/v/b/r/1.tar.gz'",
 "UPDATE runs SET object_sha='wrong'","UPDATE runs SET attempt=2"])
def test_rejects_identity_drift(record,sql):
    with sqlite3.connect(record[0]) as db:db.execute(sql)
    with pytest.raises(ValueError):m.lookup_normal(*record)

@pytest.mark.parametrize('field,value',[('status','failed'),('automatic_review',False),('admin_after_upload_requests',1),('public_trace_readback',False),('candidate_id','other'),('agent_budget_seconds',300)])
def test_browser_proof_is_independent(record,field,value):
    record[1][field]=value
    with pytest.raises(ValueError):m.lookup_normal(*record)

def test_changed_active_catalog_rejected(record):
    path=Path(record[2]['science_task_set_path']);changed=json.loads(path.read_text());changed['infra_task_index']=0;path.write_text(json.dumps(changed))
    with pytest.raises(ValueError,match='ordinary_current_catalog_mismatch'):m.lookup_normal(*record)


@pytest.fixture
def public_record(record):
    database, browser, config, audit = record
    path = Path(config['science_task_set_path'])
    catalog = json.loads(path.read_text())
    public_indices = [0, 10, 20, 30, 48]
    for task in catalog['tasks']:
        task['public'] = task['task_index'] in public_indices
    raw = json.dumps(catalog, separators=(',', ':'))
    digest = hashlib.sha256(raw.encode()).hexdigest()
    path.write_text(raw)
    browser['kind'] = 'public'
    browser['runs'] = []
    with sqlite3.connect(database) as db:
        db.execute("UPDATE batches SET kind='public'")
        db.execute('DELETE FROM runs')
        db.execute('DELETE FROM run_scientific_targets')
        db.execute('DELETE FROM usage')
        db.execute('UPDATE scientific_task_sets SET sha256=?,document=?', (digest, raw))
        db.execute('UPDATE batch_scientific_task_sets SET catalog_sha256=?', (digest,))
        for ordinal, task_index in enumerate(public_indices):
            run_id = f'r{ordinal}'
            sha = hashlib.sha256(run_id.encode()).hexdigest()
            db.execute('INSERT INTO runs VALUES(?,?,?,?,?,?,?,?,?)', (run_id, 'b', ordinal, 1, 1, 'completed', 0, f't/v/b/{run_id}/1.tar.gz', sha))
            db.execute('INSERT INTO run_scientific_targets VALUES(?,?,?,?,?,?,?,?)', (run_id, task_index, f'task_{task_index:02d}', 1, catalog['release_id'], 180, digest, 'a'*64))
            db.execute('INSERT INTO usage VALUES(?,?,?,?,?,?,?,?,?,?,?)', (run_id, 1, 'e'*32, 'f'*32, 1, 200, '', 'fixture', 1, 1, 0))
            browser['runs'].append(dict(run_id=run_id, attempt=1, r2_archive_sha256=sha))
    return record


def test_all_five_bindings_checked_in_catalog_order(public_record):
    results = m.lookup_normal_runs(*public_record)
    assert [binding['catalog_index'] for _, binding, _ in results] == [0, 10, 20, 30, 48]
    assert [index['run_id'] for index, _, _ in results] == [f'r{i}' for i in range(5)]
    with pytest.raises(ValueError, match='ordinary_expected_one_run'):
        m.lookup_normal(*public_record)


@pytest.mark.parametrize('sql', [
    "DELETE FROM runs WHERE id='r4'",
    "UPDATE runs SET ordinal=0 WHERE id='r4'",
    "UPDATE runs SET public=0 WHERE id='r4'",
    "UPDATE runs SET object_sha='bad' WHERE id='r4'",
    "UPDATE runs SET object_key='other/team/secret' WHERE id='r4'",
    "UPDATE run_scientific_targets SET task_index=30 WHERE run_id='r4'",
    "UPDATE run_scientific_targets SET task_id='task_47' WHERE run_id='r4'",
    "UPDATE run_scientific_targets SET submission_sha256='bad' WHERE run_id='r4'",
    "UPDATE batches SET kind='infra'",
])
def test_fifth_public_run_cannot_hide_failure(public_record, sql):
    with sqlite3.connect(public_record[0]) as db:
        db.execute(sql)
    with pytest.raises(ValueError):
        m.lookup_normal_runs(*public_record)


def test_duplicate_browser_proof_rejected(public_record):
    public_record[1]['runs'][4] = public_record[1]['runs'][0]
    with pytest.raises(ValueError, match='ordinary_browser_run_count'):
        m.lookup_normal_runs(*public_record)


@pytest.fixture
def native_record(public_record, tmp_path):
    from task_catalog import upgrade_catalog
    database, browser, config, audit = public_record
    source = tmp_path / 'source'
    path = Path(config['science_task_set_path'])
    catalog = json.loads(path.read_text())
    for task in catalog['tasks']:
        folder = source / 'harbor_tasks' / task['task_id']
        folder.mkdir(parents=True)
        (folder / 'task.toml').write_text('[agent]\ntimeout_sec=10800\nsetup_timeout_sec=60\n'
            '[environment]\nbuild_timeout_sec=120\ncpus=2\nmemory_mb=4096\n'
            '[verifier]\ntimeout_sec=300\n')
    catalog = upgrade_catalog(catalog, source)
    raw = json.dumps(catalog, separators=(',', ':'))
    digest = hashlib.sha256(raw.encode()).hexdigest()
    path.write_text(raw)
    config['source_root'] = str(source)
    browser['kind'] = 'infra'
    with sqlite3.connect(database) as db:
        db.execute("UPDATE batches SET kind='infra'")
        db.execute('UPDATE scientific_task_sets SET sha256=?,document=?', (digest, raw))
        db.execute('UPDATE batch_scientific_task_sets SET catalog_sha256=?', (digest,))
        db.execute('UPDATE run_scientific_targets SET catalog_sha256=?', (digest,))
    return public_record


def test_native_infra_is_five_tasks_with_original_limits(native_record):
    rows = m.lookup_normal_runs(*native_record)
    assert len(rows) == 5
    assert all(binding['native_limits']['agent_seconds'] == 10800 and
               binding['agent_budget_seconds'] == 180 and
               binding['acceptance_scope'] == 'practice-native' for _, binding, _ in rows)


def test_native_readback_rejects_changed_original_toml(native_record):
    source = Path(native_record[2]['source_root'])
    (source / 'harbor_tasks/task_00/task.toml').write_text('[agent]\ntimeout_sec=10\n')
    with pytest.raises((ValueError, KeyError)):
        m.lookup_normal_runs(*native_record)


def test_native_infra_cannot_reuse_one_task_proof(native_record):
    native_record[1]['runs'] = native_record[1]['runs'][:1]
    with pytest.raises(ValueError, match='ordinary_browser_run_count'):
        m.lookup_normal_runs(*native_record)


def test_native_inspector_derives_limits_instead_of_trusting_report(native_record, monkeypatch):
    import verify_native_candidate_evidence as checker
    config = native_record[2]
    catalog = json.loads(Path(config['science_task_set_path']).read_text())
    base = {'release_id': catalog['release_id'], 'acceptance_scope': 'bounded-three-minute'}
    monkeypatch.setattr(checker, 'identity', lambda *args: dict(base))
    claimed = {**base, 'acceptance_scope': 'practice-native', 'native_limits': {'agent_seconds': 1}}
    expected = checker.inspection_identity(config, 0, claimed)
    assert expected['native_limits'] == catalog['tasks'][0]['limits']
    assert expected['agent_budget_seconds'] == 180
    assert expected != claimed


@pytest.mark.parametrize('payload,success', [(b'', 0),
    (b'{"action":"qsim_bootstrap_context"}\n', 0),
    (b'{"action":"list_devices","tool":"list_devices","surface":"mcp"}\n', 1)])
def test_archived_qsim_recheck_preserves_absence_of_tool_use(payload, success):
    from verify_native_candidate_evidence import archived_qsim_summary, EvidenceError
    value = {'artifacts/qsim_logs/experiment_log.jsonl': payload,
             'verifier/artifacts/experiment_log.jsonl': payload}
    assert archived_qsim_summary(value)['success_count'] == success
    value['verifier/artifacts/experiment_log.jsonl'] += b'changed'
    with pytest.raises(EvidenceError):
        archived_qsim_summary(value)
