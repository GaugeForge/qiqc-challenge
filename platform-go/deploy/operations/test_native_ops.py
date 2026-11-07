import importlib.util
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

spec = importlib.util.spec_from_file_location("native_ops", Path(__file__).with_name("native_ops.py"))
ops = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ops)


class OperationsTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.root = self.base / "deployment"
        (self.root / "data/uploads").mkdir(parents=True)
        for name in ("bin", "config", "project", "source", "adapters"):
            (self.root / name).mkdir()
            (self.root / name / "example").write_text("trusted deployment fixture")
        payload = b"archive fixture, never executed"
        sha = ops.hashlib.sha256(payload).hexdigest()
        (self.root / "data/uploads" / (sha + ".zip")).write_bytes(payload)
        with sqlite3.connect(self.root / "data/platform.db") as db:
            db.execute("CREATE TABLE versions (sha256 TEXT)")
            db.execute("INSERT INTO versions VALUES (?)", (sha,))
        (self.root / "data/cache").mkdir()
        (self.root / "data/cache/private-rollout").write_text("excluded")
        self.backups = self.base / "backups"

    def tearDown(self):
        self.temp.cleanup()

    def test_backup_restore_and_rollout_exclusion(self):
        result = ops.backup(self.root, self.backups, include_host=False)
        snapshot = Path(result["snapshot"])
        self.assertEqual(ops.verify_snapshot(snapshot)["referenced_unique_zips"], 1)
        self.assertFalse((snapshot / "data/cache").exists())
        self.assertEqual(self.backups.stat().st_mode & 0o777, 0o700)

    def test_tamper_detected(self):
        snapshot = Path(ops.backup(self.root, self.backups, include_host=False)["snapshot"])
        (snapshot / "config/example").write_text("changed")
        with self.assertRaisesRegex(ValueError, "checksum"):
            ops.verify_snapshot(snapshot)

    def test_versioned_source_audit_survives_restore(self):
        name = 'source-audit-' + 'a' * 64 + '.json'
        (self.root / name).write_text('{"source":"fixture"}')
        snapshot = Path(ops.backup(self.root, self.backups, include_host=False)['snapshot'])
        self.assertEqual((snapshot / name).read_bytes(), (self.root / name).read_bytes())
        (snapshot / name).write_text('changed')
        with self.assertRaisesRegex(ValueError, 'checksum'):
            ops.verify_snapshot(snapshot)

    def test_missing_zip_preserves_previous_backup(self):
        snapshot = Path(ops.backup(self.root, self.backups, include_host=False)["snapshot"])
        next((self.root / "data/uploads").glob("*.zip")).unlink()
        with self.assertRaises(FileNotFoundError):
            ops.backup(self.root, self.backups, include_host=False)
        self.assertTrue(snapshot.exists())
        self.assertFalse(list(self.backups.glob(".pending-*")))

    def test_retention_and_unmanaged_directory_preserved(self):
        self.backups.mkdir()
        unmanaged = self.backups / "snapshot-do-not-delete"
        unmanaged.mkdir()
        for _ in range(4):
            ops.backup(self.root, self.backups, retention=2, include_host=False)
        self.assertEqual(len(list(self.backups.glob("snapshot-*/manifest.json"))), 2)
        self.assertTrue(unmanaged.exists())

    def test_dashboard_projection_excludes_private_content(self):
        state = self.base / "state"
        state.mkdir()
        report = {"sampled_at": 123, "status": "attention", "checks": {"http_api": "ok", "secret/path": "PRIVATE"}, "secret": "DO_NOT_LEAK"}
        result = ops.dashboard_projection(report, state, self.base / "absent")
        self.assertEqual(result["checks"], {"http_api": "ok"})
        self.assertNotIn("DO_NOT_LEAK", json.dumps(result))
        self.assertEqual(result["backup"], {})
        self.assertFalse(result["external_delivery"])

    def test_alerts_deduplicate_and_recover(self):
        state = self.base / "health"
        ops.update_alerts(state, {"http_api": "critical"}, 1)
        ops.update_alerts(state, {"http_api": "critical"}, 2)
        ops.update_alerts(state, {"http_api": "ok"}, 3)
        events = [json.loads(line) for line in (state / "alerts.jsonl").read_text().splitlines()]
        self.assertEqual([event["state"] for event in events], ["critical", "recovered"])

    def test_clock_record_readable_by_separate_service_identity(self):
        path = self.base / "public-status" / "clock.json"
        path.parent.mkdir(mode=0o700)
        ops.write_clock_health(path, 123, True)
        self.assertEqual(path.parent.stat().st_mode & 0o777, 0o755)
        self.assertEqual(path.stat().st_mode & 0o777, 0o644)
        self.assertEqual(json.loads(path.read_text()), {"sampled_at": 123, "synchronized": True})
        ops.write_clock_health(path, 124, False)
        self.assertEqual(json.loads(path.read_text())["synchronized"], False)
        self.assertEqual(path.stat().st_mode & 0o777, 0o644)


if __name__ == "__main__":
    unittest.main()
