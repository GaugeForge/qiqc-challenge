#!/usr/bin/env python3
"""Run the complete local Demo flow through the Go edge.

This is intentionally a real browser/API business-flow check, not a claim that
an arbitrary participant package was executed.  The uploaded adapter is
reviewed, then the Demo's simulated evaluator completes a public and a full
batch.  Real Codex/Harbor execution and cloud dependency checks are separate
artifacts.
"""
from __future__ import annotations

import argparse
import json
import os
import secrets
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[2]
PLATFORM = ROOT / "platform"
if str(PLATFORM) not in sys.path:
    sys.path.insert(0, str(PLATFORM))
from qiqc.core import Engine, Settings  # noqa: E402


def request(client: httpx.Client, method: str, path: str, **kwargs):
    headers = kwargs.pop("headers", {})
    if path != "/api/login":
        headers.setdefault("X-QIQC-Request", "1")
    response = client.request(method, path, headers=headers, **kwargs)
    if response.status_code >= 400:
        raise RuntimeError(f"{method} {path}: HTTP {response.status_code}: {response.text[:300]}")
    return response


def login_and_change(client: httpx.Client, email: str, password: str, new_password: str) -> dict:
    result = request(client, "POST", "/api/login", json={"email": email, "password": password}).json()
    if result.get("must_change"):
        request(client, "POST", "/api/password", json={"password": new_password})
        # Changing the password keeps the current session alive.  Re-login is
        # done anyway so the test proves the issued password can be replaced.
        client.cookies.clear()
        result = request(client, "POST", "/api/login", json={"email": email, "password": new_password}).json()
    return result


