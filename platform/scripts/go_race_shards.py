#!/usr/bin/env python3
"""Run every Go test with race detection; shard the slow scrypt-heavy package.

Uses local test fixtures only. No password-cost reduction or skipped test names.
A finite timeout applies to each shard and its stdout is retained as evidence.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import re
import subprocess
import time


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args();args.output.mkdir(parents=True,exist_ok=True)
    module=Path(__file__).resolve().parents[2]/'platform-go'
    listed=subprocess.check_output(['/usr/bin/go','test','./internal/platform','-list','.'],cwd=module,text=True)
    names=[line for line in listed.splitlines() if re.fullmatch(r'(Test|Example)[A-Za-z0-9_]+',line)]
    assert names and len(names)==len(set(names))
    groups=[names[i::4] for i in range(4)]
    packages=subprocess.check_output(['/usr/bin/go','list','./...'],cwd=module,text=True).splitlines()
    other=[p for p in packages if not p.endswith('/internal/platform')]
    specs=[(f'platform-{i+1}',['./internal/platform','-run','^('+('|'.join(group))+')$'],group) for i,group in enumerate(groups)]
    specs.append(('other-packages',other,[]))
    def run(spec):
        name,targets,tests=spec;started=time.monotonic()
        with (args.output/(name+'.log')).open('w') as stream:
            result=subprocess.run(['/usr/bin/go','test','-race','-timeout','20m',*targets],cwd=module,stdout=stream,stderr=subprocess.STDOUT,timeout=1250)
        return {'name':name,'exit_code':result.returncode,'tests':tests,'duration_seconds':round(time.monotonic()-started,3)}
    with ThreadPoolExecutor(max_workers=5) as pool:results=list(pool.map(run,specs))
    report={'status':'passed' if all(r['exit_code']==0 for r in results) else 'failed','platform_test_count':len(names),'shards':results,'all_packages':packages,'race_detection':True,'cloud_runs':0}
    (args.output/'report.json').write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps({'status':report['status'],'platform_test_count':len(names),'shards':len(results)}))
    return 0 if report['status']=='passed' else 1


if __name__=='__main__':raise SystemExit(main())
