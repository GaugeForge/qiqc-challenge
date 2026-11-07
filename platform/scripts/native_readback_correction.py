"""One narrowly defined offline checker correction, without changing execution."""
from __future__ import annotations
import hashlib
import json
from pathlib import Path

CHECKER = 'competition-platform/platform/scripts/science/verify_native_candidate_evidence.py'
DIRECTORIES = ('competition-platform/platform/scripts/science', 'benchmark-assets/adapters/science-probe', 'competition-platform/platform/qiqc')
CORRECTION = 'forward-configured-agent-budget-to-receipt-validator/v1'
OLD = b'        receipt, {"run_id": index["run_id"], "sha256": index["submission_sha256"]}\n'
NEW = b'        receipt, {"run_id": index["run_id"], "sha256": index["submission_sha256"],\n                  "agent_budget_seconds": config.get("agent_budget_seconds", 300)}\n'


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def read(path):
    path = Path(path)
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 2*1024*1024:
        raise ValueError('correction_requires_bounded_regular_file')
    return path.read_bytes()


def corrected_bytes(original):
    if original.count(OLD) != 1:
        raise ValueError('correction_original_call_not_unique')
    return original.replace(OLD, NEW, 1)


def source_files(root):
    result = {}
    for directory in DIRECTORIES:
        for path in sorted((Path(root)/directory).glob('*.py')):
            result[path.relative_to(root).as_posix()] = read(path)
    if not 1 <= len(result) <= 500 or CHECKER not in result:
        raise ValueError('correction_dependency_inventory_invalid')
    return result


def create(config, destination):
    """Copy only deployed trusted Python; replace exactly the omitted argument."""
    destination = Path(destination)
    files = source_files(Path(config['project_root']))
    original = files[CHECKER]
    controller = json.loads(read(config['controller_manifest']))
    if sha(original) != controller['host_files'][CHECKER]:
        raise ValueError('correction_original_checker_not_deployed')
    files[CHECKER] = corrected_bytes(original)
    root = destination / sha(files[CHECKER]) / 'project'
    root.mkdir(parents=True, exist_ok=False)
    for relative, raw in files.items():
        path = root/relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
        path.chmod(0o444)
    record = {'schema':'qiqc-readback-correction/v1','correction':CORRECTION,
              'original_controller_sha256':config['controller_sha256'],
              'original_checker_sha256':sha(original),'corrected_checker_sha256':sha(files[CHECKER]),
              'snapshot_root':str(root.resolve()), 'files':{name:sha(raw) for name,raw in files.items()},
              'execution_controller_unchanged':True,'scientific_tasks_rerun':False}
    path = root.parent/'readback-correction.json'
    path.write_text(json.dumps(record,indent=2)+'\n');path.chmod(0o444)
    verify(config,path)
    return path


def verify(config, path):
    """Never allow an arbitrary new checker digest to replace the pinned one."""
    raw = read(path);record = json.loads(raw)
    if (record.get('schema')!='qiqc-readback-correction/v1' or record.get('correction')!=CORRECTION
            or record.get('original_controller_sha256')!=config['controller_sha256']
            or record.get('execution_controller_unchanged') is not True
            or record.get('scientific_tasks_rerun') is not False):
        raise ValueError('readback_correction_identity_invalid')
    original = source_files(Path(config['project_root']))
    controller = json.loads(read(config['controller_manifest']))
    if record.get('original_checker_sha256') != controller['host_files'][CHECKER] or sha(original[CHECKER]) != record['original_checker_sha256']:
        raise ValueError('readback_correction_original_not_pinned')
    expected = dict(original);expected[CHECKER] = corrected_bytes(original[CHECKER])
    hashes = {name:sha(data) for name,data in expected.items()}
    if record.get('corrected_checker_sha256')!=hashes[CHECKER] or record.get('files')!=hashes:
        raise ValueError('readback_correction_changes_more_than_budget_argument')
    root = Path(record['snapshot_root'])
    if root.is_symlink() or not root.is_absolute() or root.parent.name!=hashes[CHECKER]:
        raise ValueError('readback_correction_snapshot_identity_invalid')
    actual = source_files(root)
    if set(actual)!=set(expected) or any(actual[name]!=expected[name] for name in expected):
        raise ValueError('readback_correction_snapshot_changed')
    return {'attestation_sha256':sha(raw),'correction':CORRECTION,
            'original_checker_sha256':record['original_checker_sha256'],
            'corrected_checker_sha256':record['corrected_checker_sha256'],
            'original_controller_sha256':record['original_controller_sha256'],
            'snapshot_root':str(root),'original_deployed_checker_passed':False,
            'execution_controller_unchanged':True,'scientific_tasks_rerun':False}
