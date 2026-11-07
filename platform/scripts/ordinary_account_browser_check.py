#!/usr/bin/env python3
"""Ordinary entrant browser acceptance; admin is used only to issue two accounts.

Nothing runs at import. --execute-real-science is required to incur cloud/model cost.
Credentials stay in a separate private record. Reports contain no trace bodies,
passwords, provider responses, or raw exceptions. Existing batches are never retried.
"""
from __future__ import annotations

import argparse
from contextlib import ExitStack
import hashlib
import io
import json
from pathlib import Path
import secrets
import sqlite3
import time
from urllib.parse import urlsplit
import zipfile


def require(condition, code):
    if not condition:
        raise ValueError(code)


def fresh_submission(source, nonce):
    """Repackage a trusted template with a unique public name; never execute it."""
    data = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(source)) as original, zipfile.ZipFile(data, 'w', zipfile.ZIP_DEFLATED) as target:
        names = original.namelist()
        require(len(names) == len(set(names)) and 'harness.json' in names, 'invalid_template')
        for info in original.infolist():
            content = original.read(info)
            if info.filename == 'harness.json':
                manifest = json.loads(content)
                manifest['name'] = 'ordinary-browser-' + nonce
                content = (json.dumps(manifest, ensure_ascii=False, indent=2) + '\n').encode()
            target.writestr(info, content)
    return data.getvalue()


def quota_delta(before, after, kind="infra"):
    require(kind in ("infra", "public"), "invalid_evaluation_kind")
    require(set(before) == set(after), 'quota_keys_changed')
    require(all(after[k] == before[k] - (1 if k == kind else 0) for k in before), 'quota_debit_mismatch')


def check_config(config, kind="infra"):
    require(kind in ("infra", "public"), "invalid_evaluation_kind")
    value = config.get('scientific_task_set', {})
    require(value.get('configured') is True and value.get('infra_ready') is True, 'scientific_infra_unavailable')
    require(kind != 'public' or value.get('public_ready') is True, 'scientific_public_unavailable')
    require(value.get('bounded') is True and value.get('leaderboard_eligible') is False, 'scientific_boundary_changed')
    require(type(value.get('agent_budget_seconds')) is int and value['agent_budget_seconds'] in (180, 300), 'invalid_budget')
    if kind == 'infra' and value.get('infrastructure_test_count') is not None:
        require(value['infrastructure_test_count'] == 3, 'scientific_infra_task_count_mismatch')
    if kind == 'public':
        require(value.get('public_task_count', 5) == 5, 'scientific_public_task_count_mismatch')
    return value


