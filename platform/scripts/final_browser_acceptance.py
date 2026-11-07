"""Repeatable local final-flow acceptance; no cloud resources or participant code execution.
Real: browsers, HTTP, SQLite, Docker static scanner, review parser, admission, archive state.
Fixtures: review provider response, scientific executor/verifier, object storage.
"""

from pathlib import Path
import argparse
import io
import json
import os
import subprocess
import tempfile
import time
import urllib.request
import zipfile
from playwright.sync_api import sync_playwright, expect

ROOT = Path(__file__).resolve().parents[2]


def package():
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        z.writestr(
            "harness.json",
            json.dumps(
                # Minimal participant manifest; the scanner derives the rest.
                {"runtime": "codex", "name": "Browser acceptance fixture"}
            ),
        )
        z.writestr("adapter/run.sh", "# Fixture is scanned only, never executed.\n")
        z.writestr("harness/README.md", "Synthetic acceptance input.\n")
    return out.getvalue()


def run(output):
    output.mkdir(parents=True, exist_ok=True)
    checks = []
    errors = []
    with tempfile.TemporaryDirectory(prefix="qiqc-browser-final-") as tmp:
        ready = Path(tmp) / "ready.json"
        env = dict(os.environ, QIQC_BROWSER_FIXTURE_READY=str(ready))
        with (output / "server.log").open("w") as log:
            proc = subprocess.Popen(
                [
                    "go",
                    "test",
                    "./internal/platform",
                    "-run",
                    "^TestFinalBrowserFixture$",
                    "-count=1",
                    "-timeout=9m",
                ],
                cwd=ROOT / "platform-go",
                env=env,
                stdout=log,
                stderr=subprocess.STDOUT,
            )
            try:
                deadline = time.monotonic() + 90
                while not ready.exists():
                    if proc.poll() is not None or time.monotonic() > deadline:
                        raise RuntimeError("fixture startup failed; see server.log")
                    time.sleep(0.2)
                cfg = json.loads(ready.read_text())
                base = cfg["url"]

                def phase(name):
                    req = urllib.request.Request(
                        base + "/__fixture?phase=" + name,
                        data=b"",
                        headers={"Authorization": "Bearer " + cfg["nonce"]},
                        method="POST",
                    )
                    with urllib.request.urlopen(req, timeout=10) as response:
                        assert response.status == 200

                with sync_playwright() as pw:
                    browser = pw.chromium.launch(headless=True, args=["--no-sandbox"])
                    ctx = browser.new_context(
                        viewport={"width": 1440, "height": 1000},
                        extra_http_headers={"X-QIQC-Workspace": "team"},
                    )
                    page = ctx.new_page()
                    page.on("pageerror", lambda e: errors.append(str(e)))
                    page.goto(base + "/submit")
                    expect(page.locator("#login-form")).to_be_visible()
                    page.locator("#email").fill("browser-team@example.test")
                    page.locator("#password").fill("Initial-password-123")
                    page.locator("#login-form button[type=submit]").click()
                    page.locator("#new-password").fill(_pw:="Browser-replacement-123");page.locator("#confirm-password").fill(_pw)
                    page.locator("#password-form button[type=submit]").click()
                    page.locator("#zip-file").wait_for()
                    expect(page.locator("#upload-button")).to_be_enabled()
                    assert page.locator("#prepare-evaluation").is_hidden()
                    checks.append(
                        "Chromium: sign in, mandatory password change, one upload action"
                    )
                    payload = package()

                    def upload(kind, total):
                        page.locator(f'input[name=kind][value="{kind}"]').check()
                        page.locator("#zip-file").set_input_files(
                            {
                                "name": "harness.zip",
                                "mimeType": "application/zip",
                                "buffer": payload,
                            }
                        )
                        with page.expect_response(
                            lambda r: (
                                r.url.endswith("/api/submissions")
                                and r.request.method == "POST"
                            )
                        ) as submitted:
                            page.locator("#upload-button").click()
                        assert submitted.value.status == 200, submitted.value.text()
                        intent = submitted.value.json()
                        deadline = time.monotonic() + 90
                        while time.monotonic() < deadline:
                            rows = ctx.request.get(base + "/api/batches").json()
                            matches = [
                                b
                                for b in rows
                                if b["kind"] == kind and b["status"] == "completed"
                            ]
                            if matches:
                                b = matches[0]
                                assert b["score"]["total"] == total
                                expect(page.locator("#batch-list")).to_contain_text(
                                    "Completed", timeout=15000
                                )
                                return intent, b
                            time.sleep(0.4)
                        raise AssertionError("automatic evaluation did not complete")

                    intent, first = upload("infra", 5)
                    assert (
                        ctx.request.get(base + "/api/leaderboard", headers={'X-QIQC-Workspace': 'team'}).json()["teams"] == []
                    )
                    page.reload()
                    expect(page.locator("#submission-status")).to_contain_text(
                        "Completed"
                    )
                    checks.append(
                        "ZIP -> Docker scanner -> automatic review -> five tasks -> archive -> results; reload recovery; no practice ranking"
                    )
                    page.screenshot(
                        path=str(output / "submit-desktop.png"), full_page=True
                    )
                    ctx.set_offline(True)
                    page.wait_for_timeout(2500)
                    ctx.set_offline(False)
                    expect(page.locator("#upload-button")).to_be_enabled(timeout=15000)
                    checks.append("Browser network loss and reconnection")
                    admin = browser.new_context(
                        extra_http_headers={"X-QIQC-Workspace": "staff"}
                    )
                    staff = admin.new_page()
                    staff.on("pageerror", lambda e: errors.append(str(e)))
                    staff.goto(base + "/admin")
                    staff.locator("#email").fill("browser-staff@example.test")
                    staff.locator("#password").fill("Initial-password-123")
                    staff.locator("#login-form button[type=submit]").click()
                    staff.locator("#new-password").fill(_pw:="Staff-replacement-123");staff.locator("#confirm-password").fill(_pw)
                    staff.locator("#password-form button[type=submit]").click()
                    staff.locator('[data-staff-tab="reviews"]').click()
                    with staff.expect_download() as downloaded:
                        staff.locator("[data-review-download]").first.click()
                    downloaded.value.save_as(str(output / "review-report.json"))
                    assert (
                        ctx.request.get(
                            base + "/api/admin/review-reports/" + intent["version_id"]
                        ).status
                        == 403
                    )
                    checks.append(
                        "Staff-only review report download; participant access denied"
                    )
                    # All staff sections, both languages, and desktop/tablet/phone layouts.
                    for locale in ["zh", "en"]:
                        staff.locator(f'[data-locale="{locale}"]').click()
                        for width in [1440, 1024, 768, 390, 320]:
                            staff.set_viewport_size({"width": width, "height": 1000})
                            for tab in ["overview", "reviews", "evaluations", "teams", "support", "announcements", "operations", "audit"]:
                                staff.locator(f'[data-staff-tab="{tab}"]').click()
                                expect(staff.locator(f'#staff-section-{tab}')).to_be_visible()
                                assert staff.evaluate("document.documentElement.scrollWidth <= innerWidth + 1"), (locale, width, tab)
                    staff.set_viewport_size({"width": 1440, "height": 1000})
                    staff.locator('[data-staff-tab="teams"]').click()
                    staff.locator('#account-reason').fill('Browser acceptance account provisioning')
                    staff.locator('#account-email').fill('new-team@example.test')
                    staff.locator('#account-team').fill('New browser team')
                    initial_password = staff.locator('#account-password').input_value()
                    staff.locator('#account-form button[type="submit"], #account-form > button').click()
                    expect(staff.locator('#account-issued')).to_be_visible()
                    with staff.expect_download() as receipt:
                        staff.locator('#accounts-download').click()
                    assert receipt.value.suggested_filename.endswith('.csv')
                    staff.locator('#accounts-clear').click()
                    staff.locator('[data-account-mode="bulk"]').click()
                    staff.locator('#accounts-csv').fill('email,team\nbulk-one@example.test,Bulk One\nbulk-two@example.test,Bulk Two')
                    staff.locator('#accounts-preview').click()
                    expect(staff.locator('#accounts-count')).to_contain_text('2 accounts')
                    staff.locator('#accounts-create').click()
                    expect(staff.locator('#account-issued')).to_be_visible()
                    with staff.expect_download():
                        staff.locator('#accounts-download').click()
                    staff.locator('#accounts-clear').click()
                    staff.locator('#accounts-csv').fill('email,team\nbulk-uncertain@example.test,Receipt Lost Team')
                    staff.locator('#accounts-preview').click()
                    def lose_account_receipt(route):
                        response = route.fetch()
                        assert response.status == 200
                        route.abort('failed')
                    staff.route('**/api/admin/accounts/bulk', lose_account_receipt, times=1)
                    staff.locator('#accounts-create').click()
                    expect(staff.locator('#account-issued-title')).to_have_text('Creation not confirmed', timeout=15000)
                    with staff.expect_download():
                        staff.locator('#accounts-download').click()
                    checks.append('Account transaction receipt lost: prepared credentials remain downloadable and result is explicitly unconfirmed')
                    directory = admin.request.get(base + '/api/admin').json()['accounts']
                    assert all(any(row['email'] == email for row in directory) for email in ['new-team@example.test','bulk-one@example.test','bulk-two@example.test','bulk-uncertain@example.test'])
                    separate = browser.new_context(extra_http_headers={"X-QIQC-Workspace": "team"})
                    issued_login = separate.request.post(base + '/api/login', data={'email':'new-team@example.test','password':initial_password})
                    assert issued_login.status == 200 and issued_login.json()['must_change']
                    separate.close()
                    checks.append('Staff single and bulk account creation, credential download, issued account first-login protection; 80 bilingual responsive section checks')
                    # A committed upload with a lost response must retain the same retry identity.
                    page.locator('input[name=kind][value="infra"]').check()
                    page.locator('#zip-file').set_input_files({'name':'harness.zip','mimeType':'application/zip','buffer':payload})
                    previous_batches = ctx.request.get(base + '/api/batches').json()
                    before_ids = {b['id'] for b in previous_batches}
                    before_batches = len(previous_batches)
                    before_quota = ctx.request.get(base + '/api/me').json()['quotas']['infra']
                    def lose_receipt(route):
                        if route.request.method != 'POST':
                            route.continue_()
                            return
                        assert page.locator('input[name=kind]:checked').is_disabled()
                        assert page.locator('#zip-file').is_disabled()
                        response = route.fetch()
                        assert response.status == 200
                        route.abort('failed')
                    page.route('**/api/submissions', lose_receipt)
                    page.locator('#upload-button').click()
                    expect(page.locator('#upload-feedback')).to_contain_text('Upload receipt not confirmed',timeout=15000)
                    page.unroute('**/api/submissions', lose_receipt)
                    deadline = time.monotonic() + 30
                    while time.monotonic() < deadline:
                        new_rows = [b for b in ctx.request.get(base + '/api/batches').json() if b['id'] not in before_ids]
                        if len(new_rows) == 1 and new_rows[0]['status'] == 'completed':
                            break
                        time.sleep(.2)
                    else:
                        raise AssertionError('Receipt-loss evaluation did not settle before changing fixture clock')
                    page.wait_for_function('batches.length > 1 && batches.every(b => b.status === "completed")',timeout=30000)
                    expect(page.locator('#upload-button')).to_be_enabled(timeout=30000)
                    with page.expect_response(lambda r:r.url.endswith('/api/submissions') and r.request.method=='POST') as retried:
                        page.locator('#upload-button').click()
                    assert retried.value.status == 200
                    expect(page.locator('#upload-feedback')).to_contain_text('Upload received',timeout=15000)
                    assert len(ctx.request.get(base + '/api/batches').json()) == before_batches + 1
                    assert ctx.request.get(base + '/api/me').json()['quotas']['infra'] == before_quota - 1
                    checks.append('Lost upload receipt after server commit: same-file retry creates no duplicate batch or quota debit')
                    staff.locator('[data-staff-tab="reviews"]').click()
                    staff.locator('#staff-review-search').fill(intent['sha256'])
                    expect(staff.locator('[data-history-page="versions"]')).to_contain_text('1 records',timeout=15000)
                    staff.locator('[data-locale="zh"]').click()
                    expect(staff.locator('[data-review-download]').first).to_have_text('下载审查报告')
                    staff.locator('[data-locale="en"]').click()
                    expect(staff.locator('[data-review-download]').first).to_have_text('Download review report')
                    checks.append('Server-backed submission search and immediate report-button language switching')
                    phase("quiet")
                    page.reload()
                    expect(page.locator("#upload-button")).to_be_disabled()
                    expect(page.locator("#phase-notice")).to_contain_text("quiet")
                    rejected = ctx.request.post(
                        base + "/api/submissions",
                        headers={
                            "X-QIQC-Request": "1",
                            "X-QIQC-Evaluation": "infra",
                            "Idempotency-Key": "quiet-fixture",
                        },
                        data=payload,
                    )
                    assert rejected.status == 409
                    checks.append(
                        "Quiet boundary: browser and backend both close admission"
                    )
                    phase("competition")
                    page.reload()
                    expect(page.locator("#upload-button")).to_be_enabled()
                    spectator = ctx.new_page()
                    spectator.goto(base + "/leaderboard")
                    expect(spectator.locator("#leader-table")).to_contain_text(
                        "No official results"
                    )
                    _, full = upload("full", 39)
                    expect(spectator.locator("#leader-table")).to_contain_text(
                        "Test team", timeout=15000
                    )
                    board = ctx.request.get(base + "/api/leaderboard", headers={'X-QIQC-Workspace': 'team'}).json()
                    assert len(board["teams"]) == 1 and board["teams"][0]["passed"] == 0
                    page.goto(base + "/leaderboard")
                    expect(page.locator("#leader-table")).to_contain_text("Test team")
                    assert (
                        ctx.request.get(
                            base + f"/api/batches/{full['id']}/traces/1"
                        ).status
                        == 404
                    )
                    checks.append(
                        "Formal full39 first result ranks immediately; hidden trace denied"
                    )
                    page.screenshot(
                        path=str(output / "leaderboard-desktop.png"), full_page=True
                    )
                    phase("closed")
                    page.goto(base + "/submit")
                    expect(page.locator("#upload-button")).to_be_disabled()
                    expect(page.locator("#phase-notice")).to_contain_text("closed")
                    checks.append("Deadline closes uploads; results stay readable")
                    for size in [(1440, 1000), (390, 844)]:
                        page.set_viewport_size({"width": size[0], "height": size[1]})
                        assert page.evaluate(
                            "document.documentElement.scrollWidth <= innerWidth + 1"
                        )
                    page.screenshot(
                        path=str(output / "submit-mobile.png"), full_page=True
                    )
                    # Revoke only this fixture session while the page is still open.
                    assert ctx.request.post(base + '/api/logout',headers={'X-QIQC-Request':'1'}).status == 200
                    page.evaluate('refresh()')
                    expect(page.locator('#login-form')).to_be_visible(timeout=15000)
                    expect(page.locator('#logout')).to_be_hidden()
                    checks.append('Expired/revoked session returns to sign-in without exposing stale private results')
                    browser.close()
                    webkit = pw.webkit.launch(headless=True)
                    wc = webkit.new_context(
                        viewport={"width": 390, "height": 844},
                        extra_http_headers={"X-QIQC-Workspace": "team"},
                    )
                    wp = wc.new_page()
                    wp.on("pageerror", lambda e: errors.append(str(e)))
                    wp.goto(base + "/submit")
                    wp.locator("#email").fill("browser-team@example.test")
                    wp.locator("#password").fill("Browser-replacement-123")
                    wp.locator("#login-form button[type=submit]").click()
                    expect(wp.locator("#upload-button")).to_be_disabled()
                    expect(wp.locator("#submission-status")).to_contain_text(
                        "Completed"
                    )
                    assert wp.evaluate(
                        "document.documentElement.scrollWidth <= innerWidth + 1"
                    )
                    wp.screenshot(
                        path=str(output / "webkit-mobile.png"), full_page=True
                    )
                    webkit.close()
                    checks.append(
                        "Chromium desktop/mobile and WebKit mobile: persistent state and no horizontal overflow"
                    )
                assert not errors, errors
                phase("stop")
                assert proc.wait(timeout=30) == 0
            finally:
                if proc.poll() is None:
                    if ready.exists():
                        try:
                            cfg = json.loads(ready.read_text())
                            req = urllib.request.Request(
                                cfg["url"] + "/__fixture?phase=stop",
                                data=b"",
                                headers={"Authorization": "Bearer " + cfg["nonce"]},
                                method="POST",
                            )
                            urllib.request.urlopen(req, timeout=5).close()
                            proc.wait(timeout=10)
                        except Exception:
                            pass
                    if proc.poll() is None:
                        proc.terminate()
                    try:
                        proc.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        proc.wait()
    (output / "report.json").write_text(
        json.dumps(
            {
                "passed": True,
                "scope": "local end-to-end; external science and AI provider are fixtures",
                "checks": checks,
                "page_errors": errors,
            },
            indent=2,
        )
        + "\n"
    )
    print(json.dumps({"passed": True, "checks": len(checks), "output": str(output)}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    run(parser.parse_args().output.resolve())
