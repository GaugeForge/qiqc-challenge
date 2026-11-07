"""Local dependency acceptance checks; never prints provider credentials or responses."""
from pathlib import Path
import hashlib, json, os, tempfile, time, uuid, sys
import httpx

ROOT=Path(__file__).resolve().parents[3]
PLATFORM=ROOT/'competition-platform'/'platform'
sys.path.insert(0,str(PLATFORM))

def load_env(path):
    for line in Path(path).read_text().splitlines():
        s=line.strip()
        if s and not s.startswith('#') and '=' in s:
            k,v=s.split('=',1); os.environ.setdefault(k.strip(),v.strip().strip('"\''))

load_env(ROOT/'.env')
results={'checked_at':time.time(),'secrets_printed':False}

# E2B: create, execute, and destroy one no-network sandbox.
from e2b import Sandbox
sb=Sandbox.create(api_key=os.environ['E2B_API_KEY'], timeout=120)
try:
    cmd=sb.commands.run("python3 -c \"print('qiqc-e2b-acceptance')\"", timeout=30)
    results['e2b']={'created':True,'exit_code':cmd.exit_code,'command_ok':cmd.stdout.strip()=='qiqc-e2b-acceptance'}
finally:
    sb.kill()
results['e2b']['destroyed']=True

# R2: put/read/idempotently re-put/delete a short-lived smoke object.
os.environ['QIQC_R2_ENABLED']='1'
from qiqc.storage import ObjectStore, digest
store=ObjectStore(ROOT/'runtime/legacy-platform-data'/'objects')
fd,name=tempfile.mkstemp(prefix='qiqc-r2-check-'); os.write(fd,b'qiqc-r2-acceptance'); os.close(fd)
key='qiqc-demo-smoke/'+uuid.uuid4().hex+'.bin'
try:
    info=store.put_file(key,name)
    with store.local_path(key) as downloaded: read_ok=downloaded.read_bytes()==b'qiqc-r2-acceptance'
    repeat=store.put_file(key,name)==info
    head=store.client.head_object(Bucket=store.bucket,Key=key)
    store.client.delete_object(Bucket=store.bucket,Key=key)
    results['r2']={'provider':info['provider'],'upload_sha_ok':info['sha256']==digest(name),'read_ok':read_ok,'idempotent':repeat,'head_size_ok':head['ContentLength']==len(b'qiqc-r2-acceptance'),'deleted':True}
finally:
    Path(name).unlink(missing_ok=True)

# Hy4 through the configured OpenAI-compatible chat endpoint.
body={'model':'tencent/hy4-preview','messages':[{'role':'user','content':'Reply with exactly QIQC-HY4-ACCEPTANCE.'}],'max_tokens':16,'temperature':0}
r=httpx.post(os.environ['HY_URL'],headers={'Authorization':'Bearer '+os.environ['HY_API'],'Content-Type':'application/json'},json=body,timeout=120,trust_env=False)
text=str(r.json()) if r.headers.get('content-type','').startswith('application/json') else ''
results['hy4']={'status':r.status_code,'model':body['model'],'has_usage':isinstance(r.json().get('usage'),dict) if r.status_code<300 else False,'choice_count':len(r.json().get('choices',[])) if r.status_code<300 else 0,'response_marker_ok':'QIQC-HY4-ACCEPTANCE' in text}

bundle=ROOT/'benchmark-assets'/'qiqcbench-runtime'
results['benchmark']={'task_count':len(list((bundle/'harbor_tasks').iterdir())),'task_config_count':len(list((bundle/'configs'/'tasks').glob('*.yaml'))),'source_checkout_present':(ROOT/'benchmark-assets'/'QIQCBench').exists()}
out=PLATFORM/'test-results'/'cloud-check.json'; out.write_text(json.dumps(results,ensure_ascii=False,indent=2))
print(json.dumps(results,ensure_ascii=False))
