#!/usr/bin/env python3
"""Local native-release switch transaction; no deployment or cloud task creation.

activate stops only native DB writers, keeps one short-lived recovery file and
uses the trusted Go registration CLI. finalize removes recovery data only after
an independently checked post-activation browser/R2 CZ run. Rollback changes the
active registration only: it never restores a stale DB over newer run/audit data.
"""
from __future__ import annotations
import argparse
import contextlib
import fcntl
import grp
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import tempfile
import time
from pathlib import Path
from native_candidate import active_release,identity,load_candidate
from native_freeze_gate import runtime_evidence_errors
from native_release_record import acceptance_evidence_errors,canonical,load_release,read_regular,validate_binding

SERVICES=tuple('qiqc-native-'+role+'.service' for role in ('api','worker','review','gateway'))
LOST='worker_lost:reconciliation_required'


def sha(data):return hashlib.sha256(data).hexdigest()


def execute(run,argv,allowed=(0,),env=None):
    kwargs={'capture_output':True,'text':True,'timeout':60,'check':False}
    if env is not None:kwargs['env']=env
    result=run(argv,**kwargs)
    if result.returncode not in allowed:raise RuntimeError('native_activation_command_failed:'+Path(argv[0]).name)
    return result


def atomic(path,data,mode=0o600,gid=None):
    path=Path(path)
    if path.is_symlink():raise ValueError('activation_symlink_forbidden')
    fd,name=tempfile.mkstemp(prefix='.'+path.name+'-',dir=path.parent)
    try:
        with os.fdopen(fd,'wb') as out:
            out.write(data);out.flush();os.fsync(out.fileno())
            os.fchmod(out.fileno(),mode)
            if gid is not None:os.fchown(out.fileno(),0,gid)
        os.replace(name,path)
        parent=os.open(path.parent,os.O_RDONLY)
        try:os.fsync(parent)
        finally:os.close(parent)
    finally:
        if os.path.exists(name):os.unlink(name)


def database_state(config):
    db=Path(config['data'])/'platform.db'
    if db.is_symlink():raise ValueError('activation_database_symlink_forbidden')
    with sqlite3.connect(db.resolve().as_uri()+'?mode=ro',uri=True) as connection:
        candidate=connection.execute("SELECT value FROM settings WHERE key='scientific_candidate'").fetchone()
        active=connection.execute("SELECT value FROM settings WHERE key='scientific_active_release'").fetchone()
        busy=connection.execute("SELECT count(*) FROM runs WHERE state IN ('queued','running','uploading') OR (state='attention' AND error=?)",(LOST,)).fetchone()[0]
    if not candidate or candidate[0]!=config['candidate_id']:raise ValueError('activation_candidate_database_mismatch')
    return {'active':active[0] if active else '', 'busy':busy}


def journal_path(config):return Path(config['candidate_root'])/'.native-release-activation'


def append_audit(config,event):
    path=Path(config['candidate_root'])/'release-activation-audit.jsonl'
    fd=os.open(path,os.O_WRONLY|os.O_APPEND|os.O_CREAT|os.O_NOFOLLOW,0o600)
    with os.fdopen(fd,'ab') as out:
        out.write(canonical({'time':time.time(),**event})+b'\n');out.flush();os.fsync(out.fileno())
    parent=os.open(path.parent,os.O_DIRECTORY)
    try:os.fsync(parent)
    finally:os.close(parent)


def verified_audit(config,release_id,browser_sha,r2_sha):
    """Find the existing completion, without treating leftover directories as proof."""
    path=Path(config['candidate_root'])/'release-activation-audit.jsonl'
    if not path.exists():return None
    matches=[]
    for line in read_regular(path,16*1024*1024).splitlines():
        event=json.loads(line)
        if event.get('action')=='activation_verified' and event.get('release_id')==release_id:
            matches.append(event)
    if not matches:return None
    # A different post-switch report cannot silently replace the durable one.
    if any(event.get('browser_sha256')!=browser_sha or event.get('r2_report_sha256')!=r2_sha
           or event.get('short_term_recovery_files_removed') is not True
           or not isinstance(event.get('actor'),str) or not event['actor'] for event in matches):
        raise ValueError('activation_completed_audit_report_mismatch')
    return matches[-1]


def save_journal(directory,journal):atomic(directory/'transaction.json',canonical(journal)+b'\n')


@contextlib.contextmanager
def locked(config):
    path=Path(config['candidate_root'])/'.native-release-activation.lock'
    fd=os.open(path,os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW,0o600)
    try:
        fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        yield
    finally:os.close(fd)


