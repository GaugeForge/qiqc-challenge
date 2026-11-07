"""Wrong ZIP then corrected ZIP in the same browser session, using real Go and Docker scan.
AI/science/archive are local fixtures. No production mutation or cloud runs.
"""
import argparse
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import urllib.request
import zipfile
from playwright.sync_api import sync_playwright, expect
from final_browser_acceptance import package, ROOT


def run(output, browser_name="chromium"):
    output.mkdir(parents=True, exist_ok=True)
    report = {'passed': False, 'checks': [], 'scope': __doc__, 'browser': browser_name}
    with tempfile.TemporaryDirectory(prefix='qiqc-correction-') as tmp:
        ready = Path(tmp) / 'ready.json'
        with (output / 'server.log').open('w') as log:
            proc = subprocess.Popen(['go','test','./internal/platform','-run','^TestFinalBrowserFixture$','-count=1','-timeout=9m'], cwd=ROOT/'platform-go', env={**os.environ,'QIQC_BROWSER_FIXTURE_READY':str(ready)}, stdout=log, stderr=subprocess.STDOUT)
            try:
                deadline = time.monotonic()+90
                while not ready.exists():
                    if proc.poll() is not None or time.monotonic()>deadline: raise RuntimeError('Fixture failed to start')
                    time.sleep(.2)
                cfg=json.loads(ready.read_text());base=cfg['url']
                with sync_playwright() as pw:
                    browser=getattr(pw,browser_name).launch(headless=True,**({'args':['--no-sandbox']} if browser_name=='chromium' else {}))
                    context=browser.new_context(extra_http_headers={'X-QIQC-Workspace':'team'})
                    page=context.new_page();errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
                    page.goto(base+'/submit')
                    page.locator('#email').fill('browser-team@example.test');page.locator('#password').fill('Initial-password-123')
                    page.locator('#login-form button[type=submit]').click()
                    for selector in ['#new-password','#confirm-password']:page.locator(selector).fill('Correction-password-123')
                    page.locator('#password-form button[type=submit]').click()
                    expect(page.locator('#upload-button')).to_be_enabled()
                    malformed=io.BytesIO()
                    with zipfile.ZipFile(malformed,'w') as z:
                        z.writestr('harness.json','{"runtime":"codex"}')
                        z.writestr('harness/README.md','Missing adapter entrypoint.')
                    keys=[]
                    for payload in [b'not a zip archive',malformed.getvalue()]:
                        page.locator('#zip-file').set_input_files({'name':'harness.zip','mimeType':'application/zip','buffer':payload})
                        with page.expect_response(lambda r:r.url.endswith('/api/submissions') and r.request.method=='POST') as response:
                            page.locator('#upload-button').click()
                        assert response.value.status==400
                        keys.append(response.value.request.headers['idempotency-key'])
                        expect(page.locator('#upload-feedback')).to_contain_text('Upload or admission failed')
                        assert context.request.get(base+'/api/versions').json()==[]
                        assert context.request.get(base+'/api/submissions').json()==[]
                        assert context.request.get(base+'/api/me').json()['quotas']['infra']==10
                        page.locator('#file-remove').click()
                        expect(page.locator('#upload-feedback')).to_have_text('No upload in progress.')
                    # Same name and metadata, different bytes: replacement must not inherit failure.
                    page.locator('#zip-file').set_input_files({'name':'harness.zip','mimeType':'application/zip','buffer':package()})
                    expect(page.locator('#upload-feedback')).to_contain_text('Ready to upload')
                    with page.expect_response(lambda r:r.url.endswith('/api/submissions') and r.request.method=='POST') as response:
                        page.locator('#upload-button').click()
                    assert response.value.status==200, response.value.text()
                    keys.append(response.value.request.headers['idempotency-key']);assert len(set(keys))==3
                    expect(page.locator('#upload-feedback')).to_contain_text('Upload received')
                    expect(page.locator('#batch-list')).to_contain_text('Completed',timeout=45000)
                    assert len(context.request.get(base+'/api/versions').json())==1
                    assert context.request.get(base+'/api/me').json()['quotas']['infra']==9
                    assert not errors,errors
                    report['checks']=['Malformed ZIP rejected without retained version, intent or quota debit','Invalid layout rejected without retaining admission state','Remove/reselect clears previous format feedback','Corrected same-name ZIP gets a fresh key, scans and completes five fixture tasks','Only one valid version and one quota debit']
                    page.screenshot(path=str(output/'corrected-upload.png'),full_page=True)
                    browser.close()
                report['passed']=True
            finally:
                if ready.exists() and proc.poll() is None:
                    cfg=json.loads(ready.read_text())
                    request=urllib.request.Request(cfg['url']+'/__fixture?phase=stop',data=b'',headers={'Authorization':'Bearer '+cfg['nonce']})
                    urllib.request.urlopen(request,timeout=5).close()
                try:proc.wait(timeout=15)
                except subprocess.TimeoutExpired:proc.kill();proc.wait()
                (output/'report.json').write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps(report))

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--output',type=Path,required=True);parser.add_argument('--browser',choices=['chromium','webkit'],default='chromium')
    args=parser.parse_args();run(args.output.resolve(),args.browser)
