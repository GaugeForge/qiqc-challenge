#!/usr/bin/env python3
"""Refresh local native acceptance progress from the freeze gate and read-only DB.

This is a documentation view, never a substitute for the independent R2 checker
or a command that starts science, freezes a release or changes admission.
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import sqlite3
from pathlib import Path

from native_candidate import active_release, identity, load_candidate
from native_controller import verify as verify_controller
from native_freeze_gate import assess


def refresh(config_path, evidence_root, capacity_path, output):
    config, _ = load_candidate(config_path)
    verify_controller(config)
    release = active_release(config)
    release_id = release['release_id'] if release else ''
    gate = assess(config, evidence_root)
    capacity = json.loads(capacity_path.read_text())
    if capacity["source_files_sha256"] != config["source_files_sha256"]:
        raise ValueError("capacity_source_mismatch")
    capacity_warnings = set(
        capacity.get("full_capacity_insufficient_indices", capacity["blocked_indices"])
    )
    attempts = {index: [] for index in range(49)}
    for path in sorted(evidence_root.rglob("*.json")):
        data = json.loads(path.read_text())
        index = data.get("catalog_index")
        if type(index) is not int or not 0 <= index < 49:
            continue
        if data.get("environment") != "isolated-local-native-candidate-real-cloud":
            continue
        if data.get("benchmark_release") == identity(config, index):
            attempts[index].append((path, data))
    active = {}
    dbpath = Path(config["data"]) / "platform.db"
    with sqlite3.connect(dbpath.resolve().as_uri() + "?mode=ro", uri=True) as db:
        registered = db.execute(
            "SELECT value FROM settings WHERE key='scientific_active_release'"
        ).fetchone()
        if (registered[0] if registered else '') != release_id:
            raise ValueError('progress_active_release_registration_mismatch')
        for index, state, run in db.execute(
            """
            SELECT t.task_index,r.state,r.id FROM runs r
            JOIN batch_scientific_targets t ON t.batch_id=r.batch_id
            WHERE r.state IN ('queued','running','uploading')
            AND t.release_id IN (?,?) ORDER BY r.rowid
        """,
            (config["candidate_id"], release_id),
        ):
            active[index] = (state, run)
    activation_verified = False
    audit_path = Path(config['candidate_root']) / 'release-activation-audit.jsonl'
    if release and audit_path.is_file():
        events = [json.loads(line) for line in audit_path.read_text().splitlines() if line.strip()]
        relevant = [event for event in events if event.get('release_id') == release_id]
        activation_verified = bool(
            relevant and relevant[-1].get('action') == 'activation_verified'
            and not (Path(config['candidate_root']) / '.native-release-activation').exists()
        )
    release_status = '尚未生成新冻结版本，尚未切换平台。'
    if release:
        release_status = (
            '已生成新冻结版本并启用；切换后的真实网页/R2复验已完成。'
            if activation_verified else
            '已登记并启用冻结版本；切换后的现场验收尚未收尾。'
        )
    now = datetime.datetime.now(datetime.UTC).isoformat()
    concurrency = gate.get("concurrency_evidence") or {}
    concurrent_status = (
        f"49题实际执行共同重叠 {concurrency['overlap_seconds']:.6f} 秒。"
        if gate["status"] == "eligible_for_explicit_freeze" else
        "49题同时执行门槛尚未通过；单题通过数不代表允许冻结。"
    )
    lines = [
        "# 49 题原生 Compose 网页验收进度",
        "",
        f"更新时间：{now}。本页由 `native_progress.py` 从只读数据库和独立验收报告生成。",
        "",
        f"当前控制程序 SHA256：`{config['controller_sha256']}`。",
        "",
        f"**当前版本已通过 {gate['passed_tasks']}/49；完整资源条件未满足 {len(capacity_warnings)} 题，这些题仍参与短程验证。{release_status}**",
        "",
        concurrent_status,
        *( ["", f"当前启用 release：`{release_id}`。"] if release else [] ),
        "",
        (
            "通过数只采用同一源码、构建回执、模板和控制程序版本下，网页报告与独立 R2 检查均满足冻结门槛的结果。"
            "正在运行、只完成镜像构建、历史文件桥记录、旧控制程序的归档恢复，都不计入通过数。"
        ),
        "",
        (
            "本轮按负责人最新要求验证全部 49 题，每题实际观察约 300 秒，再由独立 verifier 评分；不再因模型和 qsim 首次交互成功而提前停止。旧提前停止报告不计入本轮。原始源文件、科学镜像和容器限制保持原样。"
            "账号升级后的资源能力以最新实际 API 和运行证据为准；不能仅由声明的最大资源推导短程测试必然失败，实际 OOM、超时或评分错误仍不能通过。"
            "详见 [真实阻塞清单](REAL-BLOCKERS.md) 与 [逐题资源审计](NATIVE-49-RESOURCE-AND-TIMEOUT-AUDIT-20261020.md)。"
        ),
        "",
        "| 编号（0 起算） | 当前状态 | 证据 |",
        "|---|---|---|",
    ]

    def link(path, label):
        return f"[{label}](<{Path(os.path.relpath(path, output.parent)).as_posix()}>)"

    for row in gate["rows"]:
        index = row["task_index"]
        current = attempts[index]
        evidence = "—"
        if row["passed"]:
            path = evidence_root / row["selected"]["evidence"]
            browser = path.with_name(path.name[: -len("-r2.json")] + ".json")
            state = "已通过：网页与独立 R2"
            evidence = link(browser, "网页") + " / " + link(path, "R2")
        elif index in active:
            label = {
                "queued": "已从网页受理，排队等待执行",
                "running": "执行中，尚未完成验收",
                "uploading": "执行收尾，正在归档",
            }[active[index][0]]
            state = label + "（实时 DB 快照，未计通过）"
        elif current:
            path, data = current[-1]
            state = (
                "网页完成，独立 R2 尚未通过"
                if data.get("status") == "passed"
                else "本版尝试失败，待修复复验"
            )
            evidence = link(path, "网页记录")
        else:
            state = "本版尚未执行"
        if index in capacity_warnings:
            state += "；完整资源容量未签收"
        lines.append(f"| {index:02d} | {state} | {evidence} |")
    lines += [
        "",
        "## 证据边界",
        "",
        (
            "每题 Agent 按 300 秒预算实际运行，可信预算停止且实测不少于 299 秒才计入本轮五分钟门槛；自然提前退出单独记录，不空等补时间。准备、独立 verifier、归档与资源清理另计。"
            "reward 为 0 或 1 都可以证明链路工作，但只能由真实 verifier 产生。"
            "达到预算时的计划取消必须有可信宿主停止证明和对应网关账本；不能掩盖此前未恢复的模型错误。"
        ),
        "",
        (
            "历史尝试仍保存在 `platform/test-results/`，相关故障与修复见 "
            "[原生候选验收](NATIVE-CANDIDATE-ACCEPTANCE.md)。"
            "旧文件桥的逐题记录见 [历史执行说明](ALL49-WEB-E2E-ACCEPTANCE.md#历史实验实施及报告状态旧执行合同不计入本次原生49题验收)，不合并到本表。"
        ),
        "",
        (
            "只有完整 49/49 满足门槛后，才能执行 [冻结与切换操作单](NATIVE-RELEASE-CUTOVER.md)。"
            "本页的 DB 运行状态只用于观察进度，不构成云资源存活或科学通过的证明。"
        ),
        "",
    ]
    output.write_text("\n".join(lines))
    return {
        "passed_tasks": gate["passed_tasks"],
        "required_tasks": 49,
        "full_capacity_insufficient_tasks": len(capacity_warnings),
        "active_db_rows": len(active),
        "controller_sha256": config["controller_sha256"],
        "active_release_id": release_id or None,
        "post_activation_verified": activation_verified,
        "freeze_gate_status": gate["status"],
        "concurrency_evidence": gate.get("concurrency_evidence"),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("candidate", "evidence-root", "capacity", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    args = parser.parse_args()
    print(
        json.dumps(
            refresh(args.candidate, args.evidence_root, args.capacity, args.output)
        )
    )


if __name__ == "__main__":
    main()
