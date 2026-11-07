"""Retry proof requires the same request and every durable provider attempt."""

import copy
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "competition-platform/platform/scripts/science"))
sys.path.insert(0, str(ROOT / "benchmark-assets/adapters/science-probe"))
from verify_native_candidate_evidence import EvidenceError, verify_model_ledger


def fixture():
    success = {
        "schema": "qiqc-model-response/v2",
        "terminal": True,
        "success": True,
        "category": "completed",
    }
    base = {
        "status": 200,
        "adapter_request_sha256": "a" * 64,
        "forwarded_request_sha256": "a" * 64,
    }
    events = [
        {
            **base,
            "gateway_request_id": "1" * 32,
            "response_contract": {
                **success,
                "success": False,
                "category": "error_event",
            },
        },
        {**base, "gateway_request_id": "2" * 32, "response_contract": success},
    ]
    rows = [
        {"request_id": "1" * 32, "upstream_attempt": 1, "status": 504},
        {"request_id": "2" * 32, "upstream_attempt": 1, "status": 503},
        {"request_id": "2" * 32, "upstream_attempt": 2, "status": 200},
    ]
    for event in events:
        event["request_correlation"] = event["gateway_request_id"]
    for row in rows:
        row["request_correlation"] = row["request_id"]
    return events, rows


def test_native_stream_failure_and_internal_retry_remain_counted():
    events, rows = fixture()
    assert verify_model_ledger(events, rows) == {
        "gateway_request_link_verified": True,
        "gateway_logical_requests": 2,
        "gateway_provider_attempts": 3,
        "gateway_failed_provider_attempts": 2,
        "gateway_cancelled_provider_attempts": 0,
        "gateway_cancelled_before_upstream": 0,
    }


@pytest.mark.parametrize(
    "fault",
    [
        "foreign_success",
        "missing_row",
        "extra_row",
        "duplicate_event",
        "failed_final",
        "attempt_gap",
        "forbidden",
        "no_identity",
    ],
)
def test_missing_or_unrelated_recovery_cannot_pass(fault):
    events, rows = fixture()
    if fault == "foreign_success":
        events[1]["adapter_request_sha256"] = events[1]["forwarded_request_sha256"] = (
            "b" * 64
        )
    elif fault == "missing_row":
        rows.pop(0)
    elif fault == "extra_row":
        rows.append({"request_id": "3" * 32, "upstream_attempt": 1, "status": 200})
    elif fault == "duplicate_event":
        events.append(copy.deepcopy(events[1]))
    elif fault == "failed_final":
        rows[-1]["status"] = 504
    elif fault == "attempt_gap":
        rows[-1]["upstream_attempt"] = 3
    elif fault == "forbidden":
        rows[0]["status"] = 403
    elif fault == "no_identity":
        rows[0]["request_id"] = None
    with pytest.raises(EvidenceError):
        verify_model_ledger(events, rows)


@pytest.mark.parametrize(
    "status,category,fatal",
    [
        (504, "temporary", False),
        (429, "temporary", False),
        (503, "temporary", False),
        (429, "run_admission_rejected", True),
        (400, "invalid_tool", True),
    ],
)
def test_mailbox_delivers_error_then_allows_only_transient_retry(
    monkeypatch, status, category, fatal
):
    import json
    from types import SimpleNamespace

    import native_model_mailbox as mailbox

    writes = {}
    request = json.dumps(
        {"id": "a" * 32, "path": "/v1/responses", "body": {"model": mailbox.MODEL}}
    ).encode()
    monkeypatch.setattr(mailbox, "read_main_bytes", lambda *args, **kwargs: request)
    files = SimpleNamespace(
        write=lambda path, data, **kw: writes.update({path: data}),
        rename=lambda old, new, **kw: writes.update({new: writes.pop(old)}),
    )
    response = {
        "status": status,
        "type": "application/json",
        "body": json.dumps({"error": {"type": category}}).encode(),
        "gateway_request_id": "1" * 32,
    }
    events = []
    run = lambda: mailbox.poll_model_mailbox(
        SimpleNamespace(files=files), {}, events, lambda *args: response, "qr_fixture"
    )
    if fatal:
        with pytest.raises(mailbox.ProbeError):
            run()
    else:
        assert run() is True
    assert json.loads(writes[mailbox.MODEL_RESPONSE_PATH])["status"] == status
    assert events[0]["gateway_request_id"] == "1" * 32