def install_release(config,release_path):
    release=validate_binding(config,load_release(release_path))
    parent=Path(config['candidate_root'])/'releases'
    if parent.is_symlink():raise ValueError('activation_release_directory_symlink')
    parent.mkdir(mode=0o700,exist_ok=True)
    dest=parent/release['release_id'];source=Path(release_path)
    if not dest.exists():
        temp=Path(tempfile.mkdtemp(prefix='.install-',dir=parent))
        try:
            for name in [*release['payload']['files'],'release.json']:
                target=temp/name;target.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
                atomic(target,read_regular(source.parent/name),0o440)
            load_release(temp/'release.json');temp.rename(dest)
        finally:
            if temp.exists():shutil.rmtree(temp)
    if sha(read_regular(dest/'release.json'))!=sha(read_regular(source)):
        raise ValueError('activation_existing_release_differs')
    load_release(dest/'release.json')
    science=grp.getgrnam('qiqc-native-science').gr_gid
    for path in [parent,dest,*dest.rglob('*')]:
        if path.is_symlink():raise ValueError('activation_release_symlink')
        os.chown(path,0,science);path.chmod(0o550 if path.is_dir() else 0o440)
    return dest/'release.json',release


def set_pointer(config,raw):
    pointer=Path(config['candidate_root'])/'active-release.json'
    if raw is None:
        if pointer.is_symlink():raise ValueError('activation_pointer_symlink')
        pointer.unlink(missing_ok=True)
    else:atomic(pointer,raw,0o640,grp.getgrnam('qiqc-native-data').gr_gid)


def services(run,action):
    execute(run,['systemctl',action,*SERVICES])
    expected=0 if action=='start' else 3
    for name in SERVICES:
        state=execute(run,['systemctl','is-active','--quiet',name],allowed=(0,3))
        if state.returncode!=expected:raise RuntimeError('native_services_not_'+action+'ed')


def cli(config,run,release_id,actor,reason,record_sha='',rollback=False):
    argv=[str(Path(config['binaries'])/'native-release-activate'),'-data',config['data'],
          '-release-id',release_id,'-candidate-id',config['candidate_id'],'-actor',actor,'-reason',reason]
    argv+=['-rollback'] if rollback else ['-manifest-sha256',record_sha]
    # This trusted operator explicitly opts into the existing shared setgid data
    # directory. Preserve inherited settings; never print the child environment.
    execute(run,argv,env={**os.environ,'QIQC_SHARED_DATA':'1'})


def recover(config,journal,run):
    if journal.get('phase')=='verified_cleanup_pending':
        raise ValueError('activation_already_verified_retry_finalize')
    directory=journal_path(config)
    services(run,'stop')
    state=database_state(config)
    if state['busy']:raise RuntimeError('activation_rollback_requires_drained_execution')
    if state['active'] not in (journal['previous_release_id'],journal['release_id']):
        raise RuntimeError('activation_rollback_database_changed')
    if state['active']!=journal['previous_release_id']:
        cli(config,run,journal['previous_release_id'],journal['actor'],'rollback: '+journal['reason'],rollback=True)
    previous=read_regular(directory/'previous-pointer.json') if journal['had_previous_pointer'] else None
    set_pointer(config,previous)
    services(run,'start')
    if database_state(config)['active']!=journal['previous_release_id']:
        raise RuntimeError('activation_rollback_readback_mismatch')
    append_audit(config,{'action':'activation_rolled_back','release_id':journal['release_id'],
                         'actor':journal['actor'],'reason':journal['reason'],'database_history_preserved':True})
    shutil.rmtree(directory)


