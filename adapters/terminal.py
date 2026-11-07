"""Validate native CLI terminal events without invoking any agent process."""


def successful_terminal(kind, rows):
    if kind == "codex":
        return any(row.get("type") == "turn.completed" for row in rows) and not any(
            row.get("type") in ("turn.failed", "error") for row in rows
        )
    if kind == "deepseek-harness":
        return any(row.get("type") == "final" and row.get("text") for row in rows) and not any(
            row.get("type") == "error" for row in rows
        )
    if kind == "pi-agent":
        complete = any(row.get("type") == "agent_settled" and not row.get("aborted") for row in rows)
        for row in rows:
            messages = [row.get("message")]
            if isinstance(row.get("messages"), list):
                messages.extend(row["messages"])
            if any(isinstance(message, dict) and message.get("stopReason") in ("error", "aborted") for message in messages):
                return False
        return complete
    return False
