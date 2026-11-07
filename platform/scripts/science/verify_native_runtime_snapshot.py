#!/usr/bin/env python3
"""Real CLI/adapter check in E2B's original main image with a synthetic provider.

No task data, private inputs or provider credentials enter these containers.
Network is disabled. This is runtime/transport evidence, not scientific E2E.
"""

import argparse
import hashlib
import json
import time
from pathlib import Path

from e2b import Sandbox
from e2b_runtime import ROOT, load_key
from native_runtime_bundles import read_manifest

PROVIDER = r'''
import json,time
from http.server import BaseHTTPRequestHandler,HTTPServer
from pathlib import Path
class Handler(BaseHTTPRequestHandler):
 def log_message(self,*args):pass
 def do_POST(self):
  body=json.loads(self.rfile.read(int(self.headers['content-length'])))
  allowed=self.headers.get('authorization')=='Bearer qr_fixture_runtime_check'
  with open('/input/requests.jsonl','a') as f:f.write(json.dumps({'endpoint':self.path,'scoped_token_correct':allowed})+'\n')
  self.send_response(200 if allowed else 401)
  streaming=body.get('stream',False)
  self.send_header('Content-Type','text/event-stream' if streaming else 'application/json')
  self.send_header('Connection','close');self.end_headers()
  if not allowed:self.wfile.write(b'{"error":{"message":"fixture token mismatch"}}');return
  if self.path.endswith('/responses'):
   item={'id':'msg_fixture','type':'message','status':'completed','role':'assistant','content':[{'type':'output_text','text':'ready','annotations':[]}]}
   response={'id':'resp_fixture','object':'response','created_at':int(time.time()),'status':'completed','model':body['model'],'output':[item],
             'usage':{'input_tokens':10,'output_tokens':1,'total_tokens':11}}
   if not streaming:self.wfile.write(json.dumps(response).encode());return
   events=[{'type':'response.created','response':{**response,'status':'in_progress','output':[]}},
    {'type':'response.output_item.added','output_index':0,'item':{**item,'status':'in_progress','content':[]}},
    {'type':'response.content_part.added','item_id':'msg_fixture','output_index':0,'content_index':0,'part':{'type':'output_text','text':'','annotations':[]}},
    {'type':'response.output_text.delta','item_id':'msg_fixture','output_index':0,'content_index':0,'delta':'ready'},
    {'type':'response.output_text.done','item_id':'msg_fixture','output_index':0,'content_index':0,'text':'ready'},
    {'type':'response.content_part.done','item_id':'msg_fixture','output_index':0,'content_index':0,'part':item['content'][0]},
    {'type':'response.output_item.done','output_index':0,'item':item},
    {'type':'response.completed','response':response}]
   for i,e in enumerate(events):
    e['sequence_number']=i
    self.wfile.write(('event: '+e['type']+'\ndata: '+json.dumps(e)+'\n\n').encode())
  else:
   base={'id':'chat_fixture','object':'chat.completion.chunk' if streaming else 'chat.completion','created':int(time.time()),'model':body['model']}
   if streaming:
    for choice in [{'index':0,'delta':{'role':'assistant','content':'ready'},'finish_reason':None},
                   {'index':0,'delta':{},'finish_reason':'stop'}]:
     self.wfile.write(('data: '+json.dumps({**base,'choices':[choice]})+'\n\n').encode())
    self.wfile.write(b'data: [DONE]\n\n')
   else:self.wfile.write(json.dumps({**base,'choices':[{'index':0,'message':{'role':'assistant','content':'ready'},'finish_reason':'stop'}]}).encode())
HTTPServer(('127.0.0.1',8181),Handler).serve_forever()
'''

CHECK = r'''set -eu
if test -f /input/runtime.tar.gz; then
 mkdir -p /opt/qiqc-native-runtime
 tar --no-same-owner -xzf /input/runtime.tar.gz -C /opt/qiqc-native-runtime
 export PATH=/opt/qiqc-native-runtime/node/bin:/opt/qiqc-native-runtime/node_modules/.bin:$PATH
fi
python3 -c "from mcp.client.streamable_http import streamable_http_client"
mkdir -p /opt/qiqc-submission /logs/agent
python3 -m zipfile -e /input/harness.zip /opt/qiqc-submission
python3 /input/provider.py >/input/provider.log 2>&1 &
provider_pid=$!
trap 'kill "$provider_pid" 2>/dev/null || true' EXIT
python3 -c "import socket,time
for _ in range(30):
 try:
  socket.create_connection(('127.0.0.1',8181),timeout=1).close();break
 except OSError:time.sleep(.1)
else:raise SystemExit(1)"
export QIQC_TASK_INPUT=/input/task.txt QIQC_MODEL_URL=http://127.0.0.1:8181/v1
export QIQC_MODEL_TOKEN=qr_fixture_runtime_check QIQC_MODEL=tencent/hy4-preview
export QIQC_MODEL_PROTOCOL=__MODEL_PROTOCOL__ QIQC_AGENT_TIMEOUT=60
export QIQC_OUTPUT=/input/final.txt QIQC_TRAJECTORY=/input/trajectory.jsonl
export QIQC_MCP_SERVERS='[]'
cd /app
timeout --kill-after=5s 75 sh /opt/qiqc-submission/adapter/run.sh >/input/adapter.stdout 2>/input/adapter.stderr
'''


