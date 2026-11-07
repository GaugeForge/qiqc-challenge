"""Read-only resume validation. No cloud calls or database writes."""
import hashlib
import datetime
import json
import re
from pathlib import Path

ACTIVE_SQL="state IN ('queued','running','uploading') OR (state='attention' AND error='worker_lost:reconciliation_required')"
TERMINAL={'passed','browser_failed','r2_evidence_failed'}


def sha(raw):return hashlib.sha256(raw).hexdigest()


def read_json(path):
    if path.is_symlink() or not path.is_file() or path.stat().st_size>4*1024*1024:
        raise ValueError('resume_report_not_regular')
    raw=path.read_bytes()
    return json.loads(raw),sha(raw)


def binding(config, directory, manual_review, organizer_manual=False):
    result= {'candidate_config_sha256':sha(json.dumps(config,sort_keys=True,separators=(',',':')).encode()),
            'evidence_directory':str(Path(directory).resolve()),'manual_review':bool(manual_review)}
    if organizer_manual:result['organizer_template_manual_review']=True
    return result


def validate_resume(directory,indices,config,db,checker_sha,manual_review=False,organizer_manual=False):
    from native_candidate import identity
    from native_functional49_summary import pair_errors
    directory=Path(directory)
    suite,suite_digest=read_json(directory/'suite.json')
    if suite.get('schema')!='qiqc-catalog-browser-suite/v1' or suite.get('selected_indices')!=indices:
        raise ValueError('resume_original_indices_required')
    expected=binding(config,directory,manual_review,organizer_manual)
    prior=suite.get('resume_binding')
    transition=None
    if prior is not None and prior!=expected:
        # One explicit operational policy upgrade, never a wildcard candidate change.
        if not organizer_manual or manual_review or prior!=binding(config,directory,True):
            raise ValueError('resume_configuration_changed')
        transition={'from':prior,'to':expected,'previous_suite_sha256':suite_digest,
                    'reason':'explicit_organizer_template_manual_review',
                    'at':datetime.datetime.now(datetime.timezone.utc).isoformat()}
    transitions=suite.get('policy_transitions',[])
    if not isinstance(transitions,list):raise ValueError('resume_policy_history_invalid')
    rows=suite.get('attempts')
    if not isinstance(rows,list):raise ValueError('resume_attempt_history_invalid')
    latest={};seen=set();batches=set();validated_identity=False
    for row in rows:
        i,n=row.get('catalog_index'),row.get('attempt')
        if type(i) is not int or i not in indices or type(n) is not int or n<1 or (i,n) in seen:
            raise ValueError('resume_attempt_history_invalid')
        seen.add((i,n))
        if row.get('state') not in TERMINAL:raise ValueError('resume_unfinished_attempt')
        if i not in latest or n>latest[i]['attempt']:latest[i]=row
        stem=f'task-{i:02d}-attempt-{n:02d}'
        browser=None;evidence=None;bs=None
        for field,suffix in [('browser',''),('r2','-r2')]:
            name=stem+suffix+'.json'
            if row.get(field+'_report',name)!=name:raise ValueError('resume_report_path_changed')
            path=directory/name
            if not path.exists():
                if row.get(field+'_sha256') or row['state']=='passed':raise ValueError('resume_report_missing')
                continue
            obj,digest=read_json(path)
            stored=row.get(field+'_sha256')
            if stored is not None and stored!=digest:raise ValueError('resume_report_digest_changed')
            if row['state']=='passed' and stored is None:raise ValueError('resume_pass_digest_missing')
            if obj.get('benchmark_release')!=identity(config,i):raise ValueError('resume_candidate_identity_changed')
            validated_identity=True
            if obj.get('batch_id'):batches.add(obj['batch_id'])
            # Old failed reports had no saved SHA. Bind the observed file once,
            # explicitly recording that this is not a historical attestation.
            if stored is None:
                row[field+'_sha256']=digest
                row[field+'_digest_bound_at_resume']=True
            if field=='browser':browser=obj;bs=digest
            else:evidence=obj
        if row.get('batch_id'):batches.add(row['batch_id'])
        if row['state']=='passed':
            errors=pair_errors(config,i,browser,evidence,bs,checker_sha)
            if errors:raise ValueError('resume_pass_evidence_invalid')
    if prior is None and rows and not validated_identity:raise ValueError('resume_legacy_identity_unproven')
    # Reject orphan newer files: they may represent a task accepted before the
    # local checkpoint was persisted, and must never be retried blindly.
    for path in directory.glob('task-*-attempt-*.json'):
        match=re.fullmatch(r'task-(\d+)-attempt-(\d+)\.json',path.name)
        if match and tuple(map(int,match.groups())) not in seen:raise ValueError('resume_orphan_attempt')
    for batch in batches:
        if db.execute('SELECT count(*) FROM runs WHERE batch_id=? AND ('+ACTIVE_SQL+')',(batch,)).fetchone()[0]:
            raise ValueError('resume_related_run_active')
    # Target-index lookup also catches acceptance before browser batch_id save.
    for index in indices:
        if db.execute("SELECT count(*) FROM runs r JOIN batch_scientific_targets t ON t.batch_id=r.batch_id WHERE t.task_index=? AND (r.state IN ('queued','running','uploading') OR (r.state='attention' AND r.error='worker_lost:reconciliation_required'))",(index,)).fetchone()[0]:
            raise ValueError('resume_related_run_active')
    skip={i for i,row in latest.items() if row['state']=='passed'}
    if transition is not None:suite['policy_transitions']=[*transitions,transition]
    return suite,rows,skip,expected
