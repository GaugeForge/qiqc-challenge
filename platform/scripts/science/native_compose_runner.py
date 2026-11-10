#!/usr/bin/env python3
"""Run on a trusted E2B Docker host only; VPS calls this through the E2B SDK."""
from __future__ import annotations
import argparse
import asyncio
import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path
import yaml
from native_compose_contract import validate_spec, submission_manifest, model_from_identity


def make_trial_config(spec, control_dir):
    from harbor.models.trial.config import AgentConfig,EnvironmentConfig,TaskConfig,TrialConfig
    # Endpoint compatibility belongs to the selected submission adapter.
    # A valid Chat Completions adapter must not inherit Codex-only Responses.
    return TrialConfig(
        task=TaskConfig(path=Path(spec['source_root'])/'harbor_tasks'/spec['task_id']),
        trial_name=spec['run_id'],trials_dir=Path(spec['output_dir']),
        agent=AgentConfig(import_path='native_compose_agent:NativeSubmittedHarness',
                          model_name=model_from_identity(spec['candidate_identity']),override_timeout_sec=spec['agent_budget_seconds']+30,
                          override_setup_timeout_sec=spec["candidate_identity"].get("native_limits",{}).get("setup_seconds",120),kwargs={'run_handle':spec['run_id']}),
        environment=EnvironmentConfig(import_path='native_compose_environment:NativeDockerEnvironment',
            force_build=False,delete=True,extra_docker_compose=[Path(p) for p in spec['compose_overlays']],
            kwargs={'native_agent_environment_dir':str(Path(spec['source_root'])/'harbor_tasks'/spec['task_id']/'environment'),
                    'native_control_dir':str(control_dir),'native_run_handle':spec['run_id'],
                    'native_candidate_identity':spec['candidate_identity'],'native_task_id':spec['task_id']}),
        environment_build_timeout_multiplier=1 if spec["candidate_identity"].get("native_limits") else 8,
    )


def make_harbor_job_config(spec, control_dir, jobs_dir):
    """Build the same config shape used by the upstream ``harbor run`` CLI."""
    source = Path(spec['source_root'])
    return {
        'job_name': spec['run_id'],
        'jobs_dir': str(jobs_dir),
        'n_attempts': 1,
        'n_concurrent_trials': 1,
        'quiet': True,
        'environment': {
            'import_path': 'native_compose_environment:NativeDockerEnvironment',
            'force_build': False,
            'delete': True,
            'extra_docker_compose': [str(Path(p)) for p in spec['compose_overlays']],
            'kwargs': {
                'native_agent_environment_dir': str(source / 'harbor_tasks' / spec['task_id'] / 'environment'),
                'native_control_dir': str(control_dir),
                'native_run_handle': spec['run_id'],
                'native_candidate_identity': spec['candidate_identity'],
                'native_task_id': spec['task_id'],
            },
        },
        'agents': [{
            'import_path': 'native_compose_agent:NativeSubmittedHarness',
            'model_name': model_from_identity(spec['candidate_identity']),
            'override_timeout_sec': spec['agent_budget_seconds'] + 30,
            'override_setup_timeout_sec': spec['candidate_identity'].get('native_limits', {}).get('setup_seconds', 120),
            'kwargs': {'run_handle': spec['run_id']},
        }],
        'tasks': [{'path': str(source / 'harbor_tasks' / spec['task_id'])}],
    }


def trial_result_paths(job_dir):
    """Return immediate trial result.json paths, never nested submission files."""
    root = Path(job_dir)
    return sorted(p for p in root.glob("*/result.json") if p.is_file())


def main_oom_after_completion_allowed(errors, metadata, classification, reward,
                                      evidence, standard_harbor_rollout):
    """Accept a main-container OOM only after a completed, verified run.

    The main container can be OOM-killed by an orphaned tool process after the
    harness has already exited 0 and submitted. The verifier runs in its own
    container and is authoritative; a single terminal main OOM in that exact
    state must not turn a valid scientific result into an infra failure.
    """
    return (
        errors == ['main_terminal_oom_or_missing']
        and metadata.get('agent_exit') == 0
        and metadata.get('agent_stop_reason') == 'completed'
        and classification.get('infrastructure_ok') is True
        and classification.get('verifier_exit') in (0, 1)
        and reward in (0, 1)
        and evidence.get('main_stopped_before_verifier') is True
        and standard_harbor_rollout is True
    )