def activate(config,plan,actor,reason,run=subprocess.run):
    if not actor.strip() or not reason.strip():raise ValueError('activation_actor_reason_required')
    root=Path(config['candidate_root']);pointer=root/'active-release.json';directory=journal_path(config)
    if plan.get('active_pointer')!=str(pointer.absolute()) or plan.get('data')!=config['data']:
        raise ValueError('activation_plan_location_mismatch')
    if any(plan.get(key)!=config[key] for key in ('candidate_id','controller_sha256')):
        raise ValueError('activation_plan_candidate_or_controller_changed')
    with locked(config):
        if directory.exists():raise ValueError('activation_transaction_already_pending')
        current=read_regular(pointer) if pointer.exists() else None
        if (sha(current) if current is not None else None)!=plan['expected_previous_pointer_sha256']:
            raise ValueError('activation_pointer_changed_since_preparation')
        if current is not None:active_release(config)
        source=Path(plan['release_file'])
        if sha(read_regular(source))!=plan['release_sha256']:raise ValueError('activation_release_record_changed')
        path,release=install_release(config,source)
        if release['release_id']!=plan['release_id']:raise ValueError('activation_release_id_mismatch')
        state=database_state(config)
        if state['busy']:raise RuntimeError('activation_requires_drained_execution')
        previous=json.loads(current)['release_id'] if current is not None else ''
        if state['active']!=previous:raise ValueError('activation_previous_pointer_database_disagree')
        if previous==release['release_id']:raise ValueError('release_already_active')
        # Refuse to disturb a partially started platform; restoration assumes all
        # four existing native writers were healthy before this transaction.
        for name in SERVICES:execute(run,['systemctl','is-active','--quiet',name])
        directory.mkdir(mode=0o700)
        journal={'schema':'qiqc-native-activation-transaction/v1','release_id':release['release_id'],
                 'release_path':str(path),'release_sha256':sha(read_regular(path)),
                 'previous_release_id':previous,'had_previous_pointer':current is not None,
                 'actor':actor,'reason':reason,'started_at':time.time(),'phase':'prepared'}
        if current is not None:atomic(directory/'previous-pointer.json',current)
        save_journal(directory,journal)
        try:
            services(run,'stop')
            if database_state(config)['busy']:raise RuntimeError('activation_execution_started_during_drain')
            db=Path(config['data'])/'platform.db'
            with sqlite3.connect(db.resolve().as_uri()+'?mode=ro',uri=True) as src, sqlite3.connect(directory/'database-recovery.sqlite') as dst:
                src.backup(dst)
                if dst.execute('PRAGMA integrity_check').fetchone()!=('ok',):raise RuntimeError('activation_recovery_file_invalid')
            (directory/'database-recovery.sqlite').chmod(0o600)
            journal.update(phase='database_recovery_verified',recovery_sha256=sha(read_regular(directory/'database-recovery.sqlite',1024*1024*1024)))
            save_journal(directory,journal)
            cli(config,run,release['release_id'],actor,reason,journal['release_sha256'])
            journal['phase']='database_committed';save_journal(directory,journal)
            proposed={'release_id':release['release_id'],'release_path':str(path),'release_sha256':journal['release_sha256']}
            set_pointer(config,canonical(proposed)+b'\n')
            if database_state(config)['active']!=release['release_id']:raise RuntimeError('activation_database_readback_mismatch')
            services(run,'start')
            journal['phase']='awaiting_post_activation_evidence';save_journal(directory,journal)
            append_audit(config,{'action':'activation_awaiting_verification','release_id':release['release_id'],'actor':actor,'reason':reason})
            return {'status':journal['phase'],'release_id':release['release_id'],'recovery_retained':True}
        except Exception:
            try:recover(config,journal,run)
            except Exception:
                journal['phase']='rollback_required';save_journal(directory,journal)
                append_audit(config,{'action':'activation_rollback_required','release_id':journal['release_id'],'actor':actor})
            raise


