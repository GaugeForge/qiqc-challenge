"""Frozen admissions preserve candidate history and reject changed release evidence."""
import hashlib
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts/science"))
from native_candidate import active_release, admission_release_id, identity
from test_native_release import fixture, freeze


@pytest.fixture
def frozen_candidate_fixture(tmp_path):
    return fixture.__wrapped__(tmp_path)


def test_pointer_changes_new_identity_without_rewriting_candidate_history(frozen_candidate_fixture):
    cfg,*_=frozen_candidate_fixture
    old=identity(cfg,48)
    path=freeze(frozen_candidate_fixture)
    release=json.loads(path.read_text())
    pointer=Path(cfg['candidate_root'])/'active-release.json'
    pointer.write_text(json.dumps({'release_id':release['release_id'],'release_path':str(path),'release_sha256':hashlib.sha256(path.read_bytes()).hexdigest()}))
    assert identity(cfg,48)==old
    current=identity(cfg,48,admission_release_id(cfg))
    assert current['frozen'] is True and current['release_id']==release['release_id']
    with pytest.raises(ValueError,match='unregistered'):
        identity(cfg,48,'native-release-'+'0'*64)
    # Same release file with a changed attachment cannot be used for execution.
    evidence=path.parent/'evidence/00-r2.json';evidence.chmod(0o600);evidence.write_text('{}')
    with pytest.raises(ValueError,match='evidence_changed'):
        active_release(cfg)


def test_prepare_preserves_virtualenv_interpreter_symlink(tmp_path,monkeypatch):
    import native_candidate
    root=tmp_path/'candidate';audit=tmp_path/'audit.json'
    audit.write_text(json.dumps({'source_files_sha256':'a'*64,'source_commit':'b'*40}))
    interpreter=tmp_path/'venv/bin/python';interpreter.parent.mkdir(parents=True);interpreter.symlink_to(sys.executable)
    output=root/'candidate.json'
    monkeypatch.setattr(native_candidate,'verify',lambda *_:None)
    monkeypatch.setattr(native_candidate,'load_candidate',lambda *_a,**_k:None)
    args=['prepare','--source-root',str(tmp_path/'source'),'--source-audit',str(audit),'--candidate-root',str(root),'--project-root',str(tmp_path),'--python',str(interpreter),'--template-id','fixture','--output',str(output)]
    for name in ('adapter-manifest','adapters','provider-env','r2-env','review-key','scanner-socket'):
        args.extend(['--'+name,str(tmp_path/name)])
    monkeypatch.setattr(sys,'argv',['native_candidate.py',*args])
    native_candidate.main()
    assert json.loads(output.read_text())['python']==str(interpreter)
