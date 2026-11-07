"""Normal submissions require a current lease and immutable source/visibility binding."""
import hashlib
import json
import sqlite3
import time
from pathlib import Path
import pytest
from native_execution_grant import validate_execution_grant


def fixture(tmp_path):
    ids=[f'task_{i:02d}' for i in range(49)]
    audit=tmp_path/'audit.json';audit.write_text(json.dumps({'competition_task_ids':ids}))
    catalog={'schema':'qiqc-scientific-task-set/v1','release_id':'native-candidate-'+'a'*64,
             'agent_budget_seconds':180,'infra_task_index':48,
             'tasks':[{'task_index':i,'task_id':name,'public':i in (0,26,46,48)} for i,name in enumerate(ids)]}
    raw=json.dumps(catalog,separators=(',',':'));digest=hashlib.sha256(raw.encode()).hexdigest()
    lease={'run_id':'r','batch_id':'b','ordinal':0,'public':1,'attempt':1,'owner':'worker',
           'team_id':'t','version_id':'v','kind':'infra','mode':'scientific','sha256':'c'*64,'attempt_id':'a'}
    request={'lease':lease,'run_id':'r','sha256':'c'*64,'science_task_index':48,'science_task_id':'task_48',
             'science_release_id':catalog['release_id'],'agent_budget_seconds':180,'catalog_sha256':digest}
    with sqlite3.connect(tmp_path/'platform.db') as d:
        d.executescript('''CREATE TABLE runs(id,batch_id,ordinal,public,attempt,owner,lease_until,state);
        CREATE TABLE batches(id,team_id,version_id,kind,mode,status);
        CREATE TABLE versions(id,sha256,status);
        CREATE TABLE attempts(id,run_id,number,state);
        CREATE TABLE run_scientific_targets(run_id,catalog_sha256,submission_sha256,task_index,task_id,public,release_id,agent_budget_seconds);
        CREATE TABLE scientific_task_sets(sha256,document);
        CREATE TABLE batch_scientific_task_sets(batch_id,catalog_sha256);''')
        d.execute('INSERT INTO runs VALUES(?,?,?,?,?,?,?,?)',('r','b',0,1,1,'worker',time.time()+600,'running'))
        d.execute('INSERT INTO batches VALUES(?,?,?,?,?,?)',('b','t','v','infra','scientific','testing'))
        d.execute('INSERT INTO versions VALUES(?,?,?)',('v','c'*64,'approved'))
        d.execute('INSERT INTO attempts VALUES(?,?,?,?)',('a','r',1,'running'))
        d.execute('INSERT INTO run_scientific_targets VALUES(?,?,?,?,?,?,?,?)',('r',digest,'c'*64,48,'task_48',1,catalog['release_id'],180))
        d.execute('INSERT INTO scientific_task_sets VALUES(?,?)',(digest,raw))
        d.execute('INSERT INTO batch_scientific_task_sets VALUES(?,?)',('b',digest))
    return request,{'data':str(tmp_path),'source_audit':str(audit)}


def test_approved_non_allowlisted_submission_authenticated(tmp_path):
    request,cfg=fixture(tmp_path)
    assert validate_execution_grant(request,cfg)['public'] is True


@pytest.mark.parametrize('change',[
    "UPDATE versions SET status='pending_review'", "UPDATE runs SET lease_until=0",
    "UPDATE runs SET owner='other'", "UPDATE attempts SET state='completed'",
    "UPDATE batches SET team_id='other'", "UPDATE run_scientific_targets SET task_index=0",
    "UPDATE run_scientific_targets SET public=0", "UPDATE run_scientific_targets SET submission_sha256='changed'",
    "UPDATE scientific_task_sets SET document='{}'", "UPDATE batch_scientific_task_sets SET catalog_sha256='changed'",
    "UPDATE runs SET ordinal=1", "UPDATE batches SET status='cancelling'",
])
def test_stale_or_mismatched_grants_never_start_cloud(tmp_path,change):
    req,cfg=fixture(tmp_path)
    with sqlite3.connect(tmp_path/'platform.db') as d:d.execute(change)
    with pytest.raises(ValueError):validate_execution_grant(req,cfg)


def test_source_task_replacement_rejected(tmp_path):
    req,cfg=fixture(tmp_path)
    Path(cfg['source_audit']).write_text(json.dumps({'competition_task_ids':['wrong']*49}))
    with pytest.raises(ValueError,match='source_mismatch'):validate_execution_grant(req,cfg)

@pytest.mark.parametrize('formal',[False,True])
def test_native_limits_and_five_public_tasks_are_bound_to_original_config(tmp_path,formal):
    from task_catalog import upgrade_catalog
    req,cfg=fixture(tmp_path)
    cfg['source_root']=str(tmp_path/'source')
    with sqlite3.connect(tmp_path/'platform.db') as d:
        catalog=json.loads(d.execute('SELECT document FROM scientific_task_sets').fetchone()[0])
        catalog['tasks'][17]['public']=True
        for row in catalog['tasks']:
            p=Path(cfg['source_root'])/'harbor_tasks'/row['task_id']/'task.toml';p.parent.mkdir(parents=True)
            p.write_text('[agent]\ntimeout_sec = 10800\nsetup_timeout_sec = 600\n[environment]\nbuild_timeout_sec = 300\ncpus = 2\nmemory_mb = 4096\n[verifier]\ntimeout_sec = 1200\n')
        catalog=upgrade_catalog(catalog,cfg['source_root'])
        raw=json.dumps(catalog,separators=(',',':'));digest=hashlib.sha256(raw.encode()).hexdigest()
        policy={'phase':'competition' if formal else 'practice','formal_execution':{'model':'fixture','gateway_url':'http://127.0.0.1:18789'}}
        pjson=json.dumps(policy)
        d.execute('CREATE TABLE batch_policies(batch_id,policy_json,policy_sha256)')
        d.execute('INSERT INTO batch_policies VALUES(?,?,?)',('b',pjson,hashlib.sha256(pjson.encode()).hexdigest()))
        d.execute('UPDATE scientific_task_sets SET sha256=?,document=?',(digest,raw))
        d.execute('UPDATE batch_scientific_task_sets SET catalog_sha256=?',(digest,))
        budget=10800 if formal else 180
        d.execute('UPDATE run_scientific_targets SET catalog_sha256=?,agent_budget_seconds=?',(digest,budget))
        d.execute('UPDATE runs SET ordinal=4')
    req.update(native_limits=catalog['tasks'][48]['limits'],formal_execution=formal,agent_budget_seconds=budget,catalog_sha256=digest,model_id='fixture',gateway_url='http://127.0.0.1:18789')
    req['lease']['ordinal']=4
    assert validate_execution_grant(req,cfg)['public'] is True
    req['agent_budget_seconds']=300
    with pytest.raises(ValueError):validate_execution_grant(req,cfg)
    req['agent_budget_seconds']=budget
    path=Path(cfg['source_root'])/'harbor_tasks/task_48/task.toml'
    path.write_text(path.read_text().replace('10800','3600'))
    with pytest.raises(ValueError,match='original_task_limits_mismatch'):validate_execution_grant(req,cfg)
