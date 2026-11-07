"""Bounded browser retries only for an explicit, unaccepted upload response."""

RETRYABLE = {
    (429, '上传检查繁忙，请稍后重试；未扣次数'),
    (503, '隔离扫描服务暂不可用；未扣次数'),
    (503, '隔离扫描服务不可用；未扣次数'),
}


def unaccepted_upload_busy(status, payload):
    return (isinstance(payload, dict) and isinstance(payload.get('error'), str)
            and (status, payload['error']) in RETRYABLE)


def upload_with_admission_retry(page, team, url, report, expect, *, max_attempts=3):
    if type(max_attempts) is not int or not 1 <= max_attempts <= 6:
        raise ValueError("upload max_attempts must be an integer from 1 to 6")
    before_versions = team.request.get(url + '/api/versions').json()
    before_quotas = team.request.get(url + '/api/me').json()['quotas']
    history = report.setdefault('upload_admission', [])
    for attempt in range(1, max_attempts + 1):
        with page.expect_response(lambda r: r.url == url + '/api/versions'
                                  and r.request.method == 'POST', timeout=60000) as pending:
            page.locator('#upload-button').click()
        response = pending.value
        payload = response.json()
        retryable = unaccepted_upload_busy(response.status, payload)
        history.append({'attempt': attempt, 'http_status': response.status,
                        'explicit_unaccepted_busy': retryable,
                        'public_error': payload.get('error') if isinstance(payload, dict) else None})
        if response.status == 200:
            return
        if not retryable:
            raise AssertionError('upload response was not accepted or explicitly retryable')
        expect(page.locator('#toast')).to_be_visible()
        expect(page.locator('#toast')).to_have_text(payload['error'])
        expect(page.locator('#upload-button')).to_be_enabled()
        assert team.request.get(url + '/api/versions').json() == before_versions
        assert team.request.get(url + '/api/me').json()['quotas'] == before_quotas
        history[-1].update(error_visible=True, upload_control_restored=True,
                           no_version_created=True, quotas_unchanged=True)
        if attempt == max_attempts:
            raise AssertionError('upload remained explicitly busy after bounded retries')
        page.wait_for_timeout(3000 * attempt)
