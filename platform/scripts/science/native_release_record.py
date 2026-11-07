"""Read-only integrity checks for accepted native runtime releases.

No cloud, database or service mutation occurs here. A release freezes a tested
300-second diagnostic runtime, not all competition production prerequisites.
"""
from __future__ import annotations
import hashlib
import datetime
import json
import re
from pathlib import Path, PurePosixPath

SCHEMA='qiqc-native-release/v1'
BINDINGS=('candidate_id','source_files_sha256','source_audit_sha256','source_commit',
          'controller_sha256','template_id','build_plan_sha256','build_receipt_sha256','diagnostic_model','provider_profile')


ACCEPTANCE_FIELDS=('qsim_successful_interaction_verified','qsim_progress_sha256','reward',
                   'agent_stop_reason','budget_exhausted','acceptance_early_stop_verified',
                   'acceptance_readiness_sha256','acceptance_scope','observation_policy',
                   'execution_duration_class','agent_elapsed_seconds','agent_exit','agent_budget_seconds',
                   'harbor_agent_timing_verified','agent_execution_started_at','agent_execution_finished_at',
                   'terminal_runtime_verified','terminal_runtime_sha256')


def agent_window(value):
    """Parse explicitly UTC Harbor times; never synthesize missing timestamps."""
    if value.get('harbor_agent_timing_verified') is not True:
        raise ValueError('harbor_agent_timing_unverified')
    times=[]
    for key in ('agent_execution_started_at','agent_execution_finished_at'):
        raw=value.get(key)
        if not isinstance(raw,str):raise ValueError('harbor_agent_time_missing')
        try:stamp=datetime.datetime.fromisoformat(raw.replace('Z','+00:00'))
        except ValueError:raise ValueError('harbor_agent_time_invalid') from None
        if stamp.tzinfo is None or stamp.utcoffset()!=datetime.timedelta(0):
            raise ValueError('harbor_agent_time_not_utc')
        times.append(stamp)
    seconds=(times[1]-times[0]).total_seconds()
    elapsed=value.get('agent_elapsed_seconds')
    budget = 180 if value.get("acceptance_scope") == "bounded-three-minute" else 300
    if not budget-1<=seconds<=budget+60 or type(elapsed) not in (int,float) or not abs(seconds-elapsed)<=30:
        raise ValueError('harbor_agent_duration_mismatch')
    return times


def concurrency_evidence(rows):
    """All 49 scientific Agent intervals must share a positive UTC interval."""
    if len(rows)!=49:raise ValueError('concurrency_requires_all49')
    windows=[agent_window(row) for row in rows]
    start=max(pair[0] for pair in windows);finish=min(pair[1] for pair in windows)
    if start>=finish:raise ValueError('all49_agent_execution_did_not_overlap')
    return {'schema':'qiqc-all49-agent-overlap/v1','actual_concurrent_tasks':49,
            'overlap_started_at':start.isoformat(),'overlap_finished_at':finish.isoformat(),
            'overlap_seconds':(finish-start).total_seconds()}


def acceptance_evidence_errors(value):
    """Shared minimum functional proof for freeze, readback and activation."""
    errors=[]
    if value.get('qsim_successful_interaction_verified') is not True:
        errors.append('qsim_successful_interaction_verified_missing')
    if not isinstance(value.get('qsim_progress_sha256'),str) or not re.fullmatch('[a-f0-9]{64}',value['qsim_progress_sha256']):
        errors.append('qsim_progress_digest_invalid')
    if type(value.get('reward')) is not int or value['reward'] not in (0,1):
        errors.append('verifier_binary_reward_invalid')
    reason=value.get('agent_stop_reason')
    if reason not in ('completed','budget_exhausted','acceptance_early_stop'):
        errors.append('agent_stop_reason_invalid')
    if value.get('budget_exhausted') is not (reason=='budget_exhausted'):
        errors.append('budget_stop_claim_inconsistent')
    if value.get('acceptance_early_stop_verified') is not (reason=='acceptance_early_stop'):
        errors.append('early_stop_claim_inconsistent')
    if reason=='acceptance_early_stop':
        digest=value.get('acceptance_readiness_sha256')
        if not isinstance(digest,str) or not re.fullmatch('[a-f0-9]{64}',digest):
            errors.append('early_stop_readiness_digest_invalid')
    elif value.get('acceptance_readiness_sha256') is not None:
        errors.append('unexpected_early_stop_readiness')
    if value.get('acceptance_scope') in ('bounded-five-minute','bounded-three-minute'):
        budget = 180 if value['acceptance_scope']=='bounded-three-minute' else 300
        if value.get('terminal_runtime_verified') is not True:errors.append('terminal_runtime_unverified')
        digest=value.get('terminal_runtime_sha256')
        if not isinstance(digest,str) or not re.fullmatch('[a-f0-9]{64}',digest):errors.append('terminal_runtime_digest_invalid')
        try:agent_window(value)
        except ValueError as exc:errors.append(str(exc))
        if value.get('observation_policy')!=f'observe-{budget}s-no-readiness-stop/v1' or value.get('execution_duration_class')!=f'budget_{budget}s_observed':
            errors.append('five_minute_observation_policy_invalid')
        if reason!='budget_exhausted':errors.append('five_minute_budget_stop_required')
        elapsed=value.get('agent_elapsed_seconds')
        if type(elapsed) not in (int,float) or not budget-1<=elapsed<budget+30:
            errors.append('five_minute_observation_duration_invalid')
        if type(value.get('agent_exit')) is not int or value['agent_exit'] not in (124,137):
            errors.append('five_minute_budget_exit_invalid')
        if type(value.get('agent_budget_seconds')) is not int or value['agent_budget_seconds']!=budget:
            errors.append('five_minute_budget_invalid')
    return errors


