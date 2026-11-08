#!/usr/bin/env python3
"""Recover a trusted terminal result from preserved failure evidence.

The trusted controller writes `native-receipt.json` (reward, runtime, agent and
verifier exit) before packing the trial archive. A pack-limit rejection, or an
unrecovered model transport event that did not change an already terminal
budget/verifier outcome, must not rerun the Agent. This service reads only the
preserved failure archive and completes the same attempt when the receipt is
trustworthy. It never invents a score and never retries the Agent.
"""
from __future__ import annotations

import argparse
import datetime
import gzip
import hashlib
import io
import json
import os
import sqlite3
import tarfile
import time
import uuid
from fractions import Fraction
from pathlib import Path

DB = "/opt/qiqc-native-candidate/data/platform.db"
R2_ENV = "/opt/qiqc-native-candidate/config/api-r2.env"
POLL = 20.0
RECOVERABLE_CODES = {
    "native_pack_entry_limit",
    "native_pack_expanded_limit",
    "native_pack_compressed_limit",
    "native_pack_deadline",
    "native_model_unrecovered",
}


def now_iso() -> str:
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


def log(message: str) -> None:
    print(f"{now_iso()} {message}", flush=True)


def load_r2():
    values = {}
    for line in Path(R2_ENV).read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip()
    import boto3

    client = boto3.client(
        "s3",
        endpoint_url=values["R2_ENDPOINT"],
        aws_access_key_id=values["R2_ACCESS_KEY_ID"],
        aws_secret_access_key=values["R2_SECRET_ACCESS_KEY"],
    )
    return client, values["R2_BUCKET"]


def mean_seconds(values: list[str]) -> str:
    total = sum((Fraction(v) for v in values), Fraction(0)) / len(values)
    scaled = total * 1_000_000
    quotient, remainder = divmod(scaled.numerator, scaled.denominator)
    if 2 * remainder >= scaled.denominator:
        quotient += 1
    return f"{quotient // 1_000_000}.{quotient % 1_000_000:06d}"


def format_seconds(value) -> str:
    number = float(value)
    if not (number > 0):
        raise ValueError("invalid_runtime")
    return f"{number:.6f}"


def read_archive(client, bucket: str, key: str) -> dict[str, bytes]:
    body = client.get_object(Bucket=bucket, Key=key)["Body"].read()
    if len(body) > 100 * 1024 * 1024:
        raise ValueError("failure_archive_too_large")
    result = {}
    with tarfile.open(fileobj=io.BytesIO(body), mode="r:gz") as archive:
        for member in archive.getmembers():
            if not member.isreg() or member.size > 8 * 1024 * 1024:
                continue
            if member.name not in (
                "native-receipt.json",
                "model-failure-evidence.json",
                "qsim-progress.json",
                "diagnostic-completeness.json",
            ):
                continue
            stream = archive.extractfile(member)
            if stream is not None:
                result[member.name] = stream.read()
    return result


def failure_code(receipt: dict, evidence: dict) -> str:
    for value in (
        evidence.get("failure_code"),
        evidence.get("primary_error_code"),
        receipt.get("controller_error_detail"),
        receipt.get("controller_error_class"),
    ):
        if isinstance(value, str) and value in RECOVERABLE_CODES:
            return value
    return ""


