"""Static contract checks only; never execute submitted adapter code on the VPS."""

import ast
import hashlib
import json
import zipfile
from pathlib import Path
import pytest
from native_compose_contract import submission_manifest
from qiqc.archive import inspect_archive

ROOT = Path(__file__).resolve().parents[2] / "adapters"


@pytest.mark.parametrize("adapter", ["codex", "deepseek", "pi"])
def test_v1_starter_layout_and_native_contract(adapter):
    path = ROOT / adapter / "harness.zip"
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    report = inspect_archive(path.read_bytes())
    assert report["manifest"]["schema"] == "qiqc-harness/v1"
    native = submission_manifest(path, digest)
    assert native["entrypoint"] == "adapter/run.sh"
    index = json.loads((ROOT / "manifest.json").read_text())
    assert index["packages"][adapter]["sha256"] == digest
    assert index["packages"][adapter]["derived"] == report["manifest"] == native
    with zipfile.ZipFile(path) as z:
        # Participants write only the runtime; everything else is derived.
        assert json.loads(z.read("harness.json")) == {"runtime": adapter}
        assert sorted({n.split("/")[0] for n in z.namelist()}) == ["adapter", "harness", "harness.json"]
        runtime = z.read("harness/runtime.py").decode()
        compile(runtime, "template-runtime", "exec")
        tree = ast.parse(runtime)
        reads = [
            n
            for n in ast.walk(tree)
            if isinstance(n, ast.Subscript)
            and isinstance(n.slice, ast.Constant)
            and n.slice.value == "QIQC_AGENT_TIMEOUT"
        ]
        assert reads, "runtime must require the task budget supplied by the runner"
        defaults = [
            n
            for n in ast.walk(tree)
            if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute)
            and n.func.attr == "get"
            and n.args
            and isinstance(n.args[0], ast.Constant)
            and n.args[0].value == "QIQC_AGENT_TIMEOUT"
        ]
        assert not defaults, "runtime cannot silently replace the task budget"
        assert "QIQC_MCP_SERVERS" in runtime
        # Pi distinguishes environment references from literal API keys. This
        # exact contract already failed against the scoped gateway historically.
        pi_keys = [
            v.value
            for node in ast.walk(tree)
            if isinstance(node, ast.Dict)
            for k, v in zip(node.keys, node.values)
            if isinstance(k, ast.Constant)
            and k.value == "apiKey"
            and isinstance(v, ast.Constant)
        ]
        assert pi_keys == ["$QIQC_MODEL_TOKEN"]
