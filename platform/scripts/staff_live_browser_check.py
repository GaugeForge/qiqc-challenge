#!/usr/bin/env python3
"""Verify deployed staff/participant UI. No scientific jobs or provider calls.

Only the explicitly supplied newly-created staff account is rotated, and only
with --bootstrap-handoff. Passwords are never emitted into the report.
"""
import argparse
import json
import os
from pathlib import Path
import secrets
import time
from playwright.sync_api import sync_playwright, expect


def private_write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value, indent=2) + '\n')
    temporary.chmod(0o600)
    os.replace(temporary, path)


def main():
    ap=argparse.ArgumentParser()
    ap.add_argument('--url', required=True)
    ap.add_argument('--credentials',type=Path,required=True)
    ap.add_argument('--team-credentials',type=Path,required=True)
    ap.add_argument('--output',type=Path,required=True)
    ap.add_argument('--bootstrap-handoff',action='store_true')
    args=ap.parse_args()
    credentials=json.loads(args.credentials.read_text())
    team_data=json.loads(args.team_credentials.read_text())
    args.output.mkdir(parents=True,exist_ok=True)
    report={'status':'running','url':args.url,'cloud_runs':0,'tls_validation_disabled':False,'checks':[]}
    errors=[]
    try:
        with sync_playwright() as pw:
            browser=pw.chromium.launch(headless=True,args=['--no-sandbox'])
            staff=browser.new_context(extra_http_headers={'X-QIQC-Workspace':'staff'},viewport={'width':1440,'height':1000})
            page=staff.new_page();page.on('pageerror',lambda e:errors.append(type(e).__name__))
            report['stage']='homepage'
            assert page.goto(args.url+'/').status==200
            assert page.locator('a[href="/admin"]').count() == 0
            brand_font=page.locator('.site-header .brand').evaluate('e=>getComputedStyle(e).fontFamily')
            primary=page.locator('.button.primary').first.evaluate('e=>getComputedStyle(e).backgroundColor')
            page.screenshot(path=str(args.output/'homepage.png'))
            page.goto(args.url+'/admin')
            expect(page.locator('#login-form')).to_be_visible()
            assert staff.request.get(args.url+'/api/admin').status==401
            report['stage']='staff-sign-in'
            page.locator('#email').fill(credentials['email'])
            page.locator('#password').fill(credentials['temporary_password'])
            page.locator('#login-form button[type=submit]').click()
            if args.bootstrap_handoff and credentials.get('must_change_password',True):
                expect(page.locator('#new-password')).to_be_visible()
                changed=secrets.token_urlsafe(32)
                page.locator('#new-password').fill(_pw:=changed);page.locator('#confirm-password').fill(_pw)
                page.locator('#password-form button[type=submit]').click()
                expect(page.locator('#staff-host-health')).to_be_attached()
                credentials['temporary_password']=changed
                credentials['must_change_password']=False
                private_write(args.credentials,credentials)
            expect(page.locator('[data-staff-tab="overview"]')).to_be_visible()
            assert page.locator('.staff-header .brand').evaluate('e=>getComputedStyle(e).fontFamily')==brand_font
            cookies=[c for c in staff.cookies() if c['name']=='qiqc_staff_session']
            assert len(cookies)==1 and cookies[0]['secure'] and cookies[0]['httpOnly'] and cookies[0]['sameSite']=='Strict'
            report['stage']='staff-data'
            snapshot=staff.request.get(args.url+'/api/admin');assert snapshot.ok
            data=snapshot.json();assert data['host_status']['available'] and not data['host_status']['stale']
            report['stage']='staff-sections'
            for tab in ['overview','reviews','evaluations','teams','support','announcements','operations','audit']:
                page.locator(f'[data-staff-tab="{tab}"]').click()
                expect(page.locator(f'#staff-section-{tab}')).to_be_visible()
                page.evaluate('window.scrollTo(0,0)')
                page.screenshot(path=str(args.output/f'staff-{tab}.png'))
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth+1')
            report['checks']+=['Public homepage has no staff link; staff sign-in opens directly','Authenticated staff can open all eight workspace sections','Host monitor projection is present and fresh','Shared brand font matches homepage','Secure session cookie']
            denied=staff.request.post(args.url+'/api/admin/actions',headers={'Origin':'https://untrusted.example','X-QIQC-Request':'1'},data={'action':'pause','paused':True,'actor':'probe','reason':'must be denied'})
            assert denied.status==403
            report['stage']='participant-pages'
            team=browser.new_context(extra_http_headers={'X-QIQC-Workspace':'team'},viewport={'width':1440,'height':1000})
            participant=team.new_page();participant.on('pageerror',lambda e:errors.append(type(e).__name__))
            assert participant.goto(args.url+'/submit').status==200
            participant.locator('#email').fill(team_data['email']);participant.locator('#password').fill(team_data['password']);participant.locator('#login-form button[type=submit]').click()
            expect(participant.locator('#zip-file')).to_be_visible()
            assert team.request.get(args.url+'/api/admin').status==403
            assert team.request.post(args.url+'/api/admin/accounts',headers={'X-QIQC-Request':'1'},data={'role':'admin'}).status in (400,403)
            assert participant.locator('.button.primary').first.evaluate('e=>getComputedStyle(e).backgroundColor')==primary
            participant.evaluate('window.scrollTo(0,0)');participant.screenshot(path=str(args.output/'submit-desktop.png'))
            participant.goto(args.url+'/leaderboard');expect(participant.locator('#leader-table table')).to_be_visible();participant.screenshot(path=str(args.output/'leaderboard-desktop.png'))
            participant.set_viewport_size({'width':390,'height':844});participant.goto(args.url+'/submit');expect(participant.locator('#zip-file')).to_be_visible()
            assert participant.evaluate('document.documentElement.scrollWidth <= innerWidth+1')
            participant.screenshot(path=str(args.output/'submit-mobile.png'))
            page.set_viewport_size({'width':390,'height':844});page.locator('[data-staff-tab="overview"]').click();page.evaluate('window.scrollTo(0,0)')
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth+1');page.screenshot(path=str(args.output/'staff-mobile.png'))
            report['checks']+=['Team login, submission and leaderboard pages','Team cannot read management data or create staff','Cross-origin staff mutation denied','Primary button color matches homepage','Desktop and mobile have no page overflow']
            report['stage']='credential-handoff'
            if args.bootstrap_handoff:
                user=staff.request.get(args.url+'/api/me').json()
                handoff=secrets.token_urlsafe(24)
                reset=staff.request.post(args.url+'/api/admin/accounts/reset',headers={'X-QIQC-Request':'1'},data={'target':user['id'],'password':handoff,'actor':user['email'],'reason':'Rotate new bootstrap account after acceptance for private owner handoff'})
                assert reset.status==200
                credentials.update(temporary_password=handoff,must_change_password=True,login_url=args.url+'/admin')
                private_write(args.credentials,credentials)
                assert staff.request.get(args.url+'/api/admin').status!=200
                report['checks'].append('Bootstrap credential rotated for owner; test session revoked')
            assert not errors
            browser.close()
        report.update(status='passed',javascript_errors=0,checked_at=time.time())
    except Exception as error:
        report.update(status='failed',error_type=type(error).__name__,javascript_errors=len(errors))
    (args.output/'report.json').write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps(report))
    return 0 if report['status']=='passed' else 1


if __name__=='__main__':raise SystemExit(main())
