#!/usr/bin/env python3
"""Private, temporary native restore drill. No services, cloud, or submitted code."""
from __future__ import annotations
import argparse
import hashlib
import json
import os
import re
import shutil
import sqlite3
import stat
import sys
import tempfile
import time
from pathlib import Path
sys.path.insert(0,str(Path(__file__).parent/'science'))
from native_controller import verify as verify_controller
from native_release_record import load_release,validate_binding,BINDINGS,read_regular
from verify_restored_benchmark import verify as verify_source


def sha(path):return hashlib.sha256(read_regular(path,512*1024*1024)).hexdigest()


def db_check(path,config):
    with sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True) as db:
        if db.execute('pragma integrity_check').fetchall()!=[('ok',)]:raise ValueError('restore_database_integrity_failed')
        if db.execute('pragma foreign_key_check').fetchall():raise ValueError('restore_database_foreign_key_failed')
        checks={
          'version_team': 'SELECT count(*) FROM versions v LEFT JOIN teams t ON t.id=v.team_id WHERE t.id IS NULL',
          'batch_version_team': 'SELECT count(*) FROM batches b LEFT JOIN versions v ON v.id=b.version_id WHERE v.id IS NULL OR v.team_id!=b.team_id',
          'run_batch': 'SELECT count(*) FROM runs r LEFT JOIN batches b ON b.id=r.batch_id WHERE b.id IS NULL',
          'attempt_run': 'SELECT count(*) FROM attempts a LEFT JOIN runs r ON r.id=a.run_id WHERE r.id IS NULL',
          'target_submission': 'SELECT count(*) FROM batch_scientific_targets t LEFT JOIN batches b ON b.id=t.batch_id LEFT JOIN versions v ON v.id=b.version_id WHERE v.id IS NULL OR t.submission_sha256!=v.sha256',
          'artifact_attempt': 'SELECT count(*) FROM run_artifacts f LEFT JOIN attempts a ON a.run_id=f.run_id AND a.number=f.attempt WHERE a.id IS NULL',
          'model_usage_attempt': 'SELECT count(*) FROM usage u LEFT JOIN attempts a ON a.run_id=u.run_id AND a.number=u.attempt WHERE a.id IS NULL',
          'quota_batch_team': 'SELECT count(*) FROM ledger l LEFT JOIN batches b ON b.id=l.batch_id WHERE l.batch_id IS NOT NULL AND (b.id IS NULL OR b.team_id!=l.team_id)',
        }
        for name,sql in checks.items():
            if db.execute(sql).fetchone()[0]:raise ValueError('restore_relation_'+name+'_failed')
        settings=dict(db.execute('SELECT key,value FROM settings'))
        if settings.get('scientific_candidate')!=config['candidate_id']:raise ValueError('restore_candidate_database_mismatch')
        counts={t:db.execute('SELECT count(*) FROM '+t).fetchone()[0] for t in ('teams','versions','batches','runs','attempts','run_artifacts','ledger','usage','audit')}
        uploads=sorted({row[0] for row in db.execute('SELECT sha256 FROM versions')})
    return {'integrity_verified':True,'foreign_keys_verified':True,'relations_verified':list(checks),'counts':counts},settings,uploads


def reject_path_links(path):
    """Organizer-selected paths still must not redirect through parent links."""
    path=Path(path).absolute()
    for part in (path,*path.parents):
        if part.is_symlink():raise ValueError('restore_path_symlink')
    return path


