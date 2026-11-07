#!/usr/bin/env python3
"""Explicit native one-CZ 503/SIGKILL recovery drill. Default: read-only preflight.

Never run alongside other acceptance. --execute temporarily closes the native API
after the dedicated browser submission, changes the worker's local R2 endpoint/CA,
and kills that worker once after independently
confirmed VM absence AND a matching complete HTTP503 upload. No DB writes,
science reruns, lease edits, remote bucket changes, or automatic reconciliation.
"""
from __future__ import annotations
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import stat
import sqlite3
import ssl
import subprocess
import sys
import tempfile
import threading
import time

HERE=Path(__file__).resolve().parent
sys.path.insert(0,str(HERE/'science'))
from native_candidate import active_release,load_candidate,identity
from native_r2_recovery_preflight import ACTIVE,prepare,validate_pending_native
from native_release_record import read_regular
from scientific_r2_crash_check import SavedConfig,FaultServer,sha_file,atomic_private
from native_recovery_audit import reject_path_links

UNIT='qiqc-native-worker-key1.service'
API_UNIT='qiqc-native-api.service'
DROPIN=Path('/run/systemd/system')/f'{UNIT}.d/90-qiqc-native-r2-drill.conf'


class RetainedConfig(SavedConfig):
    """Keep the short rollback material until service and local cleanup succeed."""
    def check_metadata(self):
        reject_path_links(self.path)
        info=self.path.lstat()
        need(stat.S_ISREG(info.st_mode) and (stat.S_IMODE(info.st_mode),info.st_uid,info.st_gid)==(self.mode,self.uid,self.gid),
             'native_drill_config_metadata_changed_by_operator')

    def replace(self,value):
        self.check_metadata();super().replace(value)

    def restore(self):
        self.check_metadata()
        info=self.path.lstat()
        need(stat.S_ISREG(info.st_mode) and self.path.read_bytes() in (self.original,self.replacement),
             'native_drill_config_changed_by_operator')
        atomic_private(self.path,self.original,self.mode,self.uid,self.gid)
        info=self.path.stat()
        need(self.path.read_bytes()==self.original and (stat.S_IMODE(info.st_mode),info.st_uid,info.st_gid)==(self.mode,self.uid,self.gid),
             'native_drill_config_restore_mismatch')

    def discard(self):
        self.backup.unlink();self.backup.with_suffix('.metadata.json').unlink()


def remove_owned_dropin(path,expected):
    reject_path_links(path)
    info=path.lstat()
    need(stat.S_ISREG(info.st_mode) and (stat.S_IMODE(info.st_mode),info.st_uid,info.st_gid)==expected['metadata']
         and path.read_bytes()==expected['bytes'],'native_drill_dropin_changed_by_operator')
    path.unlink()


def need(ok,code):
    if not ok:raise RuntimeError(code)


def private_json(path,value):
    fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
    with os.fdopen(fd,'w') as stream:
        json.dump(value,stream,indent=2);stream.write('\n');stream.flush();os.fsync(stream.fileno())


def run(argv,timeout=30):
    result=subprocess.run(argv,capture_output=True,timeout=timeout,check=False)
    need(result.returncode==0,'native_drill_command_failed')
    need(len(result.stdout)<=1024*1024,'native_drill_command_output_limit')
    return result.stdout.decode()


def wait(seconds,probe,code):
    deadline=time.monotonic()+seconds
    while time.monotonic()<deadline:
        result=probe()
        if result:return result
        time.sleep(1)
    raise RuntimeError(code)


def provider_absence(resource_ids):
    """Read-only provider proof, never kill/reconcile resources in this helper."""
    from e2b import Sandbox,SandboxQuery
    from e2b.api.client.models.sandbox_state import SandboxState
    from e2b_runtime import load_key
    key=load_key();need(len(resource_ids)==1,'native_drill_one_vm_required')
    for resource in resource_ids:
        try:Sandbox.get_info(resource,api_key=key)
        except Exception as exc:
            need(type(exc).__name__ in ('NotFoundException','SandboxNotFoundException'),'native_drill_provider_absence_unproven')
        else:raise RuntimeError('native_drill_vm_still_exists')
    pages=Sandbox.list(api_key=key,query=SandboxQuery(metadata={'purpose':'qiqc-native-candidate'},
        state=[SandboxState.RUNNING,SandboxState.PAUSED]),limit=100)
    matches=0
    while pages.has_next:matches+=len(pages.next_items())
    need(matches==0,'native_drill_provider_other_native_vm_exists')
    return {'provider_checked':True,'confirmed_absent':sorted(resource_ids),'active_native_matches':0}


