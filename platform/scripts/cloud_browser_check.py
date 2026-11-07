#!/usr/bin/env python3
"""Explicit low-volume real science acceptance against local non-root staging.

Uses real E2B/Hy4/R2. No email, remote deployment, DNS or cloud settings changes.
Credentials remain in a root-private local file, never in the report/screenshots.
"""
import argparse
import datetime
import json
import hashlib
from pathlib import Path
import secrets
import sqlite3
import sys
import time

from playwright.sync_api import sync_playwright, expect

PLATFORM = Path(__file__).resolve().parents[1]


def candidate_agent_budget(candidate):
    budget = (candidate or {}).get('agent_budget_seconds', 300)
    if type(budget) is not int or budget not in (180, 300):
        raise ValueError('invalid_candidate_agent_budget')
    return budget


def expected_advisory_provider(candidate):
    """Require the organizer-approved provider, never accept any live label."""
    if candidate and candidate.get('provider_profile') is not None:
        from science.native_provider_profile import validate_profile_identity
        profile = validate_profile_identity(candidate['provider_profile'])
        if 'chat/completions' not in profile['endpoints']:
            raise ValueError('review_requires_chat_capability')
        return 'profile-' + profile['id'] + '-' + profile['sha256'][:12]
    if candidate and candidate.get('diagnostic_model'):
        if candidate['diagnostic_model'] != 'minimax/minimax-m3.1-flash:free':
            raise ValueError('unknown_legacy_diagnostic_model')
        return 'openrouter-minimax-free-infra-v1'
    return 'openrouter-hy4-chat-v3'


def wait_for_advisory(database, version_id, telemetry, wait, *, clock=time.monotonic):
    """Read-only queue-aware browser wait; provider deadlines remain unchanged.

    Reserve 120 seconds per queued/running advisory plus 30 seconds for polling.
    New arrivals may extend the original deadline, never beyond 600 seconds.
    Pre-advisory queue delay counts toward the absolute cap. First observed
    running gets one 150-second response window, clipped to that same cap.
    Only actual terminal advice is returned; timeout never fabricates approval.
    """
    started = clock()
    budget = 150
    telemetry.update(budget_seconds=budget, maximum_budget_seconds=600,
                     maximum_pending_advisories=0, elapsed_seconds=0,
                     last_state='not_observed', timed_out=False, state_changes=[],
                     first_running_elapsed_seconds=None)
    advice = None
    while clock() - started < budget:
        with sqlite3.connect(Path(database).resolve().as_uri() + '?mode=ro', uri=True) as db:
            pending = db.execute("SELECT count(*) FROM review_advisories WHERE state IN ('queued','running')").fetchone()[0]
            advice = db.execute('SELECT state,submission_sha,result FROM review_advisories WHERE version_id=? ORDER BY created DESC LIMIT 1', (version_id,)).fetchone()
            state = db.execute('SELECT status FROM versions WHERE id=?', (version_id,)).fetchone()
        if state is None or state[0] != 'pending_review':
            raise AssertionError('AI advisory must preserve manual review')
        budget = max(budget, min(600, 120 * max(1, pending) + 30))
        observed = advice[0] if advice else 'not_created'
        elapsed = round(clock() - started, 3)
        if observed in ('not_created', 'queued'):
            # Review worker may not have created its advisory row yet. Preserve
            # queue patience, but never move the absolute 600-second deadline.
            budget = max(budget, min(600, elapsed + 30))
        if observed == 'running' and telemetry['first_running_elapsed_seconds'] is None:
            telemetry['first_running_elapsed_seconds'] = elapsed
            budget = max(budget, min(600, elapsed + 150))
        if observed != telemetry['last_state']:
            telemetry['state_changes'].append({'state': observed, 'elapsed_seconds': elapsed})
        telemetry.update(budget_seconds=budget, elapsed_seconds=elapsed,
                         maximum_pending_advisories=max(pending, telemetry['maximum_pending_advisories']),
                         last_state=observed)
        if advice and advice[0] not in ('queued', 'running'):
            return advice
        wait(1000)
    telemetry.update(elapsed_seconds=round(clock() - started, 3), timed_out=True)
    return advice