def check_database(database, version_id, batch_id, sha, kind="infra"):
    """Independent read-only proof: system review, no human decision or target bind."""
    require(kind in ("infra", "public"), "invalid_evaluation_kind")
    # Legacy/synthetic one-case fixtures remain valid when no role-bearing
    # catalog is present. A frozen v2 catalog with three infrastructure roles
    # below upgrades the proof to the current three-case contract.
    expected_runs = 5 if kind == "public" else 1
    with sqlite3.connect(Path(database).resolve().as_uri() + '?mode=ro', uri=True) as db:
        db.row_factory = sqlite3.Row
        if kind=='infra' and db.execute("SELECT 1 FROM sqlite_master WHERE name='batch_scientific_task_sets'").fetchone():
            row=db.execute('SELECT c.document FROM batch_scientific_task_sets b JOIN scientific_task_sets c ON c.sha256=b.catalog_sha256 WHERE b.batch_id=?',(batch_id,)).fetchone()
            if row and json.loads(row[0]).get('schema')=='qiqc-scientific-task-set/v2':
                catalog=json.loads(row[0])
                roles=[task.get('evaluation_role','') for task in catalog.get('tasks',[])]
                expected_runs=3 if roles and roles.count('infrastructure') == 3 else 5
        version = db.execute('SELECT status,sha256,report FROM versions WHERE id=?', (version_id,)).fetchone()
        require(version and version['status'] == 'approved' and version['sha256'] == sha, 'version_identity_mismatch')
        audits = db.execute('SELECT action,user_id,actor,before_json,after_json FROM audit WHERE object_id IN (?,?)', (version_id, batch_id)).fetchall()
        require(not any(row['action'] in ('review', 'scientific_target_bound') for row in audits), 'human_review_or_target_binding')
        reviews = [r for r in audits if r['action'] == 'automatic_review']
        require(len(reviews) == 1, 'system_review_count')
        audit = reviews[0]
        before, after = json.loads(audit['before_json']), json.loads(audit['after_json'])
        require(audit['actor'] == 'automatic-review' and not audit['user_id'], 'system_review_identity')
        require(before == {'status': 'pending_review', 'submission_sha': sha}, 'system_review_before')
        require(after.get('status') == 'approved' and after.get('policy', '').startswith('qiqc-automatic-review/v1:qiqc-readonly-advisory/v6@'), 'system_review_policy')
        report_sha = hashlib.sha256(version['report'].encode()).hexdigest()
        require(after.get('report_sha') == report_sha, 'system_review_report_identity')
        require(len(after.get('input_sha', '')) == 64, 'system_review_input_identity')
        advisory = db.execute("SELECT input_sha FROM review_advisories WHERE version_id=? AND state='complete' ORDER BY created DESC LIMIT 1", (version_id,)).fetchone()
        require(advisory and advisory['input_sha'] == after['input_sha'], 'system_review_input_mismatch')
        batch = db.execute('SELECT status,mode,kind,version_id FROM batches WHERE id=?', (batch_id,)).fetchone()
        require(batch and dict(batch) == {'status': 'completed', 'mode': 'scientific', 'kind': kind, 'version_id': version_id}, 'batch_identity')
        ledger = db.execute('SELECT kind,delta FROM ledger WHERE batch_id=?', (batch_id,)).fetchall()
        require(len(ledger) == 1 and dict(ledger[0]) == {'kind': kind, 'delta': -1}, 'ledger_debit_mismatch')
        runs = db.execute('SELECT id,attempt,state,reward,public,object_key,object_sha FROM runs WHERE batch_id=?', (batch_id,)).fetchall()
        require(len(runs) == expected_runs, 'run_count_mismatch')
        proofs = []
        for item in runs:
            run = dict(item)
            require(run['state'] == 'completed' and run['reward'] in (0, 1) and run['public'] == 1, 'public_run_not_complete')
            require(bool(run['object_key']) and len(run['object_sha'] or '') == 64, 'archive_index_missing')
            tokens = db.execute('SELECT count(*) FROM run_tokens WHERE run_id=? AND revoked=0', (run['id'],)).fetchone()[0]
            usages = db.execute('SELECT count(*) FROM usage WHERE run_id=? AND status=200', (run['id'],)).fetchone()[0]
            errors = db.execute('SELECT count(*) FROM usage WHERE run_id=? AND status NOT IN (200,499)', (run['id'],)).fetchone()[0]
            require(tokens == 0 and usages > 0 and errors == 0, 'model_accounting_or_token_failure')
            proofs.append({'run_id': run['id'], 'attempt': run['attempt'], 'r2_archive_sha256': run['object_sha'],
                           'model_usage_rows': usages, 'active_run_tokens': tokens, 'model_error_rows': errors})
        result = {'runs': proofs, 'automatic_review': True, 'manual_review_count': 0,
                  'manual_target_binding_count': 0, 'quota_debits': 1, 'kind': kind,
                  'model_usage_rows': sum(p['model_usage_rows'] for p in proofs),
                  'active_run_tokens': 0, 'model_error_rows': 0}
        if kind == 'infra' and len(proofs) == 1:
            result.update(proofs[0])
        return result


