"""Browser acceptance must reject advice from a different model configuration."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / 'scripts'))
from cloud_browser_check import expected_advisory_provider


def profile():
    return {'schema': 'qiqc-provider-profile-identity/v1', 'id': 'other-provider',
            'sha256': 'a' * 64, 'model': 'another-model',
            'endpoints': ['responses', 'chat/completions'],
            'quota_group': 'shared-account', 'authentication': 'bearer',
            'parameter_policy': 'passthrough/v1', 'billing': 'free'}


def test_legacy_and_explicit_free_are_distinct():
    assert expected_advisory_provider(None) == 'openrouter-hy4-chat-v3'
    assert expected_advisory_provider({}) == 'openrouter-hy4-chat-v3'
    assert expected_advisory_provider({'diagnostic_model': 'minimax/minimax-m3.1-flash:free'}) == 'openrouter-minimax-free-infra-v1'
    with pytest.raises(ValueError):
        expected_advisory_provider({'diagnostic_model': 'unapproved'})


def test_profile_binds_exact_version_without_credentials():
    p = profile()
    assert expected_advisory_provider({'provider_profile': p}) == 'profile-other-provider-aaaaaaaaaaaa'
    p['sha256'] = 'b' * 64
    assert expected_advisory_provider({'provider_profile': p}) == 'profile-other-provider-bbbbbbbbbbbb'


def test_profile_must_support_review_interface():
    p = profile()
    p['endpoints'] = ['responses']
    with pytest.raises(ValueError, match='review_requires_chat_capability'):
        expected_advisory_provider({'provider_profile': p})


def test_invalid_profile_cannot_fall_back_to_hy4():
    with pytest.raises(ValueError):
        expected_advisory_provider({'provider_profile': {'id': 'unbound'}})