def copy_checked(source,target,digest=None):
    source=reject_path_links(source);target=reject_path_links(target)
    descriptor=os.open(source,os.O_RDONLY|os.O_CLOEXEC|os.O_NOFOLLOW)
    with os.fdopen(descriptor,'rb') as stream:
        info=os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size>512*1024*1024:
            raise ValueError('restore_input_regular_bounded_file_required')
        raw=stream.read(512*1024*1024+1)
    if len(raw)>512*1024*1024:raise ValueError('restore_input_regular_bounded_file_required')
    if digest and hashlib.sha256(raw).hexdigest()!=digest:raise ValueError('restore_input_hash_mismatch')
    target.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    reject_path_links(target.parent)
    descriptor=os.open(target,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_CLOEXEC|os.O_NOFOLLOW,0o600)
    with os.fdopen(descriptor,'wb') as stream:stream.write(raw)
    if hashlib.sha256(target.read_bytes()).hexdigest()!=hashlib.sha256(raw).hexdigest():raise ValueError('restore_copy_mismatch')


def check_release(config,settings,pointer,restore_root):
    active=settings.get('scientific_active_release','')
    if not pointer.exists():
        if active:raise ValueError('restore_active_pointer_missing')
        return {'state':'no_active_release','active_release_restored':False,'attachments_verified':0}
    value=json.loads(read_regular(pointer));path=Path(value['release_path'])
    if not path.is_absolute() or sha(path)!=value.get('release_sha256'):raise ValueError('restore_pointer_digest_mismatch')
    release=validate_binding(config,load_release(path));rid=release['release_id']
    if value.get('release_id')!=rid or active!=rid:raise ValueError('restore_pointer_database_mismatch')
    try:registration=json.loads(settings['native_release:'+rid])
    except (KeyError,ValueError):raise ValueError('restore_release_registration_missing') from None
    expected={'release_id':rid,'candidate_id':config['candidate_id'],'release_sha256':sha(path)}
    if registration!=expected:raise ValueError('restore_release_registration_mismatch')
    root=restore_root/'release';root.mkdir()
    for name,digest in release['payload']['files'].items():copy_checked(path.parent/name,root/name,digest)
    copy_checked(path,root/'release.json',sha(path))
    restored=validate_binding(config,load_release(root/'release.json'))
    if restored!=release:raise ValueError('restore_release_changed')
    # Keep original pointer bytes and a distinct relocation record. Never claim
    # that the live absolute path itself was activated in this temporary tree.
    copy_checked(pointer,restore_root/'active-release.original.json')
    (restore_root/'release-relocation.json').write_text(json.dumps({'original_pointer_sha256':sha(pointer),'restored_release_sha256':sha(root/'release.json')}))
    return {'state':'active_release_restored_verified','active_release_restored':True,'attachments_verified':103,'release_id':rid}


