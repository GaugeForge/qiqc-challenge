"""Organizer model profiles bind capabilities without exporting key/URL to Agents."""
import copy
import json
import sys
from pathlib import Path

import pytest

SCIENCE = Path(__file__).resolve().parents[1] / 'scripts/science'
sys.path.insert(0,str(SCIENCE))
from native_provider_profile import load_profile, profile_identity, validate_profile


def fixture():
    return json.loads((Path(__file__).resolve().parents[1]/'test-fixtures/provider-profile-canonical.json').read_text())


def test_canonical_fixture_matches_go_and_safe_identity_has_no_secrets():
    value=fixture()
    assert profile_identity(value['profile'])==value['identity']
    assert not {'key_file','base_url'} & profile_identity(value['profile']).keys()


@pytest.mark.parametrize('change',[
    {'id':'UPPER'}, {'id':'a'*65}, {'quota_group':'unsafe.dot'},
    {'base_url':'http://openrouter.ai/api/v1'}, {'base_url':'https://localhost/api'},
    {'base_url':'https://127.0.0.1/api'}, {'base_url':'https://api.example.com:8443/v1'},
    {'base_url':'https://api.example.com/%2e%2e/v1'},
    {'allow_paid_fallback':True}, {'unknown':True}, {'model':'paid-model'},
    {'requests_per_minute':21}, {'requests_per_day':1001}, {'key_file':'relative.key'},
])
def test_reject_malformed_or_unapproved_free_profile(change):
    value={**fixture()['profile'],**change}
    with pytest.raises(ValueError):validate_profile(value)


def test_paid_profile_limit_is_organizer_configured_not_free_vendor_hardcode():
    value={**fixture()['profile'],'billing':'paid','model':'organizer-approved-model',
           'base_url':'https://api.example.com/v1','requests_per_minute':5000,'requests_per_day':500000}
    assert validate_profile(value)==value
    assert profile_identity(value)['model']=='organizer-approved-model'


def test_profile_path_permissions_parent_links_and_duplicate_fields(tmp_path):
    path=tmp_path/'profile.json';path.write_text(json.dumps(fixture()['profile']));path.chmod(0o640)
    assert load_profile(path)[1]==fixture()['identity']
    path.chmod(0o644)
    with pytest.raises(ValueError):load_profile(path)
    path.chmod(0o600)
    link=tmp_path/'linked';link.symlink_to(tmp_path,target_is_directory=True)
    with pytest.raises(ValueError):load_profile(link/'profile.json')
    path.write_text('{"id":"first",'+json.dumps(fixture()['profile'])[1:])
    with pytest.raises(ValueError,match='duplicate'):load_profile(path)
