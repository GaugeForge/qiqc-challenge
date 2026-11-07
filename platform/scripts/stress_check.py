#!/usr/bin/env python3
"""Safe local queue, storage, security and recovery pressure check.

This script deliberately uses a temporary data directory and simulated tasks.
It never executes a submitted archive and never calls E2B, R2 or a model API.
The JSON report is suitable for CI artifacts and contains no credentials.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import stat
import tempfile
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from qiqc.archive import reference_package
from qiqc.core import Engine, Problem, Settings
from qiqc.worker import resume_uploads


def _write_report(path: Path, report: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")


def _finish(engine: Engine, lease: dict, reward: int = 1) -> None:
    engine.complete_task(lease, reward, "1.250000", "local stress fixture", provenance="stress-fixture")


def run(team_count: int, repeats: int) -> dict:
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="qiqc-stress-") as temp:
        root = Path(temp)
        engine = Engine(Settings(data_dir=root, scan_docker=False, slots=4, simulated_delay=0))
        teams = [
            engine.create_account(f"team-{i}@example.test", "local-stress-password", f"Team {i}", must_change=False)
            for i in range(team_count)
        ]

        # Concurrent duplicate confirmations must create one request and one debit.
        account = teams[0]
        version = engine.upload(account["id"], reference_package())
        with ThreadPoolExecutor(max_workers=min(32, repeats)) as pool:
            duplicate = list(
                pool.map(
                    lambda _: engine.confirm(account["id"], version["id"], "infra", "simulated", "stress-idem"),
                    range(repeats),
                )
            )
        assert len({item["id"] for item in duplicate}) == 1
        assert engine.me(account["id"])["quotas"]["infra"] == 9

        # Fill other teams and drain the queue while enforcing the global slot count.
        requests = [duplicate[0]]
        for item in teams[1:]:
            ver = engine.upload(item["id"], reference_package())
            requests.append(engine.confirm(item["id"], ver["id"], "infra", "simulated", f"stress-{item['id']}"))
        max_running = 0
        while True:
            active = []
            for _ in range(engine.settings.slots):
                lease = engine.claim("stress-worker")
                if lease is None:
                    break
                active.append(lease)
            if not active:
                break
            with engine.db() as conn:
                running = conn.execute("SELECT COUNT(*) FROM runs WHERE state IN ('running','uploading')").fetchone()[0]
            max_running = max(max_running, running)
            for lease in active:
                _finish(engine, lease, reward=1)
        assert max_running <= engine.settings.slots
        assert all(engine.get_batch(team["id"], req["id"])["status"] == "completed" for team, req in zip(teams, requests))

        # Storage failure must leave a retryable bundle and must not rerun/charge.
        retry_team = teams[-1]
        retry_version = engine.upload(retry_team["id"], reference_package())
        retry_req = engine.confirm(retry_team["id"], retry_version["id"], "infra", "simulated", "storage-outage")
        retry_lease = engine.claim("outage-worker")
        original_put = engine.store.put_file

        def outage(*_args, **_kwargs):
            raise OSError("injected storage outage")

        engine.store.put_file = outage
        try:
            try:
                _finish(engine, retry_lease)
            except OSError:
                pass
            cache = engine.root / "cache" / retry_lease["run_id"] / str(retry_lease["attempt"])
            assert (cache / "rollout.tar.gz").exists()
        finally:
            engine.store.put_file = original_put
        resume_uploads(engine)
        assert engine.get_batch(retry_team["id"], retry_req["id"])["status"] == "completed"
        assert not cache.exists()

        # Lease expiry: a stale completion is rejected after recovery.
        expiry_version = engine.upload(retry_team["id"], reference_package())
        expiry_req = engine.confirm(retry_team["id"], expiry_version["id"], "infra", "simulated", "lease-expiry")
        old = engine.claim("dead-worker")
        now = time.time()
        engine.clock = lambda: now + 120
        assert engine.recover() == 1
        new = engine.claim("replacement-worker")
        assert new["attempt"] == old["attempt"] + 1
        try:
            _finish(engine, old)
        except Problem:
            pass
        _finish(engine, new)
        assert engine.get_batch(retry_team["id"], expiry_req["id"])["status"] == "completed"

        # Runtime data must remain private and the stored archive must be immutable.
        assert stat.S_IMODE(engine.root.stat().st_mode) & 0o077 == 0
        stored = engine.root / "uploads" / f"{version['sha256']}.zip"
        assert hashlib.sha256(stored.read_bytes()).hexdigest() == version["sha256"]

        return {
            "status": "passed",
            "teams": team_count,
            "duplicate_confirmations": repeats,
            "unique_idempotent_batch": True,
            "max_running": max_running,
            "slot_limit": engine.settings.slots,
            "storage_outage_recovery": True,
            "stale_completion_rejected": True,
            "private_runtime_dirs": True,
            "duration_seconds": round(time.monotonic() - started, 3),
            "external_services_called": False,
        }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--teams", type=int, default=12)
    parser.add_argument("--repeats", type=int, default=32)
    parser.add_argument("--output", type=Path, default=Path(__file__).parents[1] / "test-results" / "stress-check.json")
    args = parser.parse_args()
    if args.teams < 2 or args.repeats < 2:
        parser.error("--teams and --repeats must be at least 2")
    result = run(args.teams, args.repeats)
    _write_report(args.output, result)
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
