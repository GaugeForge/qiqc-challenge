"""Read-only upstream native Docker plan and evidence validation.

Never starts Docker, imports benchmark science, or relies on qiqcbench-runtime.
All actual builds/probes must run in the trusted E2B control environment.
"""
from __future__ import annotations

import hashlib
import json
import posixpath
import re
import tomllib
from pathlib import Path, PurePosixPath

import yaml

SHA = re.compile(r'[a-f0-9]{64}\Z')
IMAGE = re.compile(r'sha256:[a-f0-9]{64}\Z')
TASK = re.compile(r'[a-z][a-z0-9_]{0,99}\Z')


class NativeContractError(ValueError):
    """A fixed safe classification, with no scientific payload in its message."""


def _canonical(value):
    if not isinstance(value, str) or not value or '\\' in value:
        raise NativeContractError('invalid_source_path')
    path = PurePosixPath(value)
    if path.is_absolute() or any(p in {'', '.', '..'} for p in value.split('/')):
        raise NativeContractError('invalid_source_path')
    return path


class Source:
    def __init__(self, root, audit):
        self.root = Path(root).resolve()
        self.audit = json.loads(Path(audit).read_text())
        if self.audit.get('schema') != 'qiqc-upstream-restoration-audit/v1':
            raise NativeContractError('invalid_restoration_audit')
        if not re.fullmatch('[a-f0-9]{40}', self.audit.get('source_commit', '')):
            raise NativeContractError('invalid_source_commit')
        self.used = {}

    def verify_tree(self):
        observed = set()
        for path in self.root.rglob('*'):
            relative = path.relative_to(self.root).as_posix()
            if relative == '.git' or relative.startswith('.git/') or path.is_dir() and not path.is_symlink():
                continue
            observed.add(relative)
            record = self.audit['files'].get(relative)
            if record is None:
                raise NativeContractError('untracked_source_input')
            if path.is_symlink():
                import os
                data = os.readlink(path).encode()
                if record.get('git_mode') != '120000':
                    raise NativeContractError('source_type_changed')
            else:
                data = path.read_bytes()
            if hashlib.sha256(data).hexdigest() != record['sha256'] or len(data) != record['bytes']:
                raise NativeContractError('source_input_changed')
        if observed != set(self.audit['files']):
            raise NativeContractError('source_inventory_changed')

    def read(self, relative):
        parts = _canonical(relative).parts
        path = self.root
        for part in parts:
            path = path / part
            if path.is_symlink():
                raise NativeContractError('linked_source_input')
        record = self.audit['files'].get(relative)
        if not record or record.get('git_mode') not in {'100644', '100755'}:
            raise NativeContractError('source_input_not_in_audit')
        data = path.read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        if digest != record['sha256'] or len(data) != record['bytes']:
            raise NativeContractError('source_input_changed')
        self.used[relative] = {'sha256': digest, 'bytes': len(data)}
        return data


def _environment(raw):
    if isinstance(raw, dict):
        return {str(k): str(v) for k, v in raw.items()}
    values = {}
    for entry in raw or []:
        key, sep, value = entry.partition('=')
        if not sep or key in values:
            raise NativeContractError('invalid_compose_environment')
        values[key] = value
    return values


def _default_environment(raw):
    """Resolve only the original Compose ${NAME:-default} form, without host env."""
    values=_environment(raw)
    for key,value in values.items():
        match=re.fullmatch(r'\$\{[A-Z0-9_]+:-([^}]*)\}',value)
        if match:
            values[key]=match.group(1)
        elif '$' in value:
            raise NativeContractError('unsupported_compose_environment_interpolation')
    return values


def _compose_memory_mb(value):
    if value is None:return None
    if isinstance(value,int) and not isinstance(value,bool):return value/(1024*1024)
    match=re.fullmatch(r'([0-9]+(?:\.[0-9]+)?)([bkmg])',str(value).lower())
    if not match:raise NativeContractError('unsupported_original_memory_limit')
    return float(match.group(1))*{'b':1/(1024*1024),'k':1/1024,'m':1,'g':1024}[match.group(2)]


