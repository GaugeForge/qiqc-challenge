import importlib.util
from pathlib import Path

spec = importlib.util.spec_from_file_location('browser_upload', Path(__file__).parents[1] / 'scripts/browser_upload.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def test_busy_admission_is_exact_not_any_provider_or_server_error():
    assert module.unaccepted_upload_busy(429, {'error': '上传检查繁忙，请稍后重试；未扣次数'})
    assert module.unaccepted_upload_busy(503, {'error': '隔离扫描服务暂不可用；未扣次数'})
    for status, payload in [(503, {'error': '隔离扫描报告无效'}), (500, {'error': 'internal'}),
                            (429, {'error': 'quota exhausted'}), (200, {'error': '上传检查繁忙，请稍后重试；未扣次数'}),
                            (503, None), (503, {'error': {}})]:
        assert not module.unaccepted_upload_busy(status, payload)


import contextlib
from types import SimpleNamespace
import pytest


def harness(statuses, *, mutated_versions=False, mutated_quota=False):
    class Reply:
        def __init__(self, status):
            self.status = status
        def json(self):
            return {'error': '隔离扫描服务暂不可用；未扣次数'} if self.status == 503 else {}

    class Page:
        clicks = 0
        def __init__(self):
            self.waits = []
        @contextlib.contextmanager
        def expect_response(self, predicate, **kwargs):
            value = next(sequence)
            if isinstance(value, Exception):
                raise value
            yield SimpleNamespace(value=Reply(value))
        def locator(self, selector):
            return self
        def click(self):
            self.clicks += 1
        def wait_for_timeout(self, value):
            self.waits.append(value)

    class Request:
        def get(self, url):
            if url.endswith('/api/versions'):
                value = ['unexpected-version'] if mutated_versions and page.clicks else []
            else:
                value = {'quotas': {'infra': 9 if mutated_quota and page.clicks else 10}}
            return SimpleNamespace(json=lambda: value)

    class Assertion:
        def to_be_visible(self):
            pass
        def to_have_text(self, text):
            assert text == '隔离扫描服务暂不可用；未扣次数'
        def to_be_enabled(self):
            pass

    sequence = iter(statuses)
    page = Page()
    return page, SimpleNamespace(request=Request()), lambda _: Assertion()


def test_six_attempts_wait_only_45_seconds_and_preserve_all_unaccepted_checks():
    page, team, expect = harness([503] * 5 + [200])
    report = {}
    module.upload_with_admission_retry(page, team, 'http://local', report, expect, max_attempts=6)
    assert page.clicks == 6
    assert page.waits == [3000, 6000, 9000, 12000, 15000]
    assert sum(page.waits) == 45000
    for row in report['upload_admission'][:-1]:
        assert row['explicit_unaccepted_busy'] and row['no_version_created'] and row['quotas_unchanged']
        assert row['error_visible'] and row['upload_control_restored']


def test_default_still_three_and_exhaustion_never_waits_after_final_attempt():
    page, team, expect = harness([503] * 6)
    with pytest.raises(AssertionError, match='bounded retries'):
        module.upload_with_admission_retry(page, team, 'http://local', {}, expect)
    assert page.clicks == 3
    assert page.waits == [3000, 6000]


@pytest.mark.parametrize('kwargs', [{'mutated_versions': True}, {'mutated_quota': True}])
def test_server_side_mutation_stops_before_retry(kwargs):
    page, team, expect = harness([503, 200], **kwargs)
    with pytest.raises(AssertionError):
        module.upload_with_admission_retry(page, team, 'http://local', {}, expect, max_attempts=6)
    assert page.clicks == 1
    assert not page.waits


def test_unknown_response_or_transport_error_never_resubmits():
    for responses, error in [([500, 200], AssertionError), ([TimeoutError('unknown acceptance')], TimeoutError)]:
        page, team, expect = harness(responses)
        with pytest.raises(error):
            module.upload_with_admission_retry(page, team, 'http://local', {}, expect, max_attempts=6)
        assert page.clicks <= 1
        assert not page.waits


@pytest.mark.parametrize('limit', [0, 7, True, 2.5])
def test_invalid_retry_limit_rejected_before_network_or_click(limit):
    with pytest.raises(ValueError):
        module.upload_with_admission_retry(None, None, '', {}, None, max_attempts=limit)
