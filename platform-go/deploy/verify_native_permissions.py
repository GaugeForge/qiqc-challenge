#!/usr/bin/env python3
"""Read-only native service preflight in each running service's mount namespace.

No credential bytes, task contents, provider calls, writes, or restarts. Run as
root. Checks the current process UID/GID/groups, not an approximate login shell.
"""
from __future__ import annotations
import argparse
import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import sqlite3
import subprocess

ROOT=Path('/opt/qiqc-native-candidate')
ROLES=('api','worker','gateway','review')
SECRETS={'provider.env':'gateway','worker-e2b.env':'worker','api-r2.env':'api',
         'worker-r2.env':'worker','review.key':'review'}
PROPERTIES=('Id','User','Group','MainPID','ActiveState','NoNewPrivileges','ProtectHome',
            'ProtectSystem','PrivateTmp','Restart','CapabilityBoundingSet','SupplementaryGroups')
PROBE="""import json,os,socket,sys
op,path=sys.argv[1:]
try:
 if op=='socket':
  s=socket.socket(socket.AF_UNIX);s.settimeout(1);s.connect(path);s.close()
 else:
  flags=os.O_RDONLY if op=='read' else os.O_WRONLY|os.O_APPEND
  fd=os.open(path,flags|os.O_CLOEXEC|os.O_NOFOLLOW);os.close(fd)
except OSError:
 print(json.dumps({'probe_completed':True,'allowed':False}));sys.exit(13)
print(json.dumps({'probe_completed':True,'allowed':True}))
"""


def run(argv):
    return subprocess.run(argv,capture_output=True,text=True,timeout=15,check=False,cwd='/')


def process_identity(pid):
    if type(pid) is not int or pid<=1:raise ValueError('service_pid_invalid')
    root=Path('/proc')/str(pid)
    fields=dict(line.split(':',1) for line in (root/'status').read_text().splitlines() if ':' in line)
    return {'pid':pid,'uid':int(fields['Uid'].split()[1]),'gid':int(fields['Gid'].split()[1]),
            'groups':sorted(int(v) for v in fields['Groups'].split()),
            'start_ticks':(root/'stat').read_text().rsplit(') ',1)[1].split()[19],
            'mount_namespace':os.readlink(root/'ns/mnt')}


def validate_service(role,props,identity):
    errors=[];name='qiqc-native-'+role+'.service'
    if props.get('Id')!=name:errors.append('wrong_service_identity')
    if props.get('ActiveState')!='active':errors.append('service_not_active')
    expected_user='root' if role=='scanner' else 'qiqc-native-'+role
    if props.get('User')!=expected_user or props.get('Group')!='qiqc-native-data':errors.append('service_user_group_mismatch')
    for key,expected in {'NoNewPrivileges':'yes','ProtectHome':'yes','ProtectSystem':'strict',
                         'PrivateTmp':'yes','Restart':'on-failure','CapabilityBoundingSet':''}.items():
        if props.get(key)!=expected:errors.append('hardening_'+key+'_mismatch')
    if (identity.get('uid')==0)!=(role=='scanner'):errors.append('process_uid_mismatch')
    if role in ROLES and identity.get('mount_namespace')==os.readlink('/proc/self/ns/mnt'):
        errors.append('service_mount_namespace_not_isolated')
    return errors


def probe_argv(identity,operation,path):
    if operation not in ('read','write','socket'):raise ValueError('invalid_probe_operation')
    if identity['uid']==0:raise ValueError('nonroot_permission_probe_required')
    return ['nsenter','--target',str(identity['pid']),'--mount','--root','--wd=/','--',
            'setpriv','--reuid',str(identity['uid']),'--regid',str(identity['gid']),
            '--groups',','.join(map(str,identity['groups'])),'--no-new-privs',
            '/usr/bin/python3','-c',PROBE,operation,str(path)]


def probe_passed(result,allowed):
    try:value=json.loads(result.stdout)
    except (TypeError,ValueError):return False
    return result.returncode==(0 if allowed else 13) and value=={'probe_completed':True,'allowed':allowed}


