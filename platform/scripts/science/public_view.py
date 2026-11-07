"""Bounded, credential-redacted view of an Agent's public-task trajectory only."""

import json
import re
from urllib.parse import quote

MAX_PUBLIC_BYTES = 200 * 1024
MAX_SOURCE_BYTES = 32 * 1024 * 1024


def trajectory_view(path, *, secrets=(), limit=MAX_PUBLIC_BYTES):
    if path.stat().st_size > MAX_SOURCE_BYTES:
        return {
            "trajectory_excerpt": "[trajectory too large for public preview]",
            "trajectory_truncated": True,
        }
    text = path.read_text(errors="replace")
    for secret in sorted(
        (s for s in secrets if isinstance(s, str) and s), key=len, reverse=True
    ):
        text = text.replace(secret, "[REDACTED]")
        text = text.replace(quote(secret, safe=""), "[REDACTED]")
    text = re.sub(r"(?i)(\bbearer\s+)[A-Za-z0-9._~+/=-]+", r"\1[REDACTED]", text)
    # Redact quoted JSON properties and shell/config assignments, including
    # escaped quotes when a command is nested inside a JSONL string.
    sensitive = r"(?:[A-Za-z0-9_]*(?:api[_-]?key|access[_-]?token|secret[_-]?key)|QIQC_MODEL_TOKEN|HY_API)"
    text = re.sub(
        r'(?i)([\\"\x27]*'
        + sensitive
        + r'[\\"\x27]*\s*[:=]\s*[\\"\x27]*)[^\s,"\x27\\}]+',
        r"\1[REDACTED]",
        text,
    )
    encoded = text.encode("utf-8")
    truncated = len(encoded) > limit
    excerpt = encoded[:limit].decode("utf-8", errors="ignore")
    return {"trajectory_excerpt": excerpt, "trajectory_truncated": truncated}


def public_view_json(view, *, limit=480 * 1024):
    """Bound the final escaped wire representation, not merely raw text."""
    view = dict(view)
    while True:
        serialized = json.dumps(view, ensure_ascii=False, indent=2) + "\n"
        if len(serialized.encode("utf-8")) <= limit:
            return serialized
        excerpt = view.get("trajectory_excerpt", "")
        if not excerpt:
            raise ValueError("Public summary exceeds transport limit")
        view["trajectory_excerpt"] = excerpt[: len(excerpt) // 2]
        view["trajectory_truncated"] = True
