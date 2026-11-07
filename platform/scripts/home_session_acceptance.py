#!/usr/bin/env python3
"""Verify original homepage visuals and independent staff/team browser sessions."""
import argparse,json,os,secrets,subprocess,sys,tempfile
from pathlib import Path
from playwright.sync_api import sync_playwright,expect
PLATFORM=Path(__file__).resolve().parents[1]; SITE=PLATFORM.parent
sys.path.insert(0,str(PLATFORM))
from isolated_e2e import port,ready

def main():
 ap=argparse.ArgumentParser();ap.add_argument('--go-binary',type=Path,required=True);args=ap.parse_args()
 out=PLATFORM/'test-results/home-session-20261021';out.mkdir(parents=True,exist_ok=True)
 report={'status':'running','checks':[],'cloud_calls':False};errors=[]
 for name in ['src/styles.css','src/app.js']:
  assert (SITE/name).read_bytes()==subprocess.check_output(['git','show','0af3cfe:'+name],cwd=SITE),name
 report['original_home_css_and_animation_match']='0af3cfe'
 with tempfile.TemporaryDirectory(prefix='qiqc-home-session-') as tmp:
  root=Path(tmp);os.environ.update(QIQC_DATA=str(root/'data'),QIQC_R2_ENABLED='0')
  from qiqc.core import Engine,Settings
  e=Engine(Settings(data_dir=root/'data',scan_docker=False));secret=secrets.token_urlsafe(24)
  e.create_account('staff@home.test',secret,'Staff',role='admin',must_change=False)
  e.create_account('team@home.test',secret,'Team',must_change=False)
  base='http://127.0.0.1:'+str(port());env={k:v for k,v in os.environ.items() if k in ['PATH','HOME','LANG','LC_ALL']};env.update(QIQC_DATA=str(root/'data'),QIQC_R2_ENABLED='0',QIQC_COMPETITION_PORTAL='1')
  with (root/'private.log').open('w') as log:
   proc=subprocess.Popen([str(args.go_binary),'-standalone','-listen',base[7:],'-data',str(root/'data'),'-site',str(SITE/'dist'),'-web',str(PLATFORM/'web'),'-adapters',str(SITE/'adapters')],env=env,stdout=log,stderr=subprocess.STDOUT)
   try:
    ready(base,proc)
    with sync_playwright() as pw:
     for engine in ['chromium','webkit']:
      browser=getattr(pw,engine).launch(**({'args':['--no-sandbox']} if engine=='chromium' else {}))
      ctx=browser.new_context(viewport={'width':1440,'height':1000});p=ctx.new_page();p.on('pageerror',lambda e:errors.append(str(e)))
      def login(page,email):
       page.locator('#email').fill(email);page.locator('#password').fill(secret);page.locator('#login-form button[type=submit]').click()
      for width in [1440,1024,390]:
       p.set_viewport_size({'width':width,'height':1000});p.goto(base+'/')
       expect(p.locator('.hero-field.ready')).to_be_attached();expect(p.locator('#rabi-canvas')).to_be_visible()
       assert p.locator('link[href*="theme.css"],link[href*="responsive.css"]').count()==0
       assert p.locator('a[href="/admin"]').count()==0
       assert p.evaluate('document.documentElement.scrollWidth<=innerWidth+1'),(engine,width)
       assert p.locator('#rabi-canvas').evaluate('e=>e.width>0&&e.height>0')
       p.screenshot(path=str(out/f'{engine}-homepage-{width}.png'))
       p.evaluate('scrollTo({top:900,behavior:"instant"})');p.wait_for_timeout(350)
       assert p.locator('.hero').get_attribute('data-layout-progress') is not None
       assert p.evaluate('document.documentElement.scrollWidth<=innerWidth+1')
       p.evaluate('scrollTo({top:0,behavior:"instant"})')
       if p.locator('#menu-toggle').is_visible():p.locator('#menu-toggle').click()
       p.locator('#navigation a[href="/submit"]').click()
       expect(p.locator('#login-form')).to_be_visible();expect(p.locator('#logout')).to_be_hidden()
      report['checks'].append(engine+': original Rabi canvas/scroll visuals, three widths, homepage Submit opens login')
      # Old shared admin cookie and a current staff cookie must never enter Submit.
      assert ctx.request.post(base+'/api/login',data={'email':'staff@home.test','password':secret}).status==200
      staff=ctx.new_page();staff.goto(base+'/admin');expect(staff.locator('#login-form')).to_be_visible();login(staff,'staff@home.test')
      expect(staff.locator('[data-staff-tab=overview]')).to_be_visible()
      p.set_viewport_size({'width':1440,'height':1000});p.goto(base+'/');p.locator('#navigation a[href="/submit"]').click()
      expect(p.locator('#login-form')).to_be_visible();expect(p.locator('#logout')).to_be_hidden();expect(p.locator('#identity')).to_have_text('')
      login(p,'staff@home.test');expect(p.locator('#toast')).to_contain_text('Use the account issued for this workspace.');expect(p.locator('#login-form')).to_be_visible()
      login(p,'team@home.test');expect(p.locator('#zip-file')).to_be_visible()
      staff.reload();expect(staff.locator('[data-staff-tab=overview]')).to_be_visible()
      for workspace,role in [('team','team'),('staff','admin')]:
       r=ctx.request.get(base+'/api/me',headers={'X-QIQC-Workspace':workspace});assert r.status==200 and r.json()['role']==role
      assert ctx.request.get(base+'/api/admin',headers={'X-QIQC-Workspace':'team'}).status==403
      p.locator('#logout').click();expect(p.locator('#login-form')).to_be_visible()
      staff.reload();expect(staff.locator('[data-staff-tab=overview]')).to_be_visible()
      login(p,'team@home.test');expect(p.locator('#zip-file')).to_be_visible()
      staff.locator('#logout').click();expect(staff.locator('#login-form')).to_be_visible()
      p.reload();expect(p.locator('#zip-file')).to_be_visible()
      assert p.locator('a[href="/admin"]').count()==0
      report['checks'].append(engine+': legacy staff excluded, wrong role rejected, simultaneous logins, independent logout, team admin API denied')
      ctx.close();browser.close()
    assert not errors,errors
    report.update(status='passed',javascript_errors=[])
   finally:
    proc.terminate();proc.wait(timeout=20)
    report['javascript_errors']=errors;(out/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
 print(json.dumps(report))
if __name__=='__main__':main()
