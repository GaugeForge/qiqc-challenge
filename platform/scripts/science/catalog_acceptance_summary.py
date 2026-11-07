#!/usr/bin/env python3
"""Read-only, fail-closed summary of bounded browser + independent R2 evidence.

This tool never contacts a provider or database. A suite label is progress only;
acceptance requires matching immutable report identities and explicit checks.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import tempfile
from collections import Counter
from pathlib import Path

BROWSER_NAME = re.compile(r"task-(\d{2})-attempt-(\d{2})\.json\Z")
SHA = re.compile(r"[0-9a-f]{64}\Z")
BATCH = re.compile(r"[0-9a-f]{32}\Z")
BROWSER_CHECKS = (
    "cloud_calls",
    "invitation_login_password_change",
    "admin_target_bound_via_browser",
    "own_hidden_trace_denied",
    "result_visible_in_browser",
    "standard_harbor_rollout",
    "all_three_sandboxes_destroyed",
    "r2_index_written",
    "other_identity_trace_denied",
    "nonranking",
    "local_rollout_cache_removed",
    "hidden_trace_required",
)


class EvidenceError(ValueError):
    pass


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def canonical(value: object) -> str:
    return digest(json.dumps(value, sort_keys=True, separators=(",", ":")).encode())


def check(condition: bool, code: str) -> None:
    if not condition:
        raise EvidenceError(code)


def load(path: Path, root: Path) -> tuple[dict, str]:
    check(
        path.resolve().is_relative_to(root.resolve()) and not path.is_symlink(),
        "unsafe_evidence_path",
    )
    try:
        raw = path.read_bytes()
        check(len(raw) <= 2 * 1024 * 1024, "oversized_report")
        value = json.loads(raw)
        check(isinstance(value, dict), "invalid_report_shape")
        return value, digest(raw)
    except (OSError, ValueError) as exc:
        if isinstance(exc, EvidenceError):
            raise
        raise EvidenceError("unreadable_report") from None


def stamp(value: object, fallback: Path) -> float:
    if isinstance(value, str):
        try:
            parsed = dt.datetime.fromisoformat(value)
            if parsed.tzinfo is not None:
                return parsed.timestamp()
        except ValueError:
            pass
    return fallback.stat().st_mtime if fallback.exists() else 0.0


def file_ref(path: Path, root: Path, sha: str | None = None) -> dict:
    value = {"path": path.relative_to(root).as_posix()}
    if sha:
        value["sha256"] = sha
    return value


def validate_pair(browser: dict, evidence: dict, index: int) -> dict:
    check(browser.get("status") == "passed", "browser_not_passed")
    check(evidence.get("status") == "passed", "r2_not_passed")
    check(evidence.get("schema") == "qiqc-catalog-role-evidence/v1", "wrong_r2_schema")
    check(
        type(browser.get("catalog_index")) is int
        and browser["catalog_index"] == index
        and type(evidence.get("catalog_index")) is int
        and evidence["catalog_index"] == index,
        "task_index_mismatch",
    )
    batch = browser.get("batch_id")
    check(
        isinstance(batch, str)
        and BATCH.fullmatch(batch) is not None
        and batch == evidence.get("batch_id"),
        "batch_id_mismatch",
    )
    archive = browser.get("r2_archive_sha256")
    check(
        isinstance(archive, str)
        and SHA.fullmatch(archive) is not None
        and archive == evidence.get("archive_sha256"),
        "archive_sha_mismatch",
    )
    check(
        all(browser.get(key) is True for key in BROWSER_CHECKS),
        "browser_checks_incomplete",
    )
    check(
        type(browser.get("agent_budget_seconds")) is int
        and type(evidence.get("agent_budget_seconds")) is int
        and browser["agent_budget_seconds"] == 300
        and evidence["agent_budget_seconds"] == 300,
        "budget_mismatch",
    )
    check(
        browser.get("active_run_tokens") == 0
        and browser.get("quota_debits") == 1
        and browser.get("javascript_errors") == 0
        and browser.get("public_trace_readback") is False,
        "browser_cleanup_or_privacy_failed",
    )
    check(
        type(browser.get("model_usage_rows")) is int
        and browser["model_usage_rows"] > 0,
        "missing_model_execution",
    )
    ai = browser.get("ai_review", {})
    check(
        isinstance(ai, dict)
        and ai.get("real_advice_available") is True
        and ai.get("manual_approval_preserved") is True,
        "review_incomplete",
    )
    check(evidence.get("benchmark_release_verified") is True, "release_not_verified")
    release = browser.get("benchmark_release")
    check(
        isinstance(release, dict) and release == evidence.get("benchmark_release"),
        "release_mismatch",
    )
    check(
        release.get("release_id") == "qiqc49-20261020-v1"
        and all(
            isinstance(release.get(key), str) and SHA.fullmatch(release[key])
            for key in ("runtime_sha256", "task_execution_sha256")
        ),
        "invalid_release_identity",
    )
    check(
        evidence.get("private_boundary_probes_passed") is True
        and evidence.get("fixed_network_probes_passed") is True,
        "boundary_checks_incomplete",
    )
    roles = evidence.get("role_manifests_verified")
    check(
        isinstance(roles, dict) and set(roles) == {"main", "qsim", "verifier"},
        "role_checks_incomplete",
    )
    for item in roles.values():
        check(
            isinstance(item, dict)
            and type(item.get("file_count")) is int
            and item["file_count"] > 0
            and isinstance(item.get("manifest_sha256"), str)
            and SHA.fullmatch(item["manifest_sha256"]),
            "role_checks_incomplete",
        )
    check(
        type(evidence.get("qsim_call_count")) is int
        and evidence["qsim_call_count"] > 0
        and type(evidence.get("qsim_success_count")) is int
        and 0 <= evidence["qsim_success_count"] <= evidence["qsim_call_count"],
        "missing_qsim_execution",
    )
    reward = evidence.get("verifier_reward")
    check(type(reward) in (int, float) and reward in (0, 1), "invalid_reward")
    score = browser.get("score", {})
    check(
        isinstance(score, dict)
        and type(score.get("total")) is int
        and score["total"] == 1
        and type(score.get("passed")) is int
        and score["passed"] == reward,
        "score_mismatch",
    )
    check(
        type(evidence.get("verifier_exit")) is int
        and type(evidence.get("agent_exit")) is int,
        "invalid_exit_code",
    )
    rejected = evidence.get("verifier_scientific_rejection")
    check(
        (evidence.get("verifier_exit") == 0 and rejected is False)
        or (evidence.get("verifier_exit") == 1 and reward == 0 and rejected is True),
        "verifier_failure",
    )
    exhausted = evidence.get("budget_exhausted")
    check(type(exhausted) is bool, "missing_stop_classification")
    if exhausted:
        check(
            evidence.get("agent_exit") in (124, 137)
            and evidence.get("agent_stop_reason") == "budget_exhausted",
            "invalid_budget_stop",
        )
    else:
        check(
            evidence.get("agent_exit") == 0
            and evidence.get("agent_stop_reason") == "completed",
            "unexpected_agent_exit",
        )
    binaries = browser.get("deployed_binaries_sha256")
    science = browser.get("deployed_science_sha256")
    check(
        isinstance(binaries, dict)
        and set(binaries) == {"api", "worker", "gateway"}
        and isinstance(science, dict)
        and bool(science),
        "missing_source_identity",
    )
    check(
        isinstance(browser.get("driver_sha256"), str)
        and SHA.fullmatch(browser["driver_sha256"]),
        "invalid_driver_identity",
    )
    check(
        all(
            isinstance(value, str) and SHA.fullmatch(value)
            for value in [*binaries.values(), *science.values()]
        ),
        "invalid_source_identity",
    )
    return {
        "batch_id": batch,
        "archive_sha256": archive,
        "benchmark_release": {
            key: release[key]
            for key in ("release_id", "runtime_sha256", "task_execution_sha256")
        },
        "completion": "budget_exhausted" if exhausted else "completed_before_budget",
        "reward": int(reward),
        "qsim_call_count": evidence["qsim_call_count"],
        "qsim_success_count": evidence["qsim_success_count"],
        "model_successful_calls": browser["model_usage_rows"],
        "source_version": canonical({"binaries": binaries, "science": science}),
        "binary_sha256": binaries,
        "science_manifest_sha256": canonical(science),
        "science_file_count": len(science),
        "driver_sha256": browser.get("driver_sha256"),
    }


def validate_model_evidence(evidence: dict) -> dict:
    model = evidence.get("model_response_evidence")
    check(isinstance(model, dict), "model_terminal_evidence_missing")
    check(
        model.get("complete_vs_incomplete_distinction_verified") is True,
        "model_complete_incomplete_distinction_unverified",
    )
    keys = (
        "agent_visible_request_count",
        "agent_visible_http_failure_count",
        "successful_responses",
        "failed_responses",
        "recovered_failed_responses",
        "unrecovered_failed_responses",
    )
    check(
        all(type(model.get(key)) is int and model[key] >= 0 for key in keys),
        "invalid_model_counters",
    )
    count = model["agent_visible_request_count"]
    check(
        0 < count <= 24
        and model["successful_responses"] > 0
        and model["successful_responses"] + model["failed_responses"] == count,
        "model_counter_mismatch",
    )
    check(
        model["agent_visible_http_failure_count"] == 0
        and model["unrecovered_failed_responses"] == 0
        and model["recovered_failed_responses"] == model["failed_responses"]
        and model.get("all_failures_recovered") is True,
        "unrecovered_model_failure",
    )
    source = model.get("terminal_evidence_source")
    check(
        source
        in (
            "archived_response_terminal_contract",
            "gateway_terminal_status_correlated_one_to_one",
        ),
        "invalid_model_terminal_source",
    )
    safe = {key: model[key] for key in keys}
    safe.update(
        terminal_evidence_source=source,
        all_failures_recovered=True,
        complete_vs_incomplete_distinction_verified=True,
    )
    if "gateway_attempt_count" in model:
        attempts, extra = (
            model.get("gateway_attempt_count"),
            model.get("extra_gateway_attempts"),
        )
        statuses = model.get("gateway_attempt_status_counts")
        check(
            type(attempts) is int
            and type(extra) is int
            and attempts >= count
            and extra == attempts - count,
            "model_gateway_counter_mismatch",
        )
        check(
            isinstance(statuses, dict)
            and all(
                isinstance(key, str)
                and re.fullmatch(r"[1-5][0-9]{2}", key)
                and type(value) is int
                and value >= 0
                for key, value in statuses.items()
            )
            and sum(statuses.values()) == attempts,
            "model_gateway_counter_mismatch",
        )
        safe.update(
            gateway_attempt_count=attempts,
            extra_gateway_attempts=extra,
            gateway_attempt_status_counts=statuses,
        )
    if source == "gateway_terminal_status_correlated_one_to_one":
        check(
            safe.get("gateway_attempt_count") == count
            and safe.get("extra_gateway_attempts") == 0,
            "ambiguous_model_correlation",
        )
        check(
            safe["gateway_attempt_status_counts"].get("200", 0)
            == model["successful_responses"],
            "model_gateway_counter_mismatch",
        )
    return safe


def summarize(root: Path) -> dict:
    root = root.resolve()
    check(root.is_dir(), "missing_evidence_directory")
    directories = {p.parent for p in root.rglob("suite.json")}
    directories.update(
        p.parent
        for p in root.rglob("task-*-attempt-*.json")
        if BROWSER_NAME.fullmatch(p.name)
    )
    attempts, warnings = [], []
    for directory in sorted(directories):
        suite_path = directory / "suite.json"
        suite, suite_sha = {}, None
        if suite_path.exists():
            try:
                suite, suite_sha = load(suite_path, root)
                check(
                    suite.get("schema") == "qiqc-catalog-browser-suite/v1"
                    and isinstance(suite.get("attempts"), list),
                    "invalid_suite",
                )
            except EvidenceError as exc:
                warnings.append(
                    {"report": file_ref(suite_path, root), "code": str(exc)}
                )
                suite = {}
        rows = {}
        for row in suite.get("attempts", []):
            if not isinstance(row, dict) or not isinstance(
                row.get("browser_report"), str
            ):
                warnings.append(
                    {"report": file_ref(suite_path, root), "code": "invalid_suite_row"}
                )
                continue
            match = BROWSER_NAME.fullmatch(row["browser_report"])
            if (
                not match
                or type(row.get("catalog_index")) is not int
                or type(row.get("attempt")) is not int
                or row["catalog_index"] != int(match[1])
                or row["attempt"] != int(match[2])
            ):
                warnings.append(
                    {
                        "report": file_ref(suite_path, root),
                        "code": "unsafe_suite_reference",
                    }
                )
                continue
            if row["browser_report"] in rows:
                warnings.append(
                    {
                        "report": file_ref(suite_path, root),
                        "code": "duplicate_suite_reference",
                    }
                )
                continue
            rows[row["browser_report"]] = row
        names = set(rows) | {
            p.name
            for p in directory.glob("task-*-attempt-*.json")
            if BROWSER_NAME.fullmatch(p.name)
        }
        for name in sorted(names):
            match = BROWSER_NAME.fullmatch(name)
            index, number = int(match[1]), int(match[2])
            if not 0 <= index < 49:
                warnings.append(
                    {
                        "report": file_ref(directory / name, root),
                        "code": "index_out_of_range",
                    }
                )
                continue
            browser_path = directory / name
            r2_path = browser_path.with_name(browser_path.stem + "-r2.json")
            row = rows.get(name, {})
            item = {
                "catalog_index": index,
                "attempt": number,
                "browser_report": file_ref(browser_path, root),
                "status": "failed",
                "failure_history": [],
                "observed_at_epoch": stamp(suite.get("updated_at"), suite_path),
            }
            if suite_sha:
                item["suite_report"] = file_ref(suite_path, root, suite_sha)
            running = suite.get("status") == "running" and row.get("state") in (
                "browser_running",
                "r2_verifying",
            )
            try:
                if not browser_path.exists():
                    check(running, "missing_browser_report")
                    item["status"] = "running"
                    item["progress"] = row["state"]
                    attempts.append(item)
                    continue
                browser, browser_sha = load(browser_path, root)
                item["browser_report"]["sha256"] = browser_sha
                item["observed_at_epoch"] = stamp(
                    browser.get("checked_at"), browser_path
                )
                if row.get("browser_sha256"):
                    check(
                        row["browser_sha256"] == browser_sha, "browser_report_changed"
                    )
                if row.get("batch_id"):
                    check(
                        row["batch_id"] == browser.get("batch_id"),
                        "suite_batch_mismatch",
                    )
                check(browser.get("status") == "passed", "browser_not_passed")
                if not r2_path.exists():
                    check(running, "missing_r2_report")
                    item["status"] = "running"
                    item["progress"] = "r2_verifying"
                    attempts.append(item)
                    continue
                evidence, r2_sha = load(r2_path, root)
                item["r2_report"] = file_ref(r2_path, root, r2_sha)
                if row.get("r2_report"):
                    check(
                        row["r2_report"] == r2_path.name, "suite_r2_reference_mismatch"
                    )
                if row.get("r2_sha256"):
                    check(row["r2_sha256"] == r2_sha, "r2_report_changed")
                if evidence.get("status") != "passed":
                    item["failure_history"].append(
                        {
                            "code": "independent_r2_validation_failed",
                            "report": file_ref(r2_path, root, r2_sha),
                        }
                    )
                    resolution_path = directory / "resolution.json"
                    check(resolution_path.exists(), "r2_not_passed")
                    resolution, resolution_sha = load(resolution_path, root)
                    fixed_name = browser_path.stem + "-r2-fixed.json"
                    check(
                        resolution.get("status") == "passed_after_evidence_driver_fix"
                        and resolution.get("browser_report") == name
                        and resolution.get("catalog_index") == index
                        and resolution.get("first_r2_failure_preserved") == r2_path.name
                        and resolution.get("verified_r2_report") == fixed_name
                        and resolution.get("harness_rerun") is False
                        and resolution.get("actual_role_hashes_match") is True,
                        "invalid_resolution_reference",
                    )
                    fixed_path = directory / fixed_name
                    evidence, r2_sha = load(fixed_path, root)
                    item["r2_report"] = file_ref(fixed_path, root, r2_sha)
                    item["resolution_report"] = file_ref(
                        resolution_path, root, resolution_sha
                    )
                    item["evidence_rechecked_without_harness_rerun"] = True
                strict_path = directory / (browser_path.stem + "-r2-model-strict.json")
                if strict_path.exists():
                    previous_ref = item["r2_report"]
                    evidence, strict_sha = load(strict_path, root)
                    item["superseded_r2_report"] = previous_ref
                    item["r2_report"] = file_ref(strict_path, root, strict_sha)
                    item["model_terminal_recheck"] = True
                item.update(validate_pair(browser, evidence, index))
                item["model_response_evidence"] = validate_model_evidence(evidence)
                item["status"] = "passed"
            except EvidenceError as exc:
                item["status"] = (
                    "needs_reverification"
                    if str(exc)
                    in (
                        "model_terminal_evidence_missing",
                        "model_complete_incomplete_distinction_unverified",
                    )
                    else "failed"
                )
                item["failure_code"] = str(exc)
                item["failure_history"].append(
                    {
                        "code": str(exc),
                        "report": item.get("r2_report", item["browser_report"]),
                    }
                )
            attempts.append(item)
    tasks = []
    for index in range(49):
        history = sorted(
            (a for a in attempts if a["catalog_index"] == index),
            key=lambda a: (a["observed_at_epoch"], a["browser_report"]["path"]),
        )
        passes = [a for a in history if a["status"] == "passed"]
        tasks.append(
            {
                "catalog_index": index,
                "status": history[-1]["status"] if history else "not_run",
                "latest_attempt": history[-1] if history else None,
                "last_verified_pass": passes[-1] if passes else None,
                "attempt_history": history,
            }
        )
    versions = {}
    for attempt in attempts:
        if (
            attempt["status"] not in ("passed", "needs_reverification")
            or "source_version" not in attempt
        ):
            continue
        key = attempt["source_version"]
        version = versions.setdefault(
            key,
            {
                "source_version": key,
                "binary_sha256": attempt["binary_sha256"],
                "science_manifest_sha256": attempt["science_manifest_sha256"],
                "science_file_count": attempt["science_file_count"],
                "verified_indices": [],
                "pending_model_indices": [],
                "browser_reports": [],
            },
        )
        indices_key = (
            "verified_indices"
            if attempt["status"] == "passed"
            else "pending_model_indices"
        )
        version[indices_key] = sorted(
            set(version[indices_key]) | {attempt["catalog_index"]}
        )
        version["browser_reports"].append(attempt["browser_report"])
    counts = dict(Counter(t["status"] for t in tasks))
    latest_passes = [t["latest_attempt"] for t in tasks if t["status"] == "passed"]
    return {
        "schema": "qiqc-catalog-acceptance-summary/v1",
        "generated_at": dt.datetime.now(dt.UTC).isoformat(),
        "status": "incomplete",
        "bounded_browser_gate_status": "passed"
        if counts.get("passed") == 49 and not warnings
        else "incomplete",
        "source_runtime_parity": {
            "status": "not_accepted",
            "verified": False,
            "reason": "Original task runtime, shared-volume semantics and original capacity contract have not completed parity acceptance.",
            "bounded_execution_pass_does_not_prove_original_contract_equivalence": True,
        },
        "counts": {
            state: counts.get(state, 0)
            for state in (
                "passed",
                "failed",
                "needs_reverification",
                "running",
                "not_run",
            )
        },
        "completion_counts": dict(Counter(a["completion"] for a in latest_passes)),
        "all_49_latest_pass_same_deployment": len(latest_passes) == 49
        and len({a["source_version"] for a in latest_passes}) == 1,
        "formal_capacity_acceptance": False,
        "formal_hidden_isolation_accepted": False,
        "reading_scope": "Existing local reports only; running means suite progress, not independently observed provider liveness.",
        "source_versions": sorted(
            versions.values(), key=lambda value: value["source_version"]
        ),
        "warnings": warnings,
        "tasks": tasks,
    }


def markdown(report: dict, root: Path, destination: Path) -> str:
    def link(ref: dict | None, title: str) -> str:
        if not ref:
            return "—"
        relative = os.path.relpath(root / ref["path"], destination.parent)
        return f"[{title}](<{relative}>)"

    counts = report["counts"]
    lines = [
        "# 49 题网页到科学执行验收进度",
        "",
        f"快照时间：{report['generated_at']}。",
        "",
        "**整体验收尚未通过：原始任务运行时等价性（source runtime parity）仍未验收。** 下表通过仅表示对应受限执行链路的证据通过，不能据此声称原始 Compose 任务合同、共享卷行为或容量上限已经等价。",
        "",
        f"最新尝试：通过 **{counts['passed']} / 49**，失败 **{counts['failed']}**，待补核 **{counts['needs_reverification']}**，运行中 **{counts['running']}**，尚未运行 **{counts['not_run']}**。",
        "",
        "通过要求网页流程报告与独立 R2 证据的任务编号、批次、归档 SHA256、冻结版本完全匹配，并通过角色、网络、评分和清理检查。suite 或 resolution 的成功标签不能单独作为通过依据。优先读取模型严格复查报告；只有 HTTP 200 或旧版 R2 通过，缺少模型响应终态证据，或未明确验证完整响应与 incomplete / length 截断区别的旧合同记录，标记为待补核；旧通过原件仍保留。",
        "",
        "每题 Agent 预算为 300 秒；初始化、verifier、归档和资源清理另计。预算停止与提前正常完成分别统计，不表示题目求解成功。运行中仅表示 suite 的最后记录，未查询进程、数据库或云端存活状态。",
        "",
        "| 编号（index） | 最新状态 | 结束方式 | 模型响应恢复 | 结果 | 批次 | 部署摘要 | 证据 |",
        "|---|---|---|---|---|---|---|---|",
    ]
    labels = {
        "passed": "通过",
        "failed": "失败",
        "running": "运行中",
        "not_run": "尚未运行",
        "needs_reverification": "待补核",
    }
    for task in report["tasks"]:
        item = task["latest_attempt"] or {}
        completion = {
            "budget_exhausted": "达到预算后评分归档",
            "completed_before_budget": "预算内正常结束",
        }.get(item.get("completion"), "—")
        links = (
            link(item.get("browser_report"), "网页")
            + " / "
            + link(item.get("r2_report"), "R2")
            if item
            else "—"
        )
        model = item.get("model_response_evidence")
        recovery = (
            f"{model['recovered_failed_responses']}/{model['failed_responses']}；未恢复 {model['unrecovered_failed_responses']}"
            if model
            else "尚无严格终态证据"
        )
        lines.append(
            f"| {task['catalog_index']:02d} | {labels[task['status']]} | {completion} | {recovery} | {item.get('reward', '—')} | {item.get('batch_id', '—')} | {item.get('source_version', '—')[:12]} | {links} |"
        )
    lines += [
        "",
        "## 部署版本边界",
        "",
        "以下每组来自实际浏览器报告记录的二进制与科学源码摘要；不同组的通过记录不能合并宣称在同一个部署版本上全部通过。文件名及隐藏题目名不在本汇总中展开。",
        "",
    ]
    for version in report["source_versions"]:
        indices = ", ".join(f"{i:02d}" for i in version["verified_indices"]) or "暂无"
        pending = (
            ", ".join(f"{i:02d}" for i in version["pending_model_indices"]) or "无"
        )
        lines += [
            f"- 部署 `{version['source_version']}`：严格通过编号 {indices}；待补模型终态证据编号 {pending}；科学源码清单摘要 `{version['science_manifest_sha256']}`，{version['science_file_count']} 个文件。"
        ]
    lines += [
        "",
        "## 失败历史与证据复查",
        "",
        "旧失败原件保留。复查仅在新的独立 R2 报告通过全部联结检查时计为通过；重新执行的任务作为新的尝试记录。",
        "",
    ]
    found = False
    for task in report["tasks"]:
        for item in task["attempt_history"]:
            for failure in item["failure_history"]:
                found = True
                lines.append(
                    f"- 编号 {task['catalog_index']:02d}：`{failure['code']}`；{link(failure['report'], '失败原件')}。"
                )
            if item.get("resolution_report"):
                lines.append(
                    f"- 编号 {task['catalog_index']:02d}：同批次、同归档复查通过，未重跑 Harness；{link(item['resolution_report'], '复查说明')}，{link(item['r2_report'], '通过的独立证据')}。"
                )
    if not found:
        lines.append("本快照未读取到失败记录。")
    if report["warnings"]:
        lines += ["", "## 输入完整性提醒", ""]
        lines.extend(
            f"- `{w['code']}`：{link(w['report'], '输入报告')}。"
            for w in report["warnings"]
        )
    lines += [
        "",
        "该汇总不证明 150–200 队持续容量或正式比赛完整隐藏隔离验收；不读取隐藏任务内容，不执行任何云调用，不修改数据库或证据原件。",
        "",
    ]
    return "\n".join(lines)


def write_atomic(path: Path, data: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=path.parent, delete=False
    ) as stream:
        temporary = Path(stream.name)
        stream.write(data)
    try:
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence-root", required=True, type=Path)
    parser.add_argument("--output-json", required=True, type=Path)
    parser.add_argument("--output-md", required=True, type=Path)
    args = parser.parse_args()
    root = args.evidence_root.resolve()
    outputs = [args.output_json.resolve(), args.output_md.resolve()]
    check(outputs[0] != outputs[1], "output_paths_must_differ")
    check(
        all(not path.is_relative_to(root) for path in outputs),
        "outputs_must_be_outside_evidence_root",
    )
    report = summarize(root)
    write_atomic(outputs[0], json.dumps(report, indent=2, ensure_ascii=False) + "\n")
    write_atomic(outputs[1], markdown(report, root, outputs[1]))
    print(json.dumps({"status": report["status"], "counts": report["counts"]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
