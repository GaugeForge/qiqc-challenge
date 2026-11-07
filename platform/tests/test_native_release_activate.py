"""Offline transaction and failure recovery tests; no services/cloud are touched."""
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
import pytest
sys.path.insert(0,str(Path(__file__).parents[1]/'scripts/science'))
import native_release_activate as activation
from native_release import prepare_activation
from native_candidate import identity
from test_native_release import fixture,freeze,sha


class FakeSystem:
    def __init__(self,cfg):self.cfg=cfg;self.active=True;self.commands=[];self.fail_start=False;self.fail_cli_after_commit=False
    def run(self,argv,**kwargs):
        self.commands.append(argv)
        if argv[0]=='systemctl':
            if argv[1]=='is-active':return SimpleNamespace(returncode=0 if self.active else 3,stdout='')
            if argv[1]=='stop':self.active=False
            if argv[1]=='start':
                self.active=True
                if self.fail_start:
                    self.fail_start=False
                    with sqlite3.connect(Path(self.cfg['data'])/'platform.db') as db:
                        db.execute("INSERT INTO audit VALUES('review_after_restart')")
                    return SimpleNamespace(returncode=1,stdout='')
            return SimpleNamespace(returncode=0,stdout='')
        assert Path(argv[0]).name=='native-release-activate'
        assert kwargs['env']['QIQC_SHARED_DATA']=='1'
        target=argv[argv.index('-release-id')+1];rollback='-rollback' in argv
        with sqlite3.connect(Path(self.cfg['data'])/'platform.db') as db:
            if target:db.execute("INSERT INTO settings VALUES('scientific_active_release',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(target,))
            else:db.execute("DELETE FROM settings WHERE key='scientific_active_release'")
            if not rollback:db.execute('INSERT OR IGNORE INTO settings VALUES(?,?)',('native_release:'+target,'registered'))
            db.execute('INSERT INTO audit VALUES(?)',('rollback' if rollback else 'activate',))
        if self.fail_cli_after_commit and not rollback:
            self.fail_cli_after_commit=False
            return SimpleNamespace(returncode=1,stdout='')
        return SimpleNamespace(returncode=0,stdout='{}')


@pytest.fixture
def activation_fixture(fixture,monkeypatch):
    cfg,*_=fixture;release=freeze(fixture)
    monkeypatch.setattr(activation.grp,'getgrnam',lambda _:SimpleNamespace(gr_gid=0))
    data=Path(cfg['data']);data.mkdir()
    with sqlite3.connect(data/'platform.db') as db:
        db.executescript('CREATE TABLE settings(key TEXT PRIMARY KEY,value TEXT);CREATE TABLE runs(state TEXT,error TEXT);CREATE TABLE audit(action TEXT);')
        db.execute("INSERT INTO settings VALUES('scientific_candidate',?)",(cfg['candidate_id'],))
        db.execute("INSERT INTO audit VALUES('old_history')")
    pointer=Path(cfg['candidate_root'])/'active-release.json'
    return cfg,release,prepare_activation(cfg,release,pointer),FakeSystem(cfg),fixture


def test_actual_transaction_keeps_recovery_until_new_browser_r2_pass(activation_fixture):
    cfg,release,plan,system,fixture=activation_fixture
    result=activation.activate(cfg,plan,'operator','test transaction',system.run)
    assert result['status']=='awaiting_post_activation_evidence'
    directory=activation.journal_path(cfg)
    assert (directory/'database-recovery.sqlite').is_file()
    pointer=json.loads((Path(cfg['candidate_root'])/'active-release.json').read_text())
    assert Path(pointer['release_path']).is_relative_to(Path(cfg['candidate_root'])/'releases')
    assert activation.database_state(cfg)['active']==result['release_id']
    # New post-publication browser and independently checked R2 pair.
    root=fixture[2];browser=json.loads((root/'task-48.json').read_text());evidence=json.loads((root/'task-48-r2.json').read_text())
    expected=identity(cfg,48,result['release_id'])
    browser.update(benchmark_release=expected,batch_id='post-switch-batch',r2_archive_sha256='9'*64)
    bp=root/'post-switch.json';bp.write_text(json.dumps(browser))
    evidence.update(benchmark_release=expected,batch_id=browser['batch_id'],run_id='post-switch-run',archive_sha256=browser['r2_archive_sha256'],browser_report_sha256=sha(bp.read_bytes()))
    ep=root/'post-switch-r2.json';ep.write_text(json.dumps(evidence))
    out=activation.finalize(cfg,bp,ep)
    assert out['status']=='activated_and_verified'
    assert not directory.exists()
    assert release.exists() and Path(pointer['release_path']).exists()
    with sqlite3.connect(Path(cfg['data'])/'platform.db') as db:
        assert db.execute('SELECT action FROM audit').fetchall()==[('old_history',),('activate',)]


@pytest.mark.parametrize('failure',['start','cli_after_commit'])
def test_rollback_preserves_new_audit_and_registered_release(activation_fixture,failure):
    cfg,_,plan,system,_=activation_fixture
    system.fail_start=failure=='start';system.fail_cli_after_commit=failure=='cli_after_commit'
    with pytest.raises(RuntimeError):activation.activate(cfg,plan,'operator','intentional failure',system.run)
    assert activation.database_state(cfg)['active']==''
    assert not (Path(cfg['candidate_root'])/'active-release.json').exists()
    assert not activation.journal_path(cfg).exists()
    with sqlite3.connect(Path(cfg['data'])/'platform.db') as db:
        events=[v[0] for v in db.execute('SELECT action FROM audit')]
        assert events[0]=='old_history' and 'activate' in events and events[-1]=='rollback'
        if failure=='start':assert 'review_after_restart' in events
        assert db.execute("SELECT count(*) FROM settings WHERE key LIKE 'native_release:%'").fetchone()[0]==1


def test_busy_execution_and_stale_pointer_never_stop_services(activation_fixture):
    cfg,_,plan,system,_=activation_fixture
    with sqlite3.connect(Path(cfg['data'])/'platform.db') as db:db.execute("INSERT INTO runs VALUES('running','')")
    with pytest.raises(RuntimeError,match='drained'):activation.activate(cfg,plan,'operator','test',system.run)
    assert not system.commands
    pointer=Path(cfg['candidate_root'])/'active-release.json';pointer.write_text('{}')
    with pytest.raises(ValueError,match='changed_since'):activation.activate(cfg,plan,'operator','test',system.run)
    assert not system.commands


def test_candidate_or_incomplete_post_switch_evidence_cannot_clear_recovery(activation_fixture):
    cfg,_,plan,system,fixture=activation_fixture
    activation.activate(cfg,plan,'operator','test',system.run)
    root=fixture[2]
    with pytest.raises(ValueError,match='post_switch_identity'):activation.finalize(cfg,root/'task-48.json',root/'task-48-r2.json')
    assert (activation.journal_path(cfg)/'database-recovery.sqlite').is_file()


def test_finalize_audit_failure_keeps_retryable_journal(activation_fixture,monkeypatch):
    cfg,_,plan,system,fixture=activation_fixture
    result=activation.activate(cfg,plan,'operator','audit fault fixture',system.run)
    root=fixture[2]
    browser=json.loads((root/'task-48.json').read_text())
    evidence=json.loads((root/'task-48-r2.json').read_text())
    expected=identity(cfg,48,result['release_id'])
    browser.update(benchmark_release=expected,batch_id='audit-fault-batch',r2_archive_sha256='9'*64)
    bp=root/'audit-fault.json';bp.write_text(json.dumps(browser))
    evidence.update(benchmark_release=expected,batch_id=browser['batch_id'],run_id='audit-fault-run',
                    archive_sha256=browser['r2_archive_sha256'],browser_report_sha256=sha(bp.read_bytes()))
    ep=root/'audit-fault-r2.json';ep.write_text(json.dumps(evidence))
    append=activation.append_audit
    def fail_audit(*_):raise OSError('synthetic audit write failure')
    monkeypatch.setattr(activation,'append_audit',fail_audit)
    with pytest.raises(OSError,match='synthetic audit'):
        activation.finalize(cfg,bp,ep)
    directory=activation.journal_path(cfg)
    assert json.loads((directory/'transaction.json').read_text())['phase']=='verified_cleanup_pending'
    assert not (directory/'database-recovery.sqlite').exists()
    assert activation.database_state(cfg)['active']==result['release_id']
    commands_before=len(system.commands)
    with pytest.raises(ValueError,match='retry_finalize'):
        activation.recover(cfg,json.loads((directory/'transaction.json').read_text()),system.run)
    assert len(system.commands)==commands_before
    # Retry still revalidates the actual evidence, rather than trusting the phase.
    original=ep.read_bytes();changed=json.loads(original);changed['status']='failed'
    ep.write_text(json.dumps(changed))
    with pytest.raises(ValueError,match='post_switch_identity'):
        activation.finalize(cfg,bp,ep)
    assert directory.is_dir()
    ep.write_bytes(original)
    monkeypatch.setattr(activation,'append_audit',append)
    assert activation.finalize(cfg,bp,ep)['status']=='activated_and_verified'
    assert not directory.exists()
    events=[json.loads(line) for line in (Path(cfg['candidate_root'])/'release-activation-audit.jsonl').read_text().splitlines()]
    assert [event['action'] for event in events].count('activation_verified')==1


def test_real_go_cli_shared_directory_activation_and_rollback(tmp_path,monkeypatch):
    """Compile the operator CLI and use only a new throwaway local database."""
    monkeypatch.delenv('QIQC_SHARED_DATA',raising=False)
    monkeypatch.setenv('QIQC_ACTIVATION_TEST_SENTINEL','retained')
    binaries=tmp_path/'bin';binaries.mkdir()
    go_root=Path(__file__).resolve().parents[2]/'platform-go'
    built=subprocess.run(['go','build','-o',str(binaries/'native-release-activate'),'./cmd/native-release-activate'],
                         cwd=go_root,capture_output=True,text=True,timeout=120,check=False)
    assert built.returncode==0,'throwaway operator CLI build failed'
    shared=tmp_path/'shared';shared.mkdir();shared.chmod(0o2770)
    cfg={'binaries':str(binaries),'data':str(shared),'candidate_id':'native-candidate-'+'a'*64}
    release='native-release-'+'b'*64
    argv=[str(binaries/'native-release-activate'),'-data',str(shared),'-release-id',release,
          '-candidate-id',cfg['candidate_id'],'-manifest-sha256','c'*64,'-actor','test','-reason','isolated integration fixture']
    # Negative control demonstrates the real shared-directory failure: no DB is
    # opened or initialized without the explicit shared-data contract.
    blocked=subprocess.run(argv,capture_output=True,text=True,timeout=30,check=False)
    assert blocked.returncode!=0 and not (shared/'platform.db').exists()
    # The first opted-in open initializes the real schema, then correctly rejects
    # activation before this fixture has a registered candidate.
    initialized=subprocess.run(argv,capture_output=True,text=True,timeout=30,check=False,
                               env={**os.environ,'QIQC_SHARED_DATA':'1'})
    assert initialized.returncode!=0 and (shared/'platform.db').is_file()
    with sqlite3.connect(shared/'platform.db') as db:
        db.execute("INSERT INTO settings(key,value) VALUES('scientific_candidate',?)",(cfg['candidate_id'],))
    observed=[]
    def checked_run(command,**kwargs):
        assert kwargs['env']['QIQC_SHARED_DATA']=='1'
        assert kwargs['env']['QIQC_ACTIVATION_TEST_SENTINEL']=='retained'
        observed.append('-rollback' in command)
        return subprocess.run(command,**kwargs)
    activation.cli(cfg,checked_run,release,'test','activate isolated fixture','c'*64)
    assert activation.database_state(cfg)['active']==release
    activation.cli(cfg,checked_run,'','test','rollback isolated fixture',rollback=True)
    assert activation.database_state(cfg)['active']==''
    assert observed==[False,True]
    assert shared.stat().st_mode&0o2770==0o2770
    with sqlite3.connect(shared/'platform.db') as db:
        assert db.execute("SELECT count(*) FROM settings WHERE key LIKE 'native_release:%'").fetchone()[0]==1
        actions={row[0] for row in db.execute('SELECT action FROM audit')}
        assert 'native_release_activated' in actions and 'native_release_rollback' in actions


@pytest.mark.parametrize('change',[
    {'qsim_successful_interaction_verified':False},{'qsim_progress_sha256':None},
    {'reward':True},{'agent_stop_reason':'acceptance_early_stop','acceptance_early_stop_verified':True,'acceptance_readiness_sha256':None},
])
def test_post_activation_invalid_functional_proof_keeps_recovery(activation_fixture,change):
    cfg,_,plan,system,fixture=activation_fixture
    result=activation.activate(cfg,plan,'operator','test',system.run)
    root=fixture[2];browser=json.loads((root/'task-48.json').read_text());evidence=json.loads((root/'task-48-r2.json').read_text())
    expected=identity(cfg,48,result['release_id'])
    browser.update(benchmark_release=expected,batch_id='post-switch-batch',r2_archive_sha256='9'*64)
    bp=root/'post-switch.json';bp.write_text(json.dumps(browser))
    evidence.update(benchmark_release=expected,batch_id=browser['batch_id'],run_id='post-switch-run',archive_sha256=browser['r2_archive_sha256'],browser_report_sha256=sha(bp.read_bytes()))
    evidence.update(change);ep=root/'post-switch-r2.json';ep.write_text(json.dumps(evidence))
    with pytest.raises(ValueError,match='runtime_evidence_missing'):activation.finalize(cfg,bp,ep)
    assert (activation.journal_path(cfg)/'database-recovery.sqlite').is_file()


def completion_reports(cfg,fixture,release_id):
    root=fixture[2]
    browser=json.loads((root/'task-48.json').read_text())
    evidence=json.loads((root/'task-48-r2.json').read_text())
    browser.update(benchmark_release=identity(cfg,48,release_id),batch_id='cleanup-retry-batch',r2_archive_sha256='8'*64)
    bp=root/'cleanup-retry.json';bp.write_text(json.dumps(browser))
    evidence.update(benchmark_release=browser['benchmark_release'],batch_id=browser['batch_id'],run_id='cleanup-retry-run',
                    archive_sha256=browser['r2_archive_sha256'],browser_report_sha256=sha(bp.read_bytes()))
    ep=root/'cleanup-retry-r2.json';ep.write_text(json.dumps(evidence))
    return bp,ep


@pytest.mark.parametrize('cleanup_fault',['before_delete','after_journal_delete','after_directory_delete'])
def test_finalize_completion_audit_recovers_interrupted_cleanup(activation_fixture,monkeypatch,cleanup_fault):
    cfg,_,plan,system,fixture=activation_fixture
    result=activation.activate(cfg,plan,'operator','cleanup fault',system.run)
    bp,ep=completion_reports(cfg,fixture,result['release_id'])
    directory=activation.journal_path(cfg);real_rmtree=activation.shutil.rmtree
    def interrupted(path,*args,**kwargs):
        assert Path(path)==directory
        if cleanup_fault=='after_journal_delete':(directory/'transaction.json').unlink()
        if cleanup_fault=='after_directory_delete':real_rmtree(path,*args,**kwargs)
        raise OSError('synthetic cleanup interruption')
    monkeypatch.setattr(activation.shutil,'rmtree',interrupted)
    with pytest.raises(OSError,match='cleanup interruption'):activation.finalize(cfg,bp,ep)
    audit_path=Path(cfg['candidate_root'])/'release-activation-audit.jsonl'
    durable_audit=audit_path.read_bytes()
    assert sum(json.loads(line)['action']=='activation_verified' for line in durable_audit.splitlines())==1
    assert activation.database_state(cfg)['active']==result['release_id']
    assert not (directory/'database-recovery.sqlite').exists()
    # Even a syntactically valid alternate report cannot reuse prior completion.
    original=ep.read_bytes();changed=json.loads(original);changed['run_id']='alternate-run'
    ep.write_text(json.dumps(changed))
    with pytest.raises(ValueError,match='completed_audit_report_mismatch'):activation.finalize(cfg,bp,ep)
    assert audit_path.read_bytes()==durable_audit
    ep.write_bytes(original)
    # Completion audit is not sufficient when the actual DB pointer has changed.
    with sqlite3.connect(Path(cfg['data'])/'platform.db') as db:
        db.execute("UPDATE settings SET value='wrong-release' WHERE key='scientific_active_release'")
    with pytest.raises(ValueError,match='current_pointer_database_mismatch'):activation.finalize(cfg,bp,ep)
    with sqlite3.connect(Path(cfg['data'])/'platform.db') as db:
        db.execute("UPDATE settings SET value=? WHERE key='scientific_active_release'",(result['release_id'],))
    commands_before=list(system.commands)
    monkeypatch.setattr(activation.shutil,'rmtree',real_rmtree)
    assert activation.finalize(cfg,bp,ep)['status']=='activated_and_verified'
    assert not directory.exists()
    assert audit_path.read_bytes()==durable_audit  # no rewrites or duplicate events
    assert system.commands==commands_before       # no rollback or service operation


def test_empty_cleanup_directory_without_completed_audit_is_not_proof(activation_fixture):
    cfg,_,plan,system,fixture=activation_fixture
    result=activation.activate(cfg,plan,'operator','unproven cleanup',system.run)
    bp,ep=completion_reports(cfg,fixture,result['release_id'])
    directory=activation.journal_path(cfg)
    for path in directory.iterdir():path.unlink()
    with pytest.raises(ValueError,match='cleanup_completion_not_proven'):activation.finalize(cfg,bp,ep)
    assert directory.is_dir()