def read_execution_states(database, batch_id):
    """Read only run state for the isolated acceptance barrier.

    The participant API intentionally hides per-run progress while a batch is
    testing or attention. The acceptance driver is organizer-only and already
    has the disposable candidate database, so it uses this read-only query to
    wait for every submitted run to reach a terminal state before shutting
    down the worker or reporting the batch result.
    """
    with sqlite3.connect(Path(database).resolve().as_uri() + '?mode=ro', uri=True) as db:
        rows = db.execute(
            'SELECT ordinal,state,error FROM runs WHERE batch_id=? ORDER BY ordinal',
            (batch_id,),
        ).fetchall()
    return [{'ordinal': ordinal, 'state': state, 'error': error} for ordinal, state, error in rows]


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--candidate', type=Path, required=True)
    ap.add_argument('--output', type=Path, required=True)
    ap.add_argument('--kind', choices=('infra', 'public'), default='infra')
    ap.add_argument('--adapter', choices=('codex', 'deepseek', 'pi'), default='codex')
    ap.add_argument('--credentials', type=Path)
    ap.add_argument('--review-timeout', type=int, default=600)
    ap.add_argument('--execution-timeout', type=int, default=3600)
    ap.add_argument('--execute-real-science', action='store_true')
    args = ap.parse_args()
    require(not args.output.exists(), 'evidence_already_exists')
    if not args.execute_real_science:
        ap.error('Explicit --execute-real-science is required; this creates five public task runs')
    from browser_credentials import isolate, record_initial_admin_change
    from playwright.sync_api import sync_playwright, expect
    candidate = json.loads(args.candidate.read_text())
    url = candidate['url'].rstrip('/')
    parsed = urlsplit(url)
    require(parsed.scheme == 'http' and parsed.hostname in ('127.0.0.1', 'localhost', '::1') and not parsed.username and not parsed.password and not parsed.path, 'local_candidate_required')
    seed = args.credentials or Path(candidate['credentials'])
    credential_file = isolate(seed, Path(candidate['candidate_root']) / 'browser-credentials')
    credentials = json.loads(credential_file.read_text())
    report = {'schema': 'qiqc-ordinary-browser/v1', 'status': 'failed', 'adapter': args.adapter, 'candidate_id': candidate['candidate_id'],
              'credential_delivery_record': str(credential_file), 'driver_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
              'kind': args.kind, 'expected_run_count': 5 if args.kind == 'public' else 3, 'cloud_task_started': False, 'admin_after_upload_requests': 0}
    stage = 'configuration'
    errors, forbidden = [], []
    started = time.monotonic()

    def save_credentials():
        credential_file.write_text(json.dumps(credentials, indent=2) + '\n')
        credential_file.chmod(0o600)

    def screenshot(page, label):
        require(page.locator('input[type="password"]').count() == 0, 'screenshot_password_form_present')
        if page.locator('#trace-dialog').is_visible():
            page.locator('#trace-dialog .dialog-close').click()
        target = args.output.with_suffix('.' + label + '.png')
        target.parent.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(target), full_page=True)
        report.setdefault('screenshots', {})[label] = {'path': str(target), 'sha256': hashlib.sha256(target.read_bytes()).hexdigest()}

    def get_json(context, path):
        response = context.request.get(url + '/api' + path)
        require(response.status == 200, 'api_read_failed')
        return response.json()

    def login(page, credential, admin=False):
        page.goto(url + ('/admin' if admin else '/submit'))
        page.locator('#email').fill(credential['email'])
        page.locator('#password').fill(credential['password'])
        page.locator('#login-form button[type=submit]').click()
        selector = '#admin-stats' if admin else '#zip-file'
        page.locator('#password-form, ' + selector).first.wait_for(timeout=60000)
        changed = page.locator('#new-password').count() > 0
        if changed:
            credential['password'] = secrets.token_urlsafe(24)
            page.locator('#new-password').fill(_pw:=credential['password']);page.locator('#confirm-password').fill(_pw)
            page.locator('#password-form button[type=submit]').click()
            page.locator(selector).wait_for(timeout=60000)
            save_credentials()
            if admin:
                record_initial_admin_change(seed, credential)
        return changed

    try:
        with sync_playwright() as pw, ExitStack() as cleanup:
            browser = pw.chromium.launch(headless=True, args=['--no-sandbox'])
            cleanup.callback(browser.close)
            admin = browser.new_context(extra_http_headers={'X-QIQC-Workspace': 'staff'})
            organizer = admin.new_page()
            login(organizer, credentials['admin'], admin=True)
            config = check_config(get_json(admin, '/config'), args.kind)
            report['expected_run_count'] = 5 if args.kind == 'public' else config.get('infrastructure_test_count', 3)
            report['agent_budget_seconds'] = config['agent_budget_seconds']
            stage = 'fixture-account-issuance'
            organizer.locator('[data-staff-tab="teams"]').click()
            credentials['teams'] = []
            for label in ('entrant', 'other'):
                credential = {'email': f'ordinary-{label}-{secrets.token_hex(6)}@qiqc-preview.local',
                              'team': f'Ordinary {label} {secrets.token_hex(4)}', 'password': secrets.token_urlsafe(24)}
                organizer.locator('#account-reason').fill('Ordinary account browser acceptance fixture only')
                for selector, key in (('email', 'email'), ('team', 'team'), ('password', 'password')):
                    organizer.locator('#account-' + selector).fill(credential[key])
                with organizer.expect_response(lambda r: r.url == url + '/api/admin/accounts' and r.request.method == 'POST') as issued:
                    organizer.locator('#account-form button[type=submit]').click()
                require(issued.value.status == 200, 'account_issuance_failed')
                expect(organizer.locator('#account-issued')).to_be_visible()
                credentials['teams'].append(credential)
                save_credentials()
                with organizer.expect_download() as download:
                    organizer.locator('#accounts-download').click()
                csv=credential_file.with_suffix('.'+label+'.csv')
                download.value.save_as(str(csv));csv.chmod(0o600)
            admin.close()  # No organizer session is available to upload/review/execute.
            stage = 'ordinary-first-login'
            team = browser.new_context(extra_http_headers={'X-QIQC-Workspace': 'team'})
            other = browser.new_context(extra_http_headers={'X-QIQC-Workspace': 'team'})
            page, other_page = team.new_page(), other.new_page()
            for context in (team, other):
                def guard(route):
                    if urlsplit(route.request.url).path.startswith(('/api/admin', '/admin')):
                        forbidden.append('admin_endpoint')
                        route.abort()
                    else:
                        route.continue_()
                context.route('**/*', guard)
                cleanup.callback(context.unroute_all, behavior='wait')
                context.on('page', lambda p: p.on('pageerror', lambda _e: errors.append('javascript_error')))
            page.on('pageerror', lambda _e: errors.append('javascript_error'))
            other_page.on('pageerror', lambda _e: errors.append('javascript_error'))
            require(login(page, credentials['teams'][0]) and login(other_page, credentials['teams'][1]), 'invitation_password_change_missing')
            report['invitation_login_password_change'] = True
            before = get_json(team, '/me')['quotas']
            archive = team.request.get(url + f'/adapters/{args.adapter}/harness.zip')
            require(archive.status == 200, 'template_download_failed')
            content = fresh_submission(archive.body(), secrets.token_hex(8))
            sha = hashlib.sha256(content).hexdigest()
            stage = 'ordinary-upload-and-automatic-review'
            page.locator(f'input[name=kind][value="{args.kind}"]').check()
            page.locator('#zip-file').set_input_files({'name': 'harness.zip', 'mimeType': 'application/zip', 'buffer': content})
            with page.expect_response(lambda r: r.url == url + '/api/submissions' and r.request.method == 'POST') as uploaded:
                page.locator('#upload-button').click()
            require(uploaded.value.status == 200, 'upload_failed')
            deadline = time.monotonic() + args.review_timeout
            version = None
            while time.monotonic() < deadline:
                rows = get_json(team, '/versions')
                version = next((v for v in rows if v['sha256'] == sha), None)
                if version:
                    report.update(version_id=version['id'], submission_sha256=sha)
                if version and version['status'] == 'approved':
                    break
                require(not version or version['status'] != 'rejected', 'automatic_review_rejected')
                page.wait_for_timeout(1000)
            require(version and version['status'] == 'approved', 'automatic_review_timeout')
            report.update(version_id=version['id'], submission_sha256=sha)
            stage = 'automatic-scientific-admission'
            screenshot(page, 'automatic-approved')
            # The upload intent is automatically admitted. No second confirmation.
            report['cloud_task_started'] = True
            stage = 'scientific-execution'
            deadline = time.monotonic() + args.execution_timeout
            batch = None
            while time.monotonic() < deadline:
                rows = get_json(team, '/batches')
                require(len(rows) <= 1, 'unexpected_batch_count')
                if not rows:
                    page.wait_for_timeout(500)
                    continue
                batch = rows[0]
                report['batch_id'] = batch['id']
                if batch['status'] == 'completed':
                    break
                # Do not terminate the five-task acceptance when one run
                # enters attention. Keep the worker alive until every run in
                # this disposable batch is terminal, then report the batch
                # outcome. This prevents four valid sibling runs from being
                # killed by the first provider/infra failure.
                states = read_execution_states(Path(candidate['data']) / 'platform.db', batch['id'])
                report['execution_run_states'] = states
                terminal = {'completed', 'attention', 'cancelled'}
                if len(states) == report['expected_run_count'] and all(row['state'] in terminal for row in states):
                    break
                page.wait_for_timeout(1000)
            require(batch and batch['status'] == 'completed', 'scientific_execution_timeout')
            # The API can publish the batch-completed response just before the
            # browser observes the last R2 upload transition.  Take one final
            # organizer-side read after the batch barrier so the report never
            # preserves an intermediate `uploading` snapshot as its summary.
            final_states = read_execution_states(Path(candidate['data']) / 'platform.db', batch['id'])
            report['execution_run_states'] = final_states
            terminal = {'completed', 'attention', 'cancelled'}
            require(
                len(final_states) == report['expected_run_count']
                and all(row['state'] in terminal for row in final_states),
                'scientific_terminal_barrier',
            )
            require(batch['mode'] == 'scientific' and batch['score']['total'] == report['expected_run_count'] and batch.get('bounded_scientific') is True and batch.get('leaderboard_eligible') is False, 'scientific_result_boundary')
            if args.kind == 'public' and config.get('public_time_limits') == 'native':
                require(batch.get('task_time_limits') == 'native' and 'agent_budget_seconds' not in batch, 'scientific_result_native_budget')
            else:
                require(batch['agent_budget_seconds'] == config['agent_budget_seconds'], 'scientific_result_budget')
            stage = 'result-public-trace-and-cross-team-denial'
            if args.kind == 'public':
                require(len(batch['public_runs']) == report['expected_run_count'], 'public_trace_missing')
                ordinals = [run['ordinal'] for run in batch['public_runs']]
                require(sorted(ordinals) == list(range(report['expected_run_count'])), 'public_trace_ordinals_mismatch')
                for ordinal in ordinals:
                    trace_path = f'/batches/{batch["id"]}/traces/{ordinal}'
                    trace = get_json(team, trace_path)
                    require(bool(trace.get('text')), 'empty_public_trace')
                    page.locator(f'[data-trace="{batch["id"]}"][data-ordinal="{ordinal}"]').click(timeout=20000)
                    expect(page.locator('#trace-dialog')).to_be_visible()
                    require(page.locator('#trace-content').inner_text() == trace['text'], 'trace_not_displayed')
                    page.locator('#trace-dialog .dialog-close').click()
                    denied = other.request.get(url + '/api' + trace_path)
                    require(denied.status in (403, 404), 'other_team_trace_leaked')
                report['public_trace_ordinals_checked'] = ordinals
            else:
                require(not batch['public_runs'], 'infra_trace_exposed')
            denied_batch = other.request.get(url + '/api/batches/' + batch['id'])
            require(denied_batch.status in (403, 404), 'other_team_batch_leaked')
            me = get_json(team, '/me')
            quota_delta(before, me['quotas'], args.kind)
            require(not any(t['team_id'] == me['team_id'] for t in get_json(team, '/leaderboard')['teams']), 'bounded_run_ranked')
            stage = 'independent-system-audit-and-cleanup'
            proof = check_database(Path(candidate['data']) / 'platform.db', version['id'], batch['id'], sha, args.kind)
            for run_proof in proof.get('runs', [proof]):
                receipt = json.loads((Path(candidate['data']) / 'scientific' / f'{run_proof["run_id"]}_{run_proof["attempt"]}.json').read_text())
                require(bool(receipt.get('resources')) and set(receipt['resources'].values()) == {'destroyed'}, 'sandbox_not_destroyed')
                require(not (Path(candidate['data']) / 'cache' / run_proof['run_id'] / str(run_proof['attempt'])).exists(), 'local_rollout_cache_remaining')
            require(not errors and not forbidden, 'browser_or_admin_boundary_failure')
            screenshot(page, 'result')
            report.update(proof, status='passed', public_trace_readback=args.kind == 'public', public_trace_visible_in_browser=args.kind == 'public',
                          other_team_trace_denied=args.kind == 'public', other_team_batch_denied=True, nonranking=True,
                          all_execution_sandboxes_destroyed=True, local_rollout_cache_removed=True, javascript_errors=0)
    except Exception as exc:
        report.update(failure_stage=stage, failure_type=type(exc).__name__)
        if isinstance(exc, ValueError) and len(exc.args) == 1 and isinstance(exc.args[0], str) and exc.args[0].replace('_', '').isalnum():
            report['failure_code'] = exc.args[0]
    finally:
        report['admin_after_upload_requests'] = len(forbidden)
        report['duration_seconds'] = round(time.monotonic() - started, 3)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps({'status': report['status'], 'report': str(args.output), 'failure_stage': report.get('failure_stage')}))
    return 0 if report['status'] == 'passed' else 1


if __name__ == '__main__':
    raise SystemExit(main())
