"""Malformed advice may use exact organizer manual review; never provider errors."""
import hashlib
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / 'scripts'))
from cloud_browser_check import validate_invalid_advice_fallback
from catalog_browser_suite import parse_args


def inputs(tmp_path):
    archive = b'organizer-fixed-reviewed-fixture'
    sha = hashlib.sha256(archive).hexdigest()
    manifest = tmp_path / 'manifest.json'
    manifest.write_text(json.dumps({'codex': {'sha256': sha}}))
    return dict(candidate={'adapter_manifest': str(manifest)}, adapter='codex',
                archive_bytes=archive, version={'sha256': sha},
                result={'outcome': 'invalid_response', 'status': 'manual_required',
                        'policy_version': 'qiqc-readonly-advisory/v3',
                        'provider_version': 'openrouter-hy4-chat-v3',
                        'failure_code': 'explanation_bounds'},
                quota_before={'infra': 10}, quota_after={'infra': 10},
                pending_state='pending_review', batch_count=0)


def test_invalid_advice_requires_exact_template_and_preserves_failure(tmp_path):
    result = validate_invalid_advice_fallback(**inputs(tmp_path))
    assert result['outcome'] == 'invalid_response'
    assert result['failure_code'] == 'explanation_bounds'
    assert result['real_advice_available'] is False
    assert 'manual_fallback_verified' not in result  # Only set after actual UI approval.


@pytest.mark.parametrize('outcome', ['provider_error', 'provider_timeout', 'provider_unavailable', 'advice_ready'])
def test_other_outcomes_never_get_manual_exception(tmp_path, outcome):
    values = inputs(tmp_path)
    values['result']['outcome'] = outcome
    with pytest.raises(ValueError):
        validate_invalid_advice_fallback(**values)


@pytest.mark.parametrize('field,value', [('archive_bytes', b'changed'), ('version', {'sha256': 'f'*64}),
    ('pending_state', 'approved'), ('batch_count', 1), ('quota_after', {'infra': 9}), ('candidate', None)])
def test_changed_identity_or_consumed_quota_rejects(tmp_path, field, value):
    values = inputs(tmp_path)
    values[field] = value
    with pytest.raises(ValueError):
        validate_invalid_advice_fallback(**values)


@pytest.mark.parametrize('field,value', [('policy_version', 'other'), ('provider_version', 'other'), ('status', 'approved')])
def test_wrong_review_contract_rejects(tmp_path, field, value):
    values = inputs(tmp_path)
    values['result'][field] = value
    with pytest.raises(ValueError):
        validate_invalid_advice_fallback(**values)


def test_manual_exception_is_explicit_cli_opt_in(tmp_path):
    base = ['--indices', '1', '--output-dir', str(tmp_path)]
    assert not parse_args(base).allow_invalid_advice_manual_review
    assert parse_args(base + ['--allow-invalid-advice-manual-review']).allow_invalid_advice_manual_review


def test_unknown_adapter_cannot_use_another_organizer_manifest_entry(tmp_path):
    values = inputs(tmp_path)
    values['adapter'] = 'unknown'
    with pytest.raises(ValueError, match='exact_organizer_template'):
        validate_invalid_advice_fallback(**values)


def test_unapproved_zip_matching_uploaded_version_is_still_rejected(tmp_path):
    values = inputs(tmp_path)
    values['archive_bytes'] = b'unreviewed-participant-harness'
    values['version']['sha256'] = hashlib.sha256(values['archive_bytes']).hexdigest()
    with pytest.raises(ValueError, match='exact_organizer_template'):
        validate_invalid_advice_fallback(**values)


