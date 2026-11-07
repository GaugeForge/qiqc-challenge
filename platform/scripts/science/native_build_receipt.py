"""Bind original image recorder output to actual Docker image inspections.

Run only on the E2B build host, after the original build/record tools complete.
This never runs a science workload or alters images. Incomplete builds fail closed.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import subprocess
from pathlib import Path

from native_contract import IMAGE, NativeContractError


def inspect_image(image_id, run=subprocess.run):
    if not IMAGE.fullmatch(image_id):
        raise NativeContractError('invalid_recorded_image_id')
    result=run(['docker','image','inspect',image_id],capture_output=True,text=True,timeout=30,check=False)
    if result.returncode:
        raise NativeContractError('recorded_image_unavailable')
    value=json.loads(result.stdout)
    if not isinstance(value,list) or len(value)!=1 or value[0].get('Id')!=image_id:
        raise NativeContractError('image_inspection_identity_mismatch')
    return value[0]


def capture(plan,evidence_dir,run=subprocess.run):
    if run is subprocess.run and Path('/root/QQ-Challenge').exists():
        raise NativeContractError('native_image_inspection_must_run_in_e2b')
    directory=Path(evidence_dir)
    agent_bytes=(directory/'agent_image.json').read_bytes()
    runtime_bytes=(directory/'runtime_images.json').read_bytes()
    agent=json.loads(agent_bytes);runtime=json.loads(runtime_bytes)
    main_bytes=(directory/'main_images.json').read_bytes() if (directory/'main_images.json').exists() else None
    main_ids=json.loads(main_bytes) if main_bytes is not None else None
    if main_ids is not None and (not isinstance(main_ids,dict) or set(main_ids)!=set(plan['tasks']) or
                                any(not isinstance(value,str) or not IMAGE.fullmatch(value) for value in main_ids.values())):
        raise NativeContractError('prebuilt_main_roster_invalid')
    if agent.get('status')!='recorded' or agent.get('tag_stable') is not True or runtime.get('qsim_status')!='recorded':
        raise NativeContractError('upstream_image_record_incomplete')
    cache={}
    def inspect(image_id):
        if image_id not in cache:cache[image_id]=inspect_image(image_id,run)
        return cache[image_id]
    agent_id=agent['image_id'];qsim_id=runtime['qsim_image_id']
    main_checks_bytes=None;main_checks=None
    if main_ids is not None:
        try:
            main_checks_bytes=(directory/'main_build_checks.json').read_bytes()
            main_checks=json.loads(main_checks_bytes)
        except (OSError,ValueError) as exc:
            raise NativeContractError('main_build_tag_checks_missing') from exc
        if any(main_checks.get(key)!=agent_id for key in ['agent_image_id_before','agent_image_id_after']):
            raise NativeContractError('main_build_agent_tag_changed')
    inspect(agent_id);qsim=inspect(qsim_id)
    if (qsim.get('Config',{}).get('Labels') or {}).get('org.opencontainers.image.revision')!=plan['source_commit']:
        raise NativeContractError('qsim_source_revision_mismatch')
    task_receipts={}
    for task,contract in plan['tasks'].items():
        record=runtime.get('verifier_images',{}).get(task,{})
        if record.get('status')!='recorded' or record.get('image_ref')!=contract['verifier_image']:
            raise NativeContractError('task_verifier_record_missing')
        verifier_id=record['image_id'];verifier=inspect(verifier_id)
        expected=plan['verifiers'][contract['verifier_image']]
        if expected['derived_from_qsim']:
            layers=qsim.get('RootFS',{}).get('Layers',[])
            if not layers or verifier.get('RootFS',{}).get('Layers',[])[:len(layers)]!=layers:
                raise NativeContractError('verifier_qsim_base_mismatch')
            if (verifier.get('Config',{}).get('Labels') or {}).get('org.opencontainers.image.revision')!=plan['source_commit']:
                raise NativeContractError('verifier_inherited_source_revision_mismatch')
        else:
            labels=verifier.get('Config',{}).get('Labels') or {}
            if labels.get('qiqcbench.verifier.source_revision')!=plan['source_commit'] or labels.get('qiqcbench.verifier.content_digest')!=expected['context']['content_digest']:
                raise NativeContractError('standalone_verifier_source_mismatch')
        task_receipts[task]={'source_commit':plan['source_commit'],'source_files_sha256':plan['source_files_sha256'],
            'verifier_context_digest':expected['context']['content_digest'],
            'image_ids':{'agent':agent_id,'qsim':qsim_id,'verifier':verifier_id}}
        if main_ids is not None:
            main_image=inspect(main_ids[task])
            base_layers=inspect(agent_id).get('RootFS',{}).get('Layers',[])
            if not base_layers or main_image.get('RootFS',{}).get('Layers',[])[:len(base_layers)]!=base_layers:
                raise NativeContractError('main_agent_base_mismatch')
            task_receipts[task]['image_ids']['main']=main_ids[task]
            task_receipts[task]['main_build_context']={
                'path':contract['main_context'],
                'dockerfile':contract['main_dockerfile'],
                'dockerfile_sha256':plan['source_file_checks'][contract['main_dockerfile']]['sha256'],
                'source_files_sha256':plan['source_files_sha256'],
                'agent_tag_checks':main_checks,
                'build_argv':[part.replace('{agent_image_id}',agent_id) for part in contract['main_build_argv_template']]}
    return {'schema':'qiqc-native-build-receipt/v1','source_commit':plan['source_commit'],
            'source_files_sha256':plan['source_files_sha256'],
            'upstream_records_sha256':{'agent_image.json':hashlib.sha256(agent_bytes).hexdigest(),'runtime_images.json':hashlib.sha256(runtime_bytes).hexdigest(),
                                      **({'main_images.json':hashlib.sha256(main_bytes).hexdigest(),
                                          'main_build_checks.json':hashlib.sha256(main_checks_bytes).hexdigest()} if main_bytes is not None else {})},
            'task_receipts':task_receipts,'image_inspects_by_id':cache,
            'main_probe':{'packages':agent['packages'],'agent_lib':agent['agent_lib']},
            'runtime_accepted':False,'task_main_images_prebuilt':main_ids is not None,
            'task_main_images_bound_after_harbor_build':False}


def task_runtime_seed(receipt,plan,task_id,main_container,run=subprocess.run):
    """Bind the task-specific main image after Harbor actually builds/starts it."""
    if task_id not in plan['tasks'] or task_id not in receipt['task_receipts']:
        raise NativeContractError('task_not_in_build_receipt')
    value=json.loads(json.dumps(receipt['task_receipts'][task_id]))
    if 'main' in value['image_ids'] and value['image_ids']['main']!=main_container.get('Image'):
        raise NativeContractError('running_main_differs_from_prebuilt_image')
    image=inspect_image(main_container.get('Image',''),run)
    base=receipt['image_inspects_by_id'][value['image_ids']['agent']]
    layers=base.get('RootFS',{}).get('Layers',[])
    if not layers or image.get('RootFS',{}).get('Layers',[])[:len(layers)]!=layers:
        raise NativeContractError('main_agent_base_mismatch')
    value['image_ids']['main']=image['Id']
    images={role:receipt['image_inspects_by_id'][image_id] for role,image_id in value['image_ids'].items() if role!='main'}
    images['main']=image
    return {'build_receipt':value,'image_inspects':images,'main_probe':receipt['main_probe'],
            'container_inspects':{'main':main_container}}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--plan',type=Path,required=True)
    parser.add_argument('--evidence-dir',type=Path,default=Path('/opt/qiqc-build-evidence'))
    parser.add_argument('--output',type=Path,default=Path('/opt/qiqc-native-build-receipt.json'))
    args=parser.parse_args()
    result=capture(json.loads(args.plan.read_text()),args.evidence_dir)
    with args.output.open('x') as stream:json.dump(result,stream,sort_keys=True,indent=2)
    print(json.dumps({'status':'build_receipt_recorded','task_count':len(result['task_receipts']),'runtime_accepted':False}))


if __name__=='__main__':main()
