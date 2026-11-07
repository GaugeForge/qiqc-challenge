"""Bundle authentication without executing any runtime or scientific code."""

import hashlib
import json

import pytest
from native_runtime_bundles import PACKAGES, SCHEMA, authenticated_bundle, read_manifest


def fixture(tmp_path):
    data = b"inert-runtime-fixture"
    packages = {}
    for kind, (package, version) in PACKAGES.items():
        (tmp_path / (kind + ".tar.gz")).write_bytes(data)
        packages[kind] = {"package": package, "version": version,
                          "node_version": "v24.9.0", "bytes": len(data),
                          "sha256": hashlib.sha256(data).hexdigest(), "lock_sha256": "a" * 64}
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"schema": SCHEMA, "packages": packages}))
    return manifest


@pytest.mark.parametrize("kind", PACKAGES)
def test_prepared_bytes_must_match_controller_bound_manifest(tmp_path, kind):
    manifest = fixture(tmp_path)
    bundle = authenticated_bundle(kind, manifest, tmp_path)
    bundle.write_bytes(b"x" * bundle.stat().st_size)
    with pytest.raises(ValueError, match="bundle_changed"):
        authenticated_bundle(kind, manifest, tmp_path)


def test_missing_and_linked_runtime_are_not_an_installation_request(tmp_path):
    manifest = fixture(tmp_path)
    target = tmp_path / "pi-agent.tar.gz"
    target.unlink()
    with pytest.raises(ValueError, match="unavailable"):
        authenticated_bundle("pi-agent", manifest, tmp_path)
    target.symlink_to(tmp_path / "deepseek-harness.tar.gz")
    with pytest.raises(ValueError, match="unavailable"):
        authenticated_bundle("pi-agent", manifest, tmp_path)


def test_manifest_rejects_unknown_or_wrong_runtime(tmp_path):
    manifest = fixture(tmp_path)
    data = json.loads(manifest.read_text())
    data["packages"]["pi-agent"]["version"] = "latest"
    manifest.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="invalid"):
        read_manifest(manifest)
    with pytest.raises(ValueError, match="unsupported"):
        authenticated_bundle("unregistered", manifest, tmp_path)


def test_controller_hash_binds_optional_runtime_manifest(tmp_path):
    from native_controller import inventory
    from native_runtime_bundles import RELATIVE_MANIFEST
    from test_native_candidate import config
    value, _ = config(tmp_path)
    before = inventory(tmp_path, value["binaries"])
    destination = tmp_path / RELATIVE_MANIFEST
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(fixture(tmp_path).read_bytes())
    after = inventory(tmp_path, value["binaries"])
    assert before != after
    assert after["remote_files"]["native-runtime-bundles.json"]["sha256"] == hashlib.sha256(destination.read_bytes()).hexdigest()


@pytest.mark.parametrize('endpoint', ['responses', 'chat/completions'])
def test_cloud_check_keeps_protocol_environment_name(endpoint):
    from verify_native_runtime_snapshot import check_script
    script = check_script(endpoint)
    assert 'export QIQC_MODEL_PROTOCOL=' + endpoint + ' ' in script
    assert '__MODEL_PROTOCOL__' not in script