def validate_receipt(receipt: dict, evidence: dict, run_id: str, submission_sha: str) -> tuple[int, str]:
    if receipt.get("run_id") != run_id:
        raise ValueError("receipt_run_mismatch")
    if receipt.get("submission_sha256") != submission_sha:
        raise ValueError("receipt_submission_mismatch")
    if receipt.get("infrastructure_ok") is not True:
        raise ValueError("receipt_infrastructure_not_ok")
    if receipt.get("standard_harbor_rollout") is not True:
        raise ValueError("receipt_not_standard_harbor")
    reward = receipt.get("reward")
    if isinstance(reward, float) and reward.is_integer():
        reward = int(reward)
    if reward not in (0, 1):
        raise ValueError("receipt_reward_invalid")
    agent_exit = receipt.get("agent_exit")
    verifier_exit = receipt.get("verifier_exit")
    if agent_exit not in (0, 124, 137):
        raise ValueError("receipt_agent_exit_invalid")
    if verifier_exit not in (0, 1):
        raise ValueError("receipt_verifier_exit_invalid")
    if reward == 1 and verifier_exit != 0:
        raise ValueError("receipt_reward_without_verifier_success")
    code = failure_code(receipt, evidence)
    if not code:
        raise ValueError("not_a_recoverable_terminal_failure")
    if code == "native_model_unrecovered":
        budget_terminal = (
            reward == 0
            and receipt.get("budget_exhausted") is True
            and receipt.get("agent_stop_reason") == "budget_exhausted"
            and agent_exit in (124, 137)
        )
        verifier_terminal = (
            reward == 0
            and verifier_exit == 1
            and receipt.get("verifier_scientific_rejection") is True
        )
        if not (budget_terminal or verifier_terminal):
            raise ValueError("model_failure_without_terminal_result")
    seconds = format_seconds(receipt.get("runtime_seconds"))
    return int(reward), seconds


def model_timeline(evidence: dict) -> str:
    lines = []
    for index, event in enumerate(evidence.get("events") or [], 1):
        timestamp = event.get("request_started_at")
        when = (
            datetime.datetime.fromtimestamp(timestamp).astimezone().strftime("%Y-%m-%d %H:%M:%S")
            if isinstance(timestamp, (int, float))
            else "?"
        )
        usage = event.get("usage") or {}
        lines.append(
            f"[{when}] model call {index:03d}: status={event.get('status')} "
            f"prompt_tokens={usage.get('prompt_tokens')} "
            f"completion_tokens={usage.get('completion_tokens')} bytes={event.get('bytes')}"
        )
    return "\n".join(lines)


def public_view(receipt: dict, evidence: dict, code: str) -> bytes:
    reward = int(receipt.get("reward") or 0)
    if code.startswith("native_pack_"):
        summary = (
            "The trusted controller completed the agent and verifier, but the full trial "
            "archive exceeded the platform's evidence packing limit. The task result is "
            f"recorded from the preserved receipt (reward {reward}). This trace is "
            "reconstructed from the preserved model-call evidence; the full rollout was "
            "not retained."
        )
    else:
        summary = (
            "A model response stream was not recovered during the task, but the trusted "
            "receipt records a terminal budget or verifier outcome. The task result is "
            f"recorded from that receipt (reward {reward}). This trace is reconstructed "
            "from the preserved model-call evidence; the full rollout was not retained."
        )
    view = {
        "mode": "scientific",
        "standard_harbor_rollout": True,
        "reward": reward,
        "runtime_seconds": float(receipt.get("runtime_seconds")),
        "agent_stop_reason": receipt.get("agent_stop_reason"),
        "agent_budget_seconds": receipt.get("agent_budget_seconds"),
        "trajectory_available": True,
        "leaderboard_eligible": False,
        "summary": summary,
        "trajectory_excerpt": model_timeline(evidence),
        "trajectory_truncated": True,
        "evidence_failure_code": code,
    }
    return (json.dumps(view, ensure_ascii=False, indent=2) + "\n").encode()


