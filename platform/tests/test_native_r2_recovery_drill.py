"""Offline fault-driver acceptance. No services, credentials, cloud, or science."""
import copy
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
import sys
import pytest
sys.path.insert(0,str(Path(__file__).parents[1]/'scripts'))
import native_r2_recovery_drill as drill
from scientific_r2_crash_check import SavedConfig,FaultServer


def counts():return {'attempts':1,'model_calls':4,'provider_admissions':4,'debits':[[-1,'accepted']],'live_tokens':0}
def absence():return {'provider_checked':True,'confirmed_absent':['vm'],'active_native_matches':0}


@pytest.mark.parametrize('fault',['pending','no503','receipt_only','foreign_vm','other_vm','no_model','double_attempt','double_debit'])
def test_kill_guard_rejects_incomplete_or_unrelated_proof(fault):
    errors=[];observed=1;proof=absence();owned={'vm':'destroyed'};usage=counts()
    if fault=='pending':errors=['missing_pending']
    if fault=='no503':observed=0
    if fault=='receipt_only':proof={}
    if fault=='foreign_vm':proof['confirmed_absent']=['other']
    if fault=='other_vm':proof['active_native_matches']=1
    if fault=='no_model':usage['model_calls']=0
    if fault=='double_attempt':usage['attempts']=2
    if fault=='double_debit':usage['debits'].append([-1,'accepted'])
    with pytest.raises(RuntimeError):drill.kill_guard(errors,observed,proof,owned,usage)


def test_valid_kill_guard_requires_all_evidence():
    drill.kill_guard([],1,absence(),{'vm':'destroyed'},counts())


@pytest.mark.parametrize('fault',['model_rerun','unrecorded_provider_call','new_attempt','second_debit','token_live','score','runtime','sha','receipt'])
def test_recovery_rejects_reexecution_or_changed_results(fault):
    before=counts();after=copy.deepcopy(before)
    pending={'bundle_sha256':'a'*64,'reward':0,'seconds':'300.100000'}
    row={'state':'completed','batch_status':'completed','attempt':1,'object_sha':'a'*64,'reward':0,'seconds':'300.100000'}
    receipt='same'
    if fault=='model_rerun':after['model_calls']+=1
    if fault=='unrecorded_provider_call':after['provider_admissions']+=1
    if fault=='new_attempt':after['attempts']=2
    if fault=='second_debit':after['debits'].append([-1,'accepted'])
    if fault=='token_live':after['live_tokens']=1
    if fault=='score':row['reward']=1
    if fault=='runtime':row['seconds']='300.200000'
    if fault=='sha':row['object_sha']='b'*64
    if fault=='receipt':receipt='changed'
    with pytest.raises(RuntimeError):drill.recovery_guard(before,after,pending,row,'same',receipt)


def test_fault_observation_requires_complete_body_identity():
    import threading
    sink=object.__new__(FaultServer);sink.event_lock=threading.Lock()
    key='team/version/batch/run/1.tar.gz'
    path=hashlib.sha256(('/drill-only/'+key).encode()).hexdigest()
    sink.events=[{'path_sha256':path,'body_sha256':'a'*64,'bytes':100,'status':503}]
    assert sink.matching(key,'a'*64,100)==1
    assert sink.matching(key,'a'*64,99)==0
    assert sink.matching(key,'b'*64,100)==0
    assert sink.matching(key.replace('run','other'),'a'*64,100)==0
    sink.events[0]['status']=500
    assert sink.matching(key,'a'*64,100)==0


def test_exact_config_restore_and_no_unreviewed_operator_overwrite(tmp_path):
    target=tmp_path/'worker.env';target.write_bytes(b'private fixture\n');target.chmod(0o600)
    before=target.stat();saved=SavedConfig(target,tmp_path/'backup')
    saved.replace(b'dummy local fault\n');saved.restore()
    after=target.stat()
    assert target.read_bytes()==b'private fixture\n'
    assert (before.st_uid,before.st_gid,before.st_mode)==(after.st_uid,after.st_gid,after.st_mode)
    assert not (tmp_path/'backup').exists()
    saved=SavedConfig(target,tmp_path/'backup');saved.replace(b'dummy\n')
    target.write_bytes(b'concurrent operator value\n')
    with pytest.raises(Exception):saved.restore()
    assert target.read_bytes()==b'concurrent operator value\n'
    assert (tmp_path/'backup').exists()  # recovery retained; never erase evidence