def validate_invalid_advice_fallback(candidate, adapter, archive_bytes, version, result,
                                     quota_before, quota_after, pending_state, batch_count, *, organizer_template=False):
    """Explicit organizer-template exception; never an AI-success assertion."""
    outcome = result.get('outcome')
    allowed = outcome == 'invalid_response'
    if organizer_template:
        allowed = outcome in ('advice_ready', 'invalid_response', 'provider_timeout')
        if outcome == 'provider_error':
            diagnostics = result.get('provider_diagnostics') or {}
            # A known reasoning budget exhaustion is output quality, unlike
            # authentication/quota/transport errors. Never generalize errors.
            allowed = (result.get('failure_code') == 'completion_token_limit'
                       and diagnostics.get('finish_reason') == 'length'
                       and all(type(diagnostics.get(k)) is int for k in
                               ('prompt_tokens', 'completion_tokens', 'reasoning_tokens', 'max_tokens'))
                       and diagnostics['prompt_tokens'] > 0
                       and diagnostics['max_tokens'] == 8192
                       and diagnostics['completion_tokens'] == diagnostics['max_tokens']
                       and diagnostics['reasoning_tokens'] == diagnostics['completion_tokens'])
    if not candidate or not allowed:
        raise ValueError('manual_fallback_requires_invalid_response_or_explicit_known_terminal')
    if (result.get('status') != 'manual_required'
            or result.get('policy_version') != 'qiqc-readonly-advisory/v3'
            or result.get('provider_version') != expected_advisory_provider(candidate)):
        raise ValueError('manual_fallback_review_identity_mismatch')
    manifest = json.loads(Path(candidate['adapter_manifest']).read_text())
    approved_sha = manifest.get(adapter, {}).get('sha256')
    actual_sha = hashlib.sha256(archive_bytes).hexdigest()
    if approved_sha != actual_sha or version.get('sha256') != actual_sha:
        raise ValueError('manual_fallback_requires_exact_organizer_template')
    if pending_state != 'pending_review' or batch_count != 0 or quota_before != quota_after:
        raise ValueError('manual_fallback_requires_pending_review_without_quota_debit')
    if outcome == 'advice_ready' and result.get('manual_followup_required') is not True:
        raise ValueError('manual_fallback_requires_manual_followup')
    return {'outcome': outcome, 'failure_code': result.get('failure_code'),
            'manual_review_policy': 'organizer-template' if organizer_template else 'invalid-advice-only',
            'ai_quality_gate_passed': False,
            'real_advice_available': outcome == 'advice_ready', 'manual_fallback_template_sha256': actual_sha,
            'quota_unchanged_before_manual_approval': True}


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--queue-only', action='store_true', help='browser submit to a stopped-worker queue barrier; no execution wait')
    ap.add_argument('--resume-queued-report', type=Path, help='read same queued account/version/batch without a second submission')
    ap.add_argument('--prepare-only',action='store_true',help='candidate browser account/upload/review/target readiness; no evaluation submission')
    ap.add_argument('--candidate', type=Path, help='explicit independent native candidate config; never changes staging')
    ap.add_argument('--adapter', choices=['codex', 'deepseek', 'pi'], default='codex')
    ap.add_argument('--catalog-index', type=int, choices=range(49), help='admin-bound frozen task ordinal; hidden evidence stays private')
    ap.add_argument('--resume-last-team', action='store_true', help='resume the latest private test account for this adapter after a driver failure')
    ap.add_argument('--output', type=Path, help='separate evidence path; preserves prior cloud reports')
    ap.add_argument('--wait-ai', action='store_true', help='wait for real reviewer outcome; valid advice or explicit manual fallback')
    ap.add_argument('--require-ai-advice', action='store_true', help='require actual valid advice and visible manual-followup notice, not fallback')
    ap.add_argument('--allow-invalid-advice-manual-review', action='store_true', help='explicit organizer-template-only manual fallback for malformed AI advice; provider errors still fail')
    ap.add_argument('--organizer-template-manual-review', action='store_true', help='fixed organizer ZIP manual review after known terminal advisory outcomes; science remains strict')
    ap.add_argument('--url', default='http://127.0.0.1:18890')
    ap.add_argument('--credentials', type=Path, help='private credentials; candidate mode reads this as an admin seed and creates an isolated attempt record')
    ap.add_argument('--data', type=Path, default=Path('/var/lib/qiqc-staging/data'))
    args = ap.parse_args()
    # Parallel browser clients can queue account hashing and admin refreshes.
    # This is a UI wait only; it does not change the frozen Agent budget.
    expect.set_options(timeout=30000)
    if args.organizer_template_manual_review and (not args.candidate or args.require_ai_advice or args.allow_invalid_advice_manual_review):
        ap.error('organizer template mode requires candidate and exactly one review policy')
    if args.allow_invalid_advice_manual_review and (not args.candidate or args.require_ai_advice):
        ap.error('manual fallback requires candidate and cannot be combined with strict advice mode')
    queued = None
    if args.resume_queued_report:
        queued_raw = args.resume_queued_report.read_bytes()
        queued = json.loads(queued_raw)
        if queued.get('status') != 'browser_queued_waiting_barrier' or args.queue_only or args.prepare_only:
            ap.error('resume requires original queued report and result-only mode')
        if args.catalog_index is not None and args.catalog_index != queued['catalog_index']:
            ap.error('queued ordinal mismatch')
        args.catalog_index = queued['catalog_index']
        args.resume_last_team = True
    if args.queue_only and (not args.candidate or args.output is None):
        ap.error('queue-only requires native candidate and separate evidence')
    if args.prepare_only and (not args.candidate or args.output is None):
        ap.error('prepare-only requires explicit candidate and separate output')
    if args.catalog_index is not None and (args.output is None or (args.resume_last_team and queued is None)):
        raise SystemExit('Catalog acceptance requires a distinct output and a new account')
    if args.output and args.output.exists():
        raise SystemExit('Refusing to overwrite prior acceptance evidence')
    candidate = None
    credential_seed = None
    if args.candidate:
        from browser_candidate_runtime import bind_candidate_imports, verify_driver_snapshot
        bind_candidate_imports(args.candidate)
        driver_dependencies = verify_driver_snapshot(__file__)
        from native_candidate import load_candidate, admission_release_id, identity as candidate_identity
        candidate, source_audit = load_candidate(args.candidate)
        if args.catalog_index is None: raise SystemExit('Native candidate requires an administrator-bound ordinal')
        args.url=candidate['url'];args.data=Path(candidate['data'])
        from browser_credentials import isolate
        credential_seed=args.credentials or Path(candidate['credentials'])
        if queued is None:
            args.credentials=isolate(credential_seed,Path(candidate['candidate_root'])/'browser-credentials')
        else:
            args.credentials=Path(queued['credential_delivery_record'])
            if args.credentials.parent.resolve() != (Path(candidate['candidate_root'])/'browser-credentials').resolve() or args.credentials.is_symlink():
                raise ValueError('queued credential record is outside private attempt directory')
            credential_seed=None
    if candidate is None and args.url != 'http://127.0.0.1:18890':
        raise SystemExit('This acceptance driver is restricted to local dedicated staging')
    args.credentials=args.credentials or Path('/etc/qiqc-staging/preview-credentials.json')
    credentials = json.loads(args.credentials.read_text())
    assert args.credentials.stat().st_mode & 0o077 == 0
    report = {'status': 'failed', 'adapter': args.adapter, 'checked_at': datetime.datetime.now(datetime.timezone.utc).isoformat(),
              'environment': 'nonroot-local-staging-real-cloud', 'cloud_calls': True, 'mail_sent': False}
    if candidate:
        report['credential_delivery_record']=str(args.credentials)
    report['deployed_binaries_sha256'] = {name: hashlib.sha256((Path(candidate['binaries'] if candidate else '/opt/qiqc-staging/current') / name).read_bytes()).hexdigest() for name in ['api', 'worker', 'gateway']}
    # Execution changes in the trusted Python bridge must not be attributed to
    # the Go binary digest alone. Only organizer source files, never task data.
    science_root = Path(candidate['project_root'] if candidate else '/opt/qiqc-staging/science')
    science_files = [
        'benchmark-assets/adapters/science-probe/' + name for name in
        ['executor.py', 'qsim_relay.py', 'qsim_call.py', 'bounded_files.py', 'relay_sandbox.py']
    ] + [
        'competition-platform/platform/scripts/science/' + name for name in
        ['harbor_e2b.py', 'harbor_agent.py', 'harbor_executor.py', 'verify_public.py', 'agent_boundary_probe.py', 'e2b_runtime.py', 'task_release.py', 'task-release-pin.json']
    ]
    report['deployed_science_sha256'] = {
        name: hashlib.sha256((science_root / name).read_bytes()).hexdigest()
        for name in science_files
    }
    if args.catalog_index is not None:
        for directory in ['benchmark-assets/adapters/science-probe', 'competition-platform/platform/scripts/science']:
            for source in sorted((science_root / directory).glob('*.py')):
                report['deployed_science_sha256'][source.relative_to(science_root).as_posix()] = hashlib.sha256(source.read_bytes()).hexdigest()
    if candidate:
        controller=json.loads(Path(candidate['controller_manifest']).read_text())
        report['deployed_science_sha256']={**controller['host_files'], **{row['source']:row['sha256'] for row in controller['remote_files'].values()}}
    report['driver_sha256'] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    if candidate and driver_dependencies is not None:
        report['driver_dependencies_sha256'] = driver_dependencies
    sys.path.insert(0, str(science_root / 'competition-platform/platform/scripts/science'))
    if candidate:
        selected_task=sorted(source_audit['competition_task_ids'])[args.catalog_index]
        report['benchmark_release']=candidate_identity(candidate,args.catalog_index,admission_release_id(candidate))
        report['candidate_id']=candidate['candidate_id']
        report['source_files_sha256']=candidate['source_files_sha256']
        report['execution_topology']='e2b-native-compose'
        report['environment']='isolated-local-native-candidate-real-cloud'
    else:
        from task_release import identity, verify_release
        release = verify_release(science_root)
        selected_task = 'tunable_coupler_cz_netzero' if args.catalog_index is None else sorted(release['tasks'])[args.catalog_index]
        report['benchmark_release'] = identity(release, selected_task)
    if args.catalog_index is not None:
        report.update(catalog_index=args.catalog_index, agent_budget_seconds=candidate_agent_budget(candidate), hidden_trace_required=True)
    if queued is not None:
        if queued.get('agent_budget_seconds') != candidate_agent_budget(candidate):
            raise ValueError('queued agent budget changed')
        if queued['benchmark_release'] != report['benchmark_release'] or queued['deployed_binaries_sha256'] != report['deployed_binaries_sha256']:
            raise ValueError('queued execution identity changed')
        for key in ('invitation_login_password_change', 'upload_freeze_review_sha256', 'admin_target_bound_via_browser', 'ai_review', 'upload_admission'):
            if key in queued: report[key]=queued[key]
        report['prepared_report_sha256']=hashlib.sha256(queued_raw).hexdigest()
        report['prepared_report_path']=str(args.resume_queued_report)
        report['prepared_driver_sha256']=queued['driver_sha256']
        report['same_queued_submission_resumed']=True
    started = time.monotonic()
    stage = 'login'
    errors = []
    def save_credentials():
        args.credentials.write_text(json.dumps(credentials, indent=2) + '\n')
        args.credentials.chmod(0o600)
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True, args=['--no-sandbox'])
            admin = browser.new_context(viewport={'width': 1440, 'height': 1050})
            organizer = admin.new_page()
            organizer.on('pageerror', lambda err: errors.append(str(err)))
            organizer.goto(args.url + '/admin')
            organizer.locator('#email').fill(credentials['admin']['email'])
            organizer.locator('#password').fill(credentials['admin']['password'])
            organizer.locator('#login-form button[type=submit]').click()
            organizer.locator('#password-form, #admin-stats').first.wait_for()
            if organizer.locator('#new-password').count():
                changed = secrets.token_urlsafe(24)
                organizer.locator('#new-password').fill(_pw:=changed);organizer.locator('#confirm-password').fill(_pw)
                organizer.locator('#password-form button[type=submit]').click()
                organizer.locator('#admin-stats .stat').first.wait_for()
                credentials['admin']['password'] = changed
                save_credentials()
                if credential_seed is not None:
                    from browser_credentials import record_initial_admin_change
                    record_initial_admin_change(credential_seed,credentials['admin'])
            organizer.locator('#admin-stats .stat').first.wait_for()
            stage = 'issue-account'
            if args.resume_last_team:
                matches = [row for row in credentials.get('teams', []) if row['email'].startswith(args.adapter+'-')]
                assert matches, 'No prior private adapter account to resume'
                credential = matches[-1]
                report['account_origin'] = 'resumed-prior-private-test-account'
            else:
                credential = {'email': f'{args.adapter}-{secrets.token_hex(4)}@qiqc-preview.local', 'password': secrets.token_urlsafe(24),
                              'team': f'{args.adapter} science preview {secrets.token_hex(2)}'}
                organizer.get_by_text('开通队伍账号 / 人工重置 / 关闭演示比赛', exact=True).click()
                organizer.locator('#admin-reason').fill('Bounded real public science browser acceptance')
                organizer.locator('#account-email').fill(credential['email'])
                organizer.locator('#account-team').fill(credential['team'])
                organizer.locator('#account-password').fill(credential['password'])
                with organizer.expect_response(lambda response: response.url == args.url + '/api/admin/accounts'
                                               and response.request.method == 'POST', timeout=60000) as issued:
                    organizer.locator('#account-form button').click()
                report['account_issue_http_status'] = issued.value.status
                assert issued.value.status == 200, 'Account issuance was not accepted'
                expect(organizer.locator('#detail-dialog')).to_be_visible(timeout=45000)
                organizer.locator('#detail-dialog .dialog-close').click()
                credentials.setdefault('teams', []).append(credential)
                save_credentials()
            team = browser.new_context(viewport={'width': 1440, 'height': 1000})
            page = team.new_page()
            page.on('pageerror', lambda err: errors.append(str(err)))
            page.goto(args.url + '/submit')
            page.locator('#email').fill(credential['email'])
            page.locator('#password').fill(credential['password'])
            page.locator('#login-form button[type=submit]').click()
            page.locator('#password-form, #zip-file').first.wait_for()
            if page.locator('#new-password').count():
                replacement = secrets.token_urlsafe(24)
                page.locator('#new-password').fill(_pw:=replacement);page.locator('#confirm-password').fill(_pw)
                page.locator('#password-form button[type=submit]').click()
                page.locator('#zip-file').wait_for()
                credential['password'] = replacement
                save_credentials()
                report['invitation_login_password_change'] = True
            else:
                assert args.resume_last_team
                report['password_change_completed_in_prior_attempt'] = True
                report['resumed_login'] = True
            if queued is None:
                stage = 'upload-review'
                quota_before_review = team.request.get(args.url + '/api/me').json()['quotas']
                manual_fallback = False
                archive = team.request.get(args.url + f'/adapters/{args.adapter}/harness.zip')
                assert archive.status == 200
                page.locator('#zip-file').set_input_files({'name': 'harness.zip', 'mimeType': 'application/zip', 'buffer': archive.body()})
                from browser_upload import upload_with_admission_retry
                upload_with_admission_retry(page, team, args.url, report, expect, max_attempts=6)
                expect(page.locator('#version-list')).to_contain_text('待人工复核', timeout=45000)
                version = team.request.get(args.url + '/api/versions').json()[0]
                if args.wait_ai or args.require_ai_advice or args.allow_invalid_advice_manual_review or args.organizer_template_manual_review:
                    stage = 'ai-advisory'
                    report['ai_review_wait'] = {}
                    advice = wait_for_advisory(args.data / 'platform.db', version['id'],
                                               report['ai_review_wait'], page.wait_for_timeout)
                    assert advice and advice[0] == 'complete' and advice[1] == version['sha256']
                    result = json.loads(advice[2])
                    assert result['status'] == 'manual_required'
                    assert result['provider_version'] == expected_advisory_provider(candidate)
                    report['ai_review'] = {'outcome':result['outcome'],'manual_approval_preserved':True,'real_advice_available':result['outcome']=='advice_ready'}
                    report['ai_review'].update(policy_version=result['policy_version'], provider_version=result['provider_version'],
                                               diagnostics=result.get('provider_diagnostics'), failure_code=result.get('failure_code'),
                                               manual_followup_required=result.get('manual_followup_required'),
                                               uncovered_scanner_risks=result.get('uncovered_scanner_risks'))
                    if args.organizer_template_manual_review or (args.allow_invalid_advice_manual_review and result['outcome'] == 'invalid_response'):
                        with sqlite3.connect((args.data / 'platform.db').as_uri() + '?mode=ro', uri=True) as db:
                            pending = db.execute('SELECT status FROM versions WHERE id=?', (version['id'],)).fetchone()[0]
                            batches = db.execute('SELECT count(*) FROM batches WHERE version_id=?', (version['id'],)).fetchone()[0]
                        fallback = validate_invalid_advice_fallback(candidate, args.adapter, archive.body(), version, result,
                            quota_before_review, team.request.get(args.url + '/api/me').json()['quotas'], pending, batches,
                            organizer_template=args.organizer_template_manual_review)
                        report['ai_review'].update(fallback)
                        manual_fallback = True
                    if args.require_ai_advice or (args.allow_invalid_advice_manual_review and not manual_fallback):
                        assert result['outcome'] == 'advice_ready'
                        assert result['policy_version'] == 'qiqc-readonly-advisory/v3'
                        assert result['manual_followup_required'] is True
                organizer.reload()
                if args.require_ai_advice or args.allow_invalid_advice_manual_review or args.organizer_template_manual_review:
                    item = organizer.locator(f'.review-item:has([data-approve="{version["id"]}"])')
                    if manual_fallback and result['outcome'] != 'advice_ready':
                        expect(item).to_contain_text('AI 审查失败/过期 · 转人工')
                        report['ai_review']['manual_fallback_notice_visible_in_admin'] = True
                    else:
                        expect(item.locator('[data-review-followup]')).to_contain_text('请继续人工复核')
                    if args.output:
                        item.screenshot(path=str(args.output.with_suffix('.review.png')))
                    report['ai_review']['manual_followup_visible_in_admin'] = result['outcome'] == 'advice_ready'
                approval_reason = ('AI ' + result['outcome'] + '/' + str(result.get('failure_code') or 'none') + '; manual decision based on pre-reviewed organizer manifest SHA ' + version['sha256']
                                   if manual_fallback else 'Approved exact organizer CLI template SHA for bounded scientific acceptance')
                organizer.locator('#admin-reason').fill(approval_reason)
                organizer.locator(f'[data-approve="{version["id"]}"]').click()
                expect(page.locator('#version-select')).to_have_value(version['id'], timeout=20000)
                if manual_fallback:
                    report['ai_review'].update(manual_fallback_verified=True, manual_approval_reason=approval_reason)
                report['upload_freeze_review_sha256'] = version['sha256']
                if args.catalog_index is not None:
                    stage = 'admin-bind-catalog-target'
                    organizer.reload()
                    budget = candidate_agent_budget(candidate)
                    live_config = team.request.get(args.url + '/api/config').json()
                    assert live_config['scientific_agent_budget_seconds'] == budget
                    expect(organizer.locator('#science-target-panel')).to_contain_text(f'Agent 最多运行 {budget} 秒')
                    organizer.locator('#admin-reason').fill(f'Bounded {budget} second catalog role-isolation acceptance, nonranking')
                    organizer.locator('#science-target-version').fill(version['id'])
                    organizer.locator('#science-target-index').select_option(str(args.catalog_index))
                    organizer.locator('#science-target-submit').click()
                    expect(organizer.locator('#toast')).to_contain_text('操作已完成并记录')
                    with sqlite3.connect((args.data / 'platform.db').as_uri() + '?mode=ro', uri=True) as db:
                        bound = db.execute('SELECT task_index,agent_budget_seconds,release_id FROM scientific_targets WHERE version_id=?', (version['id'],)).fetchone()
                    assert bound == (args.catalog_index, budget, report['benchmark_release']['release_id'])
                    report['admin_target_bound_via_browser'] = True
                if args.prepare_only:
                    if not candidate or args.output is None:raise ValueError('prepare-only requires explicit candidate and evidence output')
                    report.update(status='browser_ready_not_executed',cloud_task_started=False,submission_sha256=version['sha256'],duration_seconds=round(time.monotonic()-started,3))
                    args.output.parent.mkdir(parents=True,exist_ok=True)
                    args.output.write_text(json.dumps(report,indent=2)+'\n')
                    print(json.dumps({'status':report['status'],'cloud_task_started':False,'submission_sha256':version['sha256']}))
                    browser.close()
                    return 0
                stage = 'scientific-execution'
                page.locator('#mode-select').select_option('scientific-smoke')
                assert page.locator('input[name=kind]:checked').input_value() == 'infra'
                page.locator('#prepare-evaluation').click()
                expect(page.locator('#confirm-dialog')).to_contain_text('不计排名')
                page.locator('#confirm-evaluate').click()
                expect(page.locator('#confirm-dialog')).not_to_be_visible()
            else:
                stage = 'resume-same-queued-batch'
                versions = team.request.get(args.url + '/api/versions').json()
                version = next(v for v in versions if v['id'] == queued['version_id'])
                assert version['sha256'] == queued['submission_sha256'] and version['status'] == 'approved'
            if args.queue_only:
                items = team.request.get(args.url + '/api/batches').json()
                assert len(items) == 1 and items[0]['status'] == 'queued'
                report.update(status='browser_queued_waiting_barrier', batch_id=items[0]['id'],
                              version_id=version['id'], submission_sha256=version['sha256'],
                              cloud_task_started=False, quota_debits=1,
                              duration_seconds=round(time.monotonic()-started, 3))
                    assert team.request.get(args.url + '/api/me').json()['quotas'] == {'full': 1, 'public': 3, 'infra': 9}
                args.output.parent.mkdir(parents=True, exist_ok=True)
                args.output.write_text(json.dumps(report, indent=2) + '\n')
                browser.close()
                return 0
            # Native tasks preserve their original verifier timeout after the
            # bounded agent phase. The old 20-minute browser deadline must not
            # report a valid, still-running original verifier as a UI failure.
            # Admission rejects tasks whose complete native budget exceeds the
            # configured account window; this remains a bounded local driver.
            report['execution_wait_limit_seconds'] = 3600 if candidate else 1200
            deadline = time.monotonic() + report['execution_wait_limit_seconds']
            batch = None
            while time.monotonic() < deadline:
                items = team.request.get(args.url + '/api/batches').json()
                if items:
                    batch = next((item for item in items if item['id'] == queued['batch_id']), None) if queued else items[0]
                    assert batch is not None
                    report['batch_id'] = batch['id']
                    if batch['status'] == 'completed':
                        break
                    with sqlite3.connect((args.data / 'platform.db').as_uri() + '?mode=ro', uri=True) as db:
                        state = db.execute('SELECT status FROM batches WHERE id=?', (batch['id'],)).fetchone()[0]
                    if state == 'attention':
                        raise RuntimeError('Scientific batch requires organizer recovery; not a valid zero')
                page.wait_for_timeout(1000)
            assert batch and batch['status'] == 'completed' and batch['score']['total'] == 1
            stage = 'result-evidence'
            if args.catalog_index is None:
                page.locator(f'[data-trace="{batch["id"]}"][data-ordinal="0"]').click(timeout=20000)
                expect(page.locator('#trace-dialog')).to_be_visible()
                trace = team.request.get(args.url + f'/api/batches/{batch["id"]}/traces/0').json()
                public = json.loads(trace['text'])
                assert public['standard_harbor_rollout'] is True
                page.screenshot(path=str((args.output.parent if args.output else PLATFORM / 'test-results') / f'cloud-browser-{args.adapter}-trace.png'), full_page=True)
                page.locator('#trace-dialog .dialog-close').click()
                page.screenshot(path=str((args.output.parent if args.output else PLATFORM / 'test-results') / f'cloud-browser-{args.adapter}-submit.png'), full_page=True)
            else:
                assert batch['catalog_acceptance'] is True and batch['agent_budget_seconds'] == candidate_agent_budget(candidate)
                assert not batch.get('public_runs')
                assert team.request.get(args.url + f'/api/batches/{batch["id"]}/traces/0').status == 404
                assert selected_task not in json.dumps(batch)
                page.reload()
                expect(page.locator('#batch-list')).to_contain_text(f'限时单题验收（{candidate_agent_budget(candidate)} s）')
                expect(page.locator('#batch-list')).to_contain_text('完成', timeout=20000)
                assert page.locator(f'[data-trace="{batch["id"]}"]').count() == 0
                page.screenshot(path=str(args.output.with_suffix('.result.png')), full_page=True)
                report['own_hidden_trace_denied'] = True
                report['result_visible_in_browser'] = True
            me = team.request.get(args.url + '/api/me').json()
                assert me['quotas'] == {'full': 1, 'public': 3, 'infra': 9}
            board = team.request.get(args.url + '/api/leaderboard', headers={'X-QIQC-Workspace': 'team'}).json()
            assert not any(row['team_id'] == me['team_id'] for row in board['teams'])
            assert admin.request.get(args.url + f'/api/batches/{batch["id"]}/traces/0').status == 404
            with sqlite3.connect((args.data / 'platform.db').as_uri() + '?mode=ro', uri=True) as db:
                run_id, attempt, key, sha = db.execute('SELECT id,attempt,object_key,object_sha FROM runs WHERE batch_id=?', (batch['id'],)).fetchone()
                usages = db.execute('SELECT count(*) FROM usage WHERE run_id=? AND status=200', (run_id,)).fetchone()[0]
                active_tokens = db.execute('SELECT count(*) FROM run_tokens WHERE run_id=? AND revoked=0', (run_id,)).fetchone()[0]
            receipt = json.loads((args.data / 'scientific' / f'{run_id}_{attempt}.json').read_text())
            assert usages > 0 and active_tokens == 0
            expected_resources = 1 if candidate else 3
            assert len(receipt['resources']) == expected_resources and set(receipt['resources'].values()) == {'destroyed'}
            assert not (args.data / 'cache' / run_id / str(attempt)).exists()
            assert not errors
            report.update(status='passed', batch_id=batch['id'], score=batch['score'], standard_harbor_rollout=True,
                          model_usage_rows=usages, all_three_sandboxes_destroyed=candidate is None, all_execution_sandboxes_destroyed=True, execution_sandbox_count=expected_resources, active_run_tokens=0,
                          submission_sha256=version['sha256'],
                          r2_archive_sha256=sha, r2_index_written=True, public_trace_readback=args.catalog_index is None,
                          other_identity_trace_denied=True, quota_debits=1, nonranking=True,
                          local_rollout_cache_removed=True, javascript_errors=0)
            browser.close()
    except Exception as exc:
        report.update(failure_stage=stage, failure_type=type(exc).__name__)
    report['duration_seconds'] = round(time.monotonic() - started, 3)
    output = args.output or PLATFORM / 'test-results' / f'cloud-browser-{args.adapter}-20261020.json'
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2, ensure_ascii=False) + '\n')
    print(json.dumps(report, ensure_ascii=False))
    return 0 if report['status'] == 'passed' else 1


if __name__ == '__main__':
    raise SystemExit(main())