def test_failure_archive_keeps_unrecovered_model_metadata_without_exception_secrets():
    import json
    from types import SimpleNamespace

    from native_worker_bridge import preserve_failure_model_evidence

    events, _ = fixture()
    events = events[:1]
    files = {}

    def write(path, data, **kwargs):
        assert kwargs == {"user": "root", "request_timeout": 5}
        files[path] = data

    sandbox = SimpleNamespace(files=SimpleNamespace(write=write))
    report = preserve_failure_model_evidence(
        sandbox, {"run_id": "run"}, events, ValueError("native_model_unrecovered")
    )
    assert report["recovery"]["unrecovered_failed_responses"] == 1
    assert report["failure_code"] == "native_model_unrecovered"
    assert json.loads(next(iter(files.values())))["events"] == events
    report = preserve_failure_model_evidence(
        sandbox, {"run_id": "run"}, events, RuntimeError("secret provider token")
    )
    assert report["failure_code"] == "native_controller_failure"
    assert "secret" not in next(iter(files.values()))


def test_budget_cancel_before_headers_requires_real_cancelled_ledger():
    from test_model_response_contract import planned_event

    cancelled, expected = planned_event()
    events, rows = fixture()
    events = events[1:]
    rows = rows[1:]
    rows.append(
        {
            "request_id": "6" * 32,
            "request_correlation": cancelled["request_correlation"],
            "upstream_attempt": 1,
            "status": 499,
            "error_class": "client_cancelled",
        }
    )
    events.append(cancelled)
    assert (
        verify_model_ledger(events, rows, expected)[
            "gateway_cancelled_provider_attempts"
        ]
        == 1
    )
    rows[-1]["error_class"] = "provider_timeout"
    with pytest.raises(EvidenceError):
        verify_model_ledger(events, rows, expected)
    with pytest.raises(EvidenceError):
        verify_model_ledger(events, rows[:-1], expected)


def test_native_http_preheader_cancel_closes_actual_request_and_keeps_correlation():
    import asyncio

    import httpx
    from native_model_http import budget_stop_marker, call_model
    from test_model_response_contract import planned_event

    event, expected = planned_event()
    marker = event["planned_cancellation"]
    request = {
        "run_id": expected["run_id"],
        "sha256": expected["submission_sha256"],
        "lease": {"attempt": 1},
    }
    assert budget_stop_marker(marker, request, expected["candidate_identity"])
    assert not budget_stop_marker(
        {**marker, "main_stopped": False}, request, expected["candidate_identity"]
    )
    state = {}

    async def upstream(request):
        state["correlation"] = request.headers["x-qiqc-controller-correlation-id"]
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            state["cancelled"] = True
            raise

    result = asyncio.run(
        call_model(
            "http://gateway",
            "/v1/responses",
            {},
            "qr_test",
            lambda: marker,
            transport=httpx.MockTransport(upstream),
            poll_seconds=0.005,
        )
    )
    assert state["cancelled"] and result["status"] == 499
    assert result["gateway_request_id"] is None
    assert result["request_correlation"] == state["correlation"]
    assert (
        result["request_started_at"]
        <= result["cancellation_observed_at"]
        <= result["request_finished_at"]
    )


def test_native_http_genuine_completed_error_is_not_relabelled_budget_cancel():
    import asyncio

    import httpx
    from native_model_http import call_model

    async def upstream(request):
        return httpx.Response(
            504,
            json={"error": "provider_timeout"},
            headers={"X-QIQC-Request-ID": "1" * 32},
        )

    result = asyncio.run(
        call_model(
            "http://gateway",
            "/v1/responses",
            {},
            "qr_test",
            lambda: {"fake": True},
            transport=httpx.MockTransport(upstream),
            poll_seconds=0.005,
        )
    )
    assert result["status"] == 504 and "planned_cancellation" not in result


def test_early_readiness_requires_actual_qsim_and_no_unrecovered_model_error():
    from native_readiness import readiness

    events, _ = fixture()
    expected = {
        "run_id": "run",
        "attempt": 1,
        "submission_sha256": "c" * 64,
        "candidate_identity": {"acceptance_scope": "bounded-functional"},
    }
    progress = {
        "schema": "qiqc-native-qsim-progress/v1",
        **expected,
        "qsim_container_id": "d" * 64,
        "success_count": 1,
        "event_sha256": "e" * 64,
        "log_prefix_sha256": "f" * 64,
        "log_bytes": 100,
        "observed_at": 1000.0,
    }
    proof = readiness(events, progress, expected)
    assert (
        proof
        and proof["model"]["gateway_request_id"] == events[-1]["gateway_request_id"]
    )
    assert readiness(events, {**progress, "success_count": 0}, expected) is None
    assert readiness(events, {**progress, "run_id": "other"}, expected) is None
    assert readiness(events[:1], progress, expected) is None
    assert readiness(events + [events[0]], progress, expected) is None