def read_scoped_token(path):
    fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW)
    try:
        info=os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_mode&0o077 or info.st_size>4096:
            raise ValueError('unsafe_scoped_token_file')
        token=os.read(fd,4097).decode().strip()
        if not token.startswith('qr_') or len(token)<24:
            raise ValueError('scoped_run_token_required')
        return token
    finally:os.close(fd)


def classify_verifier(trial_path, exit_code, reward):
    # Pure evidence helper; imports no scientific task implementation.
    from verifier_catalog_probe import INFRA_MARKERS, read_regular
    verdict={'verifier_exit':exit_code,'reward':reward,'infrastructure_ok':False,
             'verifier_scientific_rejection':False,'reports':[]}
    if exit_code not in (0,1) or type(reward) not in (int,float) or reward not in (0,1):return verdict
    try:
        log=read_regular(trial_path/'verifier/test-stdout.txt',1024*1024)
        if any(marker in log for marker in INFRA_MARKERS):return verdict
        for rel in ['verifier/score_report.json','verifier/artifacts/score_report.json',
                    'artifacts/score_report.json','artifacts/logs/artifacts/score_report.json']:
            path=trial_path/rel
            if path.exists():
                report=json.loads(read_regular(path,1024*1024))
                if not isinstance(report,dict) or not report:return verdict
                verdict['reports'].append(rel)
        if exit_code==1 and (reward!=0 or not verdict['reports']):return verdict
        verdict.update(infrastructure_ok=True,verifier_scientific_rejection=exit_code==1)
    except (OSError,ValueError):pass
    return verdict