def role_resources(cfg, compose):
    """Original limits, not simultaneous physical reservations."""
    return {'main':{key:cfg['environment'].get(key) for key in ('cpus','memory_mb')},
            'verifier':{key:cfg['verifier']['environment'].get(key) for key in ('cpus','memory_mb')},
            'qsim':{'cpus':compose['services']['qsim'].get('cpus'),
                    'memory_mb':_compose_memory_mb(compose['services']['qsim'].get('mem_limit'))}}


def execution_budget(plan, task_id, continuous_limit_seconds=3600, *, acceptance_scope="full-runtime", agent_budget_seconds=300, native_limits=None):
    """Keep the original verifier timeout and a bounded diagnostic envelope.

    Provisioning/setup and artifact/archive time are organizer control budgets,
    never claimed as upstream scientific timeouts. Unsupported account windows
    fail before cloud allocation in full-runtime mode. Bounded-functional mode
    records the unmet complete window while retaining original verifier settings;
    a real verifier timeout is still a failed evaluation, never a normal reward.
    """
    if acceptance_scope not in ('full-runtime','bounded-functional','bounded-five-minute','bounded-three-minute','competition-native','practice-native'):raise NativeContractError('invalid_acceptance_scope')
    from native_compose_contract import agent_budget_from_identity
    agent_budget_from_identity({'agent_budget_seconds':agent_budget_seconds,'acceptance_scope':acceptance_scope,'native_limits':native_limits})
    task=task_contract(plan,task_id)
    verifier=task['timeouts']['verifier']
    setup=600
    if acceptance_scope in ('competition-native','practice-native'):
        if (not isinstance(native_limits,dict) or native_limits.get('agent_seconds')!=task['timeouts']['agent'] or native_limits.get('verifier_seconds')!=verifier):raise NativeContractError('native_task_limits_mismatch')
        setup=native_limits['build_seconds']+native_limits['setup_seconds']+300

    if not isinstance(verifier,(int,float)) or isinstance(verifier,bool) or verifier<=0:
        raise NativeContractError('invalid_original_verifier_timeout')
    value={'schema':'qiqc-native-execution-budget/v1','agent_seconds':agent_budget_seconds,
           'original_verifier_seconds':verifier,'provisioning_setup_seconds':setup,
           'collection_archive_cleanup_seconds':300}
    value['runner_deadline_seconds']=setup+agent_budget_seconds+verifier
    value['sandbox_ttl_seconds']=value['runner_deadline_seconds']+300
    value['account_continuous_limit_seconds']=continuous_limit_seconds
    value['full_verifier_window_supported']=value['sandbox_ttl_seconds']<=continuous_limit_seconds
    value['acceptance_scope']=acceptance_scope
    if acceptance_scope in ('bounded-functional','bounded-five-minute','bounded-three-minute'):
        value['sandbox_ttl_seconds']=min(value['sandbox_ttl_seconds'],continuous_limit_seconds)
        value['runner_deadline_seconds']=value['sandbox_ttl_seconds']-300
        if value['runner_deadline_seconds']<=600+agent_budget_seconds:raise NativeContractError('bounded_account_window_too_short')
    value['account_window_supported']=value['sandbox_ttl_seconds']<=continuous_limit_seconds
    resources=task['role_resources']
    value['minimum_role_memory_mb']=max(r.get('memory_mb') or 0 for r in resources.values())
    value['minimum_role_cpus']=max(r.get('cpus') or 0 for r in resources.values())
    value['memory_policy']='maximum_individual_original_role_limit_not_sum_of_cgroup_limits'
    return value


