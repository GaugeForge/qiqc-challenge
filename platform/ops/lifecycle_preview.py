"""Pure rollout retention preview. Does not call R2 or delete any object.

An actual deletion worker must recheck references transactionally immediately
before deletion and use a separately authorized identity. This preview alone is
not a lifecycle policy deployment or permission-boundary test.
"""
from datetime import datetime


def _instant(value: str) -> datetime:
    instant = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if instant.tzinfo is None:
        raise ValueError("Retention timestamps require an explicit timezone")
    return instant


def preview(records: list[dict], *, now: str, policy_version: str) -> dict:
    if not policy_version.strip():
        raise ValueError("A frozen policy version is required for preview")
    current = _instant(now)
    decisions, seen = [], set()
    for record in records:
        identifier = record["run_id"] + ":" + str(record["attempt"])
        if identifier in seen:
            raise ValueError("Duplicate rollout attempt in retention inventory")
        seen.add(identifier)
        if record.get("hold") or record.get("referenced"):
            reason = "active_reference_or_hold"
        elif record.get("upload_state") != "verified":
            reason = "source_not_verified"
        elif record.get("backup_verified") is not True:
            reason = "independent_copy_not_verified"
        elif not record.get("expires_at"):
            reason = "retention_not_frozen"
        elif _instant(record["expires_at"]) > current:
            reason = "unexpired"
        else:
            reason = "eligible_for_review"
        decisions.append({"run_id": record["run_id"], "attempt": record["attempt"],
                          "eligible": reason == "eligible_for_review", "reason": reason})
    return {"mode": "dry-run-only", "policy_version": policy_version,
            "delete_performed": False, "decisions": decisions}
