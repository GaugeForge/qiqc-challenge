"""Private runtime evidence collected on the trusted E2B Docker host."""
import json
import subprocess
from pathlib import Path
from native_build_receipt import task_runtime_seed

PLAN = Path('/opt/qiqc-native-build-plan.json')
BUILD_RECEIPT = Path('/opt/qiqc-native-build-receipt.json')


def load_inputs(identity):
    plan=json.loads(PLAN.read_text());receipt=json.loads(BUILD_RECEIPT.read_text())
    for key in ('source_commit','source_files_sha256'):
        if plan.get(key)!=identity.get(key) or receipt.get(key)!=identity.get(key):
            raise RuntimeError('native_build_candidate_binding_mismatch')
    from native_contract import enrich_role_resources
    enrich_role_resources(plan, '/opt/qiqcbench')
    return plan,receipt


def command_json(argv):
    result=subprocess.run(argv,check=True,capture_output=True,text=True,timeout=30)
    return json.loads(result.stdout)


def container_inspect(cid):
    return command_json(['docker','inspect',cid])[0]


def main_observation(plan,build,task_id,main_id,qsim_id,security,project,source_root,trial_root):
    main=container_inspect(main_id);qsim=container_inspect(qsim_id)
    if main.get('Image') != build['task_receipts'][task_id]['image_ids'].get('main'):
        raise RuntimeError('native_running_main_image_differs_from_build')
    result=task_runtime_seed(build,plan,task_id,main)
    if qsim.get('Image')!=result['build_receipt']['image_ids']['qsim']:
        raise RuntimeError('native_running_qsim_image_differs_from_build')
    # Only stdlib metadata/hash work inside main; never import the science modules.
    script='''import hashlib,importlib.metadata,json,pathlib
root=pathlib.Path('/opt/qiqcbench-agent')
files={p.relative_to(root).as_posix():hashlib.sha256(p.read_bytes()).hexdigest() for p in root.rglob('*') if p.is_file() and '__pycache__' not in p.parts}
print(json.dumps({'packages':{d.metadata['Name']:d.version for d in importlib.metadata.distributions()},'agent_lib':files}))
'''
    result['main_probe']=command_json(['docker','exec',main_id,'python3','-c',script])
    result['container_inspects']['qsim']=qsim
    probes=security['probes']
    info=command_json(['docker','info','--format','{{json .}}'])
    result.update(compose_project=project,source_root=source_root,trial_root=trial_root,
        negative_reads={k.removeprefix('private_read:'):v['exit_code'] for k,v in probes.items() if k.startswith('private_read:')},
        readonly_write_exit=probes['public_volume_write']['exit_code'],qsim_http_status=probes['qsim_http']['status'],
        resources={'cpus':info['NCPU'],'memory_mb':info['MemTotal']//(1024*1024)})
    return result
