"""Actual local service restart + consistent backup restoration acceptance."""
import json
import secrets
import subprocess
import time
from pathlib import Path
import httpx
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from qiqc.core import Engine,Settings

base='http://127.0.0.1:8765'
p=Path('/root/QQ-Challenge/runtime/legacy-platform-data/credentials.json');accounts=json.loads(p.read_text())

def login(email):
    c=httpx.Client(base_url=base,headers={'X-QIQC-Request':'1'},timeout=45)
    r=c.post('/api/login',json={'email':email,'password':accounts[email]['password']});r.raise_for_status()
    if r.json()['must_change']:
        password=secrets.token_urlsafe(20)
        c.post('/api/password',json={'password':password}).raise_for_status()
        accounts[email]['password']=password;accounts[email]['first_login_change_required']=False
        p.write_text(json.dumps(accounts,ensure_ascii=False,indent=2));p.chmod(0o600)
    return c

admin=login('admin@qiqc.local');team=login('orbit@qiqc.local')
action={'actor':'恢复验收脚本','reason':'实际重启前暂停派发，保留已受理请求','action':'pause','paused':True}
admin.post('/api/admin/actions',json=action).raise_for_status()
v=team.post('/api/versions',content=team.get('/api/reference.zip').content,headers={'Content-Type':'application/zip'});v.raise_for_status()
payload={'version_id':v.json()['id'],'kind':'public','mode':'simulated','idempotency_key':'service-restart-proof-1'}
r=team.post('/api/batches',json=payload);r.raise_for_status();bid=r.json()['id']
quota=team.get('/api/me').json()['quotas']['public']
subprocess.run(['systemctl','restart','qiqc-demo-api','qiqc-demo-gateway','qiqc-demo-worker'],check=True)
for i in range(40):
    try:
        if httpx.get(base+'/healthz').status_code==200:break
    except httpx.HTTPError:pass
    time.sleep(.3)
r=team.post('/api/batches',json=payload);r.raise_for_status()
assert r.json()['id']==bid
assert team.get('/api/me').json()['quotas']['public']==quota
assert team.get('/api/batches/'+bid).json()['status']=='queued'
action.update(paused=False,reason='实际重启后请求、会话与扣额一致，恢复派发')
admin.post('/api/admin/actions',json=action).raise_for_status()
for i in range(100):
    r=team.get('/api/batches/'+bid).json()
    if r['status']=='completed':break
    time.sleep(.3)
assert r['status']=='completed' and r['score']['total']==5
assert team.get('/api/me').json()['quotas']['public']==quota
engine=Engine();folder=engine.root/'backups'/('acceptance-'+str(int(time.time())))
backup=engine.backup(folder)
restored_dir=engine.root/'restore-check'
if restored_dir.exists(): raise RuntimeError('Restore destination already exists; choose a new location')
Engine.restore(backup,restored_dir)
restored=Engine(Settings(data_dir=restored_dir,scan_docker=False))
account=team.get('/api/me').json()
assert restored.get_batch(account['id'],bid)['score']==r['score']
assert restored.me(account['id'])['quotas']['public']==quota
assert '模拟执行' in restored.trace(account['id'],bid,0)['text']
result={'service_restart':'passed','idempotency_after_restart':'passed','same_quota_after_restart':True,'public_batch_completed':True,'local_backup_restore':'passed','batch_id':bid,'backup':str(backup),'restored':str(restored_dir),'not_proven':['offsite disaster recovery','R2 durability','E2B execution']}
output=Path(__file__).resolve().parents[1]/'test-results/recovery-results.json';output.write_text(json.dumps(result,ensure_ascii=False,indent=2))
print(json.dumps(result,ensure_ascii=False))
