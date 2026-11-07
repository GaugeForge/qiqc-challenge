"""Public export uses only the public Agent log and redacts scoped credentials."""
import json
from pathlib import Path
import pytest
from native_public_view import export


def fixture(tmp_path):
    (tmp_path/'agent').mkdir();(tmp_path/'verifier').mkdir()
    (tmp_path/'verifier/truth.txt').write_text('PRIVATE_TRUTH_SENTINEL')
    (tmp_path/'native-receipt.json').write_text(json.dumps({'reward':0,'runtime_seconds':180.1,'agent_stop_reason':'budget_exhausted','agent_budget_seconds':180}))
    token=tmp_path/'token';token.write_text('scoped-secret-value')
    (tmp_path/'agent/native-trajectory.jsonl').write_text('public agent output scoped-secret-value Bearer another-private-value')
    return token


def test_only_agent_output_and_no_credentials(tmp_path):
    export(tmp_path,fixture(tmp_path))
    value=(tmp_path/'public-view.txt').read_text()
    assert 'public agent output' in value
    assert 'scoped-secret-value' not in value and 'another-private-value' not in value
    assert 'PRIVATE_TRUTH_SENTINEL' not in value
    assert json.loads(value)['trajectory_available'] is True


def test_agent_symlink_cannot_export_other_material(tmp_path):
    token=fixture(tmp_path);source=tmp_path/'agent/native-trajectory.jsonl';source.unlink();source.symlink_to(tmp_path/'verifier/truth.txt')
    with pytest.raises((ValueError,OSError)):export(tmp_path,token)
    assert not (tmp_path/'public-view.txt').exists()


def test_existing_export_not_overwritten(tmp_path):
    token=fixture(tmp_path);export(tmp_path,token)
    with pytest.raises(FileExistsError):export(tmp_path,token)
