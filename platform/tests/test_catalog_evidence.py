"""Evidence must bind task, uploaded bytes, deployed roles and actual reward."""
import hashlib
import io
import json
from pathlib import Path
import sys
import tarfile

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts/science'))
import verify_catalog_evidence as evidence
from task_release import identity


@pytest.fixture
def case(monkeypatch):
    task = 'task_00'
    names = {role: f'{role}/allowed.txt' for role in ['main', 'qsim', 'verifier']}
    release = {'release_id': 'release', 'runtime_sha256': 'a' * 64,
               'tasks': {f'task_{i:02d}': {'execution_sha256': 'b' * 64} for i in range(49)},
               'files': {p: {'sha256': 'c' * 64} for p in names.values()}}
    monkeypatch.setattr(evidence, '_single_task_files', lambda task, role, files: [evidence.RUNTIME / names[role]])
    target = identity(release, task)
    metadata = {'submission_sha256': 'd' * 64, 'benchmark_release': target,
                'agent_budget_seconds': 300, 'agent_exit': 0, 'budget_exhausted': False,
                'agent_stop_reason': 'completed', 'main_network': 'all-egress-disabled',
                'qsim_transport': 'controller-file-relay',
                'model_calls': [{'status':200,'response_contract':{'schema':'qiqc-model-response/v2','success':True},'diagnostic_model_contract':'qiqc-science-diagnostic-model/v3','request_parameters':{'schema':'qiqc-model-request-parameters/v1','output_token_limits':{}},'adapter_request_sha256':'e'*64,'forwarded_request_sha256':'e'*64}],
                'model_contract': {'version':'qiqc-science-diagnostic-model/v3','scope':'catalog-diagnostic-only','parameter_policy':'adapter-passthrough','provider_attempt_budget':100,'budget_authority':'go-persisted-run-attempt-admissions'},
                'qsim_relay_calls': [{'tool': 'measure', 'success': True}],
                'boundary_probe': {**{k: True for k in ['private_paths_absent', 'master_credentials_absent', 'qsim_remote_capability_absent']},
                                   **{k: {'transport': 'TimeoutError'} for k in ['public_internet', 'e2b_management']}}}
    manifests = {role: {'task_id': task, 'role': role,
                        'files': [{'path': p, 'sha256': 'c' * 64}], 'benchmark_release': target} for role, p in names.items()}
    checks = {role: {'files_verified': 1, 'manifest_sha256': hashlib.sha256(json.dumps(m, indent=2).encode()).hexdigest()} for role, m in manifests.items()}
    files = {'result.json': {'agent_result': {'metadata': metadata}, 'verifier_result': {'rewards': {'reward': 0}}},
             'benchmark-release.json': target, 'artifacts/role-manifests.json': manifests,
             'artifacts/role-deployment-checks.json': checks, 'verifier/reward.txt': '0',
             'verifier/test-stdout.txt': '',
             'verifier/classification.json': {'verifier_exit': 0, 'reward': 0, 'infrastructure_ok': True,
                                             'process_log_sha256': hashlib.sha256(b'').hexdigest()}}
    browser = {'catalog_index': 0, 'benchmark_release': target}
    index = {'submission_sha256': 'd' * 64, 'reward': 0}
    def run():
        stream = io.BytesIO()
        with tarfile.open(fileobj=stream, mode='w:gz') as archive:
            for name, value in files.items():
                raw = (value if isinstance(value, str) else json.dumps(value, sort_keys=True)).encode()
                info = tarfile.TarInfo(name);info.size = len(raw)
                archive.addfile(info, io.BytesIO(raw))
        archive = stream.getvalue();index['sha256'] = hashlib.sha256(archive).hexdigest()
        return evidence.inspect_catalog(archive, index, browser, release)
    return files, metadata, index, browser, run


def test_complete_evidence_is_verified(case):
    assert case[-1]() ['benchmark_release_verified'] is True


def test_budget_stop_remains_explicit(case):
    case[1].update(budget_exhausted=True, agent_exit=124, agent_stop_reason='budget_exhausted')
    assert case[-1]() ['budget_exhausted'] is True