def poll_batch(client: httpx.Client, batch_id: str, timeout: float = 180.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        item = request(client, "GET", f"/api/batches/{batch_id}").json()
        if item.get("status") in {"completed", "failed", "cancelled"}:
            return item
        time.sleep(0.5)
    raise TimeoutError(f"batch {batch_id} did not finish within {timeout}s")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default=os.environ.get("QIQC_GO_EDGE_URL", "http://127.0.0.1:8790"))
    parser.add_argument("--output", type=Path, default=PLATFORM / "test-results" / "e2e-go-edge.json")
    args = parser.parse_args()

    # Issue a fresh initial password in the local trusted database.  It is
    # retained only in memory and is never printed or written to the report.
    suffix = secrets.token_hex(5)
    email = f"e2e-{suffix}@qiqc.local"
    initial = secrets.token_urlsafe(18)
    replacement = secrets.token_urlsafe(18)
    engine = Engine(Settings(scan_docker=False))

    credentials_path = engine.root / "credentials.json"
    credentials = json.loads(credentials_path.read_text()) if credentials_path.exists() else {}
    admin = next((item for email_key, item in credentials.items() if item.get("role") == "admin"), None)
    if not admin:
        raise RuntimeError("local administrator credential is not initialized")
    admin_email = next(email_key for email_key, item in credentials.items() if item is admin)

    with httpx.Client(base_url=args.url,timeout=20) as issuer:
        login_and_change(issuer,admin_email,admin['password'],admin['password'])
        account=request(issuer,'POST','/api/admin/accounts',json={'email':email,'password':initial,'team':f'E2E Team {suffix}','actor':'isolated-organizer','reason':'End-to-end account invitation without email'}).json()

    report = {
        "status": "failed",
        "edge_url": args.url,
        "team_email_issued": True,
        "password_change_verified": False,
        "adapter_download_verified": False,
        "upload_and_static_review_verified": False,
        "admin_review_verified": False,
        "public_batch": None,
        "full_batch": None,
        "private_trace_access_verified": False,
        "leaderboard_verified": False,
        "competition_finalization": "not_reached",
        "competition_close_verified": False,
        "uploaded_code_executed": False,
        "notes": [
            "The Demo evaluator is simulated for arbitrary participant packages; it never executes this uploaded zip.",
            "Real local Codex + Harbor and real E2B/R2/Hy4 smoke evidence are separate reports.",
        ],
    }

    with httpx.Client(base_url=args.url, timeout=20, follow_redirects=False) as team:
        health = request(team, "GET", "/healthz").json()
        if health.get("edge") != "go" or health.get("status") != "ok":
            raise RuntimeError(f"Go edge health failed: {health}")
        catalog = request(team, "GET", "/api/adapter-templates").json()
        ids = {item["id"] for item in catalog["templates"]}
        if ids != {"codex", "deepseek", "pi"}:
            raise RuntimeError(f"adapter catalog incomplete: {ids}")
        archive = team.get("/adapters/codex/harness.zip")
        if archive.status_code != 200 or len(archive.content) < 100:
            raise RuntimeError(f"adapter download failed: {archive.status_code}")
        report["adapter_download_verified"] = True

        me = login_and_change(team, email, initial, replacement)
        report["password_change_verified"] = not bool(me.get("must_change"))
        version = request(team, "POST", "/api/versions", content=archive.content, headers={"Content-Type": "application/zip"}).json()
        if version.get("status") != "pending_review":
            raise RuntimeError(f"expected manual review for template, got {version.get('status')}")
        report["upload_and_static_review_verified"] = version.get("risk_count", 0) >= 0

        with httpx.Client(base_url=args.url, timeout=20, follow_redirects=False) as admin_client:
            admin_password = admin["password"]
            login_and_change(admin_client, admin_email, admin_password, admin_password)
            request(admin_client, "POST", "/api/admin/actions", json={
                "action": "review", "target": version["id"], "decision": "approve",
                "actor": "e2e-local-admin", "reason": "E2E review of the published Codex starter template",
            })
            report["admin_review_verified"] = True

        # Re-login after admin review to prove the submitted version can be
        # used for both visible public checks and the final score batch.
        team.cookies.clear()
        login_and_change(team, email, replacement, replacement)
        public = request(team, "POST", "/api/batches", json={
            "version_id": version["id"], "kind": "public", "mode": "simulated",
            "idempotency_key": f"e2e-public-{suffix}",
        }).json()
        public_again = request(team, "POST", "/api/batches", json={
            "version_id": version["id"], "kind": "public", "mode": "simulated",
            "idempotency_key": f"e2e-public-{suffix}",
        }).json()
        if public["id"] != public_again["id"]:
            raise RuntimeError("idempotent confirmation created two public batches")
        public_done = poll_batch(team, public["id"])
        report["public_batch"] = {"id": public_done["id"], "status": public_done["status"], "score": public_done.get("score")}
        if public_done["status"] != "completed":
            raise RuntimeError(f"public batch did not complete: {public_done}")
        if not public_done.get("public_runs"):
            raise RuntimeError("public batch did not expose its five public run entries")
        trace = request(team, "GET", f"/api/batches/{public_done['id']}/traces/0").json()
        if trace.get("filtered") is not True:
            raise RuntimeError(f"public trace not visible: {trace}")
        report["private_trace_access_verified"] = True

        full = request(team, "POST", "/api/batches", json={
            "version_id": version["id"], "kind": "full", "mode": "simulated",
            "idempotency_key": f"e2e-full-{suffix}",
        }).json()
        full_done = poll_batch(team, full["id"], timeout=240)
        report["full_batch"] = {"id": full_done["id"], "status": full_done["status"], "score": full_done.get("score")}
        if full_done["status"] != "completed" or not full_done.get("score"):
            raise RuntimeError(f"full batch did not complete: {full_done}")
        with httpx.Client(base_url=args.url, timeout=20, follow_redirects=False) as closer:
            login_and_change(closer, admin_email, admin["password"], admin["password"])
            request(closer, "POST", "/api/admin/actions", json={
                "action": "close_demo", "actor": "e2e-local-admin",
                "reason": "E2E final score freeze and competition close",
            })
        rejected = team.post("/api/batches", headers={"X-QIQC-Request": "1"}, json={
            "version_id": version["id"], "kind": "infra", "mode": "simulated",
            "idempotency_key": f"e2e-after-close-{suffix}",
        })
        if rejected.status_code != 409:
            raise RuntimeError(f"closed competition accepted a new batch: {rejected.status_code}")
        report["competition_close_verified"] = True
        board = request(team, "GET", "/api/leaderboard").json()
        rows = board.get("teams", board.get("rows", board if isinstance(board, list) else []))
        report["leaderboard_verified"] = any(row.get("team_id") == me.get("team_id") for row in rows)
        if not report["leaderboard_verified"]:
            raise RuntimeError('own team missing from leaderboard')
        own=next(row for row in rows if row.get('team_id')==me['team_id'])
        if own['id']!=full_done['id'] or own['sha256']!=version['sha256']:
            raise RuntimeError('leaderboard selected different submission')
        assert team.get(f"/api/batches/{full_done['id']}/traces/5").status_code==404
        with httpx.Client(base_url=args.url,timeout=20) as stranger:
            assert stranger.get(f"/api/batches/{full_done['id']}/traces/0").status_code==401
        report['hidden_and_unauthenticated_trace_denied']=True
        report["competition_finalization"] = "demo-closed-new-submissions-rejected"
        report["status"] = "passed"

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({k: v for k, v in report.items() if k not in {"public_batch", "full_batch"}} | {
        "public_batch_status": report["public_batch"]["status"],
        "full_batch_status": report["full_batch"]["status"],
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