def test_execute_pipeline_kills_once_then_restores_and_checks_same_bundle(tmp_path,monkeypatch):
    """Full controller sequence using isolated fakes, including post-kill503 retry."""
    obj=object.__new__(drill.Drill)
    obj.args=SimpleNamespace(candidate=tmp_path/'candidate',phase_timeout=600,recovery_timeout=120)
    obj.config={'controller_sha256':'c'*64};obj.output=tmp_path;obj.data=tmp_path/'data'
    obj.report={};obj.run_id=None;obj.saved=None;obj.dropin=False;obj.changed=False
    obj.worker_stopped=False;obj.api_stopped=False;obj.api_stop_state={};obj.runtime=None;events=[];state={'restarted':False,'restored':False}
    obj.close_acceptance=lambda:events.append('api-stop')
    obj.api_unit=lambda:{}
    queued={'benchmark_release':{'id':'release'},'batch_id':'batch'}
    browser={**queued,'status':'passed','r2_archive_sha256':'a'*64}
    monkeypatch.setattr(drill,'prepare',lambda *args:{'ready_for_future_exclusive_setup':True,'pending_errors':[],'archive_sha256':'a'*64})
    monkeypatch.setattr(drill,'active_release',lambda cfg:{'release_id':'native-release-fixture'})
    monkeypatch.setattr(drill,'identity',lambda *args:{'id':'release'})
    monkeypatch.setattr(drill,'provider_absence',lambda ids:absence())
    monkeypatch.setattr(drill,'sha_file',lambda *args:('receipt-sha',100))
    obj.install=lambda:events.append('install')
    obj.browser=lambda resume=False:browser if resume else queued
    obj.rows=lambda *args:[{'id':'run'}]
    obj.active=lambda:set() if state['restored'] else {'run'}
    obj.start=lambda:events.append('start')
    pending={'bundle_sha256':'a'*64,'reward':0,'seconds':'300.100000'}
    receipt={'resources':{'vm':'destroyed'}}
    obj.pending=lambda:(pending,receipt,'a'*64,100,'key',1,'receipt-sha')
    obj.counts=counts
    obj.checked_process=lambda:{'MainPID':'9999999','NRestarts':'0'}
    obj.unit=lambda:{'MainPID':'9999998','NRestarts':'1','ActiveState':'active'}
    obj.state=lambda:{'state':'completed' if state['restored'] else 'uploading','attempt':1,
        'batch_status':'completed','object_sha':'a'*64,'reward':0,'seconds':'300.100000','batch_id':'batch'}
    matching=iter([1,2]);obj.fault=SimpleNamespace(matching=lambda *args:next(matching))
    def restore():events.append('restore');state['restored']=True
    obj.restore=restore
    def command(argv,timeout=30):
        assert argv==['systemctl','kill','--kill-whom=all','--signal=SIGKILL',drill.UNIT]
        events.append('kill');state['restarted']=True;return ''
    monkeypatch.setattr(drill,'run',command)
    def immediate(seconds,probe,code):
        value=probe();assert value,code;events.append(code);return value
    monkeypatch.setattr(drill,'wait',immediate)
    def checker(argv,**kwargs):
        events.append('independent-check')
        (tmp_path/'r2-readback.json').write_text(json.dumps({'status':'passed','archive_sha256':'a'*64}))
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(drill.subprocess,'run',checker)
    result=obj.execute()
    assert result['status']=='passed' and result['worker_sigkill_count']==1
    assert events.count('kill')==1
    assert events.index('api-stop')<events.index('start')
    assert events.index('kill')<events.index('native_drill_same_bundle_restart_retry_missing')<events.index('restore')
    assert events.index('restore')<events.index('independent-check')
    assert json.loads((tmp_path/'result.json').read_text())['same_attempt_same_archive_verified']


@pytest.mark.parametrize('fault',['bytes','mode','symlink'])
def test_dropin_compare_and_swap_refuses_operator_changes(tmp_path,fault):
    import stat
    path=tmp_path/'override.conf';path.write_bytes(b'owned');path.chmod(0o644)
    st=path.stat();expected={'bytes':b'owned','metadata':(stat.S_IMODE(st.st_mode),st.st_uid,st.st_gid)}
    if fault=='bytes':path.write_bytes(b'operator-change')
    if fault=='mode':path.chmod(0o600)
    if fault=='symlink':path.unlink();path.symlink_to(tmp_path/'other')
    with pytest.raises((RuntimeError,ValueError)):drill.remove_owned_dropin(path,expected)
    assert path.exists() or path.is_symlink()


def test_retained_config_preserves_rollback_until_all_services_recover(tmp_path):
    target=tmp_path/'worker.env';target.write_bytes(b'private fixture');target.chmod(0o600)
    saved=drill.RetainedConfig(target,tmp_path/'rollback')
    saved.replace(b'fault');saved.restore()
    assert target.read_bytes()==b'private fixture' and saved.backup.exists()
    saved.discard();assert not saved.backup.exists()