def finalize(config,browser_path,r2_path):
    with locked(config):
        directory=journal_path(config)
        if directory.is_symlink() or (directory.exists() and not directory.is_dir()):
            raise ValueError('activation_cleanup_directory_invalid')
        browser_bytes=read_regular(browser_path);evidence_bytes=read_regular(r2_path)
        pointer=json.loads(read_regular(Path(config['candidate_root'])/'active-release.json'))
        completed=verified_audit(config,pointer.get('release_id'),sha(browser_bytes),sha(evidence_bytes))
        journal_file=directory/'transaction.json'
        if journal_file.exists():journal=json.loads(read_regular(journal_file))
        else:
            if not completed:raise ValueError('activation_cleanup_completion_not_proven')
            # rmtree may have removed the journal before interruption. Recover
            # identity only from durable completion + the current pointer; all
            # release, DB, browser and R2 checks below still run on this retry.
            journal={**pointer,'phase':'verified_cleanup_pending','actor':completed['actor']}
        if journal['phase'] not in ('awaiting_post_activation_evidence','verified_cleanup_pending'):
            raise ValueError('activation_not_awaiting_evidence')
        release=validate_binding(config,load_release(journal['release_path']))
        if release['release_id']!=journal['release_id'] or sha(read_regular(journal['release_path']))!=journal['release_sha256']:
            raise ValueError('activation_current_release_digest_mismatch')
        if pointer!={k:journal[k] for k in ('release_id','release_path','release_sha256')} or database_state(config)['active']!=journal['release_id']:
            raise ValueError('activation_current_pointer_database_mismatch')
        browser=json.loads(browser_bytes);evidence=json.loads(evidence_bytes)
        task=release['payload']['tasks'].get('tunable_coupler_cz_netzero')
        if not task:raise ValueError('activation_cz_not_in_release')
        ordinal=task['task_index'];expected=identity(config,ordinal,journal['release_id'])
        for value in (browser,evidence):
            if value.get('status')!='passed' or value.get('catalog_index')!=ordinal or value.get('benchmark_release')!=expected:
                raise ValueError('activation_post_switch_identity_or_result_invalid')
        if evidence.get('schema')!='qiqc-native-candidate-evidence/v1' or evidence.get('browser_report_sha256')!=sha(browser_bytes):
            raise ValueError('activation_post_switch_report_changed')
        if (release['payload']['acceptance_scope']=='49-browser-bounded-five-minute' and evidence.get('acceptance_scope')!='bounded-five-minute') or runtime_evidence_errors(evidence) or acceptance_evidence_errors(evidence) or any(evidence.get(key) is not True for key in ('snapshot_build_inputs_verified','actual_uploaded_zip_executed','native_compose_execution_verified','private_network_verified','standard_harbor_rollout')):
            raise ValueError('activation_post_switch_runtime_evidence_missing')
        if any(browser.get(key) is not True for key in ('admin_target_bound_via_browser','invitation_login_password_change','result_visible_in_browser','own_hidden_trace_denied','other_identity_trace_denied','r2_index_written','all_execution_sandboxes_destroyed','local_rollout_cache_removed','nonranking')):
            raise ValueError('activation_post_switch_browser_evidence_missing')
        controller=json.loads(read_regular(Path(journal['release_path']).parent/'controller-manifest.json'))
        checker=controller['host_files']['competition-platform/platform/scripts/science/verify_native_candidate_evidence.py']
        if evidence.get('checker_sha256')!=checker or evidence.get('controller_sha256')!=config['controller_sha256'] or evidence.get('build_input_digests')!={k:config[k] for k in ('build_plan_sha256','build_receipt_sha256')}:
            raise ValueError('activation_post_switch_checker_or_build_mismatch')
        if ((evidence.get('model_response_evidence') or {}).get('complete_vs_incomplete_distinction_verified') is not True
            or browser.get('batch_id')!=evidence.get('batch_id') or browser.get('r2_archive_sha256')!=evidence.get('archive_sha256')
            or browser.get('submission_sha256')!=evidence.get('submission_sha256')
            or not isinstance(evidence.get('run_id'),str) or not evidence['run_id']
            or evidence['run_id'] in {row['run_id'] for row in release['payload']['tasks'].values()}
            or evidence.get('batch_id') in {row['batch_id'] for row in release['payload']['tasks'].values()}):
            raise ValueError('activation_post_switch_new_run_not_proven')
        audit_event={'action':'activation_verified','release_id':journal['release_id'],'actor':journal['actor'],
            'browser_report':str(Path(browser_path).resolve()),'browser_sha256':sha(browser_bytes),
            'r2_report':str(Path(r2_path).resolve()),'r2_report_sha256':sha(evidence_bytes),
            'short_term_recovery_files_removed':True,'database_history_preserved':True}
        # Keep the transaction journal until the completion audit is durable.
        # A full disk or interrupted audit append must leave finalize retryable,
        # including when the sensitive recovery payloads were already removed.
        if not completed:
            journal['phase']='verified_cleanup_pending'
            save_journal(directory,journal)
            for name in ('database-recovery.sqlite','previous-pointer.json'):
                (directory/name).unlink(missing_ok=True)
            directory_fd=os.open(directory,os.O_DIRECTORY)
            try:os.fsync(directory_fd)
            finally:os.close(directory_fd)
            append_audit(config,audit_event)
        if directory.exists():shutil.rmtree(directory)
        parent_fd=os.open(directory.parent,os.O_DIRECTORY)
        try:os.fsync(parent_fd)
        finally:os.close(parent_fd)
        return {'status':'activated_and_verified','release_id':journal['release_id'],'recovery_retained':False,
                'old_staging_admission_stopped_by_this_tool':False}


def main():
    parser=argparse.ArgumentParser(description=__doc__);sub=parser.add_subparsers(dest='action',required=True)
    activate_cmd=sub.add_parser('activate')
    for key in ('candidate','plan'):activate_cmd.add_argument('--'+key,type=Path,required=True)
    for key in ('actor','reason'):activate_cmd.add_argument('--'+key,required=True)
    finalize_cmd=sub.add_parser('finalize')
    for key in ('candidate','browser-report','r2-report'):finalize_cmd.add_argument('--'+key,type=Path,required=True)
    rollback_cmd=sub.add_parser('rollback');rollback_cmd.add_argument('--candidate',type=Path,required=True)
    args=parser.parse_args()
    if os.geteuid()!=0:raise SystemExit('Local root operator required')
    config,_=load_candidate(args.candidate)
    if Path(config['candidate_root']).resolve()!=Path('/opt/qiqc-native-candidate'):
        raise SystemExit('Only isolated local native installation is supported')
    if args.action=='activate':result=activate(config,json.loads(read_regular(args.plan)),args.actor,args.reason)
    elif args.action=='finalize':result=finalize(config,args.browser_report,args.r2_report)
    else:
        with locked(config):
            journal=json.loads(read_regular(journal_path(config)/'transaction.json'));recover(config,journal,subprocess.run)
        result={'status':'rolled_back','database_history_preserved':True}
    print(json.dumps(result))


if __name__=='__main__':main()
