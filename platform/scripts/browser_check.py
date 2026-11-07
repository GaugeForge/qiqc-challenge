"""Local end-to-end browser acceptance; no email, push, or cloud resources."""
import io
import json
import secrets
import time
import zipfile
from pathlib import Path
from playwright.sync_api import sync_playwright, expect

BASE='http://127.0.0.1:8765'
ROOT=Path(__file__).resolve().parents[2]
OUT=ROOT/'platform/test-results';OUT.mkdir(exist_ok=True)
CREDS=Path('/root/QQ-Challenge/runtime/legacy-platform-data/credentials.json')
accounts=json.loads(CREDS.read_text())
errors=[];checks=[]


def login(page,email,path='/submit'):
    page.goto(BASE+path)
    page.locator('#email').fill(email)
    page.locator('#password').fill(accounts[email]['password'])
    page.locator('#login-form button[type=submit]').click()
    page.locator("#password-form, #logout:not([hidden])").first.wait_for(state="visible")
    if page.locator('#new-password').count():
        password=secrets.token_urlsafe(20)
        page.locator('#new-password').fill(_pw:=password);page.locator('#confirm-password').fill(_pw)
        page.locator('#password-form button[type=submit]').click()
        page.wait_for_selector('#new-password',state='detached')
        accounts[email]['password']=password
        accounts[email]['first_login_change_required']=False
        CREDS.write_text(json.dumps(accounts,ensure_ascii=False,indent=2));CREDS.chmod(0o600)


def wait_batch(page,kind,timeout=90):
    deadline=time.monotonic()+timeout
    while time.monotonic()<deadline:
        values=page.request.get(BASE+'/api/batches').json()
        target=next((r for r in values if r['kind']==kind),None)
        if target and target['status']=='completed': return target
        page.wait_for_timeout(700)
    raise AssertionError(f'{kind} evaluation did not complete')


def package():
    data=io.BytesIO()
    with zipfile.ZipFile(data,'w',zipfile.ZIP_DEFLATED) as z:
        z.writestr('harness.json',json.dumps({'schema':'qiqc-harness/v1-demo','name':'Browser review fixture','entrypoint':'run.sh'}))
        z.writestr('run.sh','#!/bin/sh\n# static fixture: https://example.test/endpoint\necho demo\n')
    return data.getvalue()


