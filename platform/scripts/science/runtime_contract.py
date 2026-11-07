"""Build E2B dependency/client contracts from hash-verified upstream inputs.

No scientific modules are imported on the platform host. Upstream private checkout
is unnecessary at runtime: the retained frozen files have independently matched it.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from task_release import verified_bytes, verify_release

ASSETS = Path(__file__).with_name('runtime_contract')
PYTHON = '/opt/qiqc-python/bin/python'
UV = '/opt/qiqc-bootstrap/uv'
CLIENT_ROOT = '/opt/qiqcbench-agent'
MAIN_PACKAGES = ('qiskit', 'qiskit-aer', 'qcodes', 'httpx', 'numpy', 'scipy',
                 'pydantic', 'ruamel.yaml', 'pymatching', 'stim', 'qualtran', 'pyzx', 'mcp')
MAIN_IMPORTS = ('qiskit', 'qiskit_aer', 'qcodes', 'httpx', 'numpy', 'scipy',
                'pydantic', 'ruamel.yaml', 'pymatching', 'stim', 'qualtran', 'pyzx', 'mcp')


def contract(root, runtime, role, task_id=None):
    if role not in {'main', 'qsim', 'verifier', 'catalog'}:
        raise ValueError('Unsupported runtime role')
    release = verify_release(root, runtime)
    main = role == 'main'
    hamlearn = role == 'verifier' and task_id == 'time_budgeted_hamlearn_10q'
    docker = 'docker/agent/Dockerfile' if main else ('harbor_tasks/time_budgeted_hamlearn_10q/tests/Dockerfile' if hamlearn else 'docker/qsim/Dockerfile')
    source = verified_bytes(runtime, docker, release)
    clients = {}
    if main:
        pins = dict(re.findall(rb'"([a-zA-Z0-9_.-]+)==([^"\s]+)"', source))
        pins = {name.decode(): version.decode() for name, version in pins.items()}
        if set(pins) != set(MAIN_PACKAGES):
            raise ValueError('Upstream agent dependency contract changed')
        requirements = (ASSETS / 'main-requirements.txt').read_bytes()
        locked = {re.sub(rb'[-_.]+', b'-', name).lower(): version for name, version in re.findall(rb'^([a-zA-Z0-9_.-]+)==([^\s;\\]+)', requirements, re.M)}
        if any(locked.get(re.sub(rb'[-_.]+', b'-', name.encode()).lower()) != version.encode() for name, version in pins.items()):
            raise ValueError('Agent lock changed upstream direct dependency pin')
        for relative in sorted(release['files']):
            if relative.startswith('docker/agent/lib/'):
                if not relative.endswith('.py') or '..' in Path(relative).parts:
                    raise ValueError('Unexpected upstream client file type')
                clients[relative.removeprefix('docker/agent/lib/')] = verified_bytes(runtime, relative, release)
        if len(clients) != 9:
            raise ValueError('Upstream client library contract changed')
        req_source = 'platform/runtime_contract/main-requirements.txt'
    else:
        req_source = 'harbor_tasks/time_budgeted_hamlearn_10q/tests/requirements.txt' if hamlearn else 'docker/qsim/requirements.txt'
        requirements = verified_bytes(runtime, req_source, release)
        pins = {}
    metadata = {'role': role, 'task_id': task_id, 'python': '3.11.14',
                'source_dockerfile': docker, 'source_dockerfile_sha256': hashlib.sha256(source).hexdigest(),
                'requirements_source': req_source, 'requirements_sha256': hashlib.sha256(requirements).hexdigest(),
                'original_direct_pins': pins,
                'clients': [{'path': CLIENT_ROOT + '/' + name, 'sha256': hashlib.sha256(data).hexdigest()} for name, data in clients.items()],
                'main_transitives': 'new frozen closure; original Docker pinned direct packages only' if main else None}
    return requirements, clients, metadata


def install(sb, root, runtime, *, role, task_id=None):
    requirements, clients, metadata = contract(root, runtime, role, task_id)
    sb.files.write('/root/qiqc-bootstrap-python.py', (ASSETS / 'bootstrap_python.py').read_text(), user='root')
    sb.commands.run('python3 /root/qiqc-bootstrap-python.py', user='root', timeout=240)
    system = 'bash curl ca-certificates git build-essential'
    if role == 'main':
        system += ' procps cmake ninja-build'
    sb.commands.run('apt-get -o Acquire::Retries=3 update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends ' + system, user='root', timeout=240)
    sb.files.write('/opt/qiqc-bootstrap/requirements.txt', requirements, user='root')
    # Wheels only: dependency installation must not execute arbitrary sdist build
    # scripts. The reviewed Linux x86_64 / CPython 3.11 closure has wheels.
    sb.commands.run(UV + ' pip install --python ' + PYTHON + ' --no-deps --require-hashes --no-build --default-index https://pypi.org/simple -r /opt/qiqc-bootstrap/requirements.txt', user='root', timeout=600)
    sb.commands.run(UV + ' pip check --python ' + PYTHON, user='root', timeout=60)
    for name, data in clients.items():
        target = CLIENT_ROOT + '/' + name
        sb.commands.run('mkdir -p ' + str(Path(target).parent), user='root', timeout=15)
        sb.files.write(target, data, user='root')
    sb.files.write('/opt/qiqc-bootstrap/expected-contract.json', json.dumps(metadata, sort_keys=True), user='root')
    verifier = '''import hashlib,importlib,importlib.metadata,json,pathlib,platform
root=pathlib.Path('/opt/qiqc-bootstrap')
expected=json.loads((root/'expected-contract.json').read_text())
if platform.python_version()!=expected['python']: raise RuntimeError('Runtime Python version mismatch')
for name,version in expected['original_direct_pins'].items():
 if importlib.metadata.version(name)!=version: raise RuntimeError('Runtime package version mismatch')
for item in expected['clients']:
 if hashlib.sha256(pathlib.Path(item['path']).read_bytes()).hexdigest()!=item['sha256']: raise RuntimeError('Runtime client file mismatch')
expected['installed_distributions']={d.metadata['Name']:d.version for d in importlib.metadata.distributions()}
expected['python_origin']=json.loads((root/'python-origin.json').read_text())
pathlib.Path('/opt/runtime-environment.json').write_text(json.dumps(expected,sort_keys=True))
'''
    sb.files.write('/root/qiqc-verify-runtime.py', verifier, user='root')
    sb.commands.run(PYTHON + ' /root/qiqc-verify-runtime.py', user='root', timeout=60)
    if role == 'main':
        imports = ';'.join('import ' + name for name in MAIN_IMPORTS)
        sb.commands.run(PYTHON + ' -c ' + __import__('shlex').quote(imports), user='root', timeout=120)
    return metadata