def kill_guard(errors,observed,absence,resource_ids,counts):
    need(not errors,'native_drill_pending_contract_failed')
    need(type(observed) is int and observed>0,'native_drill_full_body_503_not_observed')
    need(absence.get('provider_checked') is True and absence.get('active_native_matches')==0
        and absence.get('confirmed_absent')==sorted(resource_ids) and len(resource_ids)==1,
        'native_drill_provider_absence_unproven')
    need(counts['attempts']==1 and counts['model_calls']>0 and counts['debits']==[[-1,'accepted']],
        'native_drill_original_execution_or_quota_invalid')


def recovery_guard(before,after,pending,row,receipt_before,receipt_after):
    need(row['state']=='completed' and row['attempt']==1 and row['batch_status']=='completed','native_drill_not_completed_same_attempt')
    need(row['object_sha']==pending['bundle_sha256'] and row['reward']==pending['reward']
         and str(row['seconds'])==pending['seconds'],'native_drill_recovered_result_changed')
    need(after['attempts']==before['attempts']==1 and after['model_calls']==before['model_calls']
         and after['provider_admissions']==before['provider_admissions']
         and after['debits']==before['debits']==[[-1,'accepted']] and after['live_tokens']==0,
         'native_drill_execution_or_quota_repeated')
    need(receipt_before==receipt_after,'native_drill_resource_receipt_changed')