def enrich_role_resources(plan, source_root):
    """Derive new runtime checks from source bytes already bound by old receipts.

    No frozen build-plan file or image is rewritten. This permits stronger
    controller verification without rebuilding identical scientific images.
    """
    root=Path(source_root)
    for task,row in plan['tasks'].items():
        values=[]
        for relative in (f'harbor_tasks/{task}/task.toml',row['compose_file']):
            path=root/relative
            if path.is_symlink():raise NativeContractError('linked_resource_contract')
            data=path.read_bytes()
            if hashlib.sha256(data).hexdigest()!=plan['source_file_checks'][relative]['sha256']:
                raise NativeContractError('resource_contract_source_mismatch')
            values.append(data.decode())
        row['role_resources']=role_resources(tomllib.loads(values[0]),yaml.safe_load(values[1]))
    return plan


def _mounts(task, service):
    output = []
    for entry in service.get('volumes', []):
        if not isinstance(entry, str):
            raise NativeContractError('unsupported_upstream_mount')
        fields = entry.split(':')
        if len(fields) not in (2, 3):
            raise NativeContractError('unsupported_upstream_mount')
        source, target = fields[:2]
        readonly = len(fields) == 3 and fields[2] == 'ro'
        kind = 'bind' if source.startswith('.') else 'volume'
        if kind == 'bind':
            source = posixpath.normpath(f'harbor_tasks/{task}/environment/{source}')
            _canonical(source)
        output.append({'type': kind, 'source': source, 'target': target, 'read_only': readonly})
    return output


def _verifier_context(source, task):
    prefix = f'harbor_tasks/{task}/tests'
    manifest = prefix + '/verifier-context.json'
    entries = []
    digest = hashlib.sha256()
    if manifest in source.audit['files']:
        value = json.loads(source.read(manifest))
        if value.get('schema_version') != 1 or set(value) != {'schema_version', 'files'}:
            raise NativeContractError('invalid_verifier_context_manifest')
        for item in value['files']:
            if set(item) != {'source', 'target', 'mode'} or item['mode'] not in {'0400', '0644', '0755'}:
                raise NativeContractError('invalid_verifier_context_entry')
            _canonical(item['source'])
            _canonical(item['target'])
            entries.append(dict(item))
        if len({e['target'] for e in entries}) != len(entries):
            raise NativeContractError('duplicate_verifier_context_target')
        for entry in sorted(entries, key=lambda e: e['target']):
            data = source.read(entry['source'])
            digest.update(entry['target'].encode() + b'\0' + entry['mode'].encode() + b'\0' + data + b'\0')
            entry['sha256'] = hashlib.sha256(data).hexdigest()
        mode = 'manifest_allowlist'
    else:
        # Same digest semantics as tools/build_qsim_images.py. Unknown added
        # files fail Source.read instead of silently entering the build context.
        for path in sorted((source.root / prefix).rglob('*')):
            if '__pycache__' in path.parts or path.is_dir():
                continue
            relative = path.relative_to(source.root).as_posix()
            target = path.relative_to(source.root / prefix).as_posix()
            data = source.read(relative)
            digest.update(target.encode() + b'\0' + data + b'\0')
            entries.append({'source': relative, 'target': target, 'sha256': hashlib.sha256(data).hexdigest()})
        mode = 'original_tests_directory'
    return {'mode': mode, 'source_directory': prefix, 'files': entries, 'content_digest': digest.hexdigest()}


