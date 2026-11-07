"""Offline fixtures only; these tests never call R2/E2B or inspect real rollouts."""

import hashlib
import io
import json
import sqlite3
import tarfile

import pytest
from verify_cloud_relay_evidence import (
    TOOLS,
    EvidenceError,
    bounded_download,
    inspect_archive,
    lookup_run,
    read_r2_config,
)


def fixture(*, missing=None, boundary=True, context=True, mutate=None, extra=None):
    metadata = {
        "submission_sha256": "a" * 64,
        "agent_exit": 0,
        "main_network": "all-egress-disabled",
        "qsim_transport": "controller-file-relay",
        "qsim_relay_calls": [
            {"tool": name, "success": True} for name in sorted(TOOLS) if name != missing
        ],
        "boundary_probe": {
            "private_paths_absent": True,
            "master_credentials_absent": True,
            "qsim_remote_capability_absent": boundary,
            "public_internet": {"transport": "URLError"},
            "e2b_management": {"transport": "URLError"},
        },
    }
    if mutate:
        mutate(metadata)
    result = {
        "agent_result": {"metadata": metadata},
        "verifier_result": {"rewards": {"reward": 0}},
        "exception_info": None,
    }
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        files = {
            "result.json": json.dumps(result).encode(),
            "agent/trajectory.jsonl": b"PRIVATE TRAJECTORY MUST NOT APPEAR",
        }
        if context:
            files["agent/context.json"] = json.dumps({"metadata": metadata}).encode()
        if extra:
            files.update(extra)
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    data = buffer.getvalue()
    return data, {
        "sha256": hashlib.sha256(data).hexdigest(),
        "submission_sha256": "a" * 64,
        "reward": 0,
    }


def test_actual_metadata_summary_has_no_trajectory_or_arbitrary_fields():
    archive, index = fixture()
    result = inspect_archive(archive, index)
    assert {row["tool"] for row in result["qsim_tools"]} == TOOLS
    assert all(row["success"] and row["count"] == 1 for row in result["qsim_tools"])
    assert result["context_metadata_matches"] is True
    assert "PRIVATE" not in json.dumps(result)
    assert result["formal_hidden_isolation_accepted"] is False


def test_frozen_release_identity_is_read_from_actual_archive():
    expected = {
        "release_id": "test-v1",
        "runtime_sha256": "a" * 64,
        "task_execution_sha256": "b" * 64,
    }
    archive, index = fixture(
        mutate=lambda metadata: metadata.update(benchmark_release=expected),
        extra={"benchmark-release.json": json.dumps(expected).encode()},
    )
    assert (
        inspect_archive(archive, index, expected)["benchmark_release_verified"] is True
    )
    with pytest.raises(EvidenceError, match="benchmark_release_mismatch"):
        inspect_archive(archive, index, {**expected, "release_id": "test-v2"})
    archive, index = fixture()
    with pytest.raises(EvidenceError, match="benchmark_release_mismatch"):
        inspect_archive(archive, index, expected)


@pytest.mark.parametrize("tool", sorted(TOOLS))
def test_missing_tool_fails_instead_of_simulated_success(tool):
    archive, index = fixture(missing=tool)
    with pytest.raises(EvidenceError):
        inspect_archive(archive, index)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda m: m.update(main_network="allowlist"),
        lambda m: m.update(qsim_transport="direct-http"),
        lambda m: m.update(submission_sha256="b" * 64),
        lambda m: m["qsim_relay_calls"][0].update(success=False),
        lambda m: m["boundary_probe"]["public_internet"].update(status=403),
    ],
)
def test_evidence_cannot_claim_success_with_invalid_contract(mutate):
    archive, index = fixture(mutate=mutate)
    with pytest.raises(EvidenceError):
        inspect_archive(archive, index)


def test_digest_mismatch_fails_before_tar_processing():
    archive, index = fixture()
    index["sha256"] = "f" * 64
    with pytest.raises(EvidenceError, match="archive_sha_mismatch"):
        inspect_archive(archive, index)