def profile_key_probes(root,role,model_gid):
    """Inspect metadata only; actual access uses the existing namespace probe."""
    profile=root/'config/provider-profile.json';key=root/'config/profile.key'
    if not profile.exists() and not key.exists():return [],[]
    errors=[]
    if not profile.exists() or not key.exists():
        return [],['provider_profile_key_pair_missing']
    info=key.lstat()
    if (not stat.S_ISREG(info.st_mode) or key.is_symlink() or info.st_nlink!=1
            or stat.S_IMODE(info.st_mode)!=0o640 or info.st_uid!=0 or info.st_gid!=model_gid):
        errors.append('provider_profile_key_metadata_invalid')
    return [('read',key,role in ('gateway','review'),'credential_open_close:profile.key')],errors


def release_target(root):
    pointer=root/'active-release.json'
    if not pointer.exists():return None
    if pointer.is_symlink():raise ValueError('release_pointer_symlink')
    value=json.loads(pointer.read_text())
    rid=value.get('release_id','')
    if not re.fullmatch('native-release-[a-f0-9]{64}',rid):raise ValueError('release_pointer_identity_invalid')
    path=root/'releases'/rid/'release.json'
    if value.get('release_path')!=str(path) or path.is_symlink() or not path.is_file():raise ValueError('release_pointer_path_invalid')
    if hashlib.sha256(path.read_bytes()).hexdigest()!=value.get('release_sha256'):raise ValueError('release_pointer_record_digest_mismatch')
    return path


def source_audit_probes(root, role):
    """Probe active and retained audits without reading any audit payload."""
    config = json.loads((root / 'candidate.json').read_text())
    current = Path(config['source_audit'])
    if (not current.is_absolute() or current.parent != root or
            current.is_symlink() or not current.is_file()):
        raise ValueError('source_audit_path_invalid')
    probes = [('read', current, role == 'worker', 'source_audit_open_close')]
    for path in sorted(root.glob('source-audit*.json')):
        if path == current:
            continue
        if path.is_symlink() or not path.is_file():
            raise ValueError('retained_source_audit_path_invalid')
        probes.append(('read', path, False,
                       'retained_source_audit_open_close:' + path.name))
    return probes


