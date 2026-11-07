"""Bounded replay before Agent delivery; no model/cloud calls or fake recovery."""
import base64
import json
from types import SimpleNamespace

import pytest
import native_model_mailbox as mailbox
from model_response_contract import native_recovery_summary
from test_native_mailbox_reliability import Files


def response(status=200, *, error=None, success=False, partial=False):
    if success:
        body = b'data: {"type":"response.completed","response":{"status":"completed"}}\n\n'
    elif status == 200:
        prefix = b'data: {"type":"response.output_item.added","item":{"type":"function_call","arguments":"PARTIAL_TOOL"}}\n\n' if partial else b''
        body = prefix + b'data: ' + json.dumps({'error': {'type': error or 'upstream_error'}}).encode() + b'\n\n'
    else:
        body = json.dumps({'error': {'type': error or 'upstream_error'}}).encode()
    return {'status': status, 'body': body,
            'type': 'text/event-stream' if status == 200 else 'application/json'}


def execute(replies, *, permission=lambda: True, files=None):
    files = files or Files()
    events, calls = [], []

    def provider(endpoint, body, token):
        assert not files.writes, 'Agent must not receive first failed or partial response'
        calls.append((endpoint, json.dumps(body, sort_keys=True), token))
        value = dict(replies[len(calls) - 1])
        value.update(gateway_request_id=f'{len(calls):032x}', request_correlation=f'{len(calls):032x}')
        return value
    mailbox.poll_model_mailbox(SimpleNamespace(files=files), {}, events, provider, 'scope-token', retry_allowed=permission)
    return files, events, calls


def test_partial_stream_failure_replays_same_body_and_records_both_gateway_attempts():
    good = response(success=True)
    files, events, calls = execute([response(partial=True), good])
    assert len(events) == len(calls) == 2
    assert calls[0] == calls[1]
    assert len({e['adapter_request_sha256'] for e in events}) == 1
    assert all(e['adapter_request_sha256'] == e['forwarded_request_sha256'] for e in events)
    assert [e['mailbox_upstream_attempt'] for e in events] == [1, 2]
    assert events[0]['gateway_request_id'] != events[1]['gateway_request_id']
    assert events[0]['request_correlation'] != events[1]['request_correlation']
    assert len(files.writes) == 1
    assert base64.b64decode(json.loads(files.writes[0][1])['body']) == good['body']
    recovery = native_recovery_summary(events)
    assert recovery['failed_responses'] == recovery['recovered_failed_responses'] == 1
    assert recovery['all_failures_recovered']


def test_two_failures_stop_after_one_replay_and_remain_unrecovered():
    files, events, calls = execute([response(), response()])
    assert len(calls) == len(events) == 2
    assert len(files.writes) == 1
    assert not native_recovery_summary(events)['all_failures_recovered']


@pytest.mark.parametrize('status,error', [(400, 'upstream_error'), (401, 'upstream_error'),
    (403, 'upstream_error'), (429, 'run_admission_rejected'), (429, 'daily_quota_exhausted'),
    (503, 'auth_error'), (503, 'run_not_authorized'), (200, 'invalid_api_key')])
def test_auth_policy_quota_or_unknown_error_never_replayed(status, error):
    calls = []
    def provider(*args):
        calls.append(args)
        return response(status, error=error)
    try:
        mailbox.poll_model_mailbox(SimpleNamespace(files=Files()), {}, [], provider, 'token', retry_allowed=lambda: True)
    except mailbox.ProbeError:
        pass
    assert len(calls) == 1


@pytest.mark.parametrize('status', [502, 503, 504])
def test_explicit_transient_http_error_retries_once(status):
    _, events, calls = execute([response(status), response(success=True)])
    assert len(calls) == 2
    assert native_recovery_summary(events)['all_failures_recovered']


def test_trusted_budget_stop_or_controller_cancel_prevents_retry():
    files, events, calls = execute([response()], permission=lambda: False)
    assert len(calls) == len(events) == len(files.writes) == 1
    assert not native_recovery_summary(events)['all_failures_recovered']


def test_budget_cancel_response_never_replayed_or_recovers_failed_attempt():
    cancel = {'status': 499, 'type': 'application/json', 'body': b'{}',
              'planned_cancellation': {'agent_stop_reason': 'budget_exhausted'}}
    _, events, calls = execute([response(), cancel])
    assert len(calls) == 2
    assert events[1]['status'] == 499
    assert not native_recovery_summary(events)['all_failures_recovered']
    _, events, calls = execute([cancel])
    assert len(calls) == 1


def test_length_incomplete_is_not_a_transient_stream_failure():
    incomplete = {'status': 200, 'type': 'text/event-stream', 'body':
        b'data: {"type":"response.incomplete","response":{"status":"incomplete"}}\n\n'}
    _, events, calls = execute([incomplete])
    assert len(calls) == 1
    assert events[0]['response_contract']['category'] == 'incomplete_response'


def test_mailbox_write_retry_does_not_add_provider_attempt(monkeypatch):
    files = Files(rename_failures=[TimeoutError(), TimeoutError()])
    actual = mailbox.deliver_response
    monkeypatch.setattr(mailbox, 'deliver_response', lambda fs, data: actual(fs, data, delay=lambda _: None))
    _, events, calls = execute([response(), response(success=True)], files=files)
    assert len(calls) == len(events) == 2
    assert len(files.writes) == 3
    assert len({raw for _, raw in files.writes}) == 1


def test_exhausted_partial_failure_is_never_delivered_as_actionable_stream():
    files, events, _ = execute([response(partial=True), response(partial=True)])
    delivered = json.loads(files.writes[-1][1])
    assert delivered['status'] == 502
    assert delivered['type'] == 'application/json'
    assert b'PARTIAL_TOOL' not in base64.b64decode(delivered['body'])
    assert [event['status'] for event in events] == [200, 200]  # Keep real upstream evidence.
    assert events[-1]['agent_delivery_sanitized'] is True
    assert not native_recovery_summary(events)['all_failures_recovered']


def test_failed_second_call_keeps_first_event_and_no_partial_delivery():
    files, events, calls = Files(), [], []
    def provider(*args):
        calls.append(args)
        if len(calls) == 2:
            raise TimeoutError('transport interrupted')
        return response(partial=True)
    with pytest.raises(TimeoutError):
        mailbox.poll_model_mailbox(SimpleNamespace(files=files), {}, events, provider, 'token', retry_allowed=lambda: True)
    assert len(calls) == 2
    assert len(events) == 1
    assert not files.writes
