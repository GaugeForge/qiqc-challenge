#!/usr/bin/env python3
"""Start only a restored Go API, behind independent network/filesystem namespaces."""
from __future__ import annotations
import argparse
import hashlib
import json
import os
import secrets
import signal
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path
from native_recovery_audit import copy_checked,db_check,sha,reject_path_links,safe_error

MINIMAL_ENV={'PATH':'/usr/sbin:/usr/bin:/sbin:/bin','LANG':'C.UTF-8'}


def descendants(pid):
    found=[];pending=[pid]
    while pending:
        current=pending.pop()
        if current in found:continue
        found.append(current)
        try:pending.extend(int(v) for v in Path(f'/proc/{current}/task/{current}/children').read_text().split())
        except FileNotFoundError:pass
    return found


def verify_api_process(parent_pid,root):
    # Identify the actual executable by inode, not a bwrap supervisor or argv.
    matches=[]
    for pid in descendants(parent_pid):
        try:
            if os.path.samefile(f'/proc/{pid}/exe',root/'api'):matches.append(pid)
        except (FileNotFoundError,PermissionError):pass
    if len(matches)!=1:raise ValueError('restore_api_process_identity_failed')
    pid=matches[0];view=Path(f'/proc/{pid}/root')
    if not os.path.samefile(view/'work/data/platform.db',root/'data/platform.db'):
        raise ValueError('restore_api_database_view_mismatch')
    if any((view/path).exists() for path in ('root/QQ-Challenge','opt/qiqc-native-candidate')):
        raise ValueError('restore_api_live_files_visible')
    for namespace in ('mnt','pid','user'):
        if os.readlink(f'/proc/{pid}/ns/{namespace}')==os.readlink(f'/proc/self/ns/{namespace}'):
            raise ValueError('restore_api_namespace_not_isolated')
    if os.readlink(f'/proc/{pid}/ns/net')!=os.readlink('/proc/self/ns/net'):
        raise ValueError('restore_api_network_namespace_mismatch')
    database_fds=[]
    for fd in Path(f'/proc/{pid}/fd').iterdir():
        try:
            if os.path.samefile(fd,root/'data/platform.db'):database_fds.append(fd.name)
        except (FileNotFoundError,OSError):pass
    if not database_fds:raise ValueError('restore_api_database_descriptor_missing')
    return pid


def stop_group(process):
    # Kill the entire probe group on both normal completion and outer timeout.
    try:os.killpg(process.pid,signal.SIGTERM)
    except ProcessLookupError:pass
    try:process.wait(timeout=8)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid,signal.SIGKILL);process.wait(timeout=5)


def process_running(pid):
    try:return Path(f'/proc/{pid}/stat').read_text().rsplit(')',1)[1].split()[0] not in ('Z','X')
    except FileNotFoundError:return False


def bwrap_command(root,port):
    return ['/usr/bin/bwrap','--die-with-parent','--unshare-user','--unshare-pid','--unshare-ipc','--unshare-uts',
        '--cap-drop','ALL','--ro-bind','/usr','/usr','--ro-bind','/lib','/lib','--ro-bind','/lib64','/lib64',
        '--proc','/proc','--dev','/dev','--tmpfs','/tmp','--bind',str(root),'/work','--chdir','/work',
        '--clearenv','--setenv','PATH','/usr/bin:/bin','--setenv','HOME','/tmp','--',
        '/work/api','-standalone','-listen',f'127.0.0.1:{port}','-data','/work/data',
        '-backend','http://127.0.0.1:1','-site','/work/empty','-web','/work/empty',
        '-adapters','/work/empty','-reference','/work/empty/reference.zip','-scanner','/work/empty/scanner.py']


def query(base,path,token=None):
    request=urllib.request.Request(base+path,headers={'Cookie':'qiqc_session='+token} if token else {})
    with urllib.request.urlopen(request,timeout=4) as response:
        if response.status!=200:raise ValueError('restore_api_http_failed')
        return json.load(response)


def compare_account(actual,expected):
    if any(actual.get(k)!=expected[k] for k in ('id','team_id','quotas')):raise ValueError('restore_api_account_quota_mismatch')


