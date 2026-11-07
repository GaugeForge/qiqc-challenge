#!/usr/bin/env python3
"""Disposable Go/browser account provisioning acceptance; no mail or cloud calls."""
import argparse
import csv
import io
import json
import os
from pathlib import Path
import secrets
import sqlite3
import subprocess
import sys
import tempfile
from playwright.sync_api import sync_playwright, expect

PLATFORM = Path(__file__).resolve().parents[1]
SITE = PLATFORM.parent
sys.path.insert(0, str(PLATFORM))
from isolated_e2e import port, ready


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--go-binary', type=Path, required=True)
    args = parser.parse_args()
    output = PLATFORM / 'test-results/staff-accounts-20261021'
    output.mkdir(parents=True, exist_ok=True)
    report = {'status': 'running', 'checks': [], 'cloud_calls': False, 'production_accounts_created': 0}
    errors = []
    with tempfile.TemporaryDirectory(prefix='qiqc-accounts-') as tmp:
        root = Path(tmp)
        os.environ.update(QIQC_DATA=str(root/'data'), QIQC_R2_ENABLED='0')
        from qiqc.core import Engine, Settings
        engine = Engine(Settings(data_dir=root/'data', scan_docker=False))
        secret = secrets.token_urlsafe(24)
        engine.create_account('staff@accounts.test', secret, 'Test staff', role='admin', must_change=False)
        base = 'http://127.0.0.1:' + str(port())
        env = {k: v for k, v in os.environ.items() if k in ['PATH', 'HOME', 'LANG', 'LC_ALL']}
        env.update(QIQC_DATA=str(root/'data'), QIQC_R2_ENABLED='0', QIQC_COMPETITION_PORTAL='1')
        with (root/'private.log').open('w') as log:
            proc = subprocess.Popen([str(args.go_binary), '-standalone', '-listen', base[7:], '-data', str(root/'data'), '-site', str(SITE/'dist'), '-web', str(PLATFORM/'web'), '-adapters', str(SITE/'adapters')], env=env, stdout=log, stderr=subprocess.STDOUT)
            try:
                ready(base, proc)
                with sync_playwright() as pw:
                    for name in ['chromium', 'webkit']:
                        browser = getattr(pw, name).launch(**({'args': ['--no-sandbox']} if name == 'chromium' else {}))
                        ctx = browser.new_context(viewport={'width': 1440, 'height': 1000}, accept_downloads=True)
                        page = ctx.new_page()
                        page.on('pageerror', lambda err: errors.append(str(err)))
                        page.goto(base+'/admin')
                        page.locator('#email').fill('staff@accounts.test')
                        page.locator('#password').fill(secret)
                        page.locator('#login-form button[type=submit]').click()
                        page.locator('[data-account-shortcut=single]').click()
                        expect(page.locator('#account-form')).to_be_visible()
                        temporary = page.locator('#account-password').input_value()
                        assert len(temporary) >= 24
                        page.locator('#account-generate').click()
                        assert temporary != page.locator('#account-password').input_value()
                        page.locator('#account-email').fill(name+'-single@accounts.test')
                        page.locator('#account-team').fill('Single team')
                        page.locator('#account-password').fill('-Exact-temporary-password-123')
                        page.locator('#account-form button[type=submit]').click()
                        expect(page.locator('#account-issued')).to_be_visible()

                        def download_credentials():
                            with page.expect_download() as event:
                                page.locator('#accounts-download').click()
                            path = root / (name+'-private.csv')
                            event.value.save_as(path)
                            path.chmod(0o600)
                            return list(csv.DictReader(io.StringIO(path.read_text(encoding='utf-8-sig'))))

                        single = download_credentials()
                        assert len(single) == 1 and single[0]['email'] == name+'-single@accounts.test'
                        page.locator('[data-account-mode=bulk]').click()
                        data = f'email,team\r\n{name}-a@accounts.test,"Orion, East"\r\n{name}-b@accounts.test,量子团队\r\n{name}-c@accounts.test,"=SUM(1,2)"\r\n'
                        page.locator('#accounts-file').set_input_files({'name':'teams.csv','mimeType':'text/csv','buffer':data.encode()})
                        expect(page.locator('#accounts-csv')).to_have_value(data.replace('\r\n','\n'))
                        page.locator('#accounts-preview').click()
                        expect(page.locator('#accounts-preview-table tbody tr')).to_have_count(3)
                        page.screenshot(path=str(output/f'{name}-bulk-preview.png'))
                        with page.expect_response('**/api/admin/accounts/bulk') as result:
                            page.locator('#accounts-create').click()
                        assert result.value.status == 200
                        expect(page.locator('#account-issued-count')).to_have_text('3 accounts')
                        credentials = download_credentials()
                        assert len(credentials) == 3 and len({r['temporary_password'] for r in credentials}) == 3
                        assert credentials[2]['team'] == "'=SUM(1,2)"
                        assert all(r['sign_in_url'] == base+'/submit' for r in credentials)
                        for row in single+credentials:
                            assert row['temporary_password'] not in page.locator('body').inner_text()
                            assert row['temporary_password'] not in page.evaluate('JSON.stringify({...localStorage,...sessionStorage})')
                            assert row['temporary_password'] not in ctx.request.get(base+'/api/admin',headers={'X-QIQC-Workspace':'staff'}).text()
                        report['checks'].append(name+': single/bulk creation, private CSV, unique passwords, formula-safe export, no plaintext in DOM/storage/audit')
                        # Existing email in the last row must not create the first row.
                        page.locator('#accounts-csv').fill(f'email,team\n{name}-rollback@accounts.test,No partial team\n{name}-a@accounts.test,Existing')
                        page.locator('#accounts-preview').click()
                        with page.expect_response('**/api/admin/accounts/bulk') as response:
                            page.locator('#accounts-create').click()
                        assert response.value.status == 409
                        expect(page.locator('#accounts-error')).to_contain_text('already has an account')
                        db = sqlite3.connect(root/'data/platform.db')
                        assert db.execute('select count(*) from users where email=?',(name+'-rollback@accounts.test',)).fetchone()[0] == 0
                        db.close()
                        for invalid in [f'a@accounts.test,One\nA@accounts.test,Two', 'email,team\n"unfinished', '\n'.join(f'a{i}@accounts.test,Team' for i in range(201))]:
                            page.locator('#accounts-csv').fill(invalid)
                            page.locator('#accounts-preview').click()
                            expect(page.locator('#accounts-error')).to_be_visible()
                            expect(page.locator('#accounts-preview-panel')).to_be_hidden()
                        page.locator('#accounts-csv').fill(f'{name}-escaped@accounts.test,<img src=x onerror=alert(1)>')
                        page.locator('#accounts-preview').click()
                        assert page.locator('#accounts-preview-table img').count() == 0
                        expect(page.locator('#accounts-preview-table')).to_contain_text('<img src=x')
                        report['checks'].append(name+': all-or-nothing conflict, duplicate/malformed/201-row rejection, HTML escaped')
                        # Simulate a lost response AFTER the server commits the transaction.
                        page.locator('#accounts-csv').fill(f'{name}-uncertain@accounts.test,Uncertain receipt')
                        page.locator('#accounts-preview').click()
                        def lose_receipt(route):
                            response = route.fetch()
                            assert response.status == 200
                            route.abort('failed')
                        page.route('**/api/admin/accounts/bulk', lose_receipt)
                        page.locator('#accounts-create').click()
                        expect(page.locator('#account-issued-title')).to_have_text('Creation not confirmed')
                        uncertain = download_credentials()
                        assert uncertain[0]['email'] == name+'-uncertain@accounts.test'
                        page.unroute('**/api/admin/accounts/bulk', lose_receipt)
                        report['checks'].append(name+': lost response preserves credentials for reconciliation')
                        for locale in ['en', 'zh']:
                            page.locator(f'[data-locale={locale}]').click()
                            for width in [1440, 390]:
                                page.set_viewport_size({'width':width,'height':1000})
                                page.locator('[data-account-mode=single]').click()
                                page.evaluate("scrollTo({top:0,behavior:'instant'})")
                                assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1')
                                page.screenshot(path=str(output/f'{name}-{locale}-accounts-{width}.png'))
                        # Even staff signed into the participant URL get no staff navigation.
                        page.goto(base+'/submit')
                        expect(page.locator('#login-form')).to_be_visible()
                        expect(page.locator('#logout')).to_be_hidden()
                        assert page.locator('a[href="/admin"]').count() == 0
                        assert 'open staff console' not in page.locator('body').inner_text().lower()
                        page.goto(base+'/admin');page.locator('#logout').click()
                        # Fresh participants use exactly the downloaded temporary credentials.
                        for row in [single[0], credentials[0], uncertain[0]]:
                            team = browser.new_context()
                            p = team.new_page();p.goto(base+'/submit')
                            p.locator('#email').fill(row['email']);p.locator('#password').fill(row['temporary_password'])
                            p.locator('#login-form button[type=submit]').click()
                            expect(p.locator('#new-password')).to_be_visible()
                            assert team.request.get(base+'/api/admin',headers={'X-QIQC-Workspace':'team'}).status == 403
                            p.locator('#new-password').fill(_pw:=secrets.token_urlsafe(24));p.locator('#confirm-password').fill(_pw);p.locator('#password-form button[type=submit]').click()
                            expect(p.locator('#zip-file')).to_be_visible()
                            assert p.locator('a[href="/admin"]').count() == 0
                            assert team.request.get(base+'/api/admin',headers={'X-QIQC-Workspace':'team'}).status == 403
                            assert p.locator('.button.primary').first.evaluate('e=>getComputedStyle(e).backgroundColor') == 'rgb(36, 51, 61)'
                            team.close()
                        report['checks'].append(name+': downloaded credentials authenticate, forced change, role boundary, dark buttons, no staff link')
                        ctx.close();browser.close()
                assert not errors, errors
                report.update(status='passed', javascript_errors=[])
            finally:
                proc.terminate();proc.wait(timeout=20)
                report['javascript_errors'] = errors
                (output/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps(report,ensure_ascii=False))


if __name__ == '__main__':
    main()
