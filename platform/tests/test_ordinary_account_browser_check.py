"""Local contracts only; these tests never contact a model or cloud provider."""
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import sqlite3
import zipfile

import pytest

SCRIPT = Path(__file__).parents[1] / 'scripts' / 'ordinary_account_browser_check.py'
spec = importlib.util.spec_from_file_location('ordinary_browser', SCRIPT)
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


def _browser_available():
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as playwright:
            return Path(playwright.chromium.executable_path).is_file()
    except Exception:
        return False


BROWSER_AVAILABLE = _browser_available()


def test_unique_submission_preserves_entrypoint_and_code():
    source = io.BytesIO()
    with zipfile.ZipFile(source, 'w') as z:
        z.writestr('harness.json', json.dumps({'name': 'template', 'entrypoint': 'adapter.py', 'schema': 'qiqc-harness/v1-demo'}))
        z.writestr('adapter.py', 'print("fixture")\n')
    changed = m.fresh_submission(source.getvalue(), 'nonce')
    with zipfile.ZipFile(io.BytesIO(changed)) as z:
        manifest = json.loads(z.read('harness.json'))
        assert manifest['name'] == 'ordinary-browser-nonce'
        assert manifest['entrypoint'] == 'adapter.py'
        assert z.read('adapter.py') == b'print("fixture")\n'
    assert changed != source.getvalue()


def test_public_not_ready_still_allows_infra():
    assert m.check_config({'scientific_task_set': {'configured': True, 'infra_ready': True, 'public_ready': False, 'bounded': True, 'leaderboard_eligible': False, 'agent_budget_seconds': 180}})['public_ready'] is False


@pytest.mark.parametrize('field,value', [('configured', False), ('infra_ready', False), ('bounded', False), ('leaderboard_eligible', True), ('agent_budget_seconds', True), ('agent_budget_seconds', 0)])
def test_configuration_fail_closed(field, value):
    config = dict(configured=True, infra_ready=True, bounded=True, leaderboard_eligible=False, agent_budget_seconds=180)
    config[field] = value
    with pytest.raises(ValueError):
        m.check_config({'scientific_task_set': config})


@pytest.mark.parametrize('after', [{'infra': 8, 'public': 3, 'full': 1}, {'infra': 9, 'public': 2, 'full': 1}, {'infra': 10, 'public': 3, 'full': 1}])
def test_only_one_infra_debit(after):
    with pytest.raises(ValueError):
        m.quota_delta({'infra': 10, 'public': 3, 'full': 1}, after)


@pytest.fixture
def database(tmp_path):
    path = tmp_path / 'platform.db'
    with sqlite3.connect(path) as db:
        db.executescript('''
        CREATE TABLE versions(id,status,sha256,report);
        CREATE TABLE audit(action,user_id,actor,before_json,after_json,object_id);
        CREATE TABLE review_advisories(version_id,state,input_sha,created);
        CREATE TABLE batches(id,status,mode,kind,version_id);
        CREATE TABLE ledger(batch_id,kind,delta);
        CREATE TABLE runs(batch_id,id,attempt,state,reward,public,object_key,object_sha);
        CREATE TABLE run_tokens(run_id,revoked);
        CREATE TABLE usage(run_id,status);
        ''')
        raw = '{"safe":"fixture"}'
        db.execute('INSERT INTO versions VALUES(?,?,?,?)', ('v', 'approved', 'a'*64, raw))
        before = {'status': 'pending_review', 'submission_sha': 'a'*64}
        after = {'status': 'approved', 'policy': 'qiqc-automatic-review/v1:qiqc-readonly-advisory/v6@fixture', 'report_sha': hashlib.sha256(raw.encode()).hexdigest(), 'input_sha': 'b'*64}
        db.execute('INSERT INTO audit VALUES(?,?,?,?,?,?)', ('automatic_review', '', 'automatic-review', json.dumps(before), json.dumps(after), 'v'))
        db.execute('INSERT INTO review_advisories VALUES(?,?,?,?)', ('v', 'complete', 'b'*64, 1))
        db.execute('INSERT INTO batches VALUES(?,?,?,?,?)', ('b', 'completed', 'scientific', 'infra', 'v'))
        db.execute('INSERT INTO ledger VALUES(?,?,?)', ('b', 'infra', -1))
        db.execute('INSERT INTO runs VALUES(?,?,?,?,?,?,?,?)', ('b', 'r', 1, 'completed', 0, 1, 'private-object', 'c'*64))
        db.execute('INSERT INTO run_tokens VALUES(?,?)', ('r', 1))
        db.execute('INSERT INTO usage VALUES(?,?)', ('r', 200))
        db.execute('INSERT INTO usage VALUES(?,?)', ('r', 499))
    return path


