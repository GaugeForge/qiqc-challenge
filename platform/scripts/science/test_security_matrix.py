"""Offline classification tests; no payload execution and no cloud calls."""

import ast
from pathlib import Path

import pytest
from security_matrix import classify, reachable


@pytest.mark.parametrize("status", [200, 403, 404, 500])
def test_any_http_status_means_network_route_reachable(status):
    assert reachable({"status": status})
    assert (
        classify({"status": 200}, {"status": status}, "deny")
        == "failed_reachable_after_restriction"
    )


def test_unavailable_positive_control_is_not_a_pass():
    assert (
        classify(
            {"error_class": "TimeoutError"}, {"error_class": "TimeoutError"}, "deny"
        )
        == "blocked_observed_positive_control_unavailable"
    )


def test_denied_after_reachable_control_passes():
    assert (
        classify({"status": 204}, {"error_class": "URLError"}, "deny")
        == "passed_with_positive_control"
    )
    assert (
        classify(
            {"application_response": True}, {"error_class": "TimeoutError"}, "deny"
        )
        == "passed_with_positive_control"
    )


def test_dns_resolver_error_does_not_prove_no_authoritative_request():
    assert (
        classify({"resolved": True}, {"resolver_code": -2}, "dns_unknown")
        == "inconclusive_authoritative_dns_sink_not_tested"
    )


@pytest.mark.parametrize("expected", [200, 403, 404])
def test_qsim_exact_behavior(expected):
    assert classify({}, {"status": expected}, f"http_{expected}") == "passed"
    assert classify({}, {"status": 503}, f"http_{expected}") == "failed"


def test_payload_guards_remain_and_compiles_without_execution():
    root = Path(__file__).parent
    for filename in ("security_probe_payload.py", "security_sink_payload.py"):
        source = (root / filename).read_text()
        ast.parse(source)
        assert "/tmp/qiqc-security-e2b-only" in source
        assert "/root/QQ-Challenge" in source
    assert "exec(" not in (root / "security_matrix_e2b.py").read_text()


def test_tcp_acceptance_alone_is_not_destination_reachability():
    assert not reachable({"tcp_accepted": True})
    assert (
        classify({"application_response": True}, {"tcp_accepted": True}, "deny")
        == "passed_with_positive_control"
    )


def test_probe_never_records_url_or_exception_secret(monkeypatch):
    from urllib.error import URLError

    import security_probe_payload as payload

    class FailingOpener:
        def open(self, *_args, **_kwargs):
            raise URLError("Bearer secret-fixture-value")

    monkeypatch.setattr(payload, "build_opener", lambda *_args: FailingOpener())
    result = payload.network_probe(
        {"name": "fixture", "url": "https://fixture.invalid/secret-fixture-value"}
    )
    assert result["error_class"] == "URLError"
    assert "secret-fixture-value" not in str(result)
    assert "url" not in result


def test_application_http_rejection_is_preserved_without_reading_body(monkeypatch):
    from urllib.error import HTTPError

    import security_probe_payload as payload

    class FailingOpener:
        def open(self, *_args, **_kwargs):
            raise HTTPError(
                "https://fixture.invalid", 403, "hidden response text", {}, None
            )

    monkeypatch.setattr(payload, "build_opener", lambda *_args: FailingOpener())
    result = payload.network_probe(
        {"name": "fixture", "url": "https://fixture.invalid"}
    )
    assert result["status"] == 403
    assert "hidden response text" not in str(result)
    assert not any("body" in key for key in result)
