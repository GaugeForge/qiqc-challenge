"""Finite transfer diagnostics; no cloud access or private file contents."""
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "benchmark-assets/adapters/science-probe"))
from bounded_files import RemoteFileError, read_remote_bytes


class FixtureStream:
    def __init__(self, chunks=(b"abc",), read_error=None, close_error=None):
        self.chunks, self.read_error, self.close_error = chunks, read_error, close_error
        self.closed = False

    def __enter__(self):
        return self

    def __iter__(self):
        yield from self.chunks
        if self.read_error:
            raise self.read_error

    def __exit__(self, *_):
        self.closed = True
        if self.close_error:
            raise self.close_error


class FixtureFiles:
    def __init__(self, stream=None, stat_error=None, open_error=None):
        self.stream = stream or FixtureStream()
        self.stat_error, self.open_error = stat_error, open_error
        self.calls = []

    def get_info(self, path, **kwargs):
        self.calls.append(("stat", kwargs))
        if self.stat_error:
            raise self.stat_error
        return SimpleNamespace(type="file", size=3, symlink_target=None)

    def read(self, path, **kwargs):
        self.calls.append(("open", kwargs))
        if self.open_error:
            raise self.open_error
        return self.stream


@pytest.mark.parametrize("operation", ["stat", "open", "read", "close"])
def test_failures_keep_operation_and_only_finite_safe_fields(operation):
    ReadTimeout = type("ReadTimeout", (Exception,), {})
    error = ReadTimeout("https://private.invalid/path?key=private-token response-private")
    stream = FixtureStream(read_error=error if operation == "read" else None,
                           close_error=error if operation == "close" else None)
    files = FixtureFiles(stream, stat_error=error if operation == "stat" else None,
                         open_error=error if operation == "open" else None)
    with pytest.raises(RemoteFileError) as caught:
        read_remote_bytes(files, "/private/path", 100, request_timeout=7, clock=lambda: 100)
    error = caught.value
    assert str(error) == "Remote file transfer failed"
    assert error.diagnostic == {"operation": operation, "underlying_class": "ReadTimeout",
        "bytes_received": 3 if operation in ("read", "close") else 0,
        "elapsed_ms": 0, "request_timeout_seconds": 7}
    assert "private" not in json.dumps(error.diagnostic) + repr(error)
    if operation in ("read", "close"):
        assert stream.closed


def test_unknown_exception_type_never_enters_diagnostic_and_access_error_is_distinct():
    PrivateType = type("private_exception_with_user_content", (Exception,), {})
    for error, code, message in [(PrivateType("private detail"), "OtherError", "Remote file transfer failed"),
                                  (PermissionError("private path"), "PermissionError", "Remote file access denied")]:
        with pytest.raises(RemoteFileError) as caught:
            read_remote_bytes(FixtureFiles(stat_error=error), "/private", 10)
        assert str(caught.value) == message
        assert caught.value.diagnostic["underlying_class"] == code
        assert "private" not in json.dumps(caught.value.diagnostic)


def test_read_failure_survives_secondary_close_failure():
    ReadTimeout = type("ReadTimeout", (Exception,), {})
    stream = FixtureStream(read_error=ReadTimeout("private read"), close_error=RuntimeError("private close"))
    with pytest.raises(RemoteFileError) as caught:
        read_remote_bytes(FixtureFiles(stream), "/private", 10)
    assert caught.value.diagnostic["operation"] == "read"
    assert caught.value.diagnostic["underlying_class"] == "ReadTimeout"
    assert caught.value.diagnostic["close_error_class"] == "RuntimeError"
    assert caught.value.diagnostic["bytes_received"] == 3


def test_stat_time_is_subtracted_from_stream_deadline_without_resetting_total_budget():
    now = [0.0]
    calls = []
    class Stream(FixtureStream):
        def __iter__(self):
            for moment in (5, 9, 11):
                now[0] = moment
                yield b"x"
    stream = Stream()
    class Files:
        def get_info(self, path, **kwargs):
            calls.append(kwargs)
            now[0] = 4
            return SimpleNamespace(type="file", size=3, symlink_target=None)
        def read(self, path, **kwargs):
            calls.append(kwargs)
            return stream
    with pytest.raises(RemoteFileError, match="total deadline exceeded") as caught:
        read_remote_bytes(Files(), "/private", 10, request_timeout=10, deadline=10, clock=lambda: now[0])
    assert calls[0]["request_timeout"] == 10
    assert calls[1]["request_timeout"] == 6
    assert caught.value.diagnostic == {"operation": "read", "underlying_class": "TotalDeadlineExceeded",
        "bytes_received": 3, "elapsed_ms": 11000, "request_timeout_seconds": 6}
    assert stream.closed


def test_deadline_exhausted_by_stat_prevents_open():
    now = [0.0]
    files = FixtureFiles()
    original = files.get_info
    def stat(*args, **kwargs):
        result = original(*args, **kwargs)
        now[0] = 5
        return result
    files.get_info = stat
    with pytest.raises(RemoteFileError, match="total deadline exceeded") as caught:
        read_remote_bytes(files, "/private", 10, request_timeout=10, deadline=5, clock=lambda: now[0])
    assert len(files.calls) == 1
    assert files.calls[0][1]["request_timeout"] == 5
    assert caught.value.diagnostic["operation"] == "stat"
    assert caught.value.diagnostic["elapsed_ms"] == 5000


def test_sdk_timeout_after_global_deadline_keeps_underlying_class_but_is_not_retryable_transfer():
    now = [0.0]
    ReadTimeout = type("ReadTimeout", (Exception,), {})
    class Stream(FixtureStream):
        def __iter__(self):
            yield b"abc"
            now[0] = 6
            raise ReadTimeout("private SDK detail")
    stream = Stream()
    with pytest.raises(RemoteFileError, match="total deadline exceeded") as caught:
        read_remote_bytes(FixtureFiles(stream), "/private", 10, request_timeout=10,
                          deadline=5, clock=lambda: now[0])
    assert caught.value.diagnostic["underlying_class"] == "ReadTimeout"
    assert caught.value.diagnostic["operation"] == "read"
    assert caught.value.diagnostic["elapsed_ms"] == 6000
    assert stream.closed


def test_byte_bound_diagnostic_counts_received_not_only_retained_bytes():
    stream = FixtureStream(chunks=(b"abc", b"def"))
    with pytest.raises(RemoteFileError, match="byte limit") as caught:
        read_remote_bytes(FixtureFiles(stream), "/private", 4)
    assert caught.value.diagnostic["operation"] == "read"
    assert caught.value.diagnostic["underlying_class"] == "ByteLimitExceeded"
    assert caught.value.diagnostic["bytes_received"] == 6
    assert stream.closed


@pytest.mark.parametrize("value", [0, -1, float("inf"), float("nan"), "private-token", True])
def test_invalid_timeout_never_enters_remote_call_or_diagnostic(value):
    files = FixtureFiles()
    with pytest.raises(ValueError, match="positive remote request timeout"):
        read_remote_bytes(files, "/private", 10, request_timeout=value)
    assert files.calls == []