def test_real_zero_reward_and_budget_cancel_valid(database):
    result = m.check_database(database, 'v', 'b', 'a'*64)
    assert result['automatic_review'] and result['model_error_rows'] == 0
    assert result['quota_debits'] == 1
    assert 'private-object' not in json.dumps(result)


def test_native_infra_rollup_keeps_all_three_model_usage_counts(database):
    with sqlite3.connect(database) as db:
        db.executescript('CREATE TABLE batch_scientific_task_sets(batch_id,catalog_sha256);'
                         'CREATE TABLE scientific_task_sets(sha256,document);')
        db.execute('INSERT INTO batch_scientific_task_sets VALUES(?,?)', ('b','catalog'))
        db.execute('INSERT INTO scientific_task_sets VALUES(?,?)', ('catalog',json.dumps({'schema':'qiqc-scientific-task-set/v2','tasks':[{'evaluation_role':'infrastructure'},{'evaluation_role':'infrastructure'},{'evaluation_role':'infrastructure'}]})))
        for i in range(1,3):
            run = f'r{i}'
            db.execute('INSERT INTO runs VALUES(?,?,?,?,?,?,?,?)', ('b',run,1,'completed',0,1,'private-object','c'*64))
            db.execute('INSERT INTO run_tokens VALUES(?,?)', (run,1))
            db.executemany('INSERT INTO usage VALUES(?,?)', [(run,200)]*(i+1))
    result = m.check_database(database,'v','b','a'*64)
    assert len(result['runs']) == 3
    assert result['model_usage_rows'] == 6
    assert 'run_id' not in result  # A three-case batch has no single representative run.


@pytest.mark.parametrize('sql', [
    "UPDATE audit SET actor='organizer'",
    "UPDATE audit SET after_json=replace(after_json,'v6@','v3@')",
    "UPDATE audit SET after_json=replace(after_json,'v6@','v4@')",
    "UPDATE audit SET after_json=replace(after_json,'v6@','v5@')",
    "UPDATE audit SET user_id='admin'",
    "UPDATE audit SET action='review'",
    "INSERT INTO audit VALUES('scientific_target_bound','admin','organizer','{}','{}','b')",
    "UPDATE review_advisories SET input_sha='wrong'",
    "UPDATE versions SET report='changed'",
    "UPDATE runs SET public=0",
    "UPDATE runs SET state='attention'",
    "UPDATE runs SET object_sha=NULL",
    "UPDATE run_tokens SET revoked=0",
    "INSERT INTO usage VALUES('r',502)",
    "INSERT INTO ledger VALUES('b','infra',-1)",
    "UPDATE batches SET mode='scientific-smoke'",
])
def test_audit_rejects_false_positive(database, sql):
    with sqlite3.connect(database) as db:
        db.execute(sql)
    with pytest.raises(ValueError):
        m.check_database(database, 'v', 'b', 'a'*64)


def test_driver_does_not_include_manual_approval_or_binding():
    source = SCRIPT.read_text()
    assert '/api/admin/approve' not in source
    assert '/api/admin/scientific-target' not in source
    assert 'cloud_browser_check' not in source
    assert source.index('admin.close()') < source.index("stage = 'ordinary-upload-and-automatic-review'")


