"""Submitted CLI configuration only; never executes the Harness or calls a model."""
import hashlib
import io
import json
import shlex
import subprocess
import zipfile

import pytest
from native_compose_template import archive_bytes
from qiqc.archive import inspect_archive


@pytest.mark.parametrize('sustained,sha', [
    (False, '0e5c458265e070110956c4f6dd477abf6f43f3e7c6badcca996577ac302fdd90')])
def test_omitted_setting_preserves_exact_historical_zip_bytes(sustained, sha):
    original = archive_bytes(sustained=sustained)
    assert hashlib.sha256(original).hexdigest() == sha
    assert original == archive_bytes(sustained=sustained, reasoning_effort=None)


@pytest.mark.parametrize('sustained', [False, True])
def test_explicit_none_is_actual_codex_cli_argument_and_distinct_zip(sustained):
    data = archive_bytes(sustained=sustained, reasoning_effort='none')
    assert data != archive_bytes(sustained=sustained)
    assert data == archive_bytes(sustained=sustained, reasoning_effort='none')
    assert inspect_archive(data)['manifest']['entrypoint'] == 'adapter.sh'
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        script = z.read('adapter.sh').decode()
        manifest = json.loads(z.read('harness.json'))
    assert manifest['version'].endswith('-reasoning-none')
    assert manifest['reasoning_effort'] == 'none'
    command = next(line for line in script.replace('\\\n', ' ').splitlines() if 'codex exec ' in line)
    argv = shlex.split(command)
    pos = argv.index('codex')
    assert argv[pos:pos+4] == ['codex', 'exec', '-c', 'model_reasoning_effort="none"']
    assert '--model' in argv and '$QIQC_MODEL' in argv
    subprocess.run(['sh', '-n'], input=script, text=True, check=True, capture_output=True)


@pytest.mark.parametrize('value', ['', 'low', 'high', 'NONE', 'none; injected', True, 0])
def test_unknown_or_injected_configuration_rejected(value):
    with pytest.raises(ValueError, match='reasoning_effort'):
        archive_bytes(sustained=True, reasoning_effort=value)
