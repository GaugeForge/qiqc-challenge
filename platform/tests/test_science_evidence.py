"""Controller evidence handling must fail closed before verifier extraction."""

import io
import sys
import tarfile
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts/science"
sys.path.insert(0, str(SCRIPTS))
from verify_public import validate_archive


def archive(name="experiment_log.jsonl", kind=tarfile.REGTYPE):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as out:
        member = tarfile.TarInfo(name)
        member.type = kind
        if kind == tarfile.REGTYPE:
            member.size = 3
            out.addfile(member, io.BytesIO(b"{}\n"))
        else:
            member.linkname = "/etc/passwd"
            out.addfile(member)
    return buf.getvalue()


def test_safe_evidence():
    validate_archive(archive())


@pytest.mark.parametrize("name", ["/etc/passwd", "../escape", "a/../../escape"])
def test_path_escape_rejected(name):
    with pytest.raises(ValueError, match="unsafe"):
        validate_archive(archive(name))


@pytest.mark.parametrize(
    "kind", [tarfile.SYMTYPE, tarfile.LNKTYPE, tarfile.FIFOTYPE, tarfile.CHRTYPE]
)
def test_special_entry_rejected(kind):
    with pytest.raises(ValueError, match="unsafe"):
        validate_archive(archive(kind=kind))