def canonical(value):
    return json.dumps(value,sort_keys=True,separators=(',',':')).encode()


def read_regular(path,limit=32*1024*1024):
    path=Path(path)
    if path.is_symlink() or not path.is_file() or path.stat().st_size>limit:
        raise ValueError('release_regular_bounded_file_required')
    return path.read_bytes()


def load_release(path):
    path=Path(path)
    value=json.loads(read_regular(path))
    payload=value.get('payload')
    if value.get('schema')!=SCHEMA or value.get('frozen') is not True or not isinstance(payload,dict):
        raise ValueError('invalid_native_release_record')
    if value.get('release_id')!='native-release-'+hashlib.sha256(canonical(payload)).hexdigest():
        raise ValueError('native_release_digest_mismatch')
    if payload.get('acceptance_scope') not in ('49-browser-bounded-300s','49-browser-bounded-functional','49-browser-bounded-five-minute') or payload.get('competition_production_ready') is not False:
        raise ValueError('native_release_scope_invalid')
    if payload.get("diagnostic_model", "") not in ("", "minimax/minimax-m3.1-flash:free"):
        raise ValueError("native_release_diagnostic_model_invalid")
    if payload.get("provider_profile") is not None:
        from native_provider_profile import validate_profile_identity
        validate_profile_identity(payload["provider_profile"])
    tasks=payload.get('tasks',{})
    if len(tasks)!=49 or sorted(row.get('task_index',-1) for row in tasks.values())!=list(range(49)):
        raise ValueError('native_release_roster_invalid')
    for name,row in tasks.items():
        if not re.fullmatch('[a-z0-9_]+',name) or set(row.get('image_ids',{}))!={'agent','main','qsim','verifier'}:
            raise ValueError('native_release_task_image_contract_invalid')
        if any(not re.fullmatch('sha256:[a-f0-9]{64}',v) for v in row['image_ids'].values()):
            raise ValueError('native_release_task_image_digest_invalid')
    if payload['acceptance_scope'] in ('49-browser-bounded-functional','49-browser-bounded-five-minute'):
        if payload.get('agent_max_seconds')!=300 or payload.get('early_stop_allowed') is not (payload['acceptance_scope']=='49-browser-bounded-functional') or payload.get('full_duration_performance_capacity_accepted') is not False:
            raise ValueError('native_bounded_release_scope_invalid')
        for row in tasks.values():
            if payload['acceptance_scope']=='49-browser-bounded-five-minute' and row.get('acceptance_scope')!='bounded-five-minute':
                raise ValueError('native_release_five_minute_scope_required')
            if acceptance_evidence_errors(row):raise ValueError('native_release_acceptance_proof_invalid')
            if row.get('source_runtime_software_verified') is not True or row.get('original_container_limits_verified') is not True or type(row.get('full_host_capacity_verified')) is not bool or row.get('source_runtime_parity_verified') is not row['full_host_capacity_verified']:
                raise ValueError('native_bounded_release_runtime_claim_invalid')
        if payload.get('full_host_capacity_verified') is not all(row['full_host_capacity_verified'] for row in tasks.values()):
            raise ValueError('native_bounded_release_capacity_claim_invalid')
    if payload['acceptance_scope']=='49-browser-bounded-five-minute':
        if payload.get('concurrency_evidence')!=concurrency_evidence(list(tasks.values())):
            raise ValueError('native_release_concurrency_claim_invalid')
    files=payload.get('files',{})
    required={'source-audit.json','controller-manifest.json','build-plan.json','build-receipt.json','freeze-gate.json'}
    if not required<=set(files) or len(files)!=103:
        raise ValueError('native_release_evidence_inventory_invalid')
    root=path.parent.resolve()
    for relative,digest in files.items():
        rel=PurePosixPath(relative)
        target=path.parent/relative
        if rel.is_absolute() or '..' in rel.parts or not target.resolve().is_relative_to(root):
            raise ValueError('native_release_evidence_path_invalid')
        if hashlib.sha256(read_regular(target)).hexdigest()!=digest:
            raise ValueError('native_release_evidence_changed')
    if payload['acceptance_scope'] in ('49-browser-bounded-functional','49-browser-bounded-five-minute'):
        for row in tasks.values():
            ordinal=row['task_index']
            evidence=json.loads(read_regular(path.parent/f'evidence/{ordinal:02d}-r2.json'))
            if payload.get("provider_profile") is not None and evidence.get("benchmark_release",{}).get("provider_profile") != payload["provider_profile"]:
                raise ValueError("native_release_profile_attachment_mismatch")
            if payload.get("diagnostic_model") and (evidence.get("diagnostic_model") != payload["diagnostic_model"] or evidence.get("model_id") != payload["diagnostic_model"]):
                raise ValueError("native_release_model_attachment_mismatch")
            fields=ACCEPTANCE_FIELDS if payload['acceptance_scope']=='49-browser-bounded-five-minute' else ACCEPTANCE_FIELDS[:7]
            if acceptance_evidence_errors(evidence) or any(evidence.get(key)!=row.get(key) for key in fields):
                raise ValueError('native_release_acceptance_attachment_mismatch')
    return value


def validate_binding(config,release):
    payload=release['payload']
    if any(payload.get(key, "" if key == "diagnostic_model" else None)!=config.get(key, "" if key == "diagnostic_model" else None) for key in BINDINGS):
        raise ValueError('native_release_runtime_binding_mismatch')
    return release
