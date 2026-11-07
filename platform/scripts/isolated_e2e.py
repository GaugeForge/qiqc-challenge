#!/usr/bin/env python3
"""Repeatable local E2E: private disposable DB, ports, processes and no cloud calls.

Does not restart systemd, touch shared Demo accounts, send mail, or call R2/E2B.
Uploaded code is never executed. The API and fixture worker are native Go; Python is only the test driver.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import secrets
import signal
import socket
import subprocess
import sys
import tempfile
import time
import traceback
import httpx

PLATFORM = Path(__file__).resolve().parents[1]
SITE = PLATFORM.parent
sys.path.insert(0, str(PLATFORM))


def port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


def ready(url, proc):
    for _ in range(200):
        if proc.poll() is not None:
            raise RuntimeError('isolated service exited before readiness')
        try:
            if httpx.get(url + '/healthz', timeout=1, trust_env=False).status_code == 200:
                return
        except httpx.HTTPError:
            pass
        time.sleep(.1)
    raise RuntimeError('isolated service did not become ready')


def main():
    ap=argparse.ArgumentParser()
    ap.add_argument('--output', type=Path, default=PLATFORM/'test-results/isolated-e2e.json')
    ap.add_argument('--pressure-teams', type=int, choices=[8,32,100,200], default=32, help='Synthetic local teams only; no cloud calls')
    ap.add_argument('--go-binary', type=Path, help='optional already-built Go executable')
    args=ap.parse_args()
    started=time.monotonic()
    from preflight import source_identity
    source_at_start=source_identity()
    report={'status':'failed','environment':'isolated-local','cloud_calls':False,
            'uploaded_code_executed':False,'shared_demo_mutated':False,
            'schema':'qiqc-acceptance/v1','source_at_start':source_at_start}
    processes=[]
    with tempfile.TemporaryDirectory(prefix='qiqc-isolated-e2e-') as temp:
        root=Path(temp)
        # Force the fixture store in this process too; never inherit R2 enabled.
        os.environ['QIQC_DATA']=str(root/'data')
        os.environ['QIQC_R2_ENABLED']='0'
        os.environ['QIQC_R2_ENV']=str(root/'absent.env')
        from qiqc.core import Engine,Settings
        e=Engine(Settings(data_dir=root/'data',scan_docker=False))
        admin_secret=secrets.token_urlsafe(24)
        e.create_account('admin@isolated.test',admin_secret,'Isolated organizer',role='admin',must_change=False)
        credentials=e.root/'credentials.json'
        credentials.write_text(json.dumps({'admin@isolated.test':{'password':admin_secret,'role':'admin'}}))
        credentials.chmod(0o600)
        env={k:v for k,v in os.environ.items() if k in ['PATH','HOME','LANG','LC_ALL']}
        env.update(PYTHONPATH=str(PLATFORM),QIQC_DATA=str(e.root),QIQC_R2_ENABLED='0',
                   QIQC_R2_ENV=str(root/'absent.env'),PYTHONUNBUFFERED='1')
        backend_port,edge_port=port(),port()
        while edge_port==backend_port: edge_port=port()
        backend=f'http://127.0.0.1:{backend_port}'
        edge=f'http://127.0.0.1:{edge_port}'
        binary=args.go_binary.resolve() if args.go_binary else root/'qiqc-go'
        stage='build'
        try:
            if not args.go_binary:
                subprocess.run(['go','build','-o',str(binary),'.'],cwd=SITE/'platform-go',check=True,capture_output=True,timeout=300)
            worker_binary=root/'qiqc-worker'
            subprocess.run(['go','build','-o',str(worker_binary),'./cmd/qiqc-worker'],cwd=SITE/'platform-go',check=True,capture_output=True,timeout=300)
            report['binary_sha256']={name:hashlib.sha256(path.read_bytes()).hexdigest() for name,path in [('api',binary),('worker',worker_binary)]}
            stage='start-services'
            with (root/'private-process.log').open('wb') as log:
                def launch(argv):
                    proc=subprocess.Popen(argv,cwd=SITE,env=env,stdout=log,stderr=subprocess.STDOUT,start_new_session=True)
                    processes.append(proc)
                    return proc
                worker=launch([str(worker_binary),'-data',str(e.root),'-slots','4'])
                report['fixture_worker']='go-native'
                report['http_backend']='go-native-standalone'
                report['python_http_process']=False
                go=launch([str(binary),'-listen',f'127.0.0.1:{edge_port}','-backend',backend,
                           '-standalone','-clock-health','','-site',str(SITE/'dist'),'-web',str(PLATFORM/'web'),'-reference',str(SITE/'adapters/reference.zip'),'-adapters',str(SITE/'adapters'),'-data',str(e.root),'-scanner',str(PLATFORM/'qiqc/archive.py')])
                ready(edge,go)
                stage='auth-bidirectional'
                # Native Go login and password hashes must also work in Python.
                with httpx.Client(base_url=edge,timeout=20,trust_env=False) as client:
                    res=client.post('/api/login',json={'email':'admin@isolated.test','password':admin_secret})
                    assert res.status_code==200 and res.headers.get('X-QIQC-Module')=='go-native'
                    token=client.cookies.get('qiqc_session')
                    assert e.session(token)['role']=='admin'
                    changed=secrets.token_urlsafe(24)
                    assert client.post('/api/password',headers={'X-QIQC-Request':'1'},json={'password':changed}).status_code==200
                    py_token,_=e.login('admin@isolated.test',changed,'isolated-e2e')
                    res=client.get('/api/me',headers={'Cookie':'qiqc_session='+py_token})
                    assert res.status_code==200 and res.headers.get('X-QIQC-Module')=='go-native'
                    credentials.write_text(json.dumps({'admin@isolated.test':{'password':changed,'role':'admin'}}))
                    credentials.chmod(0o600)
                report['go_python_auth_bidirectional']=True
                stage='browser-flow'
                from isolated_browser import run_browser
                report['browser']=run_browser(edge,'admin@isolated.test',changed,args.output.parent)
                stage='native-pressure'
                from isolated_pressure import run_pressure
                report['pressure']=run_pressure(edge,e,'admin@isolated.test',changed,teams=args.pressure_teams)
                stage='worker-crash'
                from isolated_crash import run_crash
                report['worker_crash']=run_crash(edge,e,'admin@isolated.test',changed,worker,lambda:launch([str(worker_binary),'-data',str(e.root),'-slots','4']))
                stage='business-flow'
                flow=root/'flow.json'
                run=subprocess.run([sys.executable,str(PLATFORM/'scripts/e2e_check.py'),'--url',edge,'--output',str(flow)],env=env,cwd=SITE,capture_output=True,timeout=360)
                if run.returncode:
                    raise RuntimeError('business-flow subprocess failed: '+run.stderr.decode(errors='replace')[-600:])
                report['flow']=json.loads(flow.read_text())
                report['status']='passed'
                stage='restart'
                # Restart the standalone Go HTTP process and prove persisted closure and rows.
                for proc in (go,):
                    os.killpg(proc.pid,signal.SIGTERM);proc.wait(timeout=15)
                go=launch([str(binary),'-listen',f'127.0.0.1:{edge_port}','-backend',backend,'-standalone','-clock-health','','-site',str(SITE/'dist'),'-web',str(PLATFORM/'web'),'-reference',str(SITE/'adapters/reference.zip'),'-adapters',str(SITE/'adapters'),'-data',str(e.root),'-scanner',str(PLATFORM/'qiqc/archive.py')])
                ready(edge,go)
                with httpx.Client(base_url=edge,timeout=20,trust_env=False) as client:
                    assert client.get('/api/config').json()['competition_closed'] is True
                    assert client.get('/api/leaderboard').json()['teams']
                report['restart_preserves_closed_state_and_scores']=True
        except Exception as exc:
            report['status']='failed'
            report['failure_type']=type(exc).__name__
            report['failure_stage']=stage
            report['failure_location']=[{'file':Path(f.filename).name,'line':f.lineno,'function':f.name} for f in traceback.extract_tb(exc.__traceback__)]
            # Static message only; subprocess traces can contain private responses.
        finally:
            for proc in reversed(processes):
                if proc.poll() is None:
                    os.killpg(proc.pid,signal.SIGTERM)
                    try:proc.wait(timeout=15)
                    except subprocess.TimeoutExpired:
                        os.killpg(proc.pid,signal.SIGKILL);proc.wait()
            report['all_child_processes_stopped']=all(p.poll() is not None for p in processes)
            report['duration_seconds']=round(time.monotonic()-started,3)
            report['source_sha256']=hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    report['temporary_runtime_removed']=not root.exists()
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps({k:v for k,v in report.items() if k!='flow'},ensure_ascii=False))
    return 0 if report['status']=='passed' else 1

if __name__=='__main__':raise SystemExit(main())