async def run(spec):
    if os.environ.get('QIQC_NATIVE_EXECUTION_HOST')!='e2b':
        raise RuntimeError('native_runner_requires_explicit_e2b_host')
    spec=validate_spec(spec)
    submission_manifest(spec['submission_zip'],spec['submission_sha256'])
    output=Path(spec['output_dir']);output.mkdir(mode=0o700,parents=True,exist_ok=True)
    if (output/spec['run_id']).exists():raise RuntimeError('native_trial_output_already_exists')
    control=output/('.control-'+spec['run_id']);control.mkdir(mode=0o700)
    from native_compose_agent import RUNS
    from native_compose_environment import EVIDENCE, EXECUTION_BINDINGS
    run_spec = {**spec, 'control_dir': str(control)}
    spec_path = control / 'native-spec.json'
    spec_path.write_text(json.dumps(run_spec, sort_keys=True))
    spec_path.chmod(0o600)
    RUNS[spec['run_id']]={**spec,'control_dir':control,'scoped_token':read_scoped_token(spec['scoped_token_file'])}
    receipt={'schema':'qiqc-native-compose-receipt/v1','run_id':spec['run_id'],
             'submission_sha256':spec['submission_sha256'],'infrastructure_ok':False,
             'standard_harbor_rollout':False,'agent_budget_seconds':spec['agent_budget_seconds'],
             'candidate_identity':spec['candidate_identity'],'execution_kind':'uploaded-native-zip'}
    receipt.update(model_id=model_from_identity(spec['candidate_identity']),
                   diagnostic_model=spec['candidate_identity'].get('diagnostic_model',''))
    try:
        jobs_dir = output / 'harbor-jobs'
        jobs_dir.mkdir(mode=0o700)
        config_path = control / 'harbor-job.yaml'
        config_path.write_text(yaml.safe_dump(make_harbor_job_config(spec, control, jobs_dir), sort_keys=False))
        config_path.chmod(0o600)
        # This is the same official lifecycle used by ``harbor run``'s CLI
        # implementation: Job.create -> Job.run.  Keeping it in this process
        # is required because the E2B relay and the environment evidence are
        # intentionally scoped to this Harbor job.  The previous subprocess
        # wrapper could be reaped by the E2B background shell before the job
        # finished, leaving only an initial result.json with a running trial.
        from harbor.job import Job
        from harbor.models.job.config import JobConfig
        harbor_config = JobConfig.model_validate(make_harbor_job_config(spec, control, jobs_dir))
        job = await Job.create(harbor_config)
        job_result = await job.run()
        (output / 'native-harbor-job.json').write_text(
            json.dumps(job_result.model_dump(mode='json'), indent=2)
        )
        trial_results = trial_result_paths(job.job_dir)
        if len(trial_results) != 1:
            raise RuntimeError('native_harbor_trial_result_missing_or_ambiguous')
        trial_path = trial_results[0].parent
        canonical_trial = output / spec['run_id']
        shutil.copytree(trial_path, canonical_trial)
        data=json.loads((canonical_trial/'result.json').read_text())
        evidence_path = control / 'native-evidence.json'
        if evidence_path.exists():
            EVIDENCE[spec['run_id']] = json.loads(evidence_path.read_text())
        metadata=(data.get('agent_result') or {}).get('metadata') or {}
        reward=((data.get('verifier_result') or {}).get('rewards') or {}).get('reward')
        evidence=EVIDENCE.get(spec['run_id'],{})
        from native_final_containers import validate_final_containers
        terminal=evidence.get('final_container_evidence',{})
        terminal_errors=validate_final_containers(terminal,evidence.get('runtime_observation',{}),
                                                  evidence,evidence.get('verifier_exit'))
        classification=classify_verifier(output/spec['run_id'],evidence.get('verifier_exit'),reward)
        standard_harbor_rollout=(output/spec['run_id']/'result.json').is_file()
        if main_oom_after_completion_allowed(
                terminal_errors, metadata, classification, reward, evidence,
                standard_harbor_rollout):
            terminal['warning']='main_oom_after_agent_completion'
            terminal_errors=[]
        terminal.update(errors=terminal_errors,verified=not terminal_errors)
        receipt.update({key:metadata.get(key) for key in ('agent_exit','budget_exhausted','agent_stop_reason','runtime_seconds',
                        'execution_duration_class','observation_policy')})
        receipt.update(reward=reward,verifier_exit=classification['verifier_exit'],
                       verifier_scientific_rejection=classification['verifier_scientific_rejection'],
                       verifier_classification=classification,security_evidence={k:v for k,v in evidence.items() if k!='runtime_observation'},
                       runtime_observation=evidence.get('runtime_observation',{}),
                       final_container_evidence=terminal,
                       standard_harbor_rollout=(output/spec['run_id']/'result.json').is_file())
        receipt['infrastructure_ok']=(not data.get('exception_info') and classification['infrastructure_ok']
            and receipt['standard_harbor_rollout'] and 'agent' in evidence and 'verifier' in evidence
            and metadata.get('execution_kind')=='uploaded-native-zip' and not terminal_errors)
        if terminal_errors:receipt['failure_class']='NativeTerminalContainerEvidenceFailure'
        if data.get('exception_info'):
            receipt['failure_class']=(data.get('exception_info') or {}).get('exception_type','HarborFailure')
    except Exception as exc:
        receipt['failure_class']=type(exc).__name__
        receipt['security_evidence']=EVIDENCE.get(spec['run_id'],{})
        raise
    finally:
        RUNS.pop(spec['run_id'],None);EVIDENCE.pop(spec['run_id'],None)
        EXECUTION_BINDINGS.pop(spec['run_id'], None)
        (output/'native-receipt.json').write_text(json.dumps(receipt,indent=2)+'\n')
    return receipt


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--spec',required=True);args=parser.parse_args()
    spec=json.loads(Path(args.spec).read_text())
    # Harbor logs and any private scientific stdout belong to the protected
    # output file; stdout carries only the safe receipt for the orchestrator.
    output=Path(spec['output_dir']);output.mkdir(mode=0o700,parents=True,exist_ok=True)
    import contextlib
    with (output/'native-host.log').open('a') as log,contextlib.redirect_stdout(log),contextlib.redirect_stderr(log):
        try:receipt=asyncio.run(run(spec))
        except Exception:
            import traceback
            traceback.print_exc()
            path=output/'native-receipt.json'
            receipt=json.loads(path.read_text()) if path.exists() else {'infrastructure_ok':False,'failure_class':'NativeRunnerSetupFailure'}
    print(json.dumps({key:receipt.get(key) for key in ('schema','run_id','infrastructure_ok','failure_class','agent_exit','verifier_exit','reward')}))
    return 0 if receipt.get('infrastructure_ok') else 1


if __name__=='__main__':sys.exit(main())
