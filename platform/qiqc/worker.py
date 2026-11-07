"""Persistent local worker: simulated arbitrary packages, trusted Codex reference only."""
import concurrent.futures
import hashlib
import json
import os
import re
import signal
import subprocess
import time
from datetime import datetime
from pathlib import Path

from .core import Engine, Problem
from .archive import reference_package
from .storage import digest

TASK=Path(__file__).resolve().parents[1]/'tasks'/'mock-sum'
HARBOR='/root/.local/share/uv/tools/harbor/bin/harbor'


def simulated(e,lease):
    time.sleep(e.settings.simulated_delay)
    seed=int(hashlib.sha256((lease['sha256']+str(lease['ordinal'])).encode()).hexdigest()[:8],16)
    reward=int(seed%10<8)
    seconds=str(10+(seed%20000000)/1000000)
    trace=f"[模拟执行 / 未运行上传代码]\nTask: {'public' if lease['public'] else 'hidden'}-{lease['ordinal']+1}\nHarness SHA-256: {lease['sha256']}\nRead task → plan → call mock tool → verify fixture\nFixture reward: {reward}; fixture runtime: {seconds} s\n这些数据只演示平台流程，不表示 Harness 性能。\n"
    e.complete_task(lease,reward,seconds,trace,'simulated')


def codex(e,lease):
    if lease['sha256']!=hashlib.sha256(reference_package()).hexdigest(): raise RuntimeError('Reference package hash mismatch')
    if digest(e.root/'uploads'/f"{lease['sha256']}.zip")!=lease['sha256']: raise RuntimeError('Immutable bytes changed')
    token=e.mint_token(lease)
    work=e.root/'jobs'/f"{lease['run_id']}-{lease['attempt']}"
    work.mkdir(parents=True,exist_ok=True,mode=0o700)
    config={
        'job_name':'reference','jobs_dir':str(work),'n_concurrent_trials':1,'quiet':True,
        'retry':{'max_retries':0},'environment':{'type':'docker','delete':True},
        'agents':[{'name':'codex','model_name':e.settings.model,'kwargs':{'reasoning_effort':'low','web_search':'disabled','version':'0.144.4'},'env':{'OPENAI_API_KEY':token,'OPENAI_BASE_URL':'http://172.17.0.1:8788/v1'}}],
        'tasks':[{'path':str(TASK)}],
    }
    config_path=work/'job-config.json'
    config_path.write_text(json.dumps(config)); config_path.chmod(0o600)
    env={k:v for k,v in os.environ.items() if k in ['PATH','HOME','LANG','TMPDIR']}
    env['OTEL_SDK_DISABLED']='true'
    with (work/'launcher.log').open('wb') as log:
        process=subprocess.Popen([HARBOR,'run','--config',str(config_path)],cwd=work,env=env,stdout=log,stderr=subprocess.STDOUT,start_new_session=True)
        start=time.monotonic()
        try:
            while process.poll() is None:
                e.heartbeat(lease)
                if time.monotonic()-start>360:
                    os.killpg(process.pid,signal.SIGINT)
                    try: process.wait(timeout=20)
                    except subprocess.TimeoutExpired:
                        os.killpg(process.pid,signal.SIGKILL); process.wait()
                    raise RuntimeError('Reference Harbor job exceeded watchdog')
                time.sleep(1)
        finally:
            # The real master key was never in this configuration; revoke the run capability anyway.
            with e.db(True) as c: c.execute('UPDATE run_tokens SET revoked=1 WHERE run_id=?',(lease['run_id'],))
            config_path.unlink(missing_ok=True)
    results=[]
    for p in (work/'reference').glob('*/result.json'):
        result=json.loads(p.read_text())
        if 'task_name' in result: results.append((p,result))
    if len(results)!=1: raise RuntimeError('Harbor result missing; check private launcher log')
    result_path,result=results[0]
    rewards=(result.get('verifier_result') or {}).get('rewards') or {}
    reward=rewards.get('reward')
    timing=result.get('agent_execution') or {}
    if reward not in [0,1] or not timing.get('started_at') or not timing.get('finished_at'):
        kind=(result.get('exception_info') or {}).get('exception_type','missing_reward_or_timing')
        raise RuntimeError('Harbor '+str(kind))
    seconds=str((datetime.fromisoformat(timing['finished_at'])-datetime.fromisoformat(timing['started_at'])).total_seconds())
    output=result_path.parent/'agent'/'codex.txt'
    text=output.read_text(errors='replace')[:400000] if output.exists() else 'Harbor Agent completed; raw transcript unavailable.'
    text=re.sub(r'qr_[A-Za-z0-9_-]+','[revoked run token]',text)
    text=re.sub(r'Bearer\s+\S+','Bearer [redacted]',text)
    text='[真实本地 Codex + Harbor / mock task / 非 E2B、非 Hy4]\n'+text
    e.complete_task(lease,int(reward),seconds,text,'real-local-harbor',work/'reference')
    # Complete Harbor evidence is now in the private object-store substitute.
    import shutil
    shutil.rmtree(work)


def execute(e,lease):
    try:
        if lease['mode']=='codex': codex(e,lease)
        else: simulated(e,lease)
    except Exception as exc:
        cache=e.root/'cache'/lease['run_id']/str(lease['attempt'])
        if (cache/'pending.json').exists():
            # Keep uploading state and original bundle. The loop retries storage only.
            return
        try: e.fail_task(lease,'platform',type(exc).__name__+': '+str(exc)[:180])
        except Problem: pass


def resume_uploads(e):
    for p in (e.root/'cache').glob('*/*/pending.json'):
        try:
            data=json.loads(p.read_text()); lease=data['lease']
            e.heartbeat(lease)
            # Existing bundle is used verbatim; this never reruns the agent.
            e.complete_task(lease,data['reward'],data['seconds'],'',data['provenance'])
        except (OSError,ValueError,Problem): continue


def main():
    e=Engine(); stop=False
    def handle(*_):
        nonlocal stop
        stop=True
    signal.signal(signal.SIGTERM,handle); signal.signal(signal.SIGINT,handle)
    owner='local-worker-'+str(os.getpid())
    futures={}
    with concurrent.futures.ThreadPoolExecutor(max_workers=e.settings.slots) as pool:
        last_maintenance=0
        while not stop:
            futures={f:l for f,l in futures.items() if not f.done()}
            if time.monotonic()-last_maintenance>5:
                e.recover(); resume_uploads(e)
                for lease in futures.values(): e.heartbeat(lease)
                last_maintenance=time.monotonic()
            while len(futures)<e.settings.slots:
                if lease:=e.claim(owner): futures[pool.submit(execute,e,lease)]=lease
                else: break
            time.sleep(.2)

if __name__=='__main__': main()
