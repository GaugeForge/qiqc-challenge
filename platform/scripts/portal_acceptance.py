#!/usr/bin/env python3
"""Independent bilingual staff console and competition portal acceptance."""
import argparse
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import tempfile
from playwright.sync_api import sync_playwright, expect

PLATFORM=Path(__file__).resolve().parents[1];SITE=PLATFORM.parent
sys.path.insert(0,str(PLATFORM))
from isolated_e2e import port,ready


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--go-binary',type=Path,default=Path('/tmp/qiqc-release-api'))
    args=parser.parse_args()
    output=PLATFORM/'test-results/competition-portal-20261021';output.mkdir(parents=True,exist_ok=True)
    report={'status':'running','cloud_calls':False,'checks':[],'untranslated':[]};errors=[]
    with tempfile.TemporaryDirectory(prefix='qiqc-portal-') as tmp:
        root=Path(tmp);os.environ['QIQC_DATA']=str(root/'data');os.environ['QIQC_R2_ENABLED']='0'
        from qiqc.core import Engine,Settings
        e=Engine(Settings(data_dir=root/'data',scan_docker=False));secret=secrets.token_urlsafe(24)
        e.create_account('staff@portal.test',secret,'Portal staff',role='admin',must_change=False)
        e.create_account('team@portal.test',secret,'Overview',must_change=False)
        base='http://127.0.0.1:'+str(port());env={k:v for k,v in os.environ.items() if k in ['PATH','HOME','LANG','LC_ALL']}
        env.update(QIQC_DATA=str(root/'data'),QIQC_R2_ENABLED='0',QIQC_COMPETITION_PORTAL='1')
        with (root/'private.log').open('w') as log:
            proc=subprocess.Popen([str(args.go_binary),'-standalone','-listen',base[7:],'-data',str(root/'data'),'-site',str(SITE/'dist'),'-web',str(PLATFORM/'web'),'-adapters',str(SITE/'adapters')],env=env,stdout=log,stderr=subprocess.STDOUT)
            try:
                ready(base,proc)
                with sync_playwright() as pw:
                    for engine in ['chromium','webkit']:
                        browser=getattr(pw,engine).launch(**({'args':['--no-sandbox']} if engine=='chromium' else {}));context=browser.new_context(viewport={'width':1440,'height':1000})
                        page=context.new_page();page.on('pageerror',lambda err:errors.append(str(err)))
                        page.goto(base+'/admin');expect(page.locator('.staff-header')).to_be_visible();assert page.locator('.site-header').count()==0
                        assert context.request.get(base+'/api/admin',headers={'X-QIQC-Workspace':'staff'}).status==401
                        page.locator('[data-locale=zh]').click();expect(page.locator('h1')).to_have_text('工作人员后台')
                        page.reload();expect(page.locator('html')).to_have_attribute('lang','zh-CN');expect(page.locator('#login-form')).to_contain_text('邮箱地址')
                        page.locator('#email').fill('staff@portal.test');page.locator('#password').fill(secret);page.locator('#login-form button[type=submit]').click();expect(page.locator('h1')).to_have_text('赛事运营')
                        for locale in ['zh','en']:
                            page.locator(f'[data-locale={locale}]').click()
                            for width in [1440,1024,390]:
                                page.set_viewport_size({'width':width,'height':1000})
                                for tab in ['overview','reviews','evaluations','teams','support','announcements','operations','audit']:
                                    page.locator(f'[data-staff-tab={tab}]').click();page.wait_for_timeout(40)
                                    expect(page.locator('#staff-section-'+tab)).to_be_visible()
                                    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1'),(engine,locale,width,tab)
                                    assert 'demo' not in page.locator('body').inner_text().lower(),(engine,tab)
                                    if locale=='zh' and width==1440:
                                        missing=page.locator('#staff-section-'+tab).evaluate('''root=>[...root.querySelectorAll('h2,h3,label>span,button,summary,th,p.help')].filter(e=>e.getBoundingClientRect().width>0).map(e=>e.textContent.trim()).filter(t=>/[a-zA-Z]{4}/.test(t)&&!/[\u4e00-\u9fff]/.test(t))''')
                                        report['untranslated']+=missing
                                    if width in [1440,390]:
                                        page.evaluate("scrollTo({top:0,behavior:'instant'})");page.screenshot(path=str(output/f'{engine}-{locale}-{tab}-{width}.png'))
                                    report['checks'].append(f'{engine}/{locale}/{width}/{tab}')
                        # Language changes must not replace form nodes or values.
                        page.locator('[data-staff-tab=teams]').click();page.locator('#admin-reason').fill('Bilingual GUI acceptance')
                        page.locator('#account-email').fill(engine+'-created@portal.test');page.locator('#account-team').fill('Support')
                        page.locator('#account-password').fill(secrets.token_urlsafe(24));page.locator('[data-locale=zh]').click()
                        expect(page.locator('#account-email')).to_have_value(engine+'-created@portal.test');expect(page.locator('#account-team')).to_have_value('Support')
                        page.locator('#account-form button[type=submit]').click();expect(page.locator('#account-issued')).to_be_visible()
                        page.locator('#staff-team-search').fill('Overview');expect(page.locator('#staff-team-list')).to_contain_text('Overview')
                        page.locator('#logout').click();expect(page.locator('#login-form')).to_be_visible();assert context.request.get(base+'/api/admin',headers={'X-QIQC-Workspace':'staff'}).status==401
                        page.goto(base+'/submit');expect(page.locator('#login-form')).to_be_visible();assert page.locator('[data-locale]').count()==0;expect(page.locator('html')).to_have_attribute('lang','en')
                        page.locator('#email').fill('team@portal.test');page.locator('#password').fill(secret);page.locator('#login-form button[type=submit]').click();expect(page.locator('#zip-file')).to_be_visible()
                        assert page.locator('#mode-select option').count()==1;expect(page.locator('#mode-select')).to_have_value('scientific');assert context.request.get(base+'/api/admin',headers={'X-QIQC-Workspace':'team'}).status==403
                        board=context.request.get(base+'/api/leaderboard', headers={'X-QIQC-Workspace': 'team'}).json();assert not board['teams'] and board['baseline']['passed'] is None
                        for path in ['/','/documents.html','/leaderboard','/submit']:
                            for width in [1440,1024,390]:
                                page.set_viewport_size({'width':width,'height':1000})
                                page.goto(base+path);expect(page.locator('h1')).to_be_visible()
                                assert 'demo' not in page.locator('body').inner_text().lower(),path
                                assert page.locator('#rabi-canvas').count()==(1 if path=='/' else 0),path
                                assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1'),(engine,path,width)
                                page.screenshot(path=str(output/f'{engine}-public-{path.strip("/") or "home"}-{width}.png'))
                        context.close();browser.close()
                assert not errors,errors
                assert not report["untranslated"],report["untranslated"]
                report.update(status='passed',javascript_errors=0)
            finally:
                proc.terminate();proc.wait(timeout=20)
                report['untranslated']=sorted(set(report['untranslated']))
                report['errors']=errors
                (output/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps({'status':report['status'],'states':len(report['checks']),'untranslated':report['untranslated']},ensure_ascii=False))


if __name__=='__main__':main()