def test_context_must_match_result_metadata():
    archive, index = fixture(extra={"agent/context.json": b'{"metadata":{}}'})
    with pytest.raises(EvidenceError, match="context_result_metadata_mismatch"):
        inspect_archive(archive, index)


def test_traversal_archive_rejected():
    archive, index = fixture(extra={"../secret": b"private"})
    with pytest.raises(EvidenceError, match="unsafe_archive"):
        inspect_archive(archive, index)


def test_duplicate_result_rejected():
    _original, index = fixture()
    target = io.BytesIO()
    with tarfile.open(fileobj=target, mode="w:gz") as archive:
        for _ in range(2):
            info = tarfile.TarInfo("result.json")
            info.size = 2
            archive.addfile(info, io.BytesIO(b"{}"))
    data = target.getvalue()
    index["sha256"] = hashlib.sha256(data).hexdigest()
    with pytest.raises(EvidenceError, match="unsafe_archive"):
        inspect_archive(data, index)


def test_claimed_small_download_cannot_exceed_stream_limit_and_body_closes():
    body = io.BytesIO(b"x" * 129)
    with pytest.raises(EvidenceError, match="r2_archive_too_large"):
        bounded_download({"ContentLength": 1, "Body": body}, "unused", limit=128)
    assert body.closed


def test_bounded_download_checks_sha_and_closes():
    data = b"fixture"
    body = io.BytesIO(data)
    assert (
        bounded_download(
            {"ContentLength": len(data), "Body": body}, hashlib.sha256(data).hexdigest()
        )
        == data
    )
    assert body.closed


def test_secret_config_rejects_symlink_and_broad_permissions(tmp_path):
    config = tmp_path / "private.env"
    config.write_text(
        "R2_ENDPOINT=https://test.r2.cloudflarestorage.com\nR2_BUCKET=test\nR2_ACCESS_KEY_ID=fake\nR2_SECRET_ACCESS_KEY=secret\n"
    )
    config.chmod(0o600)
    assert read_r2_config(config)["R2_BUCKET"] == "test"
    link = tmp_path / "linked.env"
    link.symlink_to(config)
    with pytest.raises(OSError):
        read_r2_config(link)
    config.chmod(0o644)
    with pytest.raises(EvidenceError):
        read_r2_config(config)


def database_fixture(
    tmp_path, *, key="team/v/batch/run/1.tar.gz", version_team="team", attempt=1
):
    path = tmp_path / "platform.db"
    sha = "c" * 64
    submission = "a" * 64
    with sqlite3.connect(path) as db:
        db.executescript(
            "CREATE TABLE teams(id TEXT); CREATE TABLE versions(id TEXT,sha256 TEXT,team_id TEXT); CREATE TABLE batches(id TEXT,status TEXT,mode TEXT,version_id TEXT,team_id TEXT); CREATE TABLE runs(id TEXT,batch_id TEXT,state TEXT,object_key TEXT,object_sha TEXT,reward INTEGER,attempt INTEGER,public INTEGER);"
        )
        db.execute("INSERT INTO teams VALUES(?)", ("team",))
        db.execute(
            "INSERT INTO versions VALUES(?,?,?)", ("v", submission, version_team)
        )
        db.execute(
            "INSERT INTO batches VALUES(?,?,?,?,?)",
            ("batch", "completed", "scientific-smoke", "v", "team"),
        )
        db.execute(
            "INSERT INTO runs VALUES(?,?,?,?,?,?,?,?)",
            ("run", "batch", "completed", key, sha, 0, attempt, 1),
        )
    return path, {
        "status": "passed",
        "batch_id": "batch",
        "standard_harbor_rollout": True,
        "r2_archive_sha256": sha,
    }