@pytest.mark.parametrize('kind', ['infra', 'public'])
@pytest.mark.parametrize('review_outcome', ['approved', 'rejected'])
@pytest.mark.skipif(not BROWSER_AVAILABLE, reason='Playwright Chromium executable is not installed; run the explicit browser acceptance after installing it')
def test_entire_driver_with_real_frontend_and_synthetic_dependencies(tmp_path, monkeypatch, review_outcome, kind):
    """Real Chromium + real app.js; accounts/model/E2B/R2 are explicit local fixtures."""
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from http.cookies import SimpleCookie
    import sys
    import threading
    from urllib.parse import urlsplit

    scripts = str(SCRIPT.parent)
    monkeypatch.syspath_prepend(scripts)
    web = SCRIPT.parents[1] / 'web'
    state = {'users': {'admin@fixture.local': dict(id='admin', email='admin@fixture.local', role='admin', team='Fixture admin', must_change=False)}, 'versions': [], 'batches': [], 'requests': [], 'uploaded': False}
    config = {'phase':dict(name='practice',server_time='2026-10-26T00:00:00Z',allowed_kinds=[kind],upload_allowed=True,reason='Synthetic practice'),'scientific_task_set': dict(configured=True, infra_ready=True, full_ready=True, public_ready=kind == 'public', bounded=True, leaderboard_eligible=False, agent_budget_seconds=180)}
    template = io.BytesIO()
    with zipfile.ZipFile(template, 'w') as z:
        z.writestr('harness.json', json.dumps(dict(name='fixture', entrypoint='adapter.py', schema='qiqc-harness/v1-demo')))
        z.writestr('adapter.py', '# fixture never executed\n')

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def reply(self, value, status=200, cookie=None, content_type='application/json'):
            body = value if isinstance(value, bytes) else json.dumps(value).encode()
            self.send_response(status)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(body)))
            if cookie:
                self.send_header('Set-Cookie', 'fixture=' + cookie + '; Path=/; HttpOnly')
            self.end_headers()
            self.wfile.write(body)

        def identity(self):
            cookies = SimpleCookie(self.headers.get('Cookie', ''))
            return state['users'].get(cookies['fixture'].value) if 'fixture' in cookies else None

        def do_GET(self):
            path = urlsplit(self.path).path
            who = self.identity()
            state['requests'].append(('GET', path, state['uploaded']))
            if path in ('/admin', '/submit'):
                return self.reply((web / ('admin.html' if path=='/admin' else 'index.html')).read_bytes(), content_type='text/html')
            if path.startswith('/platform-assets/'):
                name = path.rsplit('/', 1)[-1]
                return self.reply((web / name).read_bytes(), content_type='application/javascript' if name.endswith('.js') else 'text/css')
            if path == '/api/config':
                return self.reply(config)
            if path.startswith('/adapters/'):
                return self.reply(template.getvalue(), content_type='application/zip')
            if path == '/api/me':
                return self.reply(who or {'error': 'login'}, 200 if who else 401)
            if path == '/api/admin':
                return self.reply(dict(versions=[], batches=[], usage=dict(calls=0, input_tokens=0, output_tokens=0, known_amount=None), usage_rows=[], teams=[], appeals=[], audit=[], outbox=[], paused=False, model='fixture', slots=1, disk_free_bytes=1000000000))
            if path in ('/api/announcements', '/api/support'):
                return self.reply([])
            if path == '/api/my-appeal':
                return self.reply(None)
            if path == '/api/submissions':
                return self.reply([])
            if path == '/api/versions':
                return self.reply(state['versions'] if who and who['id'] == 'team0' else [])
            if path == '/api/batches':
                return self.reply(state['batches'] if who and who['id'] == 'team0' else [])
            if path == '/api/leaderboard':
                return self.reply({'teams': []})
            if path.startswith('/api/batches/'):
                if not who or who['id'] != 'team0':
                    return self.reply({'error': 'not found'}, 404)
                if '/traces/' in path:
                    return self.reply({'mode': 'scientific', 'text': '{"synthetic_public_trace":true}'})
                return self.reply(state['batches'][0])
            return self.reply({'error': 'not found'}, 404)

        def do_POST(self):
            path = urlsplit(self.path).path
            raw = self.rfile.read(int(self.headers.get('Content-Length', '0')))
            body = json.loads(raw) if self.headers.get('Content-Type', '').startswith('application/json') else None
            who = self.identity()
            state['requests'].append(('POST', path, state['uploaded']))
            if path == '/api/login':
                return self.reply(state['users'][body['email']], cookie=body['email'])
            if path == '/api/admin/accounts':
                n = len(state['users']) - 1
                state['users'][body['email']] = dict(id='team' + str(n), email=body['email'], role='team', team=body['team'], team_id='t' + str(n), must_change=True, quotas=dict(full=1, public=3, infra=10))
                return self.reply({'issued': True})
            if path == '/api/password':
                who['must_change'] = False
                return self.reply({'ok': True})
            if path == '/api/submissions':
                state['uploaded'] = True
                version = dict(id='v', name='fixture', sha256=hashlib.sha256(raw).hexdigest(), status=review_outcome, reason='automatic fixture', files=2, risk_count=0, created=1)
                state['versions'] = [version]
                if review_outcome=='rejected':return self.reply(dict(id='intent',version_id='v',sha256=version['sha256'],kind=kind,state='rejected'))
                who['quotas'][kind] -= 1
                count = 5 if kind == 'public' else 3
                batch = dict(id='b', kind=kind, mode='scientific', status='completed', sha256=state['versions'][0]['sha256'], score=dict(total=count, passed=0, mean_seconds='180.000000'), bounded_scientific=True, leaderboard_eligible=False, agent_budget_seconds=180, public_runs=[dict(ordinal=i, label=f'Public fixture {i}') for i in range(count)] if kind == 'public' else [], accepted_at=1, invalid=False)
                state['batches'] = [batch]
                return self.reply(dict(id='intent',version_id='v',sha256=version['sha256'],kind=kind,state='accepted',batch_id='b'))
            return self.reply({'error': 'not found'}, 404)

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    seed = tmp_path / 'seed.json'
    seed.write_text(json.dumps({'admin': {'email': 'admin@fixture.local', 'password': 'fixture-initial-password'}}))
    seed.chmod(0o600)
    candidate = tmp_path / 'candidate.json'
    candidate.write_text(json.dumps(dict(url=f'http://127.0.0.1:{server.server_port}', credentials=str(seed), candidate_root=str(tmp_path), data=str(tmp_path), candidate_id='synthetic-local-fixture')))
    (tmp_path / 'scientific').mkdir()
    run_proofs = [dict(run_id=f'r{i}', attempt=1) for i in range(5)]
    for proof in run_proofs:
        (tmp_path / f'scientific/{proof["run_id"]}_1.json').write_text(json.dumps({'resources': {'fixture': 'destroyed'}}))
    output = tmp_path / 'report.json'
    monkeypatch.setattr(m, 'check_database', lambda *args: dict(runs=run_proofs, automatic_review=True))
    monkeypatch.setattr(sys, 'argv', [str(SCRIPT), '--candidate', str(candidate), '--output', str(output), '--kind', kind, '--review-timeout', '30', '--execution-timeout', '30', '--execute-real-science'])
    try:
        result = m.main()
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=5)
    report = json.loads(output.read_text())
    if review_outcome == 'rejected':
        assert result == 1 and report['failure_code'] == 'automatic_review_rejected'
        assert report['cloud_task_started'] is False and report['version_id'] == 'v'
        assert not any(path == '/api/batches' and method == 'POST' for method, path, _ in state['requests'])
        return
    assert result == 0, report
    assert set(report['screenshots']) == {'automatic-approved', 'result'}
    for item in report['screenshots'].values():
        assert hashlib.sha256(Path(item['path']).read_bytes()).hexdigest() == item['sha256']
    assert report['public_trace_visible_in_browser'] is (kind == 'public')
    assert report['other_team_trace_denied'] is (kind == 'public')
    assert report['admin_after_upload_requests'] == 0
    assert not any(path.startswith('/api/admin') and uploaded for _, path, uploaded in state['requests'])
    assert sum(method == 'POST' and path == '/api/submissions' for method, path, _ in state['requests']) == 1
    assert not any(method=='POST' and path=='/api/batches' for method,path,_ in state['requests'])


