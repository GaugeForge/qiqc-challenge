"""Bounded native Go HTTP pressure; fixtures only, never a cloud load test."""
from concurrent.futures import ThreadPoolExecutor
import secrets
import threading
import time
import httpx


def run_pressure(base, engine, admin_email, admin_password, teams=32, repeats=4):
    started = time.monotonic()
    clients = []
    inputs = []
    accepted = []
    latencies = []
    peak = 0
    stop = threading.Event()
    headers = {'X-QIQC-Request': '1'}

    def monitor():
        nonlocal peak
        while not stop.wait(.025):
            with engine.db() as conn:
                n = conn.execute("SELECT count(*) FROM runs WHERE state IN ('running','uploading')").fetchone()[0]
            peak = max(peak, n)

    with engine.db() as conn:
        initial_ledger = conn.execute('SELECT count(*) FROM ledger').fetchone()[0]
    thread = threading.Thread(target=monitor)
    thread.start()
    try:
        with httpx.Client(base_url=base, timeout=30, trust_env=False) as admin:
            assert admin.post('/api/login', json={'email': admin_email, 'password': admin_password}).status_code == 200
            archive = admin.get('/api/reference.zip').content
            # Account creation is outside the measured request phase.
            for i in range(teams):
                email = 'pressure-' + secrets.token_hex(6) + '@isolated.test'
                password = secrets.token_urlsafe(24)
                r = admin.post('/api/admin/accounts', headers=headers, json={
                    'email': email, 'password': password, 'team': f'Pressure {i}',
                    'actor': 'pressure driver', 'reason': 'Isolated native HTTP acceptance'})
                assert r.status_code == 200
                c = httpx.Client(base_url=base, timeout=60, trust_env=False, headers=headers)
                clients.append(c)
                assert c.post('/api/login', json={'email': email, 'password': password}).status_code == 200
                assert c.post('/api/password', json={'password': secrets.token_urlsafe(24)}).status_code == 200
                # One scanner invocation per account intentionally exercises upload isolation.
                v = c.post('/api/versions', content=archive)
                assert v.status_code == 200
                if v.json()['status'] == 'pending_review':
                    # This isolated fixture has no AI worker. Exercise the actual
                    # staff review endpoint; never bypass the review in SQL.
                    review = admin.post('/api/admin/actions', headers=headers, json={
                        'action':'review','target':v.json()['id'],'decision':'approve',
                        'actor':'pressure fixture organizer','reason':'Reviewed deterministic reference for local simulated pressure'})
                    assert review.status_code == 200
                current = next(row for row in c.get('/api/versions').json() if row['id'] == v.json()['id'])
                assert current['status'] == 'approved'
                inputs.append((c, {'version_id': v.json()['id'], 'kind': 'public', 'mode': 'simulated', 'idempotency_key': 'repeated-confirm'}))

        def confirm(item):
            c, body = item
            t = time.monotonic()
            r = c.post('/api/batches', json=body)
            dt = time.monotonic() - t
            assert r.status_code == 200
            return r.json()['id'], dt

        with ThreadPoolExecutor(max_workers=32) as pool:
            for bid, dt in pool.map(confirm, inputs * repeats):
                accepted.append(bid)
                latencies.append(dt)
        assert len(set(accepted)) == teams
        deadline = time.monotonic() + max(120,teams*2)
        while time.monotonic() < deadline:
            completed = sum(c.get('/api/batches').json()[0]['status'] == 'completed' for c in clients)
            if completed == teams:
                break
            time.sleep(.2)
        assert completed == teams
        with engine.db() as conn:
            ledger = conn.execute('SELECT count(*) FROM ledger').fetchone()[0] - initial_ledger
            marker = ','.join('?' for _ in set(accepted))
            runs = conn.execute(f'SELECT count(*) FROM runs WHERE batch_id IN ({marker})', list(set(accepted))).fetchone()[0]
        assert ledger == teams and runs == teams * 5 and peak <= 4
        for c in clients:
            assert c.get('/api/me').json()['quotas']['public'] == 2
        ordered = sorted(latencies)
        percentile = lambda q: round(ordered[min(len(ordered) - 1, int((len(ordered) - 1) * q))] * 1000, 3)
        return {'status': 'passed', 'environment': 'isolated-native-go-http', 'cloud_calls': False,
                'teams': teams, 'confirmation_requests': teams * repeats, 'accepted_batches': teams,
                'task_runs': runs, 'ledger_debits': ledger, 'configured_slots': 4, 'sampled_peak_active': peak,
                'acceptance_ms': {'p50': percentile(.50), 'p95': percentile(.95), 'p99': percentile(.99)},
                'duration_seconds': round(time.monotonic() - started, 3),
                'limitations': f'Fixture execution, {teams} teams and a short local run; not a 50-slot or 200-team cloud capacity claim.'}
    finally:
        stop.set()
        thread.join(timeout=3)
        for c in clients:
            c.close()
