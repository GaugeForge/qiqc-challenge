#!/usr/bin/env python3
import argparse
import json
import os
import secrets
import subprocess
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from qiqc.core import Engine

ROOT=Path(__file__).resolve().parents[2]
PYTHON=ROOT/'platform/.venv/bin/python'
UNITS=['qiqc-demo-api','qiqc-demo-gateway','qiqc-demo-worker']

def init():
    e=Engine(); path=e.root/'credentials.json'
    existing=json.loads(path.read_text()) if path.exists() else {}
    for email,team,role in [('demo@qiqc.local','Quantum Pioneers','team'),('orbit@qiqc.local','Orbit Lab','team'),('admin@qiqc.local','赛事管理员','admin')]:
        if email not in existing:
            password=secrets.token_urlsafe(18)
            account=e.create_account(email,password,team,role=role)
            existing[email]={'password':password,'role':role,'team':team,'id':account['id'],'first_login_change_required':True}
    path.write_text(json.dumps(existing,ensure_ascii=False,indent=2)+'\n'); path.chmod(0o600)
    print('Accounts ready. Credentials file:',path)

def install():
    init()
    commands={
      'qiqc-demo-api':f'{PYTHON} -m uvicorn qiqc.app:factory --factory --host 127.0.0.1 --port 8765 --no-access-log',
      'qiqc-demo-gateway':f'{PYTHON} -m uvicorn qiqc.gateway:factory --factory --host 172.17.0.1 --port 8788 --no-access-log',
      'qiqc-demo-worker':f'{PYTHON} -m qiqc.worker',
    }
    for name,command in commands.items():
        unit=f'''[Unit]
Description=QIQC local demo {name}
After=network.target docker.service

[Service]
Type=simple
WorkingDirectory={ROOT}
Environment=PYTHONPATH={ROOT}/platform
Environment=QIQC_DATA=/root/QQ-Challenge/runtime/legacy-platform-data
Environment=PYTHONUNBUFFERED=1
Environment=PATH=/root/.local/bin:/usr/local/bin:/usr/bin:/bin
ExecStart={command}
Restart=on-failure
RestartSec=3
TimeoutStopSec=390
UMask=0077
NoNewPrivileges=true

[Install]
WantedBy=multi-user.target
'''
        (ROOT/'platform/systemd').mkdir(exist_ok=True)
        (ROOT/'platform/systemd'/f'{name}.service').write_text(unit)
        Path('/etc/systemd/system',f'{name}.service').write_text(unit)
    subprocess.run(['systemctl','daemon-reload'],check=True)
    subprocess.run(['systemctl','enable','--now',*UNITS],check=True)
    subprocess.run(['systemctl','restart',*UNITS],check=True)
    print('Local demo: http://127.0.0.1:8765/submit')

parser=argparse.ArgumentParser()
parser.add_argument('command',choices=['init','install-services','status','stop','backup'])
args=parser.parse_args()
if args.command=='init': init()
elif args.command=='install-services': install()
elif args.command=='status': subprocess.run(['systemctl','--no-pager','status',*UNITS])
elif args.command=='stop': subprocess.run(['systemctl','stop',*UNITS],check=True)
elif args.command=='backup':
    import time
    e=Engine(); print(e.backup(e.root/'backups'/str(int(time.time()))))