def test_readonly_db_matches_browser_digest(tmp_path):
    path, report = database_fixture(tmp_path)
    original = path.read_bytes()
    assert lookup_run(path, report)["submission_sha256"] == "a" * 64
    assert path.read_bytes() == original
    report["r2_archive_sha256"] = "d" * 64
    with pytest.raises(EvidenceError, match="browser_database_sha_mismatch"):
        lookup_run(path, report)


@pytest.mark.parametrize(
    "key",
    [
        "otherteam/v/batch/run/1.tar.gz",
        "team/otherversion/batch/run/1.tar.gz",
        "team/v/otherbatch/run/1.tar.gz",
        "team/v/batch/otherrun/1.tar.gz",
        "team/v/batch/run/2.tar.gz",
        "team/v/batch/run/01.tar.gz",
        "../team/v/batch/run/1.tar.gz",
        "rollouts/scientific-smoke/run/" + ("c" * 64) + ".tar.gz",
    ],
)
def test_go_object_key_requires_exact_owner_version_batch_run_attempt(tmp_path, key):
    path, report = database_fixture(tmp_path, key=key)
    with pytest.raises(EvidenceError, match="invalid_rollout_index"):
        lookup_run(path, report)


def test_version_must_belong_to_batch_owner(tmp_path):
    path, report = database_fixture(tmp_path, version_team="otherteam")
    with pytest.raises(EvidenceError, match="invalid_run_ownership_or_attempt"):
        lookup_run(path, report)


def test_attempt_must_be_positive(tmp_path):
    path, report = database_fixture(tmp_path, attempt=0)
    with pytest.raises(EvidenceError, match="invalid_run_ownership_or_attempt"):
        lookup_run(path, report)


def test_cli_refuses_overwrite_before_read_or_network(tmp_path, monkeypatch, capsys):
    import verify_cloud_relay_evidence as module

    output = tmp_path / "existing.json"
    output.write_text("original")
    monkeypatch.setattr(
        "sys.argv", ["check", "--report", "missing", "--output", str(output)]
    )
    monkeypatch.setattr(module, "fetch_archive", lambda *_: pytest.fail("no network"))
    assert module.main() == 2
    assert output.read_text() == "original"
    assert "original" not in capsys.readouterr().out


def test_cli_sanitizes_provider_error_and_returns_nonzero(
    tmp_path, monkeypatch, capsys
):
    import verify_cloud_relay_evidence as module

    output = tmp_path / "failure.json"
    monkeypatch.setattr(
        "sys.argv", ["check", "--report", "fixture", "--output", str(output)]
    )
    monkeypatch.setattr(module, "read_local_json", lambda *_: {})
    monkeypatch.setattr(module, "lookup_run", lambda *_: {})
    monkeypatch.setattr(module, "read_r2_config", lambda *_: {})

    def failed(*_):
        raise RuntimeError("SECRET PROVIDER KEY")

    monkeypatch.setattr(module, "fetch_archive", failed)
    assert module.main() == 1
    assert json.loads(output.read_text())["failure"] == "RuntimeError"
    assert "SECRET" not in output.read_text() + capsys.readouterr().out


def test_cli_offline_success_is_from_actual_fixture_metadata(
    tmp_path, monkeypatch, capsys
):
    import verify_cloud_relay_evidence as module

    output = tmp_path / "result.json"
    archive, index = fixture()
    index["batch_id"] = "fixture"
    monkeypatch.setattr(
        "sys.argv", ["check", "--report", "fixture", "--output", str(output)]
    )
    monkeypatch.setattr(module, "read_local_json", lambda *_: {})
    monkeypatch.setattr(module, "lookup_run", lambda *_: index)
    monkeypatch.setattr(module, "read_r2_config", lambda *_: {})
    monkeypatch.setattr(module, "fetch_archive", lambda *_: archive)
    assert module.main() == 0
    assert json.loads(output.read_text())["status"] == "passed"
    assert "PRIVATE TRAJECTORY" not in capsys.readouterr().out
