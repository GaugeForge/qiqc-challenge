"""Kill an actual local Go fixture worker and verify persisted lease recovery."""
import os
import secrets
import signal
import time
import httpx


def run_crash(base, engine, admin_email, admin_password, worker, restart_worker):
    started = time.monotonic()
    headers = {'X-QIQC-Request': '1'}
    with httpx.Client(base_url=base, headers=headers, timeout=30, trust_env=False) as admin, httpx.Client(base_url=base, headers=headers, timeout=30, trust_env=False) as team:
        assert admin.post('/api/login', json={'email': admin_email, 'password': admin_password}).status_code == 200
        email, password = 'crash-' + secrets.token_hex(6) + '@isolated.test', secrets.token_urlsafe(24)
        assert admin.post('/api/admin/accounts', json={'email': email, 'password': password, 'team': 'Worker crash fixture', 'actor': 'crash acceptance', 'reason': 'Private process restart drill'}).status_code == 200
        assert team.post('/api/login', json={'email': email, 'password': password}).status_code == 200
        assert team.post('/api/password', json={'password': secrets.token_urlsafe(24)}).status_code == 200
        reference = team.get('/api/reference.zip').content
        version = team.post('/api/versions', content=reference).json()
        if version['status'] == 'pending_review':
            reviewed = admin.post('/api/admin/actions', json={
                'action':'review','target':version['id'],'decision':'approve',
                'actor':'crash fixture organizer','reason':'Reviewed deterministic reference for local crash recovery'})
            assert reviewed.status_code == 200
        current = next(row for row in team.get('/api/versions').json() if row['id'] == version['id'])
        assert current['status'] == 'approved'
        request = {'version_id': version['id'], 'kind': 'full', 'mode': 'simulated', 'idempotency_key': 'crash-full'}
        response = team.post('/api/batches', json=request)
        assert response.status_code == 200
        bid = response.json()['id']
        deadline = time.monotonic() + 15
        observed = False
        while time.monotonic() < deadline:
            with engine.db() as conn:
                active = conn.execute("SELECT count(*) FROM runs WHERE batch_id=? AND state IN ('running','uploading')", (bid,)).fetchone()[0]
            if active:
                os.killpg(worker.pid, signal.SIGKILL)
                worker.wait(timeout=5)
                observed = True
                break
            time.sleep(.002)
        if not observed:
            raise RuntimeError('Could not intercept active worker; crash test is inconclusive')
        replacement = restart_worker()
        assert team.post('/api/batches', json=request).json()['id'] == bid
        deadline = time.monotonic() + 100
        while time.monotonic() < deadline:
            batch = team.get('/api/batches/' + bid).json()
            if batch['status'] == 'completed':
                break
            time.sleep(.5)
        assert batch['status'] == 'completed'
        assert team.get('/api/me').json()['quotas']['full'] == 1
        with engine.db() as conn:
            counts = conn.execute('SELECT count(*),sum(state=\'completed\'),count(DISTINCT object_key) FROM runs WHERE batch_id=?', (bid,)).fetchone()
            ledger = conn.execute('SELECT count(*) FROM ledger WHERE batch_id=?', (bid,)).fetchone()[0]
            outbox = conn.execute("SELECT count(*) FROM outbox WHERE event='evaluation_completed' AND payload LIKE ?", ('%' + bid + '%',)).fetchone()[0]
        assert tuple(counts) == (49, 49, 49) and ledger == 1 and outbox == 1
        return {'status': 'passed', 'fault': 'SIGKILL actual Go worker during active fixture batch',
                'clock_modified': False, 'request_id_preserved': True, 'tasks_completed': 49,
                'distinct_rollouts': 49, 'quota_debits': ledger, 'completion_notifications': outbox,
                'new_worker_alive': replacement.poll() is None, 'duration_seconds': round(time.monotonic() - started, 3),
                'limits': 'Real local process crash and persisted lease recovery; not physical host loss or cloud sandbox failure.'}