def check_script(protocol):
    if protocol not in ('responses', 'chat/completions'):
        raise ValueError('unsupported fixture endpoint')
    return CHECK.replace('__MODEL_PROTOCOL__', protocol)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--template', required=True)
    p.add_argument('--report', type=Path, required=True)
    p.add_argument('--adapters', nargs='+', choices=('codex', 'deepseek', 'pi'), default=['codex', 'deepseek', 'pi'])
    args = p.parse_args()
    if args.report.exists():
        p.error('Report exists')
    key = load_key()
    sandbox = None
    report = {'scope': 'native-runtime-real-cli-synthetic-provider', 'passed': False,
              'science_executed': False, 'real_model_called': False,
              'started_at': time.time(), 'checks': []}
    bundles = read_manifest(ROOT / 'competition-platform/adapters/native-runtime-bundles.json')
    try:
        sandbox = Sandbox.create(template=args.template, api_key=key, timeout=480,
                                 network={'allow_public_traffic': False, 'deny_out': ['0.0.0.0/0']},
                                 metadata={'purpose': 'qiqc-isolated-cli-check'})
        report['sandbox_id'] = sandbox.sandbox_id
        for kind, name, protocol in [('codex','codex','responses'),
                                     ('deepseek-harness','deepseek','chat/completions'),
                                     ('pi-agent','pi','chat/completions')]:
            if name not in args.adapters:
                continue
            folder = '/tmp/qiqc-cli-check-' + name
            sandbox.commands.run('mkdir -p ' + folder, user='root', timeout=10)
            if kind in bundles['packages']:
                row = bundles['packages'][kind]
                source = '/opt/qiqc-agent-runtimes/' + kind + '.tar.gz'
                check = sandbox.commands.run('sha256sum ' + source, user='root', timeout=30)
                if check.stdout.split()[0] != row['sha256']:
                    raise ValueError('runtime_snapshot_digest_mismatch')
                sandbox.commands.run('cp ' + source + ' ' + folder + '/runtime.tar.gz', user='root', timeout=30)
            package = (ROOT / 'competition-platform/adapters' / name / 'harness.zip').read_bytes()
            sandbox.files.write(folder + '/harness.zip', package, user='root')
            sandbox.files.write(folder + '/provider.py', PROVIDER, user='root')
            sandbox.files.write(folder + '/check.sh', check_script(protocol), user='root')
            sandbox.files.write(folder + '/task.txt', 'Respond with ready. Do not call any tools.', user='root')
            result = {'adapter': kind, 'passed': False, 'network': 'none', 'endpoint': protocol,
                      'submission_sha256': hashlib.sha256(package).hexdigest()}
            report['checks'].append(result)
            try:
                sandbox.commands.run('docker run --rm --network none --cap-drop ALL --security-opt no-new-privileges '
                    '--cpus 2 --memory 2g --mount type=bind,src=' + folder + ',dst=/input '
                    'qiqcbench-agent:latest sh /input/check.sh', user='root', timeout=120)
                calls = [json.loads(line) for line in sandbox.files.read(folder + '/requests.jsonl', user='root').splitlines()]
                result['calls'] = calls
                result['passed'] = bool(calls) and all(c['scoped_token_correct'] and c['endpoint'] == '/v1/' + protocol for c in calls)
            except Exception as error:
                result['error_class'] = type(error).__name__
                diagnostic = {'stdout': str(getattr(error, 'stdout', ''))[-8000:],
                              'stderr': str(getattr(error, 'stderr', ''))[-8000:]}
                (args.report.parent / (args.report.stem + '-' + name + '-command.json')).write_text(json.dumps(diagnostic, indent=2))
            for filename in ('adapter.stderr','trajectory.jsonl'):
                try:
                    text = sandbox.files.read(folder + '/' + filename, user='root')
                    target = args.report.parent / (args.report.stem + '-' + name + '-' + filename)
                    target.write_text(text)
                except Exception:
                    pass
            print(json.dumps(result), flush=True)
        report['passed'] = all(c['passed'] for c in report['checks']) and len(report['checks']) == len(set(args.adapters))
    except Exception as error:
        report['error_class'] = type(error).__name__
    finally:
        if sandbox:
            try:
                sandbox.kill()
                report['sandbox_destroyed'] = True
            except Exception as error:
                report['cleanup_error_class'] = type(error).__name__
                report['passed'] = False
        report['finished_at'] = time.time()
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, indent=2) + '\n')
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
