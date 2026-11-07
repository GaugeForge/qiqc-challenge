"""Local-only evidence recovery must not inherit mutable science imports."""
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
from types import SimpleNamespace
import pytest

SCRIPTS=Path(__file__).parents[1]/'scripts'
sys.path.insert(0,str(SCRIPTS))
import browser_candidate_runtime as runtime
import native_readback_correction as correction


def candidate(tmp_path):
    project=tmp_path/'project';science=project/'competition-platform/platform/scripts/science'
    science.mkdir(parents=True)
    for name in ('native_candidate.py','native_controller.py'):(science/name).write_text('# deployed fixture\n')
    (science/'verify_native_candidate_evidence.py').write_bytes(b'# checker fixture\n'+correction.OLD)
    manifest=tmp_path/'controller.json'
    manifest.write_text(json.dumps({'host_files':{correction.CHECKER:correction.sha((project/correction.CHECKER).read_bytes())}}))
    config={'candidate_root':str(tmp_path),'project_root':str(project),'controller_manifest':str(manifest),'controller_sha256':'a'*64}
    path=tmp_path/'candidate.json';path.write_text(json.dumps(config))
    return config,path


def test_candidate_science_is_deployed_and_escape_rejected(tmp_path):
    config,path=candidate(tmp_path)
    assert runtime.candidate_science(path)==Path(config['project_root'])/'competition-platform/platform/scripts/science'
    config['project_root']=str(tmp_path.parent/'outside');path.write_text(json.dumps(config))
    with pytest.raises(ValueError,match='project_invalid'):runtime.candidate_science(path)


def test_already_imported_workspace_science_is_not_silently_reused(tmp_path,monkeypatch):
    _,path=candidate(tmp_path)
    monkeypatch.setitem(sys.modules,'native_controller',SimpleNamespace(__file__=str(tmp_path/'workspace/native_controller.py')))
    with pytest.raises(ValueError,match='already_loaded'):runtime.bind_candidate_imports(path)


def test_driver_snapshot_keeps_old_dependency_and_rejects_tamper(tmp_path):
    source=tmp_path/'source';source.mkdir()
    for name in runtime.DRIVER_FILES:(source/name).write_text('# original\n')
    first=runtime.snapshot_driver(source,tmp_path/'reports')
    (source/'browser_credentials.py').write_text('# changed\n')
    second=runtime.snapshot_driver(source,tmp_path/'reports')
    assert first!=second
    assert runtime.verify_driver_snapshot(first)
    first.chmod(0o644);first.write_text('# tampered\n')
    with pytest.raises(ValueError,match='dependency_changed'):runtime.verify_driver_snapshot(first)


def test_corrected_readback_preserves_controller_and_exact_patch(tmp_path):
    config,_=candidate(tmp_path)
    original=(Path(config['project_root'])/correction.CHECKER).read_bytes()
    path=correction.create(config,tmp_path/'snapshot')
    result=correction.verify(config,path)
    assert result['original_controller_sha256']==config['controller_sha256']
    assert result['original_deployed_checker_passed'] is False
    assert result['scientific_tasks_rerun'] is False
    assert (Path(config['project_root'])/correction.CHECKER).read_bytes()==original
    assert result['corrected_checker_sha256']==hashlib.sha256(original.replace(correction.OLD,correction.NEW)).hexdigest()


@pytest.mark.parametrize('change',['checker','dependency','controller','attestation'])
def test_correction_never_accepts_arbitrary_code_or_identity(tmp_path,change):
    config,_=candidate(tmp_path);path=correction.create(config,tmp_path/'snapshot');record=json.loads(path.read_text())
    if change in ('checker','dependency'):
        rel=correction.CHECKER if change=='checker' else 'competition-platform/platform/scripts/science/native_candidate.py'
        target=Path(record['snapshot_root'])/rel;target.chmod(0o644);target.write_text('# unreviewed code\n')
        record['files'][rel]=correction.sha(target.read_bytes())
        if change=='checker':record['corrected_checker_sha256']=record['files'][rel]
    elif change=='controller':record['original_controller_sha256']='b'*64
    else:record['scientific_tasks_rerun']=True
    path.chmod(0o644);path.write_text(json.dumps(record))
    with pytest.raises(ValueError):correction.verify(config,path)
