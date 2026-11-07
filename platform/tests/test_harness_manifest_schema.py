"""Minimal participant harness.json: derivation, legacy compatibility, conflicts.

Parser-only checks; no submitted content is executed.
"""
import hashlib
import io
import json
import zipfile

import pytest
from native_compose_contract import submission_manifest
from qiqc.archive import RUNTIMES, inspect_archive, normalize_manifest

LEGACY = {
    "codex": {"schema": "qiqc-harness/v1", "name": "QIQC codex CLI starter", "adapter": "codex",
              "entrypoint": "adapter/run.sh", "runtime_kind": "native_cli", "runtime_package": "@openai/codex",
              "runtime_version": "0.144.4", "model_interfaces": ["responses"], "trajectory": "native_jsonl",
              "task_transport": "native-http/v1"},
    "deepseek": {"schema": "qiqc-harness/v1", "name": "QIQC deepseek-harness CLI starter", "adapter": "deepseek-harness",
                 "entrypoint": "adapter/run.sh", "runtime_kind": "native_cli", "runtime_package": "@deepseek-ai/dsh",
                 "runtime_version": "0.2.1-rc.2", "model_interfaces": ["chat/completions", "responses"],
                 "trajectory": "native_jsonl", "task_transport": "native-http/v1"},
    "pi": {"schema": "qiqc-harness/v1", "name": "QIQC pi-agent CLI starter", "adapter": "pi-agent",
           "entrypoint": "adapter/run.sh", "runtime_kind": "native_cli",
           "runtime_package": "@earendil-works/pi-coding-agent", "runtime_version": "1.3.0",
           "model_interfaces": ["chat/completions", "responses"], "trajectory": "native_jsonl",
           "task_transport": "native-http/v1"},
}


def package(manifest, entry="adapter/run.sh", extra=()):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        archive.writestr("harness.json", manifest if isinstance(manifest, str) else json.dumps(manifest))
        archive.writestr(entry, "#!/bin/sh\nexit 0\n")
        archive.writestr("harness/agent.py", "print('ok')\n")
        for name, body in extra:
            archive.writestr(name, body)
    return stream.getvalue()


@pytest.mark.parametrize("runtime", ["codex", "deepseek", "pi"])
def test_minimal_manifest_derives_every_runtime_field(runtime):
    report = inspect_archive(package({"runtime": runtime}))
    manifest = report["manifest"]
    spec = RUNTIMES[runtime]
    assert manifest["schema"] == "qiqc-harness/v1"
    assert manifest["entrypoint"] == "adapter/run.sh"
    assert manifest["runtime"] == runtime and manifest["adapter"] == spec["adapter"]
    assert manifest["runtime_kind"] == "native_cli" and manifest["task_transport"] == "native-http/v1"
    assert manifest["trajectory"] == "native_jsonl"
    assert manifest["runtime_package"] == spec["runtime_package"]
    assert manifest["runtime_version"] == spec["runtime_version"]
    assert manifest["model_interfaces"] == spec["model_interfaces"]
    assert 1 <= len(manifest["name"]) <= 80


@pytest.mark.parametrize("runtime", ["codex", "deepseek", "pi"])
def test_legacy_full_form_is_accepted_and_normalized_identically(runtime):
    legacy = inspect_archive(package(LEGACY[runtime]))["manifest"]
    minimal = inspect_archive(package({"runtime": runtime}))["manifest"]
    assert {k: v for k, v in legacy.items() if k != "name"} == {k: v for k, v in minimal.items() if k != "name"}
    assert legacy["name"] == LEGACY[runtime]["name"]


def test_optional_fields_and_aliases():
    report = inspect_archive(package({"runtime": "pi-agent", "entrypoint": "adapter/start.sh", "name": "Team bot",
                                      "description": "Pi with a planner"}, entry="adapter/start.sh"))
    manifest = report["manifest"]
    assert manifest["runtime"] == "pi" and manifest["adapter"] == "pi-agent"
    assert manifest["entrypoint"] == "adapter/start.sh" and manifest["name"] == "Team bot"
    # A legacy "version" string is tolerated but ignored: versions are numbered by the platform.
    assert normalize_manifest({"adapter": "codex", "version": "1.2"})["runtime"] == "codex"
    assert "version" not in normalize_manifest({"adapter": "codex", "version": "1.2"})
    # A declared subset of the runtime's interfaces is honoured (first is used).
    assert normalize_manifest({"runtime": "deepseek", "model_interfaces": ["responses"]})["model_interfaces"] == ["responses"]


