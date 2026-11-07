import importlib.util
import json
from pathlib import Path
import sqlite3
import time
import urllib.error

PATH = Path(__file__).parents[1] / 'scripts/native_free_budget_status.py'
spec = importlib.util.spec_from_file_location('budget_status', PATH)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def test_local_aggregate_does_not_query_provider(tmp_path, monkeypatch):
    db = tmp_path / 'copy.db'
    with sqlite3.connect(db) as c:
        c.execute('create table provider_pacing_admissions(quota_group text,started_ns integer)')
        c.executemany('insert into provider_pacing_admissions values(?,?)', [('shared', time.time_ns())] * 3 + [('other', time.time_ns())])
    monkeypatch.setattr(module, 'provider_summary', lambda _p: (_ for _ in ()).throw(AssertionError('no external call')))
    result = module.status({'quota_group': 'shared', 'requests_per_minute': 18, 'requests_per_day': 900}, db)
    assert result['local_daily_remaining'] == 897
    assert result['provider']['state'] == 'not_queried'


def test_provider_response_is_allowlisted_and_redirect_disabled(tmp_path, monkeypatch):
    key = tmp_path / 'key'
    key.write_text('synthetic-fixture-secret')
    key.chmod(0o600)
    class Response:
        def __enter__(self): return self
        def __exit__(self, *_args): pass
        def read(self, _n):
            return json.dumps({'data': {'free_model_daily_requests': {'used': 19, 'limit': 1000, 'remaining': 981}, 'secret_echo': 'never-print-this', 'usage': 123}}).encode()
    class Opener:
        def open(self, request, timeout):
            assert request.full_url == 'https://openrouter.ai/api/v1/key'
            assert timeout == 15
            return Response()
    def build(*handlers):
        assert any(isinstance(h, module.NoRedirect) for h in handlers)
        return Opener()
    monkeypatch.setattr(module.urllib.request, 'build_opener', build)
    result = module.provider_summary({'base_url': 'https://openrouter.ai/api/v1', 'key_file': str(key)})
    assert result['free_model_daily_requests']['remaining'] == 981
    assert 'never-print-this' not in json.dumps(result)
    assert 'synthetic-fixture-secret' not in json.dumps(result)
    assert module.NoRedirect().redirect_request(None) is None


def test_world_readable_key_and_symlink_refused(tmp_path):
    p = tmp_path / 'key'
    p.write_text('fixture')
    p.chmod(0o644)
    import pytest
    with pytest.raises(ValueError): module.private_bytes(p, 100)
    p.chmod(0o600)
    link = tmp_path / 'link'
    link.symlink_to(p)
    with pytest.raises(ValueError): module.private_bytes(link, 100)


def test_watcher_uses_latest_attempt_and_preserves_existing_pause(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(PATH.parent))
    import native_free_budget_watch as watch
    db = tmp_path / 'copy.db'
    with sqlite3.connect(db) as c:
        c.execute('create table runs(state text,error text)')
    lane = tmp_path / 'lane-1'
    lane.mkdir()
    suite = lane / 'suite.json'
    suite.write_text(json.dumps({'status': 'running', 'attempts': [
        {'catalog_index': 1, 'attempt': 1, 'state': 'browser_failed'},
        {'catalog_index': 1, 'attempt': 2, 'state': 'passed'}]}))
    current = watch.workload(tmp_path, db)
    assert current['passed_count'] == 1 and current['failures'] == []
    suite.write_text(json.dumps({'status': 'failed_requires_diagnosis', 'attempts': [
        {'catalog_index': 1, 'attempt': 1, 'state': 'passed'},
        {'catalog_index': 1, 'attempt': 2, 'state': 'browser_failed'}]}))
    current = watch.workload(tmp_path, db)
    assert current['passed_count'] == 0
    assert current['failures'] == [{'catalog_index': 1, 'attempt': 2, 'state': 'browser_failed'}]
    pause = tmp_path / 'pause.json'
    assert watch.pause_once(pause, {'reason': 'original_failure'}) is True
    assert watch.pause_once(pause, {'reason': 'budget_low'}) is False
    assert json.loads(pause.read_text()) == {'reason': 'original_failure'}
    assert list(tmp_path.glob('.budget-pause.*')) == []
