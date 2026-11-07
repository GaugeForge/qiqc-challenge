#!/usr/bin/env python3
"""Independent private R2 verification for one admin-bound catalog browser run.

Only safe counters and digests leave memory. No hidden task payloads or raw
rollouts are extracted to the host or included in the public-safe report.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import sqlite3
import sys
import tarfile
from pathlib import Path

from e2b_runtime import ROOT, RUNTIME, _single_task_files
from task_release import identity, verify_release
from verify_cloud_relay_evidence import (
    EvidenceError, fetch_archive, lookup_run, read_local_json, read_r2_config,
)
from verify_public import validate_archive

sys.path.insert(0, str(ROOT / "benchmark-assets/adapters/science-probe"))
from model_response_contract import recovery_summary


def audit_model_evidence(events, gateway_statuses=None):
    if not isinstance(events, list) or not events or len(events)>100 or any(not isinstance(e,dict) or e.get("status")!=200 for e in events):
        raise EvidenceError("invalid_agent_model_http_evidence")
    explicit=all(isinstance(e.get("response_contract"),dict) for e in events)
    if explicit:
        recovery=recovery_summary(events)
        source="archived_response_terminal_contract"
    elif gateway_statuses is not None and len(gateway_statuses)==len(events):
        # Old rollouts did not retain stream classification. With exactly one
        # gateway attempt per agent request, the trusted gateway's terminal
        # classifier supplies it without guessing from HTTP 200 or token usage.
        recovery=recovery_summary([{"status":e["status"],"response_contract":{"success":status==200}} for e,status in zip(events,gateway_statuses)])
        source="gateway_terminal_status_correlated_one_to_one"
    else:
        raise EvidenceError("missing_unambiguous_model_terminal_evidence")
    if not recovery["all_failures_recovered"]:
        raise EvidenceError("unrecovered_model_response_failure")
    result={"agent_visible_request_count":len(events),"agent_visible_http_failure_count":0,
            "terminal_evidence_source":source,
            "complete_vs_incomplete_distinction_verified":all((e.get("response_contract") or {}).get("schema")=="qiqc-model-response/v2" for e in events),
            **recovery}
    if gateway_statuses is not None:
        if len(gateway_statuses)<len(events):raise EvidenceError("missing_gateway_attempt_records")
        result.update(gateway_attempt_count=len(gateway_statuses),
                      gateway_attempt_status_counts={str(code):gateway_statuses.count(code) for code in sorted(set(gateway_statuses))},
                      extra_gateway_attempts=len(gateway_statuses)-len(events))
    return result


def inspect_catalog(archive, index, browser, release, gateway_statuses=None):
    ordinal = browser.get('catalog_index')
    if type(ordinal) is not int or not 0 <= ordinal < 49:
        raise EvidenceError('invalid_catalog_index')
    task = sorted(release['tasks'])[ordinal]
    expected_release = identity(release, task)
    if browser.get('benchmark_release') != expected_release:
        raise EvidenceError('browser_release_mismatch')
    if hashlib.sha256(archive).hexdigest() != index['sha256']:
        raise EvidenceError('archive_sha_mismatch')
    validate_archive(archive)
    selected = {}
    required = {'result.json', 'benchmark-release.json', 'artifacts/role-manifests.json', 'artifacts/role-deployment-checks.json', 'verifier/reward.txt', 'verifier/classification.json', 'verifier/test-stdout.txt'}
    optional = {'artifacts/score_report.json', 'verifier/score_report.json'}
    with tarfile.open(fileobj=io.BytesIO(archive), mode='r:gz') as source:
        for member in source:
            if member.name not in required | optional:
                continue
            if not member.isfile() or member.size > 4 * 1024 * 1024:
                raise EvidenceError('invalid_evidence_member')
            with source.extractfile(member) as stream:
                selected[member.name] = stream.read(4 * 1024 * 1024 + 1)
    if not required <= set(selected):
        raise EvidenceError('missing_catalog_evidence')
    if json.loads(selected['benchmark-release.json']) != expected_release:
        raise EvidenceError('archive_release_mismatch')
    result = json.loads(selected['result.json'])
    metadata = (result.get('agent_result') or {}).get('metadata') or {}
    if result.get('exception_info') or metadata.get('submission_sha256') != index['submission_sha256']:
        raise EvidenceError('failed_trial_or_wrong_submission')
    if metadata.get('benchmark_release') != expected_release:
        raise EvidenceError('result_release_mismatch')
    expected_model_contract={'version':'qiqc-science-diagnostic-model/v3','scope':'catalog-diagnostic-only','parameter_policy':'adapter-passthrough','provider_attempt_budget':100,'budget_authority':'go-persisted-run-attempt-admissions'}
    if metadata.get('model_contract') != expected_model_contract:
        raise EvidenceError('diagnostic_model_contract_mismatch')
    model_evidence = audit_model_evidence(metadata.get('model_calls'), gateway_statuses)
    if not model_evidence['complete_vs_incomplete_distinction_verified']:
        raise EvidenceError('incomplete_model_terminal_contract')
    for event in metadata['model_calls']:
        parameters=event.get('request_parameters')
        original=event.get('adapter_request_sha256')
        forwarded=event.get('forwarded_request_sha256')
        if (event.get('diagnostic_model_contract') != expected_model_contract['version']
            or not isinstance(parameters,dict)
            or parameters.get('schema') != 'qiqc-model-request-parameters/v1'
            or not isinstance(parameters.get('output_token_limits'),dict)
            or any(k not in ('max_output_tokens','max_tokens','max_completion_tokens')
                   or (v is not None and (type(v) is not int or v <= 0))
                   for k,v in parameters.get('output_token_limits',{}).items())
            or not isinstance(original,str) or len(original)!=64
            or any(c not in '0123456789abcdef' for c in original)
            or original != forwarded):
            raise EvidenceError('effective_model_request_contract_mismatch')
    reward = ((result.get('verifier_result') or {}).get('rewards') or {}).get('reward')
    if type(reward) not in (int, float) or reward not in (0, 1) or reward != index['reward']:
        raise EvidenceError('verifier_reward_mismatch')
    if float(selected['verifier/reward.txt'].decode().strip()) != reward:
        raise EvidenceError('verifier_file_mismatch')
    classification = json.loads(selected['verifier/classification.json'])
    if classification.get('reward') != reward or classification.get('infrastructure_ok') is not True:
        raise EvidenceError('invalid_verifier_classification')
    verifier_exit = classification.get('verifier_exit')
    if verifier_exit not in (0, 1) or classification.get('process_log_sha256') != hashlib.sha256(selected['verifier/test-stdout.txt']).hexdigest():
        raise EvidenceError('verifier_process_evidence_mismatch')
    if verifier_exit == 1:
        reports = classification.get('reports')
        if reward != 0 or classification.get('verifier_scientific_rejection') is not True or not reports:
            raise EvidenceError('unclassified_verifier_exit')
        for item in reports:
            name = item.get('path')
            if name not in optional or name not in selected or hashlib.sha256(selected[name]).hexdigest() != item.get('sha256'):
                raise EvidenceError('verifier_report_digest_mismatch')
            document = json.loads(selected[name])
            if not isinstance(document, dict) or not document:
                raise EvidenceError('invalid_verifier_report')
    if metadata.get('agent_budget_seconds') != 300:
        raise EvidenceError('agent_budget_mismatch')
    exhausted = metadata.get('budget_exhausted') is True
    if exhausted:
        if metadata.get('agent_exit') not in (124, 137) or metadata.get('agent_stop_reason') != 'budget_exhausted':
            raise EvidenceError('invalid_budget_stop')
    elif metadata.get('agent_exit') != 0:
        raise EvidenceError('unexpected_agent_exit')
    if metadata.get('main_network') != 'all-egress-disabled' or metadata.get('qsim_transport') != 'controller-file-relay':
        raise EvidenceError('wrong_execution_boundary')
    boundary = metadata.get('boundary_probe') or {}
    for key in ['private_paths_absent', 'master_credentials_absent', 'qsim_remote_capability_absent']:
        if boundary.get(key) is not True:
            raise EvidenceError('private_boundary_failed')
    for key in ['public_internet', 'e2b_management']:
        probe = boundary.get(key) or {}
        if probe.get('status') is not None or not probe.get('transport') or probe['transport'] == 'response':
            raise EvidenceError('fixed_network_probe_failed')
    calls = metadata.get('qsim_relay_calls')
    if not isinstance(calls, list) or not calls or len(calls) > 128:
        raise EvidenceError('missing_qsim_execution_evidence')
    for call in calls:
        if not isinstance(call, dict) or type(call.get('success')) is not bool or not isinstance(call.get('tool'), str):
            raise EvidenceError('invalid_qsim_execution_evidence')
    manifests = json.loads(selected['artifacts/role-manifests.json'])
    deployment_checks = json.loads(selected['artifacts/role-deployment-checks.json'])
    if set(manifests) != {'main', 'qsim', 'verifier'}:
        raise EvidenceError('missing_role_manifest')
    if set(deployment_checks) != set(manifests):
        raise EvidenceError('missing_actual_role_deployment_checks')
    role_summary = {}
    for role, manifest in manifests.items():
        if manifest.get('role') != role or manifest.get('task_id') != task or manifest.get('benchmark_release') != expected_release:
            raise EvidenceError('role_identity_mismatch')
        expected_files = {
            p.relative_to(RUNTIME).as_posix(): release['files'][p.relative_to(RUNTIME).as_posix()]['sha256']
            for p in _single_task_files(task, role, release['files'])
        }
        files = manifest.get('files', [])
        actual_files = {f['path']: f['sha256'] for f in files}
        if len(actual_files) != len(files) or actual_files != expected_files:
            raise EvidenceError('role_file_inventory_mismatch')
        deployment = deployment_checks[role]
        # The aggregate rollout serializes dictionaries with sorted keys. The
        # sandbox hashed the original archive manifest, whose field order is
        # fixed by archive_runtime. Reconstruct that exact expected document,
        # using already verified role paths/digests, not aggregate key order.
        deployed_document = {
            'task_id': task, 'role': role,
            'files': [{'path': name, 'sha256': digest} for name, digest in sorted(expected_files.items())],
            'benchmark_release': expected_release,
        }
        if deployment.get('files_verified') != len(files) or deployment.get('manifest_sha256') != hashlib.sha256(json.dumps(deployed_document, indent=2).encode()).hexdigest():
            raise EvidenceError('deployed_role_hash_mismatch')
        role_summary[role] = {'file_count': len(files), 'manifest_sha256': hashlib.sha256(json.dumps(manifest, sort_keys=True, separators=(',', ':')).encode()).hexdigest()}
    return {'benchmark_release_verified': True, 'benchmark_release': expected_release,
            'catalog_index': ordinal, 'role_manifests_verified': role_summary,
            'model_response_evidence': model_evidence,
            'diagnostic_model_contract': expected_model_contract,
            'verifier_reward': reward, 'verifier_exit': verifier_exit, 'verifier_scientific_rejection': verifier_exit == 1,
            'agent_budget_seconds': 300, 'budget_exhausted': exhausted,
            'agent_exit': metadata.get('agent_exit'), 'agent_stop_reason': metadata.get('agent_stop_reason'),
            'qsim_call_count': len(calls), 'qsim_success_count': sum(c['success'] for c in calls),
            'private_boundary_probes_passed': True, 'fixed_network_probes_passed': True,
            'formal_hidden_isolation_accepted': False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--report', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--db', type=Path, default=Path('/var/lib/qiqc-staging/data/platform.db'))
    parser.add_argument('--r2-config', type=Path, default=Path('/etc/qiqc-staging/api-r2.env'))
    args = parser.parse_args()
    if args.output.exists():
        raise SystemExit('Refusing to overwrite evidence')
    report = {'status': 'failed', 'schema': 'qiqc-catalog-role-evidence/v1'}
    try:
        browser = read_local_json(args.report)
        index = lookup_run(args.db, browser, catalog=True)
        # The queued target must still be the immutable administrator binding.
        with sqlite3.connect(args.db.resolve().as_uri() + '?mode=ro', uri=True) as db:
            row = db.execute('SELECT task_index, agent_budget_seconds, release_id FROM batch_scientific_targets WHERE batch_id=?', (index['batch_id'],)).fetchone()
            gateway_statuses = [r[0] for r in db.execute('SELECT status FROM usage WHERE run_id=? AND attempt=(SELECT attempt FROM runs WHERE id=?) ORDER BY rowid', (index['run_id'],index['run_id']))]
        if row != (browser['catalog_index'], 300, browser['benchmark_release']['release_id']):
            raise EvidenceError('persisted_target_mismatch')
        release = verify_release(ROOT)
        archive = fetch_archive(read_r2_config(args.r2_config), index)
        report.update(inspect_catalog(archive, index, browser, release, gateway_statuses))
        report.update(status='passed', batch_id=index['batch_id'], archive_sha256=index['sha256'])
    except Exception as exc:
        report['failure_code'] = str(exc) if isinstance(exc, EvidenceError) else type(exc).__name__
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report))
    return 0 if report['status'] == 'passed' else 1


if __name__ == '__main__':
    raise SystemExit(main())
