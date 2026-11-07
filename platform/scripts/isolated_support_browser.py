"""M11 browser checks against the disposable native Go acceptance environment."""
from playwright.sync_api import expect


def reveal_form(page, selector):
    form = page.locator(selector)
    form.wait_for(state='attached')
    if not form.is_visible():
        form.locator('xpath=ancestor::details[1]').locator('summary').click()
    expect(form).to_be_visible()


def run_support_browser(base, organizer, participant, admin_context, team_context,
                        batch, submission_sha):
    """Publish a local notice and resolve a private ticket; never sends mail."""
    organizer.reload()
    organizer.locator('#admin-stats .stat').first.wait_for(state='attached')
    organizer.locator('[data-staff-tab="announcements"]').click()
    organizer.locator('#admin-reason').fill('Isolated browser acceptance: publish operational notice')
    reveal_form(organizer, '#admin-announcement-form')
    organizer.locator('#announcement-version').fill('browser-notice-v1')
    organizer.locator('#announcement-title').fill('Browser acceptance notice')
    organizer.locator('#announcement-body').fill('Local rehearsal only. No scientific result or email delivery claim.')
    organizer.locator('#announcement-scope').select_option('public')
    expect(organizer.locator('#announcement-prd')).to_have_value(admin_context.request.get(base+'/api/config').json()['rule_version'])
    with organizer.expect_response(lambda r: r.url.endswith('/api/admin/announcements') and r.request.method == 'POST') as response:
        organizer.locator('#announcement-submit').click()
    assert response.value.status == 200
    notice = response.value.json()
    assert notice['time_zone'] == 'Asia/Singapore'
    assert notice['published_at_utc8'].endswith('+08:00')
    participant.reload()
    expect(participant.locator('#announcements-list')).to_contain_text('Browser acceptance notice')

    # Teams report problems by email; the workspace no longer shows tickets.
    # The ticket API remains, so create one directly as the team.
    expect(participant.locator('#support-create-form')).to_have_count(0)
    text = 'Browser privacy check <img src=x onerror="window.__supportXss=1">'
    response = team_context.request.post(base + '/api/support', headers={'X-QIQC-Request': '1', 'X-QIQC-Workspace': 'team'},
                                         data={'subject': 'Review my completed rehearsal', 'body': text, 'batch_id': batch['id'],
                                               'submission_sha': submission_sha, 'event_id': '', 'idempotency_key': 'browser-support-1'})
    assert response.status == 200
    ticket = response.json()
    assert ticket['batch_id'] == batch['id'] and ticket['submission_sha'] == submission_sha
    assert ticket['status'] == 'open'

    organizer.reload()
    organizer.locator('#admin-stats .stat').first.wait_for(state='attached')
    organizer.locator('[data-staff-tab="support"]').click()
    organizer.locator('#admin-reason').fill('Verified completed batch, resolve local rehearsal ticket')
    opener = organizer.locator(f'[data-support-open="{ticket["id"]}"]')
    opener.wait_for(state='visible')
    opener.click()
    expect(organizer.locator('#support-detail')).to_contain_text(text)
    assert organizer.locator('#support-detail img').count() == 0
    assert organizer.evaluate('typeof window.__supportXss') == 'undefined'
    organizer.locator('#support-reply-body').fill('Confirmed: batch completed and charged once. Rehearsal only.')
    organizer.locator('#support-reply-status').select_option('resolved')
    with organizer.expect_response(lambda r: r.url.endswith(f'/api/support/{ticket["id"]}/replies') and r.request.method == 'POST') as response:
        organizer.locator('#support-reply-submit').click()
    assert response.value.status == 200
    assert response.value.json()['status'] == 'resolved'
    saved = team_context.request.get(base + '/api/support/' + ticket['id'])
    assert saved.status == 200 and saved.json()['status'] == 'resolved'
    assert len(saved.json()['messages']) == 2
    assert admin_context.request.get(base + '/api/announcements/' + notice['id']).status == 200
    # A third context has no inherited session or cookies.
    anonymous = participant.context.browser.new_context()
    try:
        assert anonymous.request.get(base + '/api/announcements/' + notice['id']).status == 200
        assert anonymous.request.get(base + '/api/support/' + ticket['id']).status in (401, 403, 404)
    finally:
        anonymous.close()
    return ['Browser versioned public notice with UTC+8 and PRD reference',
            'Team ticket (API) -> administrator resolution in browser -> private reply, rendered as plain text']
