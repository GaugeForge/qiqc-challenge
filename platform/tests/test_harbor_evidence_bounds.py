"""Untrusted sandbox evidence must be bounded before entering trusted memory."""
import asyncio
from contextlib import contextmanager
import importlib.util
import io
from pathlib import Path
import sys
import tarfile
import tempfile
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.skipif(importlib.util.find_spec('harbor') is None,
                                reason='Harbor interpreter required')
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts/science'))


class RemoteFiles:
    def __init__(self, data, reported_size=None, symlink=False):
        self.data = data
        self.reported_size = len(data) if reported_size is None else reported_size
        self.symlink = symlink
        self.reads = []

    def get_info(self, path, **kwargs):
        return SimpleNamespace(type='file', size=self.reported_size,
                               symlink_target='/etc/private' if self.symlink else None)

    @contextmanager
    def stream(self):
        yield iter([self.data[:8], self.data[8:]])

    def read(self, path, **kwargs):
        self.reads.append(kwargs)
        assert kwargs.get('format') == 'stream', 'unbounded remote read'
        assert kwargs.get('request_timeout') == 5
        assert kwargs.get('user') == 'root'
        return self.stream()


def provider():
    from harbor_e2b import CZEnvironment
    instance = object.__new__(CZEnvironment)
    async def command(*args, **kwargs):
        return SimpleNamespace(return_code=0)
    instance._exec = command
    return instance


@pytest.mark.parametrize('symlink,reported', [(False, 1), (True, 1)])
def test_file_growth_or_symlink_preserves_existing_local_evidence(monkeypatch, symlink, reported):
    import harbor_e2b
    monkeypatch.setattr(harbor_e2b, 'FILE_EVIDENCE_LIMIT', 8, raising=False)
    files = RemoteFiles(b'oversized evidence', reported, symlink)
    with tempfile.TemporaryDirectory(dir='/dev/shm', prefix='qiqc-bounds-') as root:
        target = Path(root) / 'reward.txt'
        target.write_bytes(b'original evidence')
        with pytest.raises(ValueError):
            asyncio.run(provider()._download_file(SimpleNamespace(files=files), '/source', target))
        assert target.read_bytes() == b'original evidence'


def test_file_download_is_bounded_stream_and_atomic():
    files = RemoteFiles(b'valid evidence')
    with tempfile.TemporaryDirectory(dir='/dev/shm', prefix='qiqc-bounds-') as root:
        target = Path(root) / 'evidence.txt'
        asyncio.run(provider()._download_file(SimpleNamespace(files=files), '/source', target))
        assert target.read_bytes() == b'valid evidence'
        assert len(files.reads) == 1


def test_archive_download_stream_limit_before_unpack(monkeypatch):
    import harbor_e2b
    monkeypatch.setattr(harbor_e2b, 'ARCHIVE_EVIDENCE_LIMIT', 8, raising=False)
    files = RemoteFiles(b'larger than compressed limit', reported_size=1)
    with tempfile.TemporaryDirectory(dir='/dev/shm', prefix='qiqc-bounds-') as root:
        target = Path(root) / 'unpacked'
        with pytest.raises(ValueError):
            asyncio.run(provider()._download_dir(SimpleNamespace(files=files), '/logs', target))
        assert not target.exists()


def test_valid_archive_download_and_path_rejection():
    data = io.BytesIO()
    with tarfile.open(fileobj=data, mode='w:gz') as archive:
        info = tarfile.TarInfo('trajectory.txt')
        info.size = 4
        archive.addfile(info, io.BytesIO(b'pass'))
    files = RemoteFiles(data.getvalue())
    with tempfile.TemporaryDirectory(dir='/dev/shm', prefix='qiqc-bounds-') as root:
        target = Path(root) / 'unpacked'
        asyncio.run(provider()._download_dir(SimpleNamespace(files=files), '/logs', target))
        assert (target / 'trajectory.txt').read_bytes() == b'pass'
    with pytest.raises(ValueError, match='tmpfs'):
        asyncio.run(provider()._download_file(SimpleNamespace(files=files), '/logs', '/tmp/forbidden'))


def test_private_artifact_override_only_exact_frozen_modexp_qsim_source():
    from harbor_e2b import CZEnvironment
    from types import SimpleNamespace
    qsim=object()
    env=SimpleNamespace(task_id='modexp_ccz2t_spacetime_ftqc',qsim=qsim)
    source='/app/configs/task_materials/modexp_ccz2t_spacetime_ftqc/hidden/hidden_scorer.yaml'
    assert CZEnvironment.organizer_artifact_source(env,qsim,source) == source[len('/app/'):]
    assert CZEnvironment.organizer_artifact_source(env,object(),source) is None
    assert CZEnvironment.organizer_artifact_source(env,qsim,source+'/../secret') is None
    env.task_id='other_task'
    assert CZEnvironment.organizer_artifact_source(env,qsim,source) is None


def test_private_override_records_real_origin_without_reading_qsim(monkeypatch):
    import json, hashlib, task_release
    from harbor_e2b import CZEnvironment
    source='/app/configs/task_materials/modexp_ccz2t_spacetime_ftqc/hidden/hidden_scorer.yaml'
    relative=source[len('/app/'):]
    monkeypatch.setattr(task_release,'verify_release',lambda *args:{'fixture':True})
    monkeypatch.setattr(task_release,'verified_bytes',lambda runtime,path,release:b'private fixture' if path==relative else pytest.fail('wrong input'))
    monkeypatch.setattr(task_release,'identity',lambda *args:{'release_id':'fixture-release'})
    qsim=SimpleNamespace(files=None)
    with tempfile.TemporaryDirectory(dir='/dev/shm',prefix='qiqc-origin-') as directory:
        root=Path(directory)
        env=SimpleNamespace(task_id='modexp_ccz2t_spacetime_ftqc',qsim=qsim,trial_paths=SimpleNamespace(trial_dir=root))
        env.organizer_artifact_source=lambda sb,path:CZEnvironment.organizer_artifact_source(env,sb,path)
        target=root/'artifacts/scorer.yaml'
        asyncio.run(CZEnvironment._download_file(env,qsim,source,target))
        assert target.read_bytes()==b'private fixture'
        provenance=json.loads((root/'artifacts/organizer-input-provenance.json').read_text())
        assert provenance[0]['actual_source_kind']=='organizer_frozen_verifier_input'
        assert provenance[0]['declared_service']=='qsim'
        assert provenance[0]['sha256']==hashlib.sha256(b'private fixture').hexdigest()
