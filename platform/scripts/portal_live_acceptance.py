#!/usr/bin/env python3
"""Read-only HTTPS/asset/service acceptance for the installed portal release."""
from pathlib import Path
import argparse,json,hashlib,subprocess,httpx
from playwright.sync_api import sync_playwright,expect
root=Path(__file__).resolve().parents[3]
parser=argparse.ArgumentParser(description=__doc__)
parser.add_argument('--url',default='https://173.249.36.41')
parser.add_argument('--release-manifest',type=Path,default=root/'docs/operations/PLATFORM-RELEASE-20261021.json')
parser.add_argument('--output-dir',type=Path,default=root/'competition-platform/platform/test-results/competition-portal-20261021')
args=parser.parse_args();out=args.output_dir;out.mkdir(parents=True,exist_ok=True);base=args.url.rstrip('/')
report={'status':'running','base':base,'tls_verified':True,'checks':[],'cloud_calls':False};errors=[]
with httpx.Client(base_url=base,timeout=25,trust_env=False) as c:
 for route in ['/','/submit','/leaderboard','/admin','/admin/','/documents.html','/healthz','/platform-assets/admin-i18n.js','/platform-assets/staff-accounts.js','/theme.css','/downloads/participant-manual.en.pdf','/downloads/participant-manual.zh-CN.pdf','/adapters/codex/harness.zip','/adapters/deepseek/harness.zip','/adapters/pi/harness.zip']:
  r=c.get(route);assert r.status_code==200,(route,r.status_code);report['checks'].append({'route':route,'status':r.status_code})
 assert c.get('/api/admin').status_code==401
 assert c.get('/api/config').json()['competition_portal'] is True
 assert c.get('/api/leaderboard').status_code==401  # standings are for signed-in teams and staff only
 for route in ['/downloads/participant-guide.html','/downloads/partner-handbook.html','/assets/quantum-circuit.svg']:
  assert c.get(route).status_code==404,route
with sync_playwright() as pw:
 for engine in ['chromium','webkit']:
  browser=getattr(pw,engine).launch(**({'args':['--no-sandbox']} if engine=='chromium' else {}))
  ctx=browser.new_context(viewport={'width':1440,'height':1000});page=ctx.new_page();page.on('pageerror',lambda e:errors.append(str(e)))
  for route in ['/','/submit','/leaderboard','/documents.html','/admin']:
   page.goto(base+route);expect(page.locator('h1')).to_be_visible();assert 'demo' not in page.locator('body').inner_text().lower(),route
   if route != '/admin':
    assert page.locator('a[href="/admin"]').count()==0
    assert 'open staff console' not in page.locator('body').inner_text().lower()
   assert page.locator('#rabi-canvas').count()==(1 if route=='/' else 0)
   assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1'),route
  expect(page.locator('.staff-header')).to_be_visible();assert page.locator('.site-header').count()==0
  page.locator('[data-locale=zh]').click();expect(page.locator('h1')).to_have_text('工作人员后台')
  page.reload();expect(page.locator('html')).to_have_attribute('lang','zh-CN')
  page.set_viewport_size({'width':390,'height':844});assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1')
  page.screenshot(path=str(out/f'live-{engine}-staff-login-390.png'))
  ctx.close();browser.close()
assert not errors,errors
units=['nginx.service','qiqc-native-api.service','qiqc-native-worker-key2.service','qiqc-native-worker-key1.service','qiqc-native-scanner.service','qiqc-native-review.service','qiqc-native-gateway.service','qiqc-ops-health.timer','qiqc-ops-backup.timer','qiqc-certbot-renew.timer']
report['services']={}
for unit in units:
 active=subprocess.check_output(['systemctl','is-active',unit],text=True).strip();enabled=subprocess.check_output(['systemctl','is-enabled',unit],text=True).strip()
 assert active=='active' and enabled=='enabled',(unit,active,enabled)
 report['services'][unit]={'active':active,'enabled':enabled}
manifest=json.loads(args.release_manifest.read_text())
for name,sha in manifest['files'].items():assert hashlib.sha256((Path(manifest['root'])/name).read_bytes()).hexdigest()==sha,name
report.update(status='passed',javascript_errors=errors,release_digests_verified=len(manifest['files']),language_persistence=True,anonymous_admin_status=401)
(out/'live-report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
print(json.dumps({'status':'passed','https_routes':len(report['checks']),'services':len(units),'release_digests':len(manifest['files'])}))