class Drill:
    def __init__(self,args):
        self.args=args;self.config,_=load_candidate(args.candidate)
        self.data=Path(self.config['data']);self.output=args.output
        self.run_id=None;self.saved=None;self.fault=None;self.runtime=None;self.dropin=False
        self.worker_stopped=False;self.changed=False;self.thread=None
        self.api_stopped=False;self.api_stop_state=None;self.dropin_expected=None
        self.report={'schema':'qiqc-native-r2-recovery-drill/v1','status':'not_run',
            'driver_sha256':sha_file(Path(__file__),1024*1024)[0],
            'controller_sha256':self.config['controller_sha256'],'worker_sigkill_count':0,
            'fault_injected':False,'science_reexecution_authorized':False}

    def rows(self,sql,params=()):
        with sqlite3.connect((self.data/'platform.db').resolve().as_uri()+'?mode=ro',uri=True) as db:
            db.row_factory=sqlite3.Row
            return [dict(row) for row in db.execute(sql,params)]

    def save_rollback_state(self):
        # Durable operator metadata survives SIGKILL/power loss, when finally
        # cannot run. Credential bytes remain only in the separate 0600 backup.
        expected=self.dropin_expected
        value={'schema':'qiqc-native-r2-rollback/v1','run_id':self.run_id,
               'api_original_state':self.report.get('api_original_state'),
               'api_stop_state':self.api_stop_state,'api_recovery_pending':self.api_stopped,
               'worker_original_state':self.report.get('worker_original_state'),
               'worker_recovery_pending':bool(self.worker_stopped or self.changed),
               'config_backup':str(self.output/'worker-r2.backup') if self.saved else None,
               'dropin_path':str(DROPIN),'dropin_expected':None if expected is None else {
                   'bytes_utf8':expected['bytes'].decode(),'metadata':expected['metadata']},
               'runtime_path':str(self.runtime) if self.runtime else None}
        path=reject_path_links(self.output/'rollback-state.json')
        atomic_private(path,(json.dumps(value,indent=2)+'\n').encode(),0o600,os.getuid(),os.getgid())

    def active(self):return {r['id'] for r in self.rows('SELECT id FROM runs WHERE '+ACTIVE)}

    def state(self):
        rows=self.rows('SELECT r.*,b.status batch_status,b.team_id,b.version_id,b.mode,b.kind,v.sha256 submission_sha256 FROM runs r JOIN batches b ON b.id=r.batch_id JOIN versions v ON v.id=b.version_id WHERE r.id=?',(self.run_id,))
        need(len(rows)==1,'native_drill_run_missing');return rows[0]

    def unit(self):
        props=run(['systemctl','show',UNIT,'-p','MainPID','-p','NRestarts','-p','ActiveState','-p','SubState'])
        return dict(line.split('=',1) for line in props.splitlines() if '=' in line)

    def api_unit(self):
        props=run(['systemctl','show',API_UNIT,'-p','MainPID','-p','ActiveState','-p','SubState',
                   '-p','InvocationID','-p','ActiveEnterTimestampMonotonic','-p','InactiveEnterTimestampMonotonic'])
        return dict(line.split('=',1) for line in props.splitlines() if '=' in line)

    def close_acceptance(self):
        before=self.api_unit()
        need(before['ActiveState']=='active' and before['SubState']=='running' and int(before['MainPID'])>1,
             'native_drill_api_initial_state_invalid')
        self.report['api_original_state']=before
        # Mark responsibility before systemctl; a timeout can still have stopped it.
        self.api_stopped=True
        self.save_rollback_state()
        run(['systemctl','stop',API_UNIT])
        stopped=self.api_unit()
        need(stopped['ActiveState']=='inactive' and stopped['SubState']=='dead' and int(stopped['MainPID'])==0,
             'native_drill_api_stop_unproven')
        self.api_stop_state=stopped
        self.save_rollback_state()
        self.report['acceptance_closed_for_exclusive_drill']=True
        need(self.active()=={self.run_id},'native_drill_queue_not_exclusive_after_api_stop')

    def restore_acceptance(self):
        if not self.api_stopped:return
        need(self.api_stop_state is not None and self.api_unit()==self.api_stop_state,
             'native_drill_api_state_changed_requires_operator')
        run(['systemctl','start',API_UNIT])
        wait(30,lambda:self.api_unit()['ActiveState']=='active','native_drill_api_restore_timeout')
        value=self.api_unit()
        need(value['SubState']=='running' and int(value['MainPID'])>1,'native_drill_api_restore_unproven')
        self.api_stopped=False;self.report['api_original_running_state_restored']=True
        self.save_rollback_state()

    def checked_process(self):
        info=self.unit();pid=int(info['MainPID'])
        need(info['ActiveState']=='active' and info['SubState']=='running' and pid>1,'native_drill_worker_not_running')
        # /proc/PID/exe is a kernel symlink by design; verify its actual bytes,
        # not a caller-controlled on-disk link accepted by sha_file.
        with open(f'/proc/{pid}/exe','rb') as executable:
            raw=executable.read(64*1024*1024+1)
        need(len(raw)<=64*1024*1024 and hashlib.sha256(raw).hexdigest()==sha_file(Path(self.config['binaries'])/'worker')[0],
             'native_drill_worker_binary_mismatch')
        need(UNIT in Path(f'/proc/{pid}/cgroup').read_text(),'native_drill_worker_cgroup_mismatch')
        argv=Path(f'/proc/{pid}/cmdline').read_bytes().decode().split('\0')
        need('-data' in argv and argv[argv.index('-data')+1]==str(self.data),
             'native_drill_worker_data_mismatch')
        return info

    def start(self):
        run(['systemctl','start',UNIT]);self.worker_stopped=False
        wait(30,lambda:self.unit()['ActiveState']=='active','native_drill_start_timeout')
        self.checked_process()

    def counts(self):
        return {'attempts':self.rows('SELECT count(*) n FROM attempts WHERE run_id=?',(self.run_id,))[0]['n'],
            'model_calls':self.rows('SELECT count(*) n FROM usage WHERE run_id=?',(self.run_id,))[0]['n'],
            'provider_admissions':self.rows('SELECT COALESCE(sum(used),0) n FROM model_admissions WHERE run_id=?',(self.run_id,))[0]['n'],
            'live_tokens':self.rows('SELECT count(*) n FROM run_tokens WHERE run_id=? AND revoked=0',(self.run_id,))[0]['n'],
            'debits':[[r['delta'],r['action']] for r in self.rows('SELECT delta,action FROM ledger WHERE batch_id=?',(self.state()['batch_id'],))]}

    def browser(self,resume=False):
        dest=self.output/('browser-result.json' if resume else 'browser-queued.json')
        command=[sys.executable,str(HERE/'cloud_browser_check.py'),'--candidate',str(self.args.candidate),
                 '--catalog-index','48','--require-ai-advice','--output',str(dest)]
        command+=['--resume-queued-report',str(self.output/'browser-queued.json')] if resume else ['--queue-only']
        with dest.with_suffix('.log').open('xb') as log:
            result=subprocess.run(command,stdout=log,stderr=log,timeout=600,check=False)
        need(result.returncode==0,'native_drill_browser_failed')
        return json.loads(read_regular(dest))

    def install(self):
        need(not self.active(),'native_drill_not_idle')
        self.report['worker_original_state']=self.checked_process()
        env=run(['systemctl','show',UNIT,'--property=Environment','--value'])
        need(not any(k+'=' in env for k in ('R2_ENDPOINT','R2_ACCESS_KEY_ID','R2_SECRET_ACCESS_KEY','R2_BUCKET','SSL_CERT_FILE')),
             'native_drill_direct_env_override')
        config_path=reject_path_links(Path(self.config['candidate_root'])/'config/worker-r2.env')
        env_files=run(['systemctl','show',UNIT,'--property=EnvironmentFiles','--value'])
        need(str(config_path) in env_files,'native_drill_worker_config_not_bound')
        need(not DROPIN.exists() and not DROPIN.is_symlink(),'native_drill_dropin_exists')
        self.worker_stopped=True;self.save_rollback_state()
        run(['systemctl','stop',UNIT])
        need(self.unit()['ActiveState']=='inactive' and not self.active(),'native_drill_stop_not_idle')
        self.runtime=Path(tempfile.mkdtemp(prefix='qiqc-native-r2-drill-',dir='/run'));self.runtime.chmod(0o755)
        cert=self.runtime/'cert.pem';key=self.runtime/'key.pem'
        run(['openssl','req','-x509','-newkey','rsa:2048','-nodes','-days','1','-subj','/CN=127.0.0.1',
             '-addext','subjectAltName=IP:127.0.0.1','-keyout',str(key),'-out',str(cert)])
        key.chmod(0o600);cert.chmod(0o644)
        system_ca=ssl.get_default_verify_paths().cafile;need(bool(system_ca),'native_drill_system_ca_missing')
        ca=self.runtime/'ca.pem';ca.write_bytes(Path(system_ca).read_bytes()+b'\n'+cert.read_bytes());ca.chmod(0o644)
        self.fault=FaultServer(cert,key);self.thread=threading.Thread(target=self.fault.serve_forever,daemon=True);self.thread.start()
        self.saved=RetainedConfig(config_path,self.output/'worker-r2.backup')
        self.save_rollback_state()
        DROPIN.parent.mkdir(parents=True,exist_ok=True)
        reject_path_links(DROPIN)
        fd=os.open(DROPIN,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o644);self.dropin=True
        content=('[Service]\nEnvironment=SSL_CERT_FILE='+str(ca)+'\n').encode()
        info=os.fstat(fd);self.dropin_expected={'bytes':content,'metadata':(stat.S_IMODE(info.st_mode),info.st_uid,info.st_gid)}
        with os.fdopen(fd,'wb') as stream:
            self.save_rollback_state()
            stream.write(content);stream.flush();os.fsync(stream.fileno())
        self.saved.replace((f'R2_ENDPOINT=https://127.0.0.1:{self.fault.server_port}\nR2_ACCESS_KEY_ID=drill-only\n'
            'R2_SECRET_ACCESS_KEY=drill-only-not-a-cloud-secret\nR2_BUCKET=drill-only\n').encode())
        self.changed=True;run(['systemctl','daemon-reload']);self.report['fault_injected']=True
        self.save_rollback_state()

    def pending(self):
        need(self.active()=={self.run_id},'native_drill_not_exclusive')
        row=self.state();need(row['state'] in ('queued','running','uploading'),'native_drill_science_failed')
        cache=self.data/'cache'/self.run_id/'1'
        if row['state']!='uploading' or not (cache/'pending.json').exists():return None
        pending=json.loads(read_regular(cache/'pending.json',65536))
        receipt_path=self.data/'scientific'/f'{self.run_id}_1.json'
        receipt=json.loads(read_regular(receipt_path,65536));sha,size=sha_file(cache/'rollout.tar.gz')
        targets=self.rows('SELECT * FROM batch_scientific_targets WHERE batch_id=?',(row['batch_id'],))
        need(len(targets)==1 and targets[0]['release_id']==self.release_id,'native_drill_release_binding_changed')
        attempts=self.rows('SELECT id,number,state FROM attempts WHERE run_id=? ORDER BY number',(self.run_id,))
        errors=validate_pending_native(row,pending,receipt,targets[0],sha,attempts,list(self.active()))
        need(not errors,'native_drill_pending_contract_failed')
        key='/'.join([row['team_id'],row['version_id'],row['batch_id'],self.run_id,'1.tar.gz'])
        observed=self.fault.matching(key,sha,size)
        if not observed:return None
        return pending,receipt,sha,size,key,observed,sha_file(receipt_path,65536)[0]

    def restore(self):
        if self.saved:self.saved.restore();self.report['config_exactly_restored']=True
        if self.dropin:remove_owned_dropin(DROPIN,self.dropin_expected);self.dropin=False
        if self.changed or self.worker_stopped:
            need(self.active()<=({self.run_id} if self.run_id else set()),'native_drill_restore_other_work_requires_operator')
            run(['systemctl','daemon-reload'])
            run(['systemctl','restart',UNIT]);self.worker_stopped=False;self.changed=False
            wait(30,lambda:self.unit()['ActiveState']=='active','native_drill_restore_restart_timeout')
            self.checked_process()
        if self.fault:
            self.fault.shutdown();self.fault.server_close();self.thread.join(5)
            need(not self.thread.is_alive(),'native_drill_fault_server_cleanup_unproven')
            self.fault=None
        if self.runtime:shutil.rmtree(self.runtime);self.runtime=None
        self.restore_acceptance()
        if self.saved:self.saved.discard();self.saved=None
        (self.output/'rollback-state.json').unlink(missing_ok=True)

    def execute(self):
        before=prepare(self.args.candidate)
        need(before['ready_for_future_exclusive_setup'],'native_drill_requires_idle_active_release')
        self.release_id=active_release(self.config)['release_id']
        self.report['release_id']=self.release_id
        self.report['status']='failed'
        try:
            self.install();queued=self.browser()
            need(queued['benchmark_release']==identity(self.config,48,self.release_id),'native_drill_queue_release_mismatch')
            rows=self.rows('SELECT id FROM runs WHERE batch_id=?',(queued['batch_id'],));need(len(rows)==1,'native_drill_batch_not_single')
            self.run_id=rows[0]['id'];self.report['run_id']=self.run_id
            need(self.active()=={self.run_id},'native_drill_queue_not_exclusive')
            self.close_acceptance();self.start()
            pending,receipt,sha,size,key,observed,receipt_sha=wait(self.args.phase_timeout,self.pending,'native_drill_no_full_pending_503_no_kill')
            resources=receipt['resources'];absence=provider_absence(resources);counts=self.counts()
            kill_guard([],observed,absence,resources,counts)
            # Recheck disk/DB/controller immediately before the single kill.
            pre=prepare(self.args.candidate,self.run_id)
            need(not pre.get('pending_errors') and pre.get('archive_sha256')==sha
                 and active_release(self.config)['release_id']==self.release_id,'native_drill_pre_kill_changed')
            need(self.api_unit()==self.api_stop_state,'native_drill_acceptance_reopened_before_kill')
            old=self.checked_process();old_pid=int(old['MainPID']);restart=int(old['NRestarts'])
            self.report.update(archive_sha256=sha,archive_bytes=size,provider_absence_before=absence,
                observed_full_body_503=observed,counts_before=counts,attempt=1)
            run(['systemctl','kill','--kill-whom=all','--signal=SIGKILL',UNIT]);self.report['worker_sigkill_count']=1
            def restarted():
                value=self.unit()
                return (value['ActiveState']=='active' and int(value['MainPID']) not in (0,old_pid)
                    and int(value['NRestarts'])>restart and not Path(f'/proc/{old_pid}').exists())
            wait(45,restarted,'native_drill_automatic_restart_unproven');self.checked_process()
            baseline=self.fault.matching(key,sha,size)
            wait(90,lambda:self.fault.matching(key,sha,size)>baseline,'native_drill_same_bundle_restart_retry_missing')
            need(self.state()['attempt']==1 and self.counts()==counts,'native_drill_execution_repeated_during_outage')
            self.restore()
            wait(self.args.recovery_timeout,lambda:self.state()['state']=='completed','native_drill_real_r2_recovery_timeout')
            row=self.state();after=self.counts()
            recovery_guard(counts,after,pending,row,receipt_sha,sha_file(self.data/'scientific'/f'{self.run_id}_1.json',65536)[0])
            need(not self.active(),'native_drill_active_work_after_recovery')
            wait(30,lambda:not (self.data/'cache'/self.run_id/'1').exists(),'native_drill_cache_remaining')
            final_absence=provider_absence(resources)
            browser=self.browser(resume=True)
            need(browser['batch_id']==row['batch_id'] and browser['r2_archive_sha256']==sha and browser['status']=='passed','native_drill_browser_result_mismatch')
            r2=self.output/'r2-readback.json'
            with (self.output/'r2-readback.log').open('xb') as log:
                checked=subprocess.run([sys.executable,str(HERE/'science/verify_native_candidate_evidence.py'),
                    '--candidate',str(self.args.candidate),'--report',str(self.output/'browser-result.json'),'--output',str(r2)],
                    stdout=log,stderr=log,timeout=180,check=False)
            need(checked.returncode==0,'native_drill_independent_r2_check_failed')
            checked=json.loads(read_regular(r2));need(checked['status']=='passed' and checked['archive_sha256']==sha,'native_drill_readback_identity_mismatch')
            self.report.update(status='passed',counts_after=after,provider_absence_after=final_absence,
                same_attempt_same_archive_verified=True,independent_r2_verified=True,original_browser_result_verified=True)
        except BaseException:
            self.report['status']='failed'
            raise
        finally:
            try:self.restore()
            except BaseException:
                self.report['status']='failed'
                self.report['rollback_requires_operator']=True
                raise
            finally:
                self.report['fault_config_remaining']=bool(self.saved or self.dropin or self.changed)
                self.report['api_recovery_pending']=self.api_stopped
                self.report['temporary_runtime_remaining']=self.runtime is not None
                self.report['worker_recovery_pending']=bool(self.worker_stopped or self.changed)
                self.report['cleanup_verified']=not any(self.report[key] for key in
                    ('fault_config_remaining','api_recovery_pending','temporary_runtime_remaining','worker_recovery_pending'))
                if self.report.get('rollback_requires_operator'):self.report['cleanup_verified']=False
                private_json(self.output/'result.json',self.report)
        return self.report


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--candidate',type=Path,required=True)
    p.add_argument('--execute',action='store_true');p.add_argument('--output',type=Path)
    p.add_argument('--phase-timeout',type=int,default=1800);p.add_argument('--recovery-timeout',type=int,default=300)
    args=p.parse_args()
    if not args.execute:print(json.dumps(prepare(args.candidate),indent=2));return
    need(os.geteuid()==0 and args.output is not None,'native_drill_root_explicit_output_required')
    need(600<=args.phase_timeout<=2700 and 120<=args.recovery_timeout<=600,'native_drill_timeout_bounds')
    need(args.candidate.resolve()==Path('/opt/qiqc-native-candidate/candidate.json'),'native_drill_isolated_candidate_only')
    args.output=reject_path_links(args.output);args.output.mkdir(mode=0o700,parents=False)
    lock_path=reject_path_links(Path('/run/lock/qiqc-native-r2-drill.lock'))
    lock_fd=os.open(lock_path,os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW,0o600)
    with os.fdopen(lock_fd,'a') as lock:
        info=os.fstat(lock.fileno())
        need(stat.S_ISREG(info.st_mode) and info.st_uid==0 and stat.S_IMODE(info.st_mode)==0o600,'native_drill_lock_not_private_owned')
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        def interrupted(*_):raise RuntimeError('native_drill_operator_interrupted')
        signal.signal(signal.SIGTERM,interrupted)
        Drill(args).execute()

if __name__=='__main__':main()