@pytest.mark.parametrize("runtime", ["codex", "deepseek", "pi"])
def test_supported_runtime_window_accepts_every_listed_version(runtime):
    spec = RUNTIMES[runtime]
    for version in spec["runtime_versions"]:
        manifest = normalize_manifest({"runtime": runtime, "runtime_version": version})
        # The normalized report records the organizer-managed execution snapshot,
        # while every listed declaration is accepted for compatibility.
        assert manifest["runtime_version"] == spec["deployed_version"]




@pytest.mark.parametrize("manifest, message", [
    ({}, '"runtime" is required'),
    ({"runtime": "claude"}, 'must be one of codex, deepseek or pi'),
    ({"runtime": ["codex"]}, 'must be one of codex, deepseek or pi'),
    ({"runtime": "codex", "adapter": "pi-agent"}, 'name different runtimes'),
    ({"runtime": "codex", "runtime_version": "0.1.0"}, '"runtime_version" is not a supported codex version'),
    ({"runtime": "deepseek", "runtime_package": "@openai/codex"}, '"runtime_package" does not match'),
    ({"runtime": "codex", "model_interfaces": ["chat/completions"]}, '"model_interfaces" is not supported by the codex runtime'),
    ({"runtime": "pi", "model_interfaces": []}, '"model_interfaces" is not supported'),
    ({"runtime": "pi", "model_interfaces": ["responses", "responses"]}, '"model_interfaces" is not supported'),
    ({"runtime": "codex", "runtime_kind": "python"}, '"runtime_kind" must be native_cli'),
    ({"runtime": "codex", "task_transport": "stdio"}, '"task_transport" must be native-http/v1'),
    ({"runtime": "codex", "trajectory": "text"}, '"trajectory" must be native_jsonl'),
    ({"runtime": "codex", "schema": "qiqc-harness/v2"}, '"schema" is optional'),
    ({"runtime": "codex", "api_key": "x"}, 'unsupported field'),
    ({"runtime": "codex", "version": 3}, '"version" is no longer needed'),
    ({"runtime": "codex", "name": ""}, '"name" is optional'),
    ({"runtime": "codex", "name": "x" * 81}, '"name" is optional'),
    ({"runtime": "codex", "description": 5}, '"description" is optional'),
    ({"runtime": "codex", "entrypoint": "run.sh"}, 'Entrypoint must be inside adapter/'),
    ({"runtime": "codex", "entrypoint": "adapter/run.py"}, 'shell script ending in .sh'),
    ([], 'one JSON object'),
])
def test_contradictory_or_invalid_manifests_have_clear_errors(manifest, message):
    with pytest.raises(ValueError, match=None) as error:
        inspect_archive(package(manifest))
    assert message in str(error.value)
    assert len(str(error.value)) <= 300


def test_layout_rules_still_apply_to_minimal_manifest():
    with pytest.raises(ValueError, match="Entrypoint file missing"):
        inspect_archive(package({"runtime": "codex", "entrypoint": "adapter/other.sh"}))
    with pytest.raises(ValueError, match="Only harness/, adapter/ and harness.json"):
        inspect_archive(package({"runtime": "codex"}, extra=[("README.md", "x")]))


def test_errors_never_echo_submitted_values():
    marker = "<script>alert(1)</script>"
    for manifest in ({"runtime": marker}, {"runtime": "codex", "runtime_version": marker},
                     {"runtime": "codex", marker: 1}, {"adapter": marker}):
        with pytest.raises(ValueError) as error:
            normalize_manifest(manifest)
        assert marker not in str(error.value)


@pytest.mark.parametrize("manifest", [{"runtime": "deepseek"}, LEGACY["deepseek"]])
def test_science_contract_uses_the_same_normalizer(tmp_path, manifest):
    path = tmp_path / "harness.zip"
    path.write_bytes(package(manifest))
    native = submission_manifest(path, hashlib.sha256(path.read_bytes()).hexdigest())
    assert native["adapter"] == "deepseek-harness"
    assert native["model_interfaces"][0] == "chat/completions"
    assert native["entrypoint"] == "adapter/run.sh"


def test_science_contract_rejects_conflicts(tmp_path):
    path = tmp_path / "harness.zip"
    path.write_bytes(package({"runtime": "codex", "adapter": "pi-agent"}))
    with pytest.raises(ValueError, match="native_submission_contract_required"):
        submission_manifest(path, hashlib.sha256(path.read_bytes()).hexdigest())
