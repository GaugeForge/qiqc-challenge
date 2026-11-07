"""Organizer-owned model configuration; the safe identity contains no key/path/URL."""

from __future__ import annotations

import hashlib
import ipaddress
import json
import re
from pathlib import Path
from urllib.parse import unquote, urlsplit

FULL_KEYS = {
    "schema",
    "id",
    "base_url",
    "model",
    "endpoints",
    "key_file",
    "quota_group",
    "requests_per_minute",
    "requests_per_day",
    "allow_paid_fallback",
    "parameter_policy",
    "authentication",
    "billing",
}
SAFE_KEYS = {
    "schema",
    "id",
    "sha256",
    "model",
    "endpoints",
    "quota_group",
    "parameter_policy",
    "authentication",
    "billing",
}


def canonical(value):
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode()


def _strings(value):
    if isinstance(value, dict):
        return all(_strings(k) and _strings(v) for k, v in value.items())
    if isinstance(value, list):
        return all(_strings(v) for v in value)
    return not isinstance(value, str) or (
        value.isascii() and not any(ord(c) < 32 or ord(c) > 126 for c in value)
    )


def _common(value):
    if (
        not _strings(value)
        or not re.fullmatch(r"[a-z0-9_-]{1,64}", str(value.get("id", "")))
        or not re.fullmatch(r"[a-z0-9_-]{1,64}", str(value.get("quota_group", "")))
        or not isinstance(value.get("model"), str)
        or not 1 <= len(value["model"]) <= 200
        or value["model"].strip() != value["model"]
        or not isinstance(value.get("endpoints"), list)
        or not value["endpoints"]
        or any(v not in ("responses", "chat/completions") for v in value["endpoints"])
        or len(set(value["endpoints"])) != len(value["endpoints"])
        or value.get("authentication") != "bearer"
        or value.get("parameter_policy") != "passthrough/v1"
        or value.get("billing") not in ("free", "paid")
    ):
        raise ValueError("invalid_provider_profile")


def validate_profile_identity(value):
    if (
        not isinstance(value, dict)
        or set(value) != SAFE_KEYS
        or value.get("schema") != "qiqc-provider-profile-identity/v1"
    ):
        raise ValueError("invalid_provider_profile_identity")
    _common(value)
    if not isinstance(value["sha256"], str) or not re.fullmatch(
        r"[a-f0-9]{64}", value["sha256"]
    ):
        raise ValueError("invalid_provider_profile_identity_digest")
    return value


def _public_host(host):
    host = host.lower().rstrip(".")
    if host == "localhost" or host.endswith(".localhost") or "." not in host:
        return False
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return True
    return address.is_global and not address.is_multicast and not address.is_unspecified


def validate_profile(value):
    if (
        not isinstance(value, dict)
        or set(value) != FULL_KEYS
        or value.get("schema") != "qiqc-provider-profile/v1"
    ):
        raise ValueError("invalid_provider_profile_fields")
    _common(value)
    url = urlsplit(value["base_url"])
    if (
        url.scheme != "https"
        or not url.hostname
        or url.username
        or url.password
        or url.query
        or url.fragment
        or url.port not in (None, 443)
        or ".." in unquote(url.path)
        or not _public_host(url.hostname)
        or not isinstance(value["key_file"], str)
        or not Path(value["key_file"]).is_absolute()
        or value["allow_paid_fallback"] is not False
        or type(value["requests_per_minute"]) is not int
        or not 1 <= value["requests_per_minute"] <= 100000
        or type(value["requests_per_day"]) is not int
        or not 1 <= value["requests_per_day"] <= 10000000
    ):
        raise ValueError("invalid_provider_profile_policy")
    if url.hostname.lower().rstrip(".") == "openrouter.ai" and value["billing"] == "free":  # noqa: SIM102 — separate provider-specific pricing guard.
        if (
            not value["model"].endswith(":free")
            or value["requests_per_minute"] > 20
            or value["requests_per_day"] > 1000
        ):
            raise ValueError("openrouter_free_profile_limits_invalid")
    return value


def profile_identity(value):
    validate_profile(value)
    safe = {key: value[key] for key in SAFE_KEYS - {"schema", "sha256"}}
    safe.update(
        schema="qiqc-provider-profile-identity/v1",
        sha256=hashlib.sha256(canonical(value)).hexdigest(),
    )
    return validate_profile_identity(safe)


def load_profile(path):
    path = Path(path)
    if (
        not path.is_absolute()
        or any(parent.is_symlink() for parent in (path, *path.parents))
        or not path.is_file()
        or path.stat().st_size > 16384
        or path.stat().st_mode & 0o037
    ):
        raise ValueError("private_provider_profile_file_required")

    def pairs(items):
        out = {}
        for key, value in items:
            if key in out:
                raise ValueError("duplicate_provider_profile_field")
            out[key] = value
        return out

    value = validate_profile(json.loads(path.read_bytes(), object_pairs_hook=pairs))
    return value, profile_identity(value)
