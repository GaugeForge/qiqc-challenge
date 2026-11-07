"""Trusted public prompt assembly only; never launch Codex, qsim, or science."""
import hashlib
import io
import json
import os
import shlex
import subprocess
import zipfile

import pytest
from native_compose_template import ADAPTER, archive_bytes


def members(**kwargs):
    with zipfile.ZipFile(io.BytesIO(archive_bytes(**kwargs))) as bundle:
        return {name: bundle.read(name) for name in bundle.namelist()}


def test_ordinary_adapter_and_zip_remain_byte_identical():
    assert hashlib.sha256(ADAPTER.encode()).hexdigest() == "9fab2c8a9d38d5b60e0b1b9d378759ffa0b98fe6a51c2110cceea0ee2283fc1b"
    assert hashlib.sha256(archive_bytes()).hexdigest() == "0e5c458265e070110956c4f6dd477abf6f43f3e7c6badcca996577ac302fdd90"
    normal = members()
    assert b"QIQC_CONNECTIVITY_FIRST" not in normal["adapter.sh"]
    assert json.loads(normal["harness.json"])["version"] == "native-compose-candidate/1"


@pytest.mark.parametrize("reasoning_effort", [None, "none"])
def test_sustained_v2_is_deterministic_and_declares_diagnostic_strategy(reasoning_effort):
    data = archive_bytes(sustained=True, reasoning_effort=reasoning_effort)
    assert data == archive_bytes(sustained=True, reasoning_effort=reasoning_effort)
    assert hashlib.sha256(data).hexdigest() != "81215e29563eb289ab7903850a6af8ef3d870e003f3d05290a1dbf0955859742"
    files = members(sustained=True, reasoning_effort=reasoning_effort)
    manifest = json.loads(files["harness.json"])
    expected = "native-compose-sustained-acceptance/2" + ("-reasoning-none" if reasoning_effort else "")
    assert manifest["version"] == expected
    assert b"not a competition baseline" in files["README.md"]
    assert b"never invokes qsim on behalf of the model" in files["README.md"]
    subprocess.run(["sh", "-n"], input=files["adapter.sh"], check=True, capture_output=True)


def test_prompt_constructor_prepends_real_tool_requirement_preserves_input_verbatim(tmp_path):
    script = members(sustained=True)["adapter.sh"].decode()
    constructor, loop = script.split("while :; do", 1)
    # Execute only the organizer-owned text constructor against a public fixture.
    # The Codex loop is discarded and no benchmark material is read or executed.
    assert "codex exec" not in constructor
    assert "codex exec" in loop
    assert constructor.count("prompt=/tmp/qiqc-sustained-task-input.txt") == 1
    output = tmp_path / "assembled.txt"
    constructor = constructor.replace("prompt=/tmp/qiqc-sustained-task-input.txt", "prompt=" + shlex.quote(str(output)))
    source = tmp_path / "public-input.txt"
    original = b"Public fixture only.\n$(touch THIS_MUST_NOT_EXECUTE)\n\xff\x00verbatim-no-final-newline"
    source.write_bytes(original)
    subprocess.run(["sh"], input=constructor.encode(), check=True, capture_output=True,
                   cwd=tmp_path, env={**os.environ, "QIQC_TASK_INPUT": str(source)})
    assembled = output.read_bytes()
    prefix, rest = assembled.split(original, 1)
    assert b"qsim MCP tool list_devices with empty arguments" in prefix
    assert b"Wait for its real successful" in prefix
    assert b"Do not call trusted host controls" in prefix
    assert b"bounded infrastructure acceptance session" in rest
    assert assembled.count(original) == 1
    assert source.read_bytes() == original
    assert not (tmp_path / "THIS_MUST_NOT_EXECUTE").exists()
