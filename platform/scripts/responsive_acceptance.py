#!/usr/bin/env python3
"""Real Chromium/WebKit layout checks against an isolated Go API and private DB.

No cloud jobs or shared accounts. Includes signed-in staff/team screens and long
account names. Linux WebKit coverage is not a claim of physical Mac testing.
"""
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import tempfile
import time
from playwright.sync_api import sync_playwright, expect

PLATFORM = Path(__file__).resolve().parents[1]
SITE = PLATFORM.parent
sys.path.insert(0, str(PLATFORM))
from isolated_e2e import port, ready


def inspect(page, label, output, report, screenshot=True):
    page.evaluate('document.fonts.ready')
    page.wait_for_timeout(120)
    metrics=page.evaluate('''() => {
      const visible=e=>e.getBoundingClientRect().width>0&&e.getBoundingClientRect().height>0;
      const links=[...document.querySelectorAll('.site-header .brand,.site-header nav a')].filter(visible);
      const overlaps=[];
      for(let i=0;i<links.length;i++)for(let j=i+1;j<links.length;j++){
        const a=links[i].getBoundingClientRect(),b=links[j].getBoundingClientRect();
        if(Math.min(a.right,b.right)-Math.max(a.left,b.left)>1&&Math.min(a.bottom,b.bottom)-Math.max(a.top,b.top)>1)overlaps.push([links[i].textContent,links[j].textContent]);
      }
      return {viewport:innerWidth,scroll:document.documentElement.scrollWidth,overlaps,
        controls:[...document.querySelectorAll('input:not([type=file]),select,textarea')].filter(visible).map(e=>({width:e.getBoundingClientRect().width,height:e.getBoundingClientRect().height,font:parseFloat(getComputedStyle(e).fontSize)})),
        h1:document.querySelector('h1')?.getBoundingClientRect().width};
    }''')
    assert metrics['scroll']<=metrics['viewport']+1, (label,metrics)
    assert not metrics['overlaps'], (label,metrics)
    assert metrics['h1'] and metrics['h1']>100, (label,metrics)
    assert all(c['width']>=120 and c['height']>=44 and c['font']>=16 for c in metrics['controls']), (label,metrics)
    report['checks'].append({'screen':label,**metrics})
    if screenshot:page.screenshot(path=str(output/(label+'.png')),full_page=('-staff-' in label or '-team-' in label))


def main():
    output=PLATFORM/'test-results/responsive-20261021'
    output.mkdir(parents=True,exist_ok=True)
    report={'status':'running','cloud_calls':False,'physical_mac_tested':False,'checks':[]}
    errors=[]
    proc=None
    with tempfile.TemporaryDirectory(prefix='qiqc-responsive-') as temporary:
        root=Path(temporary)
        os.environ['QIQC_DATA']=str(root/'data');os.environ['QIQC_R2_ENABLED']='0'
        from qiqc.core import Engine,Settings
        engine=Engine(Settings(data_dir=root/'data',scan_docker=False))
        password=secrets.token_urlsafe(24)
        engine.create_account('staff@layout.test',password,'Layout staff',role='admin',must_change=False)
        engine.create_account('team@layout.test',password,'Quantum Engineering · Independent Control Laboratory',must_change=False)
        for i in range(12):engine.create_account(f'participant-{i}@layout.test',password,f'Quantum control and verification research group number {i}',must_change=False)
        base='http://127.0.0.1:'+str(port())
        env={k:v for k,v in os.environ.items() if k in ['PATH','HOME','LANG','LC_ALL']}
        env.update(QIQC_R2_ENABLED='0',QIQC_DATA=str(root/'data'))
        try:
            with (root/'server.log').open('w') as log:
                proc=subprocess.Popen(['/opt/qiqc-native-candidate/bin/api','-standalone','-listen',base.removeprefix('http://'),'-data',str(root/'data'),'-site',str(SITE/'dist'),'-web',str(PLATFORM/'web'),'-adapters',str(SITE/'adapters'),'-reference',str(SITE/'adapters/reference.zip')],env=env,stdout=log,stderr=subprocess.STDOUT)
                ready(base,proc)
                with sync_playwright() as pw:
                    for browser_name in ['chromium','webkit']:
                        browser=getattr(pw,browser_name).launch(headless=True,**({'args':['--no-sandbox']} if browser_name=='chromium' else {}))
                        context=browser.new_context(viewport={'width':1440,'height':1000},device_scale_factor=2)
                        page=context.new_page();page.on('pageerror',lambda e: errors.append(str(e)))
                        for width in [1920,1440,1280,1024,768,390,375]:
                            page.set_viewport_size({'width':width,'height':900})
                            for name,path in [('home','/'),('documents','/documents.html'),('leaderboard','/leaderboard'),('login','/submit')]:
                                response=page.goto(base+path)
                                assert response.status in (200,304), (browser_name,width,path,response.status)
                                if name in ['leaderboard','login']:expect(page.locator('h1')).to_be_visible()
                                inspect(page,f'{browser_name}-{name}-{width}',output,report,width in [1440,390])
                                if name=='home':
                                    expect(page.locator('.entry-basics')).to_be_visible()
                                    assert not page.locator('.entry-basics').evaluate('e=>e.inert')
                                if name=='login' and width<=900:
                                    page.locator('#console-menu-toggle').click()
                                    expect(page.locator('#console-navigation')).to_be_visible()
                                    inspect(page,f'{browser_name}-menu-{width}',output,report,False)
                        for role,path in [('team','/submit'),('staff','/admin')]:
                            page.goto(base+path)
                            page.locator('#email').fill(role+'@layout.test');page.locator('#password').fill(password);page.locator('#login-form button[type=submit]').click()
                            expect(page.locator('#zip-file' if role=='team' else '#staff-tab-overview')).to_be_visible()
                            for width in [1440,1280,1024,768,390,375]:
                                page.set_viewport_size({'width':width,'height':1000})
                                tabs=['overview','reviews','evaluations','teams','support','announcements','operations','audit'] if role=='staff' else ['submission']
                                for tab in tabs:
                                    if role=='staff':page.locator(f'[data-staff-tab="{tab}"]').click()
                                    page.evaluate('scrollTo(0,0)')
                                    inspect(page,f'{browser_name}-{role}-{tab}-{width}',output,report,width in [1440,390])
                            page.locator('#logout').click();expect(page.locator('#login-form')).to_be_visible()
                        context.close();browser.close()
                assert not errors, errors
                report.update(status='passed',javascript_errors=0)
        finally:
            if proc is not None:proc.terminate();proc.wait(timeout=15)
            report['javascript_errors']=errors
            (output/'report.json').write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps({'status':report['status'],'screens':len(report['checks']),'output':str(output)}))


if __name__=='__main__':main()
