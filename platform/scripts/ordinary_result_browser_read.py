#!/usr/bin/env python3
"""Read an already completed ordinary batch; no submission, review, or cloud run.

Only ordinary login and GET requests are allowed. Keeps original browser and R2
reports immutable and writes a separate display correction proof and screenshot.
"""
from __future__ import annotations
import argparse
from contextlib import ExitStack
import hashlib
import json
from pathlib import Path
import sqlite3
from urllib.parse import urlsplit

from browser_credentials import _read_private


def require(condition, code):
    if not condition:
        raise ValueError(code)


def validate_result(batch, original):
    require(batch.get('id') == original['batch_id'] and batch.get('status') == 'completed', 'existing_batch_not_complete')
    require(batch.get('mode') == 'scientific' and batch.get('bounded_scientific') is True and batch.get('leaderboard_eligible') is False, 'scientific_display_boundary')
    require(batch.get('sha256') == original['submission_sha256'] and batch.get('agent_budget_seconds') == original['agent_budget_seconds'], 'existing_batch_identity_changed')
    require((batch.get('score') or {}).get('total') == 1, 'existing_batch_score_missing')
    runs = batch.get('public_runs') or []
    require(len(runs) == 1 and type(runs[0].get('ordinal')) is int and runs[0]['ordinal'] == 0, 'public_run_ordinal_changed')
    label = runs[0].get('label')
    require(isinstance(label, str) and label.strip() and 'mock' not in label.casefold() and '模拟' not in label, 'public_label_still_mock')
    return runs[0]


def sql_state(database, batch_id):
    with sqlite3.connect(Path(database).resolve().as_uri() + '?mode=ro', uri=True) as db:
        team = db.execute('SELECT team_id FROM batches WHERE id=?', (batch_id,)).fetchone()
        require(team is not None, 'existing_batch_missing')
        return {'team_batches': db.execute('SELECT count(*) FROM batches WHERE team_id=?', team).fetchone()[0],
                'team_versions': db.execute('SELECT count(*) FROM versions WHERE team_id=?', team).fetchone()[0],
                'team_ledger': db.execute('SELECT count(*),sum(delta) FROM ledger WHERE team_id=?', team).fetchone(),
                'runs': db.execute('SELECT id,attempt,state,object_sha FROM runs WHERE batch_id=? ORDER BY ordinal', (batch_id,)).fetchall()}


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--candidate', type=Path, required=True)
    ap.add_argument('--browser-report', type=Path, required=True)
    ap.add_argument('--r2-report', type=Path, required=True)
    ap.add_argument('--output', type=Path, required=True)
    args = ap.parse_args()
    require(not args.output.exists(), 'correction_report_exists')
    original_bytes = args.browser_report.read_bytes()
    original = json.loads(original_bytes)
    r2_bytes = args.r2_report.read_bytes()
    r2 = json.loads(r2_bytes)
    source_sha = hashlib.sha256(original_bytes).hexdigest()
    require(original.get('status') == 'passed' and r2.get('status') == 'passed' and r2.get('browser_report_sha256') == source_sha, 'original_proof_not_passed')
    config = json.loads(args.candidate.read_text())
    require(config['candidate_id'] == original['candidate_id'], 'candidate_changed')
    url = config['url'].rstrip('/')
    parsed = urlsplit(url)
    require(parsed.scheme == 'http' and parsed.hostname in ('127.0.0.1', 'localhost', '::1') and not parsed.username and not parsed.path, 'local_candidate_required')
    credential = _read_private(original['credential_delivery_record'])['teams'][0]
    report = {'schema': 'qiqc-existing-scientific-display/v1', 'status': 'failed', 'original_browser_report_sha256': source_sha,
              'original_r2_report_sha256': hashlib.sha256(r2_bytes).hexdigest(), 'batch_id': original['batch_id'],
              'verification_scope': 'read-only-existing-scientific-batch-display', 'driver_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
    database = Path(config['data']) / 'platform.db'
    before = sql_state(database, original['batch_id'])
    denied, errors = [], []
    try:
        from playwright.sync_api import sync_playwright, expect
        with sync_playwright() as pw, ExitStack() as cleanup:
            browser = pw.chromium.launch(headless=True, args=['--no-sandbox'])
            cleanup.callback(browser.close)
            context = browser.new_context(viewport={'width': 1440, 'height': 1050})
            def guard(route):
                path = urlsplit(route.request.url).path
                allowed = route.request.method in ('GET', 'HEAD') or (route.request.method == 'POST' and path == '/api/login')
                if not allowed or path.startswith(('/admin', '/api/admin')):
                    denied.append('unexpected_request')
                    route.abort()
                else:
                    route.continue_()
            context.route('**/*', guard)
            cleanup.callback(context.unroute_all, behavior='wait')
            page = context.new_page()
            page.on('pageerror', lambda _e: errors.append('javascript_error'))
            page.goto(url + '/submit')
            page.locator('#email').fill(credential['email'])
            page.locator('#password').fill(credential['password'])
            page.locator('#login-form button[type=submit]').click()
            page.locator('#zip-file').wait_for(timeout=30000)
            response = context.request.get(url + '/api/batches/' + original['batch_id'])
            require(response.status == 200, 'existing_batch_read_failed')
            batch = response.json()
            public = validate_result(batch, original)
            button = page.locator(f'[data-trace-open="{batch["id"]}"]')
            expect(button).to_have_text('View traces')
            button.click()
            expect(page.locator('#trace-panel')).to_be_visible()
            trace_button = page.locator(f'[data-panel-trace-view="{batch["id"]}"][data-panel-trace-ordinal="{public["ordinal"]}"]')
            trace_button.click()
            expect(page.locator('#trace-preview')).to_be_visible()
            require(bool(page.locator('#trace-preview-content').inner_text()), 'public_trace_not_visible')
            require(page.locator('input[type="password"]').count() == 0, 'password_form_on_result')
            shot = args.output.with_suffix('.png')
            shot.parent.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(shot), full_page=True)
            require(not denied and not errors, 'display_browser_error')
            require(sql_state(database, original['batch_id']) == before, 'existing_scientific_state_changed')
            require(hashlib.sha256(args.browser_report.read_bytes()).hexdigest() == source_sha and args.r2_report.read_bytes() == r2_bytes, 'original_evidence_changed')
            report.update(status='passed', public_label=public['label'], public_trace_visible=True,
                          ordinary_login_only=True, no_new_submissions_or_evaluations=True, sql_state_unchanged=True,
                          screenshot={'path': str(shot), 'sha256': hashlib.sha256(shot.read_bytes()).hexdigest()}, javascript_errors=0)
    except Exception as exc:
        report['failure_type'] = type(exc).__name__
        if isinstance(exc, ValueError) and str(exc).replace('_', '').isalnum():
            report['failure_code'] = str(exc)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open('x') as stream:
        json.dump(report, stream, indent=2)
        stream.write('\n')
    print(json.dumps({'status': report['status'], 'report': str(args.output), 'failure_code': report.get('failure_code')}))
    return 0 if report['status'] == 'passed' else 1


if __name__ == '__main__':
    raise SystemExit(main())