def build_plan(source_root, restoration_audit=None):
    """Produce a candidate plan. This does not freeze or approve a release."""
    root = Path(source_root)
    audit = restoration_audit or root.parent / 'benchmark-releases/restored-source-audit-20261020.json'
    source = Source(root, audit)
    source.verify_tree()
    tasks = source.audit['competition_task_ids']
    if len(tasks) != 49 or len(set(tasks)) != 49 or any(not TASK.fullmatch(t) for t in tasks):
        raise NativeContractError('invalid_competition_task_roster')
    commit = source.audit['source_commit']
    for relative in ['AGENTS.md', 'README.md', 'PLAN.md', '.dockerignore', 'docker/agent/Dockerfile',
                     'docker/qsim/Dockerfile', 'docker/qsim/requirements.txt', 'docker/qsim/entrypoint.sh',
                     'tools/build_qsim_images.py', 'tools/record_agent_image.py']:
        source.read(relative)
    agent_pins = dict(re.findall(rb'"([A-Za-z0-9_.-]+)==([^"\s]+)"', source.read('docker/agent/Dockerfile')))
    agent_pins = {name.decode(): value.decode() for name,value in agent_pins.items()}
    clients = {}
    for path in sorted(source.audit['files']):
        if path.startswith('docker/agent/lib/'):
            clients[path.removeprefix('docker/agent/lib/')] = hashlib.sha256(source.read(path)).hexdigest()
    rows, verifiers = {}, {}
    for task in sorted(tasks):
        directory = f'harbor_tasks/{task}'
        cfg = tomllib.loads(source.read(directory + '/task.toml').decode())
        compose = yaml.safe_load(source.read(directory + '/environment/docker-compose.yaml'))
        source.read(directory + '/environment/Dockerfile')
        docker = source.read(directory + '/tests/Dockerfile').decode()
        if set(compose['services']) != {'main','qsim'} or cfg['verifier']['environment_mode'] != 'separate':
            raise NativeContractError('unexpected_upstream_role_topology')
        qsim = compose['services']['qsim']
        image = cfg['verifier']['environment']['docker_image']
        context = _verifier_context(source, task)
        task_config = yaml.safe_load(source.read(f'configs/tasks/{task}.yaml'))
        resource = cfg['environment']
        main_mounts = _mounts(task, compose['services']['main'])
        for mount in main_mounts:
            if mount['type'] == 'bind' and (mount['source'] != f'configs/task_materials/{task}/public' or not mount['read_only']):
                raise NativeContractError('main_mount_crosses_private_boundary')
        rows[task] = {
            'task_id': task, 'main_dockerfile': directory + '/environment/Dockerfile',
            'main_context': directory + '/environment', 'compose_file': directory + '/environment/docker-compose.yaml',
            'main_build_argv_template': ['docker','build','--build-arg','AGENT_IMAGE=qiqcbench-agent:latest',
                                         '-t','qiqc-native-main-' + task + ':candidate', directory + '/environment'],
            'main_build_base_policy': {'reference':'qiqcbench-agent:latest',
                                       'require_tag_image_id_before_after_equal_recorded_agent':True,
                                       'require_result_agent_layer_prefix':True},
            'qsim_image': qsim['image'], 'verifier_image': image,
            'main_mounts': main_mounts, 'qsim_mounts': _mounts(task,qsim),
            'main_environment': _default_environment(compose['services']['main'].get('environment')),
            'qsim_environment_keys': sorted(_environment(qsim.get('environment'))),
            'qsim_environment_sha256': hashlib.sha256(json.dumps(_environment(qsim.get('environment')),sort_keys=True).encode()).hexdigest(),
            'qsim_runtime_environment_sha256': hashlib.sha256(json.dumps(_default_environment(qsim.get('environment')),sort_keys=True).encode()).hexdigest(),
            'resources': {'cpus': resource['cpus'], 'memory_mb': resource['memory_mb'],
                          'scope': 'original Harbor environment request; outer sandbox also needs qsim/Docker headroom'},
            'role_resources': role_resources(cfg,compose),
            'timeouts': {name:cfg[name].get('timeout_sec') for name in ['agent','verifier']},
            'surfaces': {key:task_config.get(key,[]) for key in ['default_surfaces','allowed_surfaces']},
            'artifacts':cfg.get('artifacts',[]),
            'private_read_negative_paths': ['/var/run/docker.sock','/app/configs', '/app/qiqcbench_configs',
                                            '/qsim_logs/experiment_log.jsonl','/qsim_logs/final_answer.json',
                                            '/qsim_logs/private','/verifier/private'],
        }
        if image not in verifiers:
            staged = '__STAGED_VERIFIER_CONTEXT__/' + task if context['mode']=='manifest_allowlist' else context['source_directory']
            verifiers[image] = {'tasks': [], 'context': context, 'build_argv_template': ['docker','build','--build-arg',f'SOURCE_REVISION={commit}',
                                '--build-arg','CONTENT_DIGEST='+context['content_digest'],'-t',image,staged],
                                'derived_from_qsim': re.findall(r'^FROM\s+(\S+)',docker,re.M)[-1]=='qiqcbench-qsim:latest'}
        elif context['content_digest'] != verifiers[image]['context']['content_digest']:
            # Upstream builder intentionally uses the first task context for a
            # shared image. Preserve that behavior and make the difference visible.
            rows[task]['shared_verifier_uses_first_context'] = True
        verifiers[image]['tasks'].append(task)
    return {'schema':'qiqc-native-build-plan/v1','status':'candidate-not-runtime-accepted-not-frozen',
            'source_commit':commit,'source_files_sha256':source.audit['source_files_sha256'],
            'source_tree':source.audit['source_tree'],'source_root':str(root.resolve()),
            'commands_run_on_host':False, 'source_file_checks':source.used,
            'agent':{'image':'qiqcbench-agent:latest','context':'.','dockerfile':'docker/agent/Dockerfile',
                     'build_argv':['docker','build','-t','qiqcbench-agent:latest','-f','docker/agent/Dockerfile','.'],
                     'direct_packages':agent_pins,'client_sha256':clients},
            'qsim':{'image':'qiqcbench-qsim:latest','context':'.','dockerfile':'docker/qsim/Dockerfile',
                    'build_argv':['docker','build','--build-arg',f'SOURCE_REVISION={commit}','-t','qiqcbench-qsim:latest','-f','docker/qsim/Dockerfile','.'],
                    'requirements_sha256':hashlib.sha256(source.read('docker/qsim/requirements.txt')).hexdigest()},
            'verifiers':verifiers,'tasks':rows,
            'build_order':['agent base','qsim base','selected separate verifier','selected task main FROM original agent tag with before/after immutable-ID verification'],
            'required_receipt':['source_files_sha256','source_commit','build_context_digest','Docker image ID','RootFS layers','base image IDs','actual installed package versions']}