def compare_batch(actual,expected,version_sha):
    if actual.get('id')!=expected['id'] or actual.get('version_id')!=expected['version_id'] or actual.get('sha256')!=version_sha:
        raise ValueError('restore_api_batch_version_mismatch')
    state='testing' if expected['status']=='attention' else expected['status']
    if actual.get('status')!=state:raise ValueError('restore_api_batch_state_mismatch')
    if state=='completed':
        score=actual.get('score') or {}
        if score.get('passed')!=expected['passed'] or score.get('mean_seconds')!=expected['mean']:
            raise ValueError('restore_api_result_mismatch')


def inside(root):
    # This trusted probe is entered only by unshare --net. It launches the Go
    # binary in a second mount/user/PID namespace with no live workspace mounts.
    subprocess.run(['/usr/sbin/ip','link','set','lo','up'],check=True,capture_output=True)
    links=json.loads(subprocess.check_output(['/usr/sbin/ip','-j','link','show']))
    if [v['ifname'] for v in links]!=['lo']:raise ValueError('restore_api_network_not_isolated')
    if json.loads(subprocess.check_output(['/usr/sbin/ip','-j','route','show'])):raise ValueError('restore_api_route_present')
    data=root/'data';database=data/'platform.db';token=secrets.token_hex(32)
    with sqlite3.connect(database) as db:
        db.row_factory=sqlite3.Row
        user=db.execute("SELECT u.id,u.team_id FROM users u WHERE u.role='team' AND u.must_change=0 AND EXISTS(SELECT 1 FROM batches b WHERE b.team_id=u.team_id AND b.status='completed') ORDER BY u.id LIMIT 1").fetchone()
        if not user:raise ValueError('restore_api_no_completed_team_fixture')
        uid,team=user['id'],user['team_id'];expected={'id':uid,'team_id':team,'quotas':{'full':1,'public':3,'infra':10}}
        for kind,delta in db.execute('SELECT kind,sum(delta) FROM ledger WHERE team_id=? GROUP BY kind',(team,)):expected['quotas'][kind]=expected['quotas'].get(kind,0)+delta
        versions={r['id']:r['sha256'] for r in db.execute('SELECT id,sha256 FROM versions WHERE team_id=?',(team,))}
        batches=[dict(r) for r in db.execute('SELECT id,version_id,status,passed,mean FROM batches WHERE team_id=?',(team,))]
        # Synthetic session exists only in the throwaway restored database.
        # Authentication delivery/password recovery is explicitly not tested.
        db.execute('DELETE FROM sessions')
        db.execute('INSERT INTO sessions(hash,user_id,expires,ip) VALUES(?,?,?,?)',(hashlib.sha256(token.encode()).hexdigest(),uid,time.time()+300,'127.0.0.1'))
        baseline={table:db.execute('SELECT count(*) FROM '+table).fetchone()[0] for table in ('batches','runs','ledger','usage','audit')}
    with socket.socket() as sock:sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
    binary=root/'api';binary.chmod(0o700)
    process=None;api_pid=None
    with (root/'api-private.log').open('wb') as log:
        try:
            process=subprocess.Popen(bwrap_command(root,port),env=MINIMAL_ENV,stdout=log,stderr=log,start_new_session=True)
            base=f'http://127.0.0.1:{port}'
            for _ in range(60):
                if process.poll() is not None:raise ValueError('restore_api_process_failed')
                try:
                    health=query(base,'/healthz')
                    if health.get('backend')=='go-native':break
                except (OSError,ValueError):pass
                time.sleep(.1)
            else:raise ValueError('restore_api_start_timeout')
            # Check the actual process filesystem view, not only bwrap intent.
            api_pid=verify_api_process(process.pid,root)
            compare_account(query(base,'/api/me',token),expected)
            actual_versions=query(base,'/api/versions',token)
            if {v['id']:v['sha256'] for v in actual_versions}!=versions:raise ValueError('restore_api_versions_mismatch')
            actual_batches=query(base,'/api/batches',token)
            if {v['id'] for v in actual_batches}!={b['id'] for b in batches}:raise ValueError('restore_api_batches_mismatch')
            for batch in batches:compare_batch(query(base,'/api/batches/'+batch['id'],token),batch,versions[batch['version_id']])
            with sqlite3.connect(database) as db:
                if any(db.execute('SELECT count(*) FROM '+table).fetchone()[0]!=count for table,count in baseline.items()):raise ValueError('restore_api_business_queries_mutated_rows')
            return {'passed':True,'restored_go_api_started':True,'health_verified':True,'account_verified':True,'quota_verified':True,
                'version_count_verified':len(versions),'batch_count_verified':len(batches),'completed_scores_verified':sum(b['status']=='completed' for b in batches),
                'business_http_methods':['GET'],'business_row_counts_unchanged':True,'network_interfaces':['lo'],'default_routes':0,
                'actual_api_pid_verified':True,'restored_database_descriptor_verified':True,'api_process_stopped':True,
                'live_workspace_visible_to_api':False,'worker_started':False,'cloud_credentials_loaded':False,'synthetic_recovery_only_session':True}
        finally:
            if process:stop_group(process)
            if api_pid:
                for _ in range(50):
                    if not process_running(api_pid):break
                    time.sleep(.02)
                else:raise ValueError('restore_api_process_cleanup_failed')


