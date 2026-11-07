"""M02 adversarial corpus: parser only; no submitted content is executed."""
import io
import json
import random
import stat
import struct
import warnings
import zipfile

import pytest
from qiqc.archive import inspect_archive, safe_name


def package(entries=(), manifest=None, compression=zipfile.ZIP_STORED):
    stream = io.BytesIO()
    with warnings.catch_warnings():
        warnings.simplefilter('ignore', UserWarning)
        with zipfile.ZipFile(stream, 'w', compression) as archive:
            archive.writestr('harness.json', json.dumps(manifest or {'schema':'qiqc-harness/v1-demo','name':'test','entrypoint':'adapter.sh'}))
            archive.writestr('adapter.sh', '#!/bin/sh\nexit 0\n')
            for entry, value in entries:
                archive.writestr(entry, value)
    return stream.getvalue()


@pytest.mark.parametrize('name', ['../x', '/x', 'C:/x', r'a\x', 'a/../b', './x', 'a//b', '.env', 'x\x00y', 'a\u202eb', 'a\nb'])
def test_path_corpus(name):
    with pytest.raises(ValueError):
        safe_name(name)


@pytest.mark.parametrize('entries', [
    [('adapter.sh', 'second')], [('ADAPTER.SH', 'second')],
    [('café', 'one'), ('cafe\u0301', 'two')],
    [('a', 'file'), ('a/b', 'child')], [('a/b', 'child'), ('a', 'file')],
    [('dir/', 'nonempty directory')],
])
def test_duplicates_and_conflicting_paths(entries):
    with pytest.raises(ValueError):
        inspect_archive(package(entries))


@pytest.mark.parametrize('kind', [stat.S_IFLNK, stat.S_IFIFO, stat.S_IFCHR, stat.S_IFBLK, stat.S_IFSOCK, stat.S_IFDIR])
def test_special_file_corpus(kind):
    entry = zipfile.ZipInfo('special')
    entry.external_attr = (kind | 0o644) << 16
    with pytest.raises(ValueError):
        inspect_archive(package([(entry, 'payload')]))


def test_nul_truncation_is_rejected_from_original_header():
    blob = package([('badXname', 'payload')]).replace(b'badXname', b'bad\x00name')
    with pytest.raises(ValueError):
        inspect_archive(blob)


def test_crc_corruption_and_local_header_mismatch():
    blob = package([('unique.txt', 'UNIQUE_PAYLOAD')])
    bad = blob.replace(b'UNIQUE_PAYLOAD', b'CORRUPT_DATA__')
    with pytest.raises(zipfile.BadZipFile):
        inspect_archive(bad)
    bad = blob.replace(b'unique.txt', b'unique.bad', 1)
    with pytest.raises(zipfile.BadZipFile):
        inspect_archive(bad)


def test_compression_bomb_and_file_count_bound():
    with pytest.raises(ValueError):
        inspect_archive(package([('bomb', 'A' * 1000000)], compression=zipfile.ZIP_DEFLATED))
    with pytest.raises(ValueError):
        inspect_archive(package([(f'x{i}', '') for i in range(1000)]))


@pytest.mark.parametrize('manifest', [
    {'schema':'qiqc-harness/v1-demo','name':'test','entrypoint':'ADAPTER.SH'},
    {'schema':'qiqc-harness/v1-demo','name':'test','entrypoint':'missing'},
    {'schema':'qiqc-harness/v1-demo','name':'test','entrypoint':'../adapter.sh'},
])
def test_manifest_must_resolve_exact_file(manifest):
    with pytest.raises(ValueError):
        inspect_archive(package(manifest=manifest))


def test_seeded_header_mutation_corpus_has_no_unhandled_parser_state():
    # Reproducible bounded fuzz corpus; valid mutations may still be accepted.
    rng = random.Random(20261020)
    original = package([('unicode/量子.txt', 'science')])
    for _ in range(256):
        blob = bytearray(original)
        for _ in range(rng.randint(1, 4)):
            blob[rng.randrange(len(blob))] ^= rng.randrange(1, 256)
        try:
            report = inspect_archive(blob)
        except (ValueError, zipfile.BadZipFile, UnicodeError, NotImplementedError, RuntimeError, EOFError, struct.error):
            continue
        assert report['ruleset_version'] == 'qiqc-archive/2'
        assert report['files'] <= 1000
        assert report['expanded_bytes'] <= 100 * 1024 * 1024


def test_valid_unicode_regular_files_and_ruleset():
    report = inspect_archive(package([('量子/结果.txt', 'science')]))
    assert report['ruleset_version'] == 'qiqc-archive/2'
    assert report['files'] == 3


@pytest.mark.parametrize('kind', [0x000d, 0x5855, 0x756e, 0x7075])
def test_alternate_link_or_path_metadata_rejected(kind):
    entry=zipfile.ZipInfo('looks-normal')
    payload=b'../../secret'
    entry.extra=struct.pack('<HH',kind,len(payload))+payload
    with pytest.raises(ValueError):
        inspect_archive(package([(entry,'content')]))