def test_short_acceptance_stopped_marker_keeps_reason_and_no_budget_claim():
    from native_model_http import budget_stop_marker
    from test_model_response_contract import planned_event

    event, expected = planned_event()
    marker = {
        **event["planned_cancellation"],
        "agent_stop_reason": "acceptance_early_stop",
        "agent_exit": 137,
        "elapsed_seconds": 13.0,
        "budget_exhausted": False,
        "acceptance_early_stop_verified": True,
        "acceptance_readiness_sha256": "a" * 64,
    }
    request = {
        "run_id": expected["run_id"],
        "sha256": expected["submission_sha256"],
        "lease": {"attempt": 1},
    }
    assert budget_stop_marker(marker, request, expected["candidate_identity"])
    for delta in (
        {"acceptance_early_stop_verified": False},
        {"elapsed_seconds": 300},
        {"main_stopped": False},
        {"budget_exhausted": True},
    ):
        assert not budget_stop_marker(
            {**marker, **delta}, request, expected["candidate_identity"]
        )


def test_queue_cancel_is_not_provider_attempt_and_needs_zero_usage_proof():
    from test_model_response_contract import planned_event
    cancelled, expected = planned_event()
    events, rows = fixture()
    events, rows = events[1:]+[cancelled], rows[1:]
    zero = {'request_id':'6'*32,'request_correlation':cancelled['request_correlation'],
            'upstream_attempt':0,'status':499,'error_class':'client_cancelled_before_upstream',
            'input_tokens':0,'output_tokens':0,'cached_tokens':0}
    report=verify_model_ledger(events,rows+[zero],expected)
    assert report['gateway_cancelled_before_upstream']==1
    assert report['gateway_cancelled_provider_attempts']==0
    assert report['gateway_provider_attempts']==2
    for change in ({'upstream_attempt':1},{'input_tokens':None},{'output_tokens':2},
                   {'status':504},{'cached_tokens':False}):
        with pytest.raises(EvidenceError):verify_model_ledger(events,rows+[{**zero,**change}],expected)


def cancelled_internal_retry_fixture(status=503, *, queued=False):
    from test_model_response_contract import planned_event

    cancelled, expected = planned_event()
    events, rows = fixture()
    events, rows = events[1:] + [cancelled], rows[1:]
    base = {'request_id': '6' * 32, 'request_correlation': cancelled['request_correlation']}
    prefix = {**base, 'upstream_attempt': 1, 'status': status, 'error_class': 'provider_http_error'}
    terminal = {**base, 'upstream_attempt': 2, 'status': 499, 'error_class': 'client_cancelled'}
    if queued:
        terminal.update(upstream_attempt=0, error_class='client_cancelled_before_upstream',
                        input_tokens=0, output_tokens=0, cached_tokens=0)
    return events, rows + [prefix, terminal], expected


@pytest.mark.parametrize('status', [429, 502, 503, 504])
@pytest.mark.parametrize('queued', [False, True])
def test_only_internal_http_retry_can_precede_trusted_cancel(status, queued):
    events, rows, expected = cancelled_internal_retry_fixture(status, queued=queued)
    report = verify_model_ledger(events, rows, expected)
    assert report['gateway_failed_provider_attempts'] == 2  # Existing successful request also retried once.
    assert report['gateway_cancelled_before_upstream'] == int(queued)
    assert report['gateway_cancelled_provider_attempts'] == int(not queued)


@pytest.mark.parametrize('change', [
    {'error_class': 'incomplete_stream'},
    {'error_class': 'provider_transport_error'},
    {'error_class': 'invalid_provider_body'},
    {'error_class': 'client_cancelled'},
    {'error_class': None},
    {'status': 200},
    {'status': 401},
    {'status': 499},
    {'upstream_attempt': 2},
    {'request_id': '7' * 32},
    {'request_correlation': '7' * 32},
])
def test_cancel_never_accepts_arbitrary_or_stream_failure_prefix(change):
    events, rows, expected = cancelled_internal_retry_fixture(502, queued=True)
    rows[-2].update(change)
    with pytest.raises(EvidenceError):
        verify_model_ledger(events, rows, expected)


@pytest.mark.parametrize('change', [
    {'status': 504}, {'error_class': 'provider_timeout'},
    {'upstream_attempt': 2}, {'input_tokens': None}, {'output_tokens': 1},
])
def test_internal_retry_cancel_still_requires_real_zero_attempt_terminal(change):
    events, rows, expected = cancelled_internal_retry_fixture(503, queued=True)
    rows[-1].update(change)
    with pytest.raises(EvidenceError):
        verify_model_ledger(events, rows, expected)


def test_internal_retry_cancel_cannot_recover_other_failed_response():
    events, rows, expected = cancelled_internal_retry_fixture(503, queued=True)
    prior_events, prior_rows = fixture()
    # Keep a real failed response without its same-SHA successful recovery.
    prior_events[0]['adapter_request_sha256'] = prior_events[0]['forwarded_request_sha256'] = 'b' * 64
    with pytest.raises(EvidenceError, match='candidate_model_response_not_verified'):
        verify_model_ledger([prior_events[0]] + events, [prior_rows[0]] + rows, expected)