def drill(candidate):
    config=json.loads(reject_path_links(candidate).read_bytes());started=time.monotonic()
    with tempfile.TemporaryDirectory(prefix='qiqc-native-api-restore-') as name:
        root=Path(name);root.chmod(0o700);(root/'data').mkdir(mode=0o700)
        live=reject_path_links(Path(config['data'])/'platform.db');copy=root/'data/platform.db'
        with sqlite3.connect(live.resolve().as_uri()+'?mode=ro',uri=True) as source,sqlite3.connect(copy) as target:source.backup(target,pages=256,sleep=.01)
        copy.chmod(0o600);_,_,uploads=db_check(copy,config)
        for digest in uploads:copy_checked(Path(config['data'])/'uploads'/(digest+'.zip'),root/'data/uploads'/(digest+'.zip'),digest)
        expected=json.loads(Path(config['controller_manifest']).read_bytes())['binaries']['api']
        copy_checked(Path(config['binaries'])/'api',root/'api',expected)
        # Parent deliberately provides only a clean environment. No credential,
        # proxy or native candidate service variables reach the isolated probe.
        process=subprocess.Popen(['/usr/bin/unshare','--net','--',sys.executable,str(Path(__file__).resolve()),'--inside',str(root)],
            env=MINIMAL_ENV,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,start_new_session=True)
        try:stdout,_=process.communicate(timeout=45)
        finally:stop_group(process)
        if process.returncode!=0:raise ValueError('restore_api_isolated_probe_failed')
        value=json.loads(stdout)
        if value.get('passed') is not True or value.get('api_process_stopped') is not True:
            raise ValueError('restore_api_incomplete_evidence')
        value.update(schema='qiqc-native-restored-api/v1',api_binary_sha256=expected,
            immutable_uploads_restored=len(uploads),elapsed_seconds=round(time.monotonic()-started,3),
            not_proven=['real password login or credential recovery','worker or active run resumption','R2 and E2B recovery','offsite disaster recovery'])
    value.update(temporary_copy_removed=not root.exists())
    return value


def main():
    p=argparse.ArgumentParser();p.add_argument('--candidate',type=Path);p.add_argument('--report',type=Path);p.add_argument('--inside',type=Path);args=p.parse_args()
    if args.inside:
        try:print(json.dumps(inside(args.inside)));return 0
        except Exception as exc:print(json.dumps({'passed':False,'error_type':type(exc).__name__,'error_code':safe_error(exc)}));return 1
    result=drill(args.candidate)
    fd=os.open(reject_path_links(args.report),os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
    with os.fdopen(fd,'w') as f:json.dump(result,f,indent=2);f.write('\n')
    print(json.dumps(result));return 0

if __name__=='__main__':raise SystemExit(main())
