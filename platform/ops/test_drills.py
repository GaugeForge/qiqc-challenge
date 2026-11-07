"""Isolated operations drills. Never reads credentials or touches live services."""
import json
import sqlite3
from pathlib import Path

import pytest

from ops.snapshot_bundle import create, restore, verify
from qiqc.archive import reference_package
from qiqc.core import Engine, Settings, Problem
from qiqc.storage import digest
from qiqc.worker import resume_uploads


@pytest.fixture
def environment(tmp_path, monkeypatch):
    monkeypatch.setenv("QIQC_R2_ENABLED", "0")
    monkeypatch.setenv("QIQC_R2_ENV", str(tmp_path / "no-credentials"))
    engine = Engine(Settings(data_dir=tmp_path / "source", scan_docker=False))
    account = engine.create_account("drill@example.test", "fixture-not-a-live-password", "Drill", must_change=False)
    other = engine.create_account("other@example.test", "fixture-not-a-live-password", "Other", must_change=False)
    version = engine.upload(account["id"], reference_package())
    batch = engine.confirm(account["id"], version["id"], "infra", "simulated", "local-drill")
    lease = engine.claim("local-drill")
    return engine, account, other, batch, lease


def completed(environment):
    engine, account, other, batch, lease = environment
    engine.complete_task(lease, 1, "1.000001", "synthetic public trace")
    return engine, account, other, batch, lease


def test_real_engine_restore_preserves_quota_score_trace_and_authorization(environment, tmp_path):
    engine, account, other, batch, lease = completed(environment)
    before = engine.get_batch(account["id"], batch["id"])
    report = create(engine.root, tmp_path / "snapshot", quiesced=True, synthetic_objects=True)
    result = restore(tmp_path / "snapshot", tmp_path / "restored")
    assert report["offsite"] is False and report["encrypted"] is False and result["verified"]
    restored = Engine(Settings(data_dir=tmp_path / "restored", scan_docker=False))
    assert restored.me(account["id"])["quotas"] == engine.me(account["id"])["quotas"]
    assert restored.get_batch(account["id"], batch["id"])["score"] == before["score"]
    assert restored.trace(account["id"], batch["id"], 0)["text"] == "synthetic public trace"
    assert restored.confirm(account["id"], batch["version_id"], "infra", "simulated", "local-drill")["id"] == batch["id"]
    with pytest.raises(Problem):
        restored.trace(other["id"], batch["id"], 0)
    assert (tmp_path / "restored" / "platform.db").stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("component", ["platform.db", "uploads", "objects"])
def test_corrupted_backup_component_rejected_before_restore(environment, tmp_path, component):
    engine, *_ = completed(environment)
    bundle = tmp_path / "snapshot"
    create(engine.root, bundle, quiesced=True, synthetic_objects=True)
    path = bundle / component
    if path.is_dir():
        path = next(p for p in path.rglob("*") if p.is_file())
    with path.open("ab") as stream:
        stream.write(b"corrupt")
    with pytest.raises(ValueError, match="digest"):
        restore(bundle, tmp_path / "restored")
    assert not (tmp_path / "restored").exists()


@pytest.mark.parametrize("mutation", ["missing", "extra", "traversal", "symlink"])
def test_invalid_bundle_is_rejected(environment, tmp_path, mutation):
    engine, *_ = completed(environment)
    bundle = tmp_path / "snapshot"
    create(engine.root, bundle, quiesced=True)
    manifest_path = bundle / "manifest.json"
    if mutation == "missing":
        next((bundle / "uploads").glob("*.zip")).unlink()
    elif mutation == "extra":
        (bundle / "unlisted").write_text("unexpected")
    elif mutation == "symlink":
        (bundle / "leak").symlink_to(tmp_path)
    else:
        manifest = json.loads(manifest_path.read_text())
        manifest["files"]["../outside"] = {"sha256": "unused", "size": 0}
        manifest_path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError):
        restore(bundle, tmp_path / "restored")
    assert not (tmp_path / "restored").exists()