def task_contract(plan, task_id):
    if task_id not in plan['tasks']:
        raise NativeContractError('task_not_in_native_plan')
    return plan['tasks'][task_id]


def validate_runtime(plan, task_id, observed, *, acceptance_scope="full-runtime"):
    """Fail closed over trusted probe evidence; return safe reason codes only.

    `observed`: build_receipt, image_inspects (agent/main/qsim/verifier),
    container_inspects (main/qsim/verifier), main_probe (packages/agent_lib),
    negative_reads (path -> exit code), readonly_write_exit, qsim_http_status,
    resources, compose_project, source_root. Raw Docker inspect is retained only
    privately; never return its Env or task data through this API.
    """
    if acceptance_scope not in ('full-runtime','bounded-functional','bounded-five-minute','bounded-three-minute','competition-native','practice-native'):raise NativeContractError('invalid_acceptance_scope')
    task = task_contract(plan, task_id)
    errors = []
    receipt = observed.get('build_receipt', {})
    if any(receipt.get(key) != plan[key] for key in ['source_commit','source_files_sha256']):
        errors.append('build_source_binding_mismatch')
    verifier = plan['verifiers'][task['verifier_image']]
    if receipt.get('verifier_context_digest') != verifier['context']['content_digest']:
        errors.append('verifier_context_digest_mismatch')
    images, containers = observed.get('image_inspects',{}), observed.get('container_inspects',{})
    for role in ['agent','main','qsim','verifier']:
        image = images.get(role,{})
        image_id = image.get('Id','')
        if not IMAGE.fullmatch(image_id) or image_id != receipt.get('image_ids',{}).get(role):
            errors.append(role+'_image_identity_mismatch')
        if role != 'agent':
            container = containers.get(role,{})
            if container.get('Image') != image_id:
                errors.append(role+'_running_image_mismatch')
            host = container.get('HostConfig',{})
            if host.get('Privileged') is not False or host.get('PidMode')=='host' or host.get('NetworkMode')=='host' or set(host.get('CapAdd') or []) & {'SYS_ADMIN','ALL'}:
                errors.append(role+'_unsafe_container_privileges')
    for child,base in [('main','agent'), *([('verifier','qsim')] if verifier['derived_from_qsim'] else [])]:
        parent_layers = images.get(base,{}).get('RootFS',{}).get('Layers',[])
        child_layers = images.get(child,{}).get('RootFS',{}).get('Layers',[])
        if not parent_layers or child_layers[:len(parent_layers)] != parent_layers:
            errors.append(child+'_base_layers_mismatch')
    labels = images.get('qsim',{}).get('Config',{}).get('Labels') or {}
    if labels.get('org.opencontainers.image.revision') != plan['source_commit']:
        errors.append('qsim_source_label_mismatch')
    probe = observed.get('main_probe',{})
    normalize = lambda name: re.sub(r'[-_.]+','-',name).lower()
    installed = {normalize(k):v for k,v in probe.get('packages',{}).items()}
    if any(installed.get(normalize(k)) != v for k,v in plan['agent']['direct_packages'].items()):
        errors.append('main_scientific_dependencies_mismatch')
    if probe.get('agent_lib') != plan['agent']['client_sha256']:
        errors.append('main_client_library_mismatch')
    project = observed.get('compose_project','')
    source_root = observed.get('source_root','')
    if not re.fullmatch(r'[a-z0-9][a-z0-9_-]{0,62}',project) or not source_root.startswith('/'):
        errors.append('runtime_location_binding_missing')
    for role in ['main','qsim']:
        actual = containers.get(role,{}).get('Mounts',[])
        by_target = {mount.get('Destination'):mount for mount in actual}
        for mount in task[role+'_mounts']:
            got = by_target.get(mount['target'],{})
            source_matches = (got.get('Name')==project+'_'+mount['source']) if mount['type']=='volume' else (got.get('Source')==source_root+'/'+mount['source'])
            if got.get('Type') != mount['type'] or got.get('RW') != (not mount['read_only']) or not source_matches:
                errors.append(role+'_declared_mount_mismatch')
        if role=='main':
            expected = {mount['target'] for mount in task['main_mounts']}
            # Exact stock Harbor Trial._agent_env_mounts mappings, not arbitrary
            # host binds hidden behind an allowed container destination.
            trial_root=observed.get('trial_root','')
            # The competition adapter removes stock Harbor's shared verifier
            # output bind from main: background Agent processes must not see or
            # mutate grading outputs while the separate verifier is running.
            outputs={'/logs/agent':'agent','/logs/artifacts':'artifacts/logs/artifacts'}
            for target,relative in outputs.items():
                if target in by_target:
                    got=by_target[target]
                    if not trial_root.startswith('/') or trial_root=='/' or '..' in PurePosixPath(trial_root).parts or got.get('Type')!='bind' or got.get('Source')!=trial_root+'/'+relative or got.get('RW') is not True:
                        errors.append('main_harbor_output_mount_mismatch')
            extras = set(by_target)-expected-set(outputs)
            if extras:
                errors.append('main_unapproved_mount')
    for role in ['main','qsim','verifier']:
        for entry in containers.get(role,{}).get('Config',{}).get('Env',[]):
            if entry.partition('=')[0] in {'E2B_API_KEY','R2_ACCESS_KEY_ID','R2_SECRET_ACCESS_KEY','HY_API','OPENROUTER_API_KEY'}:
                errors.append(role+'_backend_credential_exposed')
            if role == 'main' and entry.partition('=')[0] in {
                'QIQCBENCH_HIDDEN_COMMITMENT_SECRET', 'QIQCBENCH_EXECUTION_CONTEXT_ID',
                'QIQCBENCH_SHADOW_EXPECTED_EXECUTION_CONTEXT_ID', 'QIQCBENCH_EVAL_VERIFIER_CONTEXT'}:
                errors.append('main_private_execution_binding_exposed')
    for role in ['main','qsim']:
        raw_env=containers.get(role,{}).get('Config',{}).get('Env',[])
        env={entry.partition('=')[0]:entry.partition('=')[2] for entry in raw_env}
        if len(env)!=len(raw_env):
            errors.append(role+'_duplicate_environment_key')
        if role=='main':
            if any(env.get(key)!=value for key,value in task['main_environment'].items()):
                errors.append('main_original_environment_mismatch')
        else:
            selected={key:env.get(key) for key in task['qsim_environment_keys']}
            actual_hash=hashlib.sha256(json.dumps(selected,sort_keys=True).encode()).hexdigest()
            if actual_hash!=task.get('qsim_runtime_environment_sha256'):
                errors.append('qsim_original_environment_mismatch')
    negatives = observed.get('negative_reads',{})
    if any(type(negatives.get(path)) is not int or negatives[path] != 1 for path in task['private_read_negative_paths']):
        errors.append('main_private_read_negative_check_missing_or_failed')
    if observed.get('readonly_write_exit') != 1:
        errors.append('main_public_volume_write_negative_check_missing_or_failed')
    if observed.get('qsim_http_status') != 200:
        errors.append('main_qsim_service_unreachable')
    resources = observed.get('resources',{})
    if not isinstance(resources,dict) or any(type(resources.get(key)) not in (int,float) or not 0<resources[key]<1000000000 for key in ('cpus','memory_mb')):
        errors.append('actual_host_resources_missing_or_invalid')
        resources={'cpus':0,'memory_mb':0}
    if resources.get('cpus',0) < task['resources']['cpus'] or resources.get('memory_mb',0) < task['resources']['memory_mb']:
        errors.append('outer_sandbox_below_original_task_resources')
    for role,required in task['role_resources'].items():
        host=containers.get(role,{}).get('HostConfig',{})
        memory=required.get('memory_mb');cpu=required.get('cpus')
        if memory is not None:
            if resources.get('memory_mb',0)<memory:errors.append('outer_sandbox_below_'+role+'_original_memory')
            if host.get('Memory')!=int(memory*1024*1024):errors.append(role+'_original_memory_limit_mismatch')
        elif host.get('Memory',0)!=0:errors.append(role+'_unexpected_memory_limit')
        if cpu is not None:
            if resources.get('cpus',0)<cpu:errors.append('outer_sandbox_below_'+role+'_original_cpus')
            if host.get('NanoCpus')!=int(cpu*1000000000):errors.append(role+'_original_cpu_limit_mismatch')
        elif host.get('NanoCpus',0)!=0 or host.get('CpuQuota',0)>0:
            errors.append(role+'_unexpected_cpu_limit')
        if containers.get(role,{}).get('State',{}).get('OOMKilled') is True:
            errors.append(role+'_oom_killed')
    if acceptance_scope in ('bounded-functional','bounded-five-minute','bounded-three-minute'):
        errors=[error for error in errors if not error.startswith('outer_sandbox_below_')]
    return sorted(set(errors))


def runtime_verification(plan,task_id,observed):
    """Separate original software/cgroup proof from full physical capacity."""
    errors=validate_runtime(plan,task_id,observed)
    capacity=[e for e in errors if e.startswith('outer_sandbox_below_')]
    limits=[e for e in errors if e.endswith(('_memory_limit_mismatch','_cpu_limit_mismatch','_unexpected_memory_limit','_unexpected_cpu_limit'))]
    functional=[e for e in errors if e not in capacity]
    return {'source_runtime_software_verified':not [e for e in functional if e not in limits],
            'original_container_limits_verified':not limits,
            'full_host_capacity_verified':not capacity,
            'source_runtime_parity_verified':not errors,
            'bounded_functional_errors':functional,'full_host_capacity_errors':capacity}


def probe_commands(task):
    """Data-free checks to run inside MAIN; results carry exit codes only."""
    import shlex
    return {'negative_reads':{p:'test -r '+shlex.quote(p) for p in task['private_read_negative_paths']},
            'readonly_write':'touch /qsim_logs/public_job_results/.qiqc-readonly-probe',
            'service':'curl --silent --output /dev/null --write-out "%{http_code}" http://qsim:8123/healthz'}