def build_archive(files: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with gzip.GzipFile(fileobj=buffer, mode="wb", mtime=0) as compressed:
        with tarfile.open(fileobj=compressed, mode="w") as archive:
            for name, data in files.items():
                info = tarfile.TarInfo(name)
                info.size = len(data)
                info.mode = 0o600
                info.mtime = 0
                archive.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


def process_run(client, bucket: str, db_path: str, row: sqlite3.Row, dry_run: bool) -> bool:
    run_id = row["run_id"]
    attempt = row["attempt"]
    artifact = row["artifact_key"]
    raw = read_archive(client, bucket, artifact)
    receipt_raw = raw.get("native-receipt.json")
    evidence_raw = raw.get("model-failure-evidence.json")
    if receipt_raw is None or evidence_raw is None:
        return False
    receipt = json.loads(receipt_raw)
    evidence = json.loads(evidence_raw)
    reward, seconds = validate_receipt(receipt, evidence, run_id, row["submission_sha256"])
    code = failure_code(receipt, evidence)

    result = {
        "provenance": "pack_failure_recovery",
        "run_id": run_id,
        "attempt": attempt,
        "reward": reward,
        "seconds": seconds,
        "failure_code": code,
        "agent_exit": receipt.get("agent_exit"),
        "verifier_exit": receipt.get("verifier_exit"),
        "agent_stop_reason": receipt.get("agent_stop_reason"),
        "runtime_seconds": receipt.get("runtime_seconds"),
        "archived_at": now_iso(),
    }
    files = {"platform-result.json": (json.dumps(result, indent=2) + "\n").encode()}
    if row["batch_kind"] == "public" and row["public"]:
        files["public-view.txt"] = public_view(receipt, evidence, code)
    else:
        files["evidence-summary.txt"] = (
            "Scientific run reached a trusted terminal result; the full rollout could "
            "not be retained by the evidence packer or model recovery path. The result "
            f"is recorded from the trusted receipt. Failure code: {code}.\n"
        ).encode()
    archive = build_archive(files)
    digest = hashlib.sha256(archive).hexdigest()
    key = f"{row['team_id']}/{row['version_id']}/{row['batch_id']}/{run_id}/{attempt}.tar.gz"

    log(
        f"recover run={run_id} attempt={attempt} reward={reward} seconds={seconds} "
        f"code={code} archive={len(archive)}B dry_run={dry_run}"
    )
    if dry_run:
        out = Path(f"/tmp/opencode/recovered-{run_id}-{attempt}.tar.gz")
        out.write_bytes(archive)
        log(f"dry-run wrote {out} sha256={digest}")
        return True

    client.put_object(Bucket=bucket, Key=key, Body=archive, ContentType="application/gzip")
    stored = client.head_object(Bucket=bucket, Key=key)["ContentLength"]
    if stored != len(archive):
        raise ValueError("recovered_archive_size_mismatch")

    connection = sqlite3.connect(db_path, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout=30000")
    connection.execute("BEGIN IMMEDIATE")
    try:
        current = connection.execute(
            """SELECT r.state,r.attempt,b.status AS batch_status
               FROM runs r JOIN batches b ON b.id=r.batch_id
               WHERE r.id=? AND r.batch_id=?""",
            (run_id, row["batch_id"]),
        ).fetchone()
        if (
            current is None
            or current["state"] != "attention"
            or current["attempt"] != attempt
            or current["batch_status"] in ("cancelled", "completed")
        ):
            connection.rollback()
            log(f"skip run={run_id} state changed")
            return False
        stamp = time.time()
        connection.execute(
            "UPDATE runs SET state='completed',reward=?,seconds=?,object_key=?,object_sha=?,owner=NULL,lease_until=NULL,error=NULL WHERE id=?",
            (reward, seconds, key, digest, run_id),
        )
        connection.execute(
            "UPDATE attempts SET state='completed',finished=?,error=NULL WHERE run_id=? AND number=?",
            (stamp, run_id, attempt),
        )
        connection.execute("UPDATE run_tokens SET revoked=1 WHERE run_id=?", (run_id,))
        states = {
            r["state"]: r["n"]
            for r in connection.execute(
                "SELECT state,COUNT(*) AS n FROM runs WHERE batch_id=? GROUP BY state",
                (row["batch_id"],),
            )
        }
        active = states.get("running", 0) + states.get("uploading", 0)
        attention = states.get("attention", 0)
        batch_done = False
        if active > 0:
            connection.execute(
                "UPDATE batches SET status='testing' WHERE id=? AND status NOT IN ('cancelled','completed','cancelling')",
                (row["batch_id"],),
            )
        elif attention > 0:
            connection.execute(
                "UPDATE batches SET status='attention' WHERE id=? AND status NOT IN ('cancelled','completed','cancelling')",
                (row["batch_id"],),
            )
        else:
            completed = connection.execute(
                "SELECT reward,seconds FROM runs WHERE batch_id=? AND state='completed'",
                (row["batch_id"],),
            ).fetchall()
            total = connection.execute("SELECT COUNT(*) FROM runs WHERE batch_id=?", (row["batch_id"],)).fetchone()[0]
            if len(completed) == total and total:
                passed = sum(int(r["reward"]) for r in completed)
                mean = mean_seconds([r["seconds"] for r in completed])
                connection.execute(
                    "UPDATE batches SET status='completed',passed=?,mean=? WHERE id=?",
                    (passed, mean, row["batch_id"]),
                )
                payload = json.dumps({"batch_id": row["batch_id"], "mode": row["batch_mode"]})
                connection.execute(
                    "INSERT INTO outbox(id,event,team_id,payload,created) VALUES(?,?,?,?,?)",
                    (uuid.uuid4().hex, "evaluation_completed", row["team_id"], payload, stamp),
                )
                batch_done = True
        connection.execute(
            """INSERT INTO audit(id,created,user_id,actor,action,object_id,reason,before_json,after_json,session_fingerprint,ip)
               VALUES(?,?,'','automatic-pack-recovery','recover_pack_failure',?,?,?,?,'','')""",
            (
                uuid.uuid4().hex,
                stamp,
                run_id,
                "A recoverable evidence/model failure preserved a trusted terminal receipt; recovered the same attempt and published a bounded diagnostic archive.",
                json.dumps({"state": "attention", "attempt": attempt}),
                json.dumps({"state": "completed", "reward": reward, "seconds": seconds, "object_key": key, "failure_code": code, "batch_completed": batch_done}),
            ),
        )
        connection.commit()
        log(f"recovered run={run_id} attempt={attempt} batch_completed={batch_done}")
        return True
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def candidates(db_path: str) -> list[sqlite3.Row]:
    connection = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=10)
    connection.row_factory = sqlite3.Row
    try:
        return list(
            connection.execute(
                """SELECT r.id AS run_id,r.batch_id,r.attempt,r.public,
                          b.team_id,b.version_id,b.kind AS batch_kind,b.mode AS batch_mode,
                          t.submission_sha256,a.object_key AS artifact_key
                   FROM runs r
                   JOIN batches b ON b.id=r.batch_id
                   JOIN run_scientific_targets t ON t.run_id=r.id
                   JOIN run_artifacts a ON a.run_id=r.id AND a.attempt=r.attempt AND a.kind='failed-rollout'
                   WHERE r.state='attention'
                     AND r.error='platform:operator_review_required'
                     AND b.kind IN ('public','infra','full','elo')
                     AND b.status NOT IN ('completed','cancelled','cancelling')
                   ORDER BY b.accepted_at,r.ordinal"""
            )
        )
    finally:
        connection.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=DB)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    client, bucket = load_r2()
    log(f"pack-failure recovery started db={args.db} dry_run={args.dry_run}")
    while True:
        try:
            for row in candidates(args.db):
                try:
                    process_run(client, bucket, args.db, row, args.dry_run)
                except Exception as error:
                    log(f"skip run={row['run_id']} attempt={row['attempt']} reason={type(error).__name__}:{error}")
        except Exception as error:
            log(f"scan error {type(error).__name__}: {error}")
        if args.once:
            return
        time.sleep(POLL)


if __name__ == "__main__":
    main()
