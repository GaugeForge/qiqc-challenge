#!/usr/bin/env python3
"""Prepare all original task main images and immutable build evidence in E2B.

This creates a candidate build snapshot, not a competition release. No task or
contestant program is executed; Dockerfile RUN instructions stay inside E2B.
"""
import argparse
import hashlib
import json
import shlex
import time
from pathlib import Path

from e2b import Sandbox
from e2b_runtime import load_key

REMOTE_BUILD = '''import json,pathlib,subprocess
plan=json.load(open('/opt/qiqc-native-build-plan.json'))
agent=json.loads(subprocess.check_output(['docker','image','inspect','qiqcbench-agent:latest']))[0]['Id']
images={}
for task,row in plan['tasks'].items():
 argv=[a.replace('{agent_image_id}',agent) for a in row['main_build_argv_template']]
 with open('/opt/qiqc-native-evidence/main-build.log','a') as out:
  subprocess.run(argv,cwd='/opt/qiqcbench',stdout=out,stderr=out,check=True)
 tag=argv[argv.index('-t')+1]
 images[task]=json.loads(subprocess.check_output(['docker','image','inspect',tag]))[0]['Id']
 print(json.dumps({'phase':'task_main_built','completed':len(images),'total':len(plan['tasks'])}),flush=True)
pathlib.Path('/opt/qiqc-native-evidence/main_images.json').write_text(json.dumps(images,sort_keys=True,indent=2))
after=json.loads(subprocess.check_output(['docker','image','inspect','qiqcbench-agent:latest']))[0]['Id']
assert after==agent
pathlib.Path('/opt/qiqc-native-evidence/main_build_checks.json').write_text(json.dumps({'agent_image_id_before':agent,'agent_image_id_after':after}))
'''


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--template',required=True)
    p.add_argument('--plan',type=Path,required=True)
    p.add_argument('--report',type=Path,required=True)
    p.add_argument('--resume-report',type=Path)
    args=p.parse_args()
    if args.report.exists():p.error('report exists')
    key=load_key();sandbox=None
    report={'scope':'original-native-complete-build','started_at':time.time(),'passed':False,
            'all49_e2e_accepted':False,'competition_release_frozen':False,'phases':[]}
    report['build_plan_sha256']=hashlib.sha256(args.plan.read_bytes()).hexdigest()
    previous=json.loads(args.resume_report.read_text()) if args.resume_report else None
    completed={r['name'] for r in previous['phases'] if r['passed']} if previous else set()
    if previous:
        if previous.get('passed'):p.error('cannot resume a successful immutable build')
        report['resumed_from']=str(args.resume_report)
        report['phases']=[r for r in previous['phases'] if r['passed']]
    def save():args.report.write_text(json.dumps(report,indent=2,default=str)+'\n')
    def run(name,command,timeout=120):
        if name in completed:return ''
        row={'name':name,'passed':False,'started_at':time.time()};report['phases'].append(row);save()
        print(json.dumps({'phase':name,'state':'started'}),flush=True)
        result=sandbox.commands.run(command,user='root',timeout=timeout)
        row.update(passed=result.exit_code==0,finished_at=time.time());save()
        if result.exit_code:raise RuntimeError('phase_failed:'+name)
        print(json.dumps({'phase':name,'state':'passed'}),flush=True)
        return result.stdout
    try:
        sandbox=Sandbox.connect(previous['sandbox_id'],api_key=key) if previous else Sandbox.create(template=args.template,api_key=key,timeout=1800,
                               network={'allow_public_traffic':False},metadata={'purpose':'qiqc-original-finalize-build'})
        report['sandbox_id']=sandbox.sandbox_id;save()
        run('docker_snapshot_boot','for i in $(seq 1 30); do docker info >/dev/null 2>&1 && exit 0; sleep 1; done; '
            'nohup dockerd --host=unix:///var/run/docker.sock >/tmp/native-dockerd.log 2>&1 </dev/null & '
            'for i in $(seq 1 30); do docker info >/dev/null 2>&1 && exit 0; sleep 1; done; exit 1')
        run('build_cache_only_cleanup','docker builder prune -af >/tmp/native-build-cache-cleanup.log 2>&1 && '
            'mkdir -p /opt/qiqc-native-tools /opt/qiqc-native-evidence && df -h /var/lib/docker')
        sandbox.files.write('/opt/qiqc-native-build-plan.json',args.plan.read_bytes(),user='root')
        sandbox.files.write('/opt/qiqc-native-tools/build_mains.py',REMOTE_BUILD,user='root')
        for name in ('native_build_receipt.py','native_contract.py','verify_restored_benchmark.py'):
            sandbox.files.write('/opt/qiqc-native-tools/'+name,(Path(__file__).parent/name).read_bytes(),user='root')
        run('build_all49_original_task_mains','/opt/harbor/bin/python /opt/qiqc-native-tools/build_mains.py '
            '>/opt/qiqc-native-evidence/main-progress.jsonl 2>&1',900)
        tasks=','.join(json.loads(args.plan.read_text())['tasks'])
        run('record_all49_original_images','cd /opt/qiqcbench && python3 tools/record_agent_image.py '
            '/opt/qiqc-native-evidence --tasks '+shlex.quote(tasks)+' >/opt/qiqc-native-evidence/record.log 2>&1',300)
        run('bind_actual_image_receipt','/opt/harbor/bin/python /opt/qiqc-native-tools/native_build_receipt.py '
            '--plan /opt/qiqc-native-build-plan.json --evidence-dir /opt/qiqc-native-evidence '
            '--output /opt/qiqc-native-build-receipt.json >/opt/qiqc-native-evidence/receipt.log 2>&1',180)
        for name in ('agent_image.json','runtime_images.json','main_images.json','main_build_checks.json'):
            text=sandbox.files.read('/opt/qiqc-native-evidence/'+name,user='root')
            (args.report.parent/(args.report.stem+'-'+name)).write_text(text)
        content=sandbox.files.read('/opt/qiqc-native-build-receipt.json',user='root')
        receipt=json.loads(content)
        if len(receipt['task_receipts'])!=49 or any('main' not in row['image_ids'] for row in receipt['task_receipts'].values()):
            raise RuntimeError('all49_main_receipt_incomplete')
        (args.report.parent/(args.report.stem+'-receipt.json')).write_text(content)
        run('clean_source_before_snapshot','test -z "$(git -C /opt/qiqcbench status --porcelain)" && '
            'docker builder prune -af >/tmp/native-build-cache-cleanup-final.log 2>&1 && sync')
        info=sandbox.get_info();report['resources']={'cpu_count':info.cpu_count,'memory_mb':info.memory_mb}
        report['snapshot']=vars(sandbox.create_snapshot(name='qiqc-original-all49-images-20261020',mode='filesystem'))
        report['passed']=True
    except Exception as exc:
        report['error_type']=type(exc).__name__;report['error']=str(exc).replace(key,'[REDACTED]')[:1000]
        if sandbox:
            try:
                result=sandbox.commands.run('tail -c 2000 /opt/qiqc-native-evidence/main-progress.jsonl '
                    '/opt/qiqc-native-evidence/main-build.log /opt/qiqc-native-evidence/record.log '
                    '/opt/qiqc-native-evidence/receipt.log 2>/dev/null || true',user='root',timeout=20)
                report['build_diagnostic']=result.stdout.replace(key,'[REDACTED]')[-8000:]
            except Exception:report['diagnostic_unavailable']=True
    finally:
        if sandbox and report['passed']:sandbox.kill();report['build_sandbox_destroyed']=True
        report['finished_at']=time.time();save()
    print(json.dumps({'passed':report['passed'],'report':str(args.report)}),flush=True)
    return 0 if report['passed'] else 1

if __name__=='__main__':raise SystemExit(main())
