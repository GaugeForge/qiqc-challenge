"""Synthetic secrets only: actual public trajectory redaction contract."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts/science"))
from public_view import public_view_json, trajectory_view


def test_exact_tokens_and_generic_headers(tmp_path):
    path = tmp_path / "trajectory.jsonl"
    path.write_text(
        'progress tool result\nBearer abc.def-123\n{"api_key":"sk-test-123", "traffic_access_token":"qsimsecret"}\nHY_API=hytest123\nmodelsecret'
    )
    result = trajectory_view(path, secrets=["qsimsecret", "modelsecret"])
    for secret in (
        "abc.def-123",
        "sk-test-123",
        "qsimsecret",
        "hytest123",
        "modelsecret",
    ):
        assert secret not in result["trajectory_excerpt"]
    assert "progress tool result" in result["trajectory_excerpt"]
    assert result["trajectory_truncated"] is False


def test_redaction_before_byte_limit(tmp_path):
    path = tmp_path / "trajectory.jsonl"
    path.write_text("科学" * 20 + "private-token" + "x" * 100)
    result = trajectory_view(path, secrets=["private-token"], limit=125)
    assert result["trajectory_truncated"] is True
    assert len(result["trajectory_excerpt"].encode()) <= 125
    assert "private" not in result["trajectory_excerpt"]


def test_nested_escaped_config(tmp_path):
    path = tmp_path / "trajectory.jsonl"
    path.write_text(r'{"cmd":"API_KEY=hidden123 QIQC_MODEL_TOKEN=hidden456"}')
    text = trajectory_view(path)["trajectory_excerpt"]
    assert "hidden123" not in text
    assert "hidden456" not in text


def test_final_serialized_size_with_escape_expansion():
    import json

    serialized = public_view_json(
        {"trajectory_excerpt": "\x01" * (200 * 1024), "trajectory_truncated": False}
    )
    assert len(serialized.encode()) <= 480 * 1024
    assert json.loads(serialized)["trajectory_truncated"] is True


def test_final_unicode_wire_size():
    serialized = public_view_json(
        {"trajectory_excerpt": "科学" * 100000, "trajectory_truncated": False}
    )
    assert len(serialized.encode()) <= 480 * 1024
    assert "科学" in serialized