def test_existing_destination_and_nonquiesced_snapshot_rejected(environment, tmp_path):
    engine, *_ = completed(environment)
    with pytest.raises(ValueError, match="quiesced"):
        create(engine.root, tmp_path / "snapshot")
    create(engine.root, tmp_path / "snapshot", quiesced=True)
    with pytest.raises(ValueError, match="exist"):
        create(engine.root, tmp_path / "snapshot", quiesced=True)
    with pytest.raises(ValueError, match="exist"):
        restore(tmp_path / "snapshot", engine.root)


def test_default_bundle_excludes_rollouts(environment, tmp_path):
    engine, *_ = completed(environment)
    create(engine.root, tmp_path / "snapshot", quiesced=True)
    manifest = verify(tmp_path / "snapshot")
    assert not manifest["synthetic_objects"]
    assert not any(key.startswith("objects/") for key in manifest["files"])


@pytest.mark.parametrize("fault", ["429 SlowDown", "503 Unavailable", "Permission denied", "Connection reset"])
def test_upload_fault_then_restart_only_reuploads_original_bytes(environment, tmp_path, fault):
    engine, account, other, batch, lease = environment
    def unavailable(*args):
        raise OSError(fault)
    engine.store.put_file = unavailable
    with pytest.raises(OSError):
        engine.complete_task(lease, 1, "1", "synthetic trace")
    bundle = engine.root / "cache" / lease["run_id"] / str(lease["attempt"]) / "rollout.tar.gz"
    original_digest = digest(bundle)
    assert engine.get_batch(account["id"], batch["id"])["score"] is None
    restarted = Engine(engine.settings)
    assert restarted.claim("must-not-rerun") is None
    resume_uploads(restarted)
    assert restarted.get_batch(account["id"], batch["id"])["score"]["passed"] == 1
    assert restarted.me(account["id"])["quotas"]["infra"] == 9
    assert not bundle.exists()
    with restarted.db() as db:
        row = db.execute("SELECT attempt,object_key,object_sha FROM runs WHERE id=?", (lease["run_id"],)).fetchone()
        assert row["attempt"] == 1 and row["object_sha"] == original_digest
    assert digest(restarted.store.path(row["object_key"])) == original_digest
    resume_uploads(restarted)
    assert restarted.me(account["id"])["quotas"]["infra"] == 9


@pytest.mark.parametrize("change,eligible,reason", [
    ({}, True, "eligible_for_review"),
    ({"hold": True}, False, "active_reference_or_hold"),
    ({"referenced": True}, False, "active_reference_or_hold"),
    ({"upload_state": "pending"}, False, "source_not_verified"),
    ({"backup_verified": False}, False, "independent_copy_not_verified"),
    ({"expires_at": None}, False, "retention_not_frozen"),
    ({"expires_at": "2026-10-21T00:00:00Z"}, False, "unexpired"),
])
def test_retention_dry_run_preserves_referenced_unverified_and_unexpired(change, eligible, reason):
    from ops.lifecycle_preview import preview
    record = {"run_id": "synthetic", "attempt": 1, "hold": False, "referenced": False,
              "upload_state": "verified", "backup_verified": True, "expires_at": "2026-10-20T00:00:00Z"}
    record.update(change)
    result = preview([record], now="2026-10-20T00:00:00Z", policy_version="test-policy-not-production")
    assert result["delete_performed"] is False
    assert result["decisions"][0]["eligible"] is eligible
    assert result["decisions"][0]["reason"] == reason


def test_retention_requires_timezone_and_unique_attempts():
    from ops.lifecycle_preview import preview
    record = {"run_id": "synthetic", "attempt": 1, "hold": True}
    with pytest.raises(ValueError, match="timezone"):
        preview([record], now="2026-10-20T00:00:00", policy_version="test")
    with pytest.raises(ValueError, match="Duplicate"):
        preview([record, record], now="2026-10-20T00:00:00Z", policy_version="test")
