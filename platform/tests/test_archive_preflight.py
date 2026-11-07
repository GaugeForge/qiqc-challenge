"""Archive preflight attacks are inert bytes; no sandbox/cloud/fixture execution."""
import gzip
import io
from pathlib import Path
import sys
import tarfile

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts/science'))
from verify_public import validate_archive


def archive(*, fmt=tarfile.PAX_FORMAT, name='safe.txt', pax=None, payload=b'x', mode='w:gz'):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode=mode, format=fmt) as tf:
        item = tarfile.TarInfo(name)
        item.size = len(payload)
        item.pax_headers = pax or {}
        tf.addfile(item, io.BytesIO(payload))
    return buf.getvalue()


@pytest.mark.parametrize('kind', ['pax', 'gnu'])
def test_large_extended_header_rejected_before_tarfile_recursive_parser(kind, monkeypatch):
    if kind == 'pax':
        raw = archive(pax={'comment': 'x' * (70 * 1024)})
    else:
        raw = archive(fmt=tarfile.GNU_FORMAT, name='a' * (70 * 1024))
    original = tarfile.open
    parsed = []
    def observe(*args, **kwargs):
        parsed.append(True)
        return original(*args, **kwargs)
    monkeypatch.setattr(tarfile, 'open', observe)
    with pytest.raises(ValueError, match='metadata'):
        validate_archive(raw)
    assert not parsed, 'expanded metadata reached high-level tarfile parser'


@pytest.mark.parametrize('mode', ['w:', 'w:gz'])
@pytest.mark.parametrize('fmt', [tarfile.USTAR_FORMAT, tarfile.PAX_FORMAT, tarfile.GNU_FORMAT])
def test_normal_archives_remain_supported(mode, fmt):
    name = 'safe.txt' if fmt == tarfile.USTAR_FORMAT else 'folder/' + 'a' * 160
    pax = {'comment': 'bounded ordinary metadata'} if fmt == tarfile.PAX_FORMAT else None
    validate_archive(archive(mode=mode, fmt=fmt, name=name, pax=pax))


def test_whole_expanded_tar_including_zero_padding_is_counted():
    raw = archive(mode='w:')
    assert len(raw) == 10240
    with pytest.raises(ValueError, match='expanded'):
        validate_archive(gzip.compress(raw), expanded_limit=4096)


def test_gzip_concatenated_members_cannot_escape_total_limit():
    raw = archive(mode='w:')
    combined = gzip.compress(raw) + gzip.compress(b'\0' * 8192)
    with pytest.raises(ValueError, match='expanded'):
        validate_archive(combined, expanded_limit=len(raw) + 4096)


def test_nonzero_trailing_member_is_not_silently_ignored():
    raw = archive(mode='w:')
    with pytest.raises(ValueError, match='trailing'):
        validate_archive(gzip.compress(raw) + gzip.compress(archive(mode='w:')))


@pytest.mark.parametrize('pax', [{'size': '999999999999999999'}, {'GNU.sparse.map': '0,999999999999'}, {'GNU.sparse.realsize': '999999999999'}])
def test_metadata_cannot_override_data_offsets(pax):
    with pytest.raises(ValueError, match='metadata'):
        validate_archive(archive(pax=pax))


def test_unknown_compression_rejected():
    with pytest.raises(ValueError, match='format'):
        validate_archive(archive(mode='w:bz2'))


def test_truncated_gzip_rejected_even_after_tar_end():
    raw = archive()
    with pytest.raises(ValueError):
        validate_archive(raw[:-4])


def test_metadata_limit_can_be_scaled_without_large_allocations():
    with pytest.raises(ValueError, match='metadata'):
        validate_archive(archive(pax={'comment': 'x' * 1024}), metadata_limit=512)


def test_existing_path_and_link_guards_survive_preflight():
    with pytest.raises(ValueError, match='unsafe'):
        validate_archive(archive(name='../outside'))
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode='w:gz') as tf:
        item = tarfile.TarInfo('link'); item.type = tarfile.SYMTYPE; item.linkname = '/etc/passwd'
        tf.addfile(item)
    with pytest.raises(ValueError, match='unsafe'):
        validate_archive(buf.getvalue())


def test_directory_data_cannot_hide_uninspected_extended_header():
    header = tarfile.TarInfo('folder')
    header.type = tarfile.DIRTYPE
    header.size = 512
    nested = tarfile.TarInfo('PaxHeader')
    nested.type = tarfile.XHDTYPE
    nested.size = 1024 * 1024
    raw = header.tobuf() + nested.tobuf() + b'\0' * 1024
    with pytest.raises(ValueError, match='offsets'):
        validate_archive(gzip.compress(raw))


def test_metadata_chain_cannot_exhaust_recursive_parser():
    header = tarfile.TarInfo('././@LongLink')
    header.type = tarfile.GNUTYPE_LONGNAME
    header.size = 2
    block = header.tobuf(format=tarfile.GNU_FORMAT) + b'x\0' + b'\0' * 510
    with pytest.raises(ValueError, match='metadata'):
        validate_archive(gzip.compress(block * 33 + b'\0' * 1024))


def test_gnu_sparse_unsupported_even_with_small_declared_size():
    header = tarfile.TarInfo('sparse')
    header.type = tarfile.GNUTYPE_SPARSE
    with pytest.raises(ValueError, match='unsafe'):
        validate_archive(gzip.compress(header.tobuf(format=tarfile.GNU_FORMAT) + b'\0' * 1024))