with sync_playwright() as p:
    browser=p.chromium.launch(headless=True,args=['--no-sandbox'])
    team=browser.new_context(viewport={'width':1440,'height':1000},device_scale_factor=1)
    page=team.new_page();page.on('pageerror',lambda error:errors.append(str(error)))
    page.goto(BASE+'/');page.get_by_role('link',name='Submit',exact=True).wait_for();checks.append('Existing homepage links to local platform')
    login(page,'demo@qiqc.local')
    page.wait_for_selector('#quota-stats .stat')
    # Wait for the already accepted real reference evaluation to release the team lock.
    reference=wait_batch(page,'infra',timeout=240)
    assert reference['mode']=='codex' and reference['score']['passed']==1,reference
    checks.append('Real Codex + Harbor mock result visible in team UI')
    ref=page.request.get(BASE+'/api/reference.zip').body()
    page.locator('#zip-file').set_input_files({'name':'harness.zip','mimeType':'application/zip','buffer':ref})
    page.locator('#upload-button').click()
    expect(page.locator("#version-select")).not_to_have_value("")
    page.locator('input[name=kind][value="full"]').check()
    page.locator('#prepare-evaluation').click()
    page.locator('#confirm-dialog').wait_for(state='visible')
    assert '取消不会退回' in page.locator('#confirm-dialog').inner_text()
    page.locator('#confirm-evaluate').click()
    page.locator('#confirm-dialog').wait_for(state='hidden')
    full=wait_batch(page,'full')
    assert full['score']['total']==39 and len(full['public_runs'])==0
    page.wait_for_timeout(2800)
    page.screenshot(path=str(OUT/'submit-desktop.png'),full_page=True)
    page.locator(f'[data-trace="{full["id"]}"][data-ordinal="0"]').click()
    page.locator('#trace-dialog').wait_for(state='visible')
    assert '模拟执行' in page.locator('#trace-content').inner_text()
    page.locator('#trace-dialog .dialog-close').click()
    assert page.request.get(BASE+f'/api/batches/{full["id"]}/traces/5').status==404
    checks.append('Upload → review → confirm → 39 simulated tasks → private public trace; hidden trace denied')
    page.locator('#zip-file').set_input_files({'name':'harness.zip','mimeType':'application/zip','buffer':package()})
    page.locator('#upload-button').click()
    page.get_by_text('Browser review fixture',exact=True).wait_for()
    page.get_by_text('待人工复核',exact=True).wait_for()
    checks.append('Modified package routes to manual review')

    admin=browser.new_context(viewport={'width':1440,'height':1050})
    ap=admin.new_page();ap.on('pageerror',lambda error:errors.append(str(error)))
    login(ap,'admin@qiqc.local','/admin')
    ap.wait_for_selector('#admin-stats .stat')
    ap.locator('#admin-reason').fill('已只读检查样例源码；批准本地模拟运行')
    ap.locator('[data-review-detail]').first.click()
    assert '外部网络地址' in ap.locator('#detail-content').inner_text()
    ap.locator('#detail-dialog .dialog-close').click()
    ap.locator('[data-approve]').first.click()
    ap.locator("[data-approve]").wait_for(state="detached")
    checks.append('Admin inspects risk and approves with recorded reason')
    ap.locator('#admin-reason').fill('浏览器验收：暂停新派发以检查取消流程')
    ap.locator('#pause-button').click()
    ap.get_by_role('button',name='恢复新派发 →').wait_for()
    # Queue a public batch, cancel it, confirm quota stays consumed.
    page.wait_for_timeout(2800)
    page.locator('input[name=kind][value="public"]').check()
    page.locator('#prepare-evaluation').click();page.locator('#confirm-evaluate').click()
    page.locator('#confirm-dialog').wait_for(state='hidden')
    page.wait_for_selector('[data-cancel]')
    page.locator('[data-cancel]').first.click()
    page.get_by_text('已取消',exact=True).wait_for()
    assert page.request.get(BASE+'/api/me').json()['quotas']['public']==2
    ap.locator('#admin-reason').fill('浏览器验收完成，恢复正常派发')
    ap.locator('#pause-button').click();ap.get_by_role('button',name='暂停新派发').wait_for()
    checks.append('Pause → queue → cancel; attempt stays consumed; resume dispatch')
    ap.screenshot(path=str(OUT/'admin-desktop.png'),full_page=True)

    board=browser.new_page(viewport={'width':1440,'height':1000})
    board.on('pageerror',lambda error:errors.append(str(error)))
    board.goto(BASE+'/leaderboard');board.get_by_text('Quantum Pioneers',exact=True).wait_for()
    board.screenshot(path=str(OUT/'leaderboard-desktop.png'),full_page=True)
    assert 'SIMULATED' in board.inner_text('body')
    assert 'demo@qiqc.local' not in board.inner_text('body')
    checks.append('Anonymous leaderboard exposes aggregate simulated score only')
    page.set_viewport_size({'width':390,'height':844});page.goto(BASE+'/submit');page.wait_for_selector('#quota-stats .stat')
    assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth'), 'Mobile horizontal overflow'
    page.screenshot(path=str(OUT/'submit-mobile.png'),full_page=True)
    checks.append('Mobile viewport has no document overflow')
    checks.append('No browser JavaScript exceptions')
    assert errors==[],errors
    (OUT/'browser-results.json').write_text(json.dumps({'passed':checks,'errors':errors,'real_reference_batch':reference['id'],'simulated_full_batch':full['id']},ensure_ascii=False,indent=2))
    browser.close()
print(json.dumps({'passed':len(checks),'errors':errors,'screenshots':str(OUT)},ensure_ascii=False))
