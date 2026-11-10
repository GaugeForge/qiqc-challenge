"""Bounded archive validation shared by trusted rollout readers; no cloud imports."""
import io
import tarfile
from pathlib import Path


MAX_ARCHIVE_BYTES = 96 * 1024 * 1024
MAX_EXPANDED_BYTES = 192 * 1024 * 1024
MAX_ENTRIES = 20_000


def validate_archive(data, *, expanded_limit=MAX_EXPANDED_BYTES, metadata_limit=64 * 1024):
    """Bound the *entire* tar stream before tarfile interprets extended headers.

    These are diagnostic transfer guards, not the formal competition policy.
    PAX/GNU metadata is processed by tarfile before it yields normal members;
    counting only yielded member.size therefore misses a compression bomb.
    This preflight counts headers, padding, trailing bytes and concatenated gzip
    members, and refuses oversized metadata before allocating its payload.
    """
    import gzip
    import zlib

    if (type(expanded_limit) is not int or expanded_limit < 1024
            or type(metadata_limit) is not int or metadata_limit < 1):
        raise ValueError("invalid evidence preflight limits")
    if not isinstance(data, bytes) or len(data) > MAX_ARCHIVE_BYTES:
        raise ValueError("evidence archive too large")
    if not data.startswith(b"\x1f\x8b") and (len(data) < 1024 or len(data) % 512):
        raise ValueError("unsupported evidence archive format")
    source = io.BytesIO(data)
    stream = gzip.GzipFile(fileobj=source, mode="rb") if data.startswith(b"\x1f\x8b") else source
    consumed = 0
    headers = 0
    extensions = 0
    zero_blocks = 0
    extended = {tarfile.XHDTYPE, tarfile.XGLTYPE, tarfile.GNUTYPE_LONGNAME, tarfile.GNUTYPE_LONGLINK}
    ordinary = {tarfile.REGTYPE, tarfile.AREGTYPE, tarfile.DIRTYPE, tarfile.SYMTYPE, tarfile.LNKTYPE}

    def read_bounded(size):
        nonlocal consumed
        chunk = stream.read(min(size, expanded_limit - consumed + 1))
        consumed += len(chunk)
        if consumed > expanded_limit:
            raise ValueError("expanded evidence too large")
        return chunk

    def metadata_records(payload):
        # Validate framing ourselves; reject size/sparse overrides that could
        # make tarfile seek beyond the physical offsets this preflight checked.
        offset = 0
        while offset < len(payload):
            space = payload.find(b" ", offset, min(offset + 12, len(payload)))
            if space < 0 or not payload[offset:space].isdigit():
                raise ValueError("invalid evidence metadata")
            length = int(payload[offset:space])
            end = offset + length
            if end > len(payload) or end <= space + 1 or payload[end - 1:end] != b"\n":
                raise ValueError("invalid evidence metadata")
            record = payload[space + 1:end - 1]
            key, separator, _value = record.partition(b"=")
            if not separator or not key or len(key) > 256 or b"\0" in record:
                raise ValueError("invalid evidence metadata")
            if key == b"size" or key.startswith(b"GNU.sparse") or key in (b"SCHILY.realsize", b"SCHILY.filetype"):
                raise ValueError("unsupported evidence metadata offsets")
            offset = end

    try:
        with stream:
            while True:
                block = read_bounded(512)
                if not block:
                    if zero_blocks < 2:
                        raise ValueError("incomplete evidence archive")
                    break
                if len(block) != 512:
                    raise ValueError("incomplete evidence archive")
                if block == b"\0" * 512:
                    zero_blocks += 1
                    continue
                if zero_blocks:
                    raise ValueError("nonzero trailing evidence data")
                try:
                    header = tarfile.TarInfo.frombuf(block, encoding="utf-8", errors="surrogateescape")
                except (tarfile.HeaderError, ValueError, OverflowError):
                    raise ValueError("unsupported evidence archive format") from None
                headers += 1
                if headers > MAX_ENTRIES:
                    raise ValueError("too many evidence entries")
                if header.size < 0 or header.type not in ordinary | extended:
                    raise ValueError("unsafe evidence entry")
                if header.type in (tarfile.DIRTYPE, tarfile.SYMTYPE, tarfile.LNKTYPE) and header.size != 0:
                    raise ValueError("unsafe evidence entry offsets")
                if header.type in extended:
                    extensions += 1
                    if header.size > metadata_limit or extensions > 32:
                        raise ValueError("evidence metadata too large")
                else:
                    extensions = 0
                # Reject before reading a large claimed file or padding area.
                padded = (header.size + 511) // 512 * 512
                if padded > expanded_limit - consumed:
                    raise ValueError("expanded evidence too large")
                payload = bytearray() if header.type in extended else None
                remaining = padded
                while remaining:
                    chunk = read_bounded(min(remaining, 64 * 1024))
                    if not chunk:
                        raise ValueError("incomplete evidence archive")
                    if payload is not None:
                        payload.extend(chunk)
                    remaining -= len(chunk)
                if header.type in (tarfile.XHDTYPE, tarfile.XGLTYPE):
                    metadata_records(bytes(payload[:header.size]))
    except (gzip.BadGzipFile, EOFError, OSError, tarfile.TarError, OverflowError, zlib.error):
        raise ValueError("invalid evidence archive format") from None

    # Parsing expanded headers is now bounded; preserve the existing logical
    # path/link/duplicate rules for PAX and GNU long-name substitutions too.
    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz" if data.startswith(b"\x1f\x8b") else "r:") as tf:
            total = 0
            seen = set()
            for index, member in enumerate(tf):
                if index >= MAX_ENTRIES:
                    raise ValueError("too many evidence entries")
                p = Path(member.name)
                if (p.is_absolute() or ".." in p.parts or "\0" in member.name
                        or member.issym() or member.islnk()
                        or not (member.isfile() or member.isdir())):
                    raise ValueError("unsafe evidence entry")
                if member.isfile() and str(p) in seen:
                    raise ValueError("duplicate evidence entry")
                seen.add(str(p))
                total += member.size
                if member.size < 0 or total > expanded_limit:
                    raise ValueError("expanded evidence too large")
    except (tarfile.TarError, EOFError, OSError, OverflowError, RecursionError):
        raise ValueError("invalid evidence archive format") from None