def test_changed_profile_digest_cannot_accept_old_provider_advice(tmp_path):
    values = inputs(tmp_path)
    values['candidate']['provider_profile'] = {
        'schema': 'qiqc-provider-profile-identity/v1', 'id': 'review-model',
        'sha256': 'b' * 64, 'model': 'approved-model', 'endpoints': ['chat/completions'],
        'quota_group': 'shared', 'authentication': 'bearer',
        'parameter_policy': 'passthrough/v1', 'billing': 'free'}
    values['result']['provider_version'] = 'profile-review-model-' + 'a' * 12
    with pytest.raises(ValueError, match='identity_mismatch'):
        validate_invalid_advice_fallback(**values)


def test_actual_ui_labels_invalid_advice_as_manual_and_renders_no_findings():
    import shutil
    import subprocess
    node = shutil.which('node')
    if node is None:
        pytest.skip('Node required to execute actual UI rendering functions')
    app = (Path(__file__).parents[1] / 'web/app.js').read_text()
    functions = app[app.index('function reviewAdviceLabel('):app.index('function renderAdmin(')]
    script = functions + '''
const invalid={outcome:'invalid_response',summary:'UNTRUSTED',findings:[{explanation:'UNTRUSTED'}]};
process.stdout.write(JSON.stringify({label:reviewAdviceLabel(invalid),html:reviewAdviceHTML(invalid),
 ready:reviewAdviceLabel({outcome:'advice_ready',provider_version:'profile-test'})}));
'''
    rendered = json.loads(subprocess.run([node, '-e', script], check=True, capture_output=True, text=True).stdout)
    assert rendered == {'label': 'Review incomplete or stale · check the version status and reason', 'html': '', 'ready': 'AI risk advice · requires review'}


def limited_reasoning_result(values):
    values['result'].update(outcome='provider_error', failure_code='completion_token_limit',
        provider_diagnostics={'finish_reason': 'length', 'prompt_tokens': 1152,
            'completion_tokens': 8192, 'reasoning_tokens': 8192, 'max_tokens': 8192})
    return values


@pytest.mark.parametrize('outcome', ['advice_ready', 'invalid_response', 'provider_timeout'])
def test_explicit_organizer_policy_known_terminal_still_checks_exact_zip(tmp_path, outcome):
    values = inputs(tmp_path)
    values['result'].update(outcome=outcome, manual_followup_required=True)
    result = validate_invalid_advice_fallback(**values, organizer_template=True)
    assert result['outcome'] == outcome
    assert not result['ai_quality_gate_passed']
    assert result['real_advice_available'] == (outcome == 'advice_ready')
    values['archive_bytes'] = b'unknown'
    values['version']['sha256'] = hashlib.sha256(b'unknown').hexdigest()
    with pytest.raises(ValueError, match='exact_organizer_template'):
        validate_invalid_advice_fallback(**values, organizer_template=True)


def test_known_reasoning_limit_permitted_only_in_explicit_organizer_mode(tmp_path):
    values = limited_reasoning_result(inputs(tmp_path))
    result = validate_invalid_advice_fallback(**values, organizer_template=True)
    assert result['failure_code'] == 'completion_token_limit'
    assert not result['real_advice_available']
    with pytest.raises(ValueError):
        validate_invalid_advice_fallback(**values)


@pytest.mark.parametrize('field,value', [('finish_reason', 'stop'), ('prompt_tokens', 0),
    ('completion_tokens', 8191), ('reasoning_tokens', 8191), ('max_tokens', 4096), ('completion_tokens', True)])
def test_partial_or_wrong_token_metadata_never_bypasses_provider_failure(tmp_path, field, value):
    values = limited_reasoning_result(inputs(tmp_path))
    values['result']['provider_diagnostics'][field] = value
    with pytest.raises(ValueError):
        validate_invalid_advice_fallback(**values, organizer_template=True)


@pytest.mark.parametrize('failure', ['auth_error', 'quota_error', 'transport_error', None, 'unknown'])
def test_provider_error_is_not_general_manual_fallback(tmp_path, failure):
    values = limited_reasoning_result(inputs(tmp_path))
    values['result']['failure_code'] = failure
    with pytest.raises(ValueError):
        validate_invalid_advice_fallback(**values, organizer_template=True)