def test_public_config_and_single_quota_debit():
    config = dict(configured=True, infra_ready=True, public_ready=True, bounded=True, leaderboard_eligible=False, agent_budget_seconds=180)
    assert m.check_config({'scientific_task_set': config}, 'public')['public_ready']
    config['public_ready'] = False
    with pytest.raises(ValueError, match='scientific_public_unavailable'):
        m.check_config({'scientific_task_set': config}, 'public')
    m.quota_delta(dict(public=3, infra=10, full=1), dict(public=2, infra=10, full=1), 'public')
    with pytest.raises(ValueError):
        m.quota_delta(dict(public=3, infra=10, full=1), dict(public=3, infra=9, full=1), 'public')


def test_public_five_database_runs_require_all_tokens_revoked(database):
    with sqlite3.connect(database) as db:
        db.execute("UPDATE batches SET kind='public'")
        db.execute("UPDATE ledger SET kind='public'")
        for i in range(1, 5):
            db.execute('INSERT INTO runs VALUES(?,?,?,?,?,?,?,?)', ('b', f'r{i}', 1, 'completed', 0, 1, 'private-object', 'c'*64))
            db.execute('INSERT INTO usage VALUES(?,?)', (f'r{i}', 200))
    result = m.check_database(database, 'v', 'b', 'a'*64, 'public')
    assert len(result['runs']) == 5 and result['quota_debits'] == 1
    with sqlite3.connect(database) as db:
        db.execute("INSERT INTO run_tokens VALUES('r4',0)")
    with pytest.raises(ValueError, match='model_accounting_or_token_failure'):
        m.check_database(database, 'v', 'b', 'a'*64, 'public')