def check(root=ROOT):
    if os.geteuid()!=0:raise ValueError('root_required_for_mount_namespace_checks')
    missing=[name for name in ('systemctl','nsenter','setpriv') if not shutil.which(name)]
    if missing:raise ValueError('required_tools_missing:'+','.join(missing))
    checks=[];failures=[];services={}
    release=release_target(root)
    with sqlite3.connect((root/'data/platform.db').as_uri()+'?mode=ro',uri=True) as db:
        active=db.execute("SELECT value FROM settings WHERE key='scientific_active_release'").fetchone()
    registered=active[0] if active else ''
    if registered!=(release.parent.name if release else ''):failures.append('active_release_database_pointer_mismatch')
    for role in (*ROLES,'scanner'):
        name='qiqc-native-'+role+'.service'
        result=run(['systemctl','show',name,'--property='+','.join(PROPERTIES)])
        if result.returncode:failures.append(name+':service_query_failed');continue
        props=dict(line.split('=',1) for line in result.stdout.splitlines() if '=' in line)
        try:identity=process_identity(int(props.get('MainPID','0')))
        except (ValueError,OSError):failures.append(name+':process_identity_unavailable');continue
        errors=validate_service(role,props,identity)
        import pwd,grp
        expected_uid=0 if role=='scanner' else pwd.getpwnam('qiqc-native-'+role).pw_uid
        if identity['uid']!=expected_uid or identity['gid']!=grp.getgrnam('qiqc-native-data').gr_gid:
            errors.append('actual_process_identity_mismatch')
        if role in ROLES:
            expected_groups={grp.getgrnam('qiqc-native-data').gr_gid}
            if role in ('api','worker'):expected_groups.add(grp.getgrnam('qiqc-native-science').gr_gid)
            if (root/'config/provider-profile.json').exists() and role in ('gateway','review'):
                expected_groups.add(grp.getgrnam('qiqc-native-model').gr_gid)
            if set(identity['groups'])!=expected_groups:errors.append('actual_supplementary_groups_mismatch')
        services[role]={'service':name,'process':identity,'hardening_errors':errors}
        failures.extend(name+':'+error for error in errors)
        if role=='scanner' or errors:continue
        probes=[]
        probes.extend(source_audit_probes(root, role))
        if (root/'config/provider-profile.json').exists() or (root/'config/profile.key').exists():
            profile_probes,profile_errors=profile_key_probes(root,role,grp.getgrnam('qiqc-native-model').gr_gid)
            probes.extend(profile_probes)
            failures.extend(role+':'+error for error in profile_errors)
        for filename,owner in SECRETS.items():
            path=root/'config'/filename
            info=path.lstat()
            if not stat.S_ISREG(info.st_mode) or path.is_symlink() or stat.S_IMODE(info.st_mode)!=0o600 or info.st_nlink!=1 or info.st_uid!=pwd.getpwnam('qiqc-native-'+owner).pw_uid:
                failures.append(filename+':secret_metadata_invalid')
            probes.append(('read',path,role==owner,'credential_open_close:'+filename))
        probes.extend([
            ('read',root/'source',role=='worker','source_directory_open_close'),
            ('read',root/'candidate.json',role=='worker','candidate_open_close'),
            ('read',Path('/root/QQ-Challenge/.env'),False,'workspace_secret_denied'),
            ('write',root/'bin/api',False,'deployed_binary_write_denied'),
            ('socket',Path('/run/docker.sock'),False,'docker_socket_denied')])
        upload_samples = sorted((root/'data/uploads').glob('*.zip'))
        if upload_samples:
            probes.append(('read',upload_samples[0],role in ('api','worker','review'),'frozen_upload_read'))
            if role in ('gateway','review'):
                probes.append(('write',upload_samples[0],False,'frozen_upload_write_denied'))
        if release:
            probes.append(('read',release,role in ('api','worker'),'active_release_open_close'))
        for operation,path,allowed,label in probes:
            if not path.exists():
                failures.append(role+':'+label+':host_positive_control_missing');continue
            result=run(probe_argv(identity,operation,path))
            # 13 means the actual open/connect was denied; nsenter/setpriv/python
            # startup failures must not be mistaken for successful isolation.
            passed=probe_passed(result,allowed)
            checks.append({'role':role,'check':label,'expected_allowed':allowed,'passed':passed,
                           'exit_code':result.returncode,'mount_namespace':identity['mount_namespace']})
            if not passed:failures.append(role+':'+label)
        if process_identity(identity['pid'])!=identity:failures.append(role+':process_changed_during_probe')
    return {'schema':'qiqc-native-permissions/v1','checked_at':datetime.datetime.now(datetime.timezone.utc).isoformat(),
            'status':'passed' if not failures else 'failed','profile':'native','services':services,'checks':checks,
            'errors':sorted(set(failures)),'release_phase':'active' if release else 'candidate',
            'secret_bytes_read':False,'task_bytes_read':False,'cloud_called':False,'services_restarted':False,
            'writes_performed':False,'limits':['Does not prove cloud credential scope, host reboot or offsite recovery.']}


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    try:value=check()
    except (ValueError,OSError,KeyError,sqlite3.Error,subprocess.TimeoutExpired) as exc:
        value={'schema':'qiqc-native-permissions/v1','status':'failed','error_type':type(exc).__name__,
               'error':'preflight could not complete; no secret or task payload retained'}
    with args.output.open('x') as output:json.dump(value,output,indent=2);output.write('\n')
    print(json.dumps({'status':value['status'],'output':str(args.output)}))
    raise SystemExit(0 if value['status']=='passed' else 1)


if __name__=='__main__':main()