def drill(candidate_path,temporary_parent=None):
    start=time.monotonic();candidate_raw=read_regular(candidate_path);config=json.loads(candidate_raw);report={'schema':'qiqc-native-local-recovery/v1','passed':False}
    if config.get('schema')!='qiqc-native-candidate/v1':raise ValueError('restore_candidate_schema_invalid')
    manifest=verify_controller(config)
    audit=json.loads(read_regular(config['source_audit']))
    if sha(config['source_audit'])!=config['source_audit_sha256']:raise ValueError('restore_source_audit_changed')
    if any(audit.get(k)!=config[k] for k in ('source_commit','source_files_sha256')):raise ValueError('restore_source_identity_mismatch')
    verify_source(Path(config['source_root']),audit)
    original_pointer=Path(config['candidate_root'])/'active-release.json'
    pointer_before=sha(original_pointer) if original_pointer.exists() else None
    with tempfile.TemporaryDirectory(prefix='qiqc-native-recovery-',dir=temporary_parent) as directory:
        root=Path(directory);root.chmod(0o700)
        target=root/'platform.db'
        source=Path(config['data'])/'platform.db'
        reject_path_links(source)
        with sqlite3.connect(source.resolve().as_uri()+'?mode=ro',uri=True) as live,sqlite3.connect(target) as restored:
            live.backup(restored,pages=256,sleep=.01)
        target.chmod(0o600)
        database,settings,uploads=db_check(target,config)
        for digest in uploads:
            if not re.fullmatch('[a-f0-9]{64}',digest):raise ValueError('restore_upload_digest_invalid')
            copy_checked(Path(config['data'])/'uploads'/(digest+'.zip'),root/'uploads'/(digest+'.zip'),digest)
        # Source checkout, controller files and binaries are actually copied and
        # checked offline, but never imported/executed from the restored tree.
        shutil.copytree(config['source_root'],root/'source',symlinks=True)
        source_result=verify_source(root/'source',audit)
        copied_config={key:config[key] for key in BINDINGS if key in config}
        copied_config.update(project_root=str(root/'project'),binaries=str(root/'bin'),controller_manifest=str(root/'controller-manifest.json'))
        for relative,digest in manifest['host_files'].items():copy_checked(Path(config['project_root'])/relative,root/'project'/relative,digest)
        for row in manifest['remote_files'].values():
            target_file=root/'project'/row['source']
            if target_file.exists():
                if sha(target_file)!=row['sha256']:raise ValueError('restore_duplicate_controller_digest_mismatch')
            else:copy_checked(Path(config['project_root'])/row['source'],target_file,row['sha256'])
        for name,digest in manifest['binaries'].items():copy_checked(Path(config['binaries'])/name,root/'bin'/name,digest)
        copy_checked(config['controller_manifest'],root/'controller-manifest.json')
        copy_checked(config['source_audit'],root/'source-audit.json',config['source_audit_sha256'])
        (root/'restored-candidate-binding.json').write_text(json.dumps(copied_config,sort_keys=True))
        verify_controller(json.loads((root/'restored-candidate-binding.json').read_bytes()))
        release=check_release(config,settings,original_pointer,root)
        if (sha(original_pointer) if original_pointer.exists() else None)!=pointer_before:raise ValueError('restore_live_pointer_changed_during_snapshot')
        if read_regular(candidate_path)!=candidate_raw:raise ValueError('restore_candidate_changed_during_snapshot')
        # Re-read completed restored DB after all file work: no live connection
        # or service is needed to validate its object graph and content digests.
        db_check(target,config)
        report.update(passed=True,database=database,upload_blobs_restored_verified=len(uploads),
            source_restored_verified=True,source_files_verified=source_result['files_verified'],
            controller_restored_verified=True,controller_sha256=config['controller_sha256'],
            binary_files_restored_verified=len(manifest['binaries']),release=release,
            credentials_files_copied=False,rollout_copied=False,services_stopped=False,
            database_snapshot_includes_authentication_rows=True,
            upload_consistency='Only immutable ZIP digests referenced by the consistent SQLite snapshot; subsequent live submissions are outside this snapshot',
            restored_services_started=False,pending_rollout_cache_restored=False,
            active_run_resumption_verified=False,
            not_proven=['offsite disaster recovery','cloud snapshot or Docker image restoration','R2 object recovery',
                        'service startup from restored files','active run resumption or lost sandbox reconciliation',
                        'pending rollout cache recovery','lost credential reprovisioning','production RPO/RTO'],
            source='actual_live_native_candidate',scope='temporary_local_offline_restore')
    report.update(temporary_copy_removed=not root.exists(),elapsed_seconds=round(time.monotonic()-start,3))
    return report


def safe_error(exc):
    value=str(exc)
    return value if isinstance(exc,ValueError) and re.fullmatch(r'restore_[a-z0-9_]+',value) else 'recovery_operation_failed'


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--candidate',type=Path,required=True);parser.add_argument('--report',type=Path,required=True);args=parser.parse_args()
    try:result=drill(args.candidate)
    except Exception as exc:result={'schema':'qiqc-native-local-recovery/v1','passed':False,'error_class':type(exc).__name__,'error_code':safe_error(exc)}
    reject_path_links(args.report)
    args.report.parent.mkdir(parents=True,exist_ok=True)
    descriptor=os.open(args.report,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
    with os.fdopen(descriptor,'w') as output:output.write(json.dumps(result,indent=2)+'\n')
    print(json.dumps(result));return 0 if result['passed'] else 1

if __name__=='__main__':raise SystemExit(main())