@pytest.mark.parametrize('mutation,reason', [
    (lambda f,m,i,b: i.update(reward=1), 'verifier_reward_mismatch'),
    (lambda f,m,i,b: m.update(submission_sha256='e' * 64), 'failed_trial_or_wrong_submission'),
    (lambda f,m,i,b: m.update(agent_exit=1), 'unexpected_agent_exit'),
    (lambda f,m,i,b: m.update(agent_budget_seconds=600), 'agent_budget_mismatch'),
    (lambda f,m,i,b: m['boundary_probe'].update(private_paths_absent=False), 'private_boundary_failed'),
    (lambda f,m,i,b: m['boundary_probe']['public_internet'].update(status=200), 'fixed_network_probe_failed'),
    (lambda f,m,i,b: f['artifacts/role-manifests.json']['main']['files'][0].update(path='private/truth'), 'role_file_inventory_mismatch'),
    (lambda f,m,i,b: f['artifacts/role-deployment-checks.json']['main'].update(files_verified=0), 'deployed_role_hash_mismatch'),
    (lambda f,m,i,b: f.pop('artifacts/role-deployment-checks.json'), 'missing_catalog_evidence'),
    (lambda f,m,i,b: f['verifier/classification.json'].update(verifier_exit=2), 'verifier_process_evidence_mismatch'),
    (lambda f,m,i,b: f['verifier/classification.json'].update(verifier_exit=1), 'unclassified_verifier_exit'),
    (lambda f,m,i,b: f.update({'verifier/test-stdout.txt': 'changed'}), 'verifier_process_evidence_mismatch'),
])
def test_tampering_cannot_count_as_complete(case, mutation, reason):
    mutation(*case[:4])
    with pytest.raises(evidence.EvidenceError, match=reason):
        case[-1]()


def test_legacy_stream_failure_recovery_uses_gateway_terminal_evidence():
    report=evidence.audit_model_evidence([{'status':200}]*3,[200,502,200])
    assert report['failed_responses']==report['recovered_failed_responses']==1
    assert report['extra_gateway_attempts']==0
    assert report['terminal_evidence_source']=='gateway_terminal_status_correlated_one_to_one'


@pytest.mark.parametrize('statuses',[[200,502],[502],[200,502,200]])
def test_legacy_http200_cannot_hide_last_failure_or_ambiguous_retries(statuses):
    with pytest.raises(evidence.EvidenceError):
        evidence.audit_model_evidence([{'status':200}]*2,statuses)


def test_explicit_contract_distinguishes_gateway_retry_from_native_retry():
    result=evidence.audit_model_evidence([{'status':200,'response_contract':{'success':True}}],[502,200])
    assert result['extra_gateway_attempts']==1 and result['failed_responses']==0


@pytest.mark.parametrize('change,reason',[
    (lambda m:m['model_contract'].update(scope='formal-competition'),'diagnostic_model_contract_mismatch'),
    (lambda m:m['model_contract'].update(parameter_policy='force-output'),'diagnostic_model_contract_mismatch'),
    (lambda m:m['model_contract'].update(provider_attempt_budget=99),'diagnostic_model_contract_mismatch'),
    (lambda m:m['model_contract'].update(budget_authority='memory-counter'),'diagnostic_model_contract_mismatch'),
    (lambda m:m['model_calls'][0].update(diagnostic_model_contract='old'),'effective_model_request_contract_mismatch'),
    (lambda m:m['model_calls'][0].update(forwarded_request_sha256='f'*64),'effective_model_request_contract_mismatch'),
    (lambda m:m['model_calls'][0]['request_parameters'].update(output_token_limits={'max_tokens':True}),'effective_model_request_contract_mismatch'),
    (lambda m:m['model_calls'][0]['request_parameters'].update(output_token_limits={'max_tokens':-1}),'effective_model_request_contract_mismatch'),
    (lambda m:m['model_calls'][0]['response_contract'].pop('schema'),'incomplete_model_terminal_contract'),
])
def test_model_contract_and_actual_requests_cannot_diverge(case,change,reason):
    change(case[1])
    with pytest.raises(evidence.EvidenceError,match=reason):case[-1]()