def api_object():
    obj=object.__new__(drill.Drill);obj.run_id='owned';obj.api_stopped=False;obj.api_stop_state=None;obj.report={}
    obj.active=lambda:{'owned'}
    obj.save_rollback_state=lambda:None
    return obj


def test_api_stop_failure_never_proves_closed_or_starts_worker(monkeypatch):
    obj=api_object();obj.api_unit=lambda:{'ActiveState':'active','SubState':'running','MainPID':'123'}
    monkeypatch.setattr(drill,'run',lambda *a:None)
    with pytest.raises(RuntimeError,match='stop_unproven'):obj.close_acceptance()
    assert obj.api_stop_state is None and obj.api_stopped


def test_api_restore_rejects_external_state_change(monkeypatch):
    obj=api_object();obj.api_stopped=True
    obj.api_stop_state={'ActiveState':'inactive','MainPID':'0','InactiveEnterTimestampMonotonic':'1'}
    obj.api_unit=lambda:{**obj.api_stop_state,'InactiveEnterTimestampMonotonic':'2'}
    monkeypatch.setattr(drill,'run',lambda *a:pytest.fail('must not overwrite operator service state'))
    with pytest.raises(RuntimeError,match='state_changed'):obj.restore_acceptance()


def test_api_stop_rechecks_unique_batch_after_inflight_requests_drain(monkeypatch):
    obj=api_object();obj.active=lambda:{'owned','foreign'}
    states=iter([{'ActiveState':'active','SubState':'running','MainPID':'123'},
                 {'ActiveState':'inactive','SubState':'dead','MainPID':'0'}])
    obj.api_unit=lambda:next(states);monkeypatch.setattr(drill,'run',lambda *a:None)
    with pytest.raises(RuntimeError,match='queue_not_exclusive'):obj.close_acceptance()
    assert obj.api_stopped and obj.api_stop_state is not None


def test_finally_cleanup_failure_cannot_leave_passed_report(tmp_path,monkeypatch):
    obj=api_object();obj.args=SimpleNamespace(candidate=tmp_path/'candidate')
    obj.config={};obj.output=tmp_path;obj.saved=True;obj.dropin=True;obj.changed=True
    obj.worker_stopped=True;obj.runtime=tmp_path/'runtime'
    monkeypatch.setattr(drill,'prepare',lambda *args:{'ready_for_future_exclusive_setup':True})
    monkeypatch.setattr(drill,'active_release',lambda *args:{'release_id':'release-fixture'})
    def interrupted():
        obj.report['status']='passed'  # even an earlier incorrect optimistic state must be cleared
        raise KeyboardInterrupt()
    obj.install=interrupted
    def rollback_failed():raise RuntimeError('operator-changed-config')
    obj.restore=rollback_failed
    with pytest.raises(RuntimeError,match='operator-changed'):obj.execute()
    report=json.loads((tmp_path/'result.json').read_text())
    assert report['status']=='failed' and report['rollback_requires_operator']
    assert report['fault_config_remaining'] and report['worker_recovery_pending']
    assert report['cleanup_verified'] is False


def test_config_permission_drift_keeps_backup_for_operator(tmp_path):
    target=tmp_path/'worker.env';target.write_bytes(b'private fixture');target.chmod(0o600)
    saved=drill.RetainedConfig(target,tmp_path/'rollback');saved.replace(b'fault')
    target.chmod(0o400)
    with pytest.raises(RuntimeError,match='metadata_changed'):saved.restore()
    assert saved.backup.exists() and target.stat().st_mode&0o777==0o400


def test_durable_rollback_metadata_excludes_credentials(tmp_path):
    obj=api_object();obj.output=tmp_path;obj.saved=SimpleNamespace(original=b'never-print-credential')
    obj.worker_stopped=True;obj.changed=True;obj.dropin_expected={'bytes':b'[Service]\nEnvironment=SSL_CERT_FILE=/run/example/ca.pem\n','metadata':(0o644,0,0)}
    obj.runtime=tmp_path/'runtime';obj.api_stopped=True
    drill.Drill.save_rollback_state(obj)
    path=tmp_path/'rollback-state.json';value=json.loads(path.read_text())
    assert path.stat().st_mode&0o777==0o600
    assert value['api_recovery_pending'] and value['worker_recovery_pending']
    assert value['config_backup']==str(tmp_path/'worker-r2.backup')
    assert 'never-print-credential' not in path.read_text()
