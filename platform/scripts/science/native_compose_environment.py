"""Thin E2B-host guard around unmodified Harbor DockerEnvironment operations."""
from __future__ import annotations
import asyncio
import json
import secrets
import subprocess
import time
from pathlib import Path
import yaml
from harbor.environments.docker.docker import DockerEnvironment, _sanitize_docker_compose_project_name
from harbor.models.task.config import NetworkMode
from native_compose_contract import validate_main_container

EVIDENCE = {}
EXECUTION_BINDINGS = {}


def bind_private_execution(overlay, task_id, role, run_handle):
    """Use Shadow's upstream direct binding for standalone Harbor trials.

    The original scorer explicitly supports these two verifier variables. The
    qsim constructor writes the real bootstrap event and device commitments;
    no organizer-side tool call or fabricated science evidence is involved.
    These opaque values belong only to qsim/verifier, never contestant main.
    Other tasks retain their original standalone execution contract.
    """
    if task_id != 'time_budgeted_shadow_surrogate_60q':
        return
    if run_handle not in EXECUTION_BINDINGS:
        EXECUTION_BINDINGS[run_handle] = (secrets.token_hex(32), secrets.token_hex(32))
    context, secret = EXECUTION_BINDINGS[run_handle]
    service = overlay['services']['qsim' if role == 'agent' else 'main']
    env = service.setdefault('environment', {})
    env['QIQCBENCH_HIDDEN_COMMITMENT_SECRET'] = secret
    env['QIQCBENCH_EXECUTION_CONTEXT_ID' if role == 'agent' else
        'QIQCBENCH_SHADOW_EXPECTED_EXECUTION_CONTEXT_ID'] = context


class NativeDockerEnvironment(DockerEnvironment):
    def _requires_egress_control(self, *, startup_network_policy, phase_network_policies):
        if self.native_role == 'verifier':
            # This role has a permanent Docker `network_mode: none` overlay.
            # Do not launch Harbor's separate egress sidecar/kernel probe: it
            # would be redundant and is not part of the prebuilt task images.
            for policy in (startup_network_policy, *phase_network_policies):
                if policy.network_mode not in (NetworkMode.PUBLIC, NetworkMode.NO_NETWORK):
                    raise ValueError('native_verifier_dynamic_network_policy_unsupported')
            return False
        return super()._requires_egress_control(
            startup_network_policy=startup_network_policy,
            phase_network_policies=phase_network_policies)

    def validate_network_policy_support(self, network_policy=None):
        policy = network_policy or self.network_policy
        if self.native_role == 'verifier' and policy.network_mode == NetworkMode.NO_NETWORK:
            if self._is_windows_container:
                raise ValueError('native_verifier_windows_unsupported')
            # Enforcement is the immutable none overlay plus actual inspect
            # in start(), before verifier exec. No task policy is rewritten.
            return
        return super().validate_network_policy_support(policy)

    def __init__(self, *args, native_agent_environment_dir, native_control_dir,
                 native_run_handle, native_candidate_identity, native_task_id, **kwargs):
        from native_security import compose_overlay, verifier_overlay
        self.native_run_handle = native_run_handle
        self.native_control_dir = Path(native_control_dir)
        self.native_acceptance_scope = native_candidate_identity.get('acceptance_scope','full-runtime')
        self.native_task_id = native_task_id
        self.native_source_root = str(Path(native_agent_environment_dir).parents[2])
        self.native_trial_root = str(kwargs['trial_paths'].trial_dir)
        from native_compose_observation import load_inputs
        self.native_plan,self.native_build = load_inputs(native_candidate_identity)
        self.native_role = ('agent' if Path(kwargs['environment_dir']).resolve() ==
                            Path(native_agent_environment_dir).resolve() else 'verifier')
        # Build and execution are separate phases. These IDs come from builds of
        # the original per-task Dockerfiles, never from an alternative base.
        from native_contract import IMAGE
        self.native_image_ids = self.native_build['task_receipts'][native_task_id]['image_ids']
        for role in ('main', 'qsim', 'verifier'):
            if not IMAGE.fullmatch(self.native_image_ids.get(role, '')):
                raise RuntimeError('native_prebuilt_task_image_missing:' + role)
        main_image = self.native_image_ids['main' if self.native_role == 'agent' else 'verifier']
        kwargs['task_env_config'] = kwargs['task_env_config'].model_copy(update={'docker_image': main_image})
        self.native_security_ready = False
        self.native_security_receipt = {}
        super().__init__(*args, **kwargs)
        if self.native_role == 'agent':
            self._mounts = [mount for mount in self._mounts if mount.get('target') != '/logs/verifier']
        project = _sanitize_docker_compose_project_name(self.session_id)
        overlay = compose_overlay(project) if self.native_role == 'agent' else verifier_overlay()
        bind_private_execution(overlay, native_task_id, self.native_role, native_run_handle)
        overlay['services']['main'].update(image=main_image, pull_policy='never')
        if self.native_role == 'agent':
            overlay['services']['qsim'].update(image=self.native_image_ids['qsim'], pull_policy='never')
        path = Path(native_control_dir) / (project+'-isolation.yaml')
        path.write_text(yaml.safe_dump(overlay, sort_keys=False))
        path.chmod(0o600)
        self.extra_docker_compose_paths.append(path)

    async def _run_docker_compose_command(self, command, *args, **kwargs):
        if command and command[0] in ('build', 'pull'):
            raise RuntimeError('native_runtime_build_or_pull_forbidden')
        if command and command[0] == 'up':
            command = [command[0], '--no-build', '--pull', 'never', *command[1:]]
        return await super()._run_docker_compose_command(command, *args, **kwargs)

    async def _container_id(self, service):
        result=await self._run_docker_compose_command(['ps','-q',service],check=True)
        cid=(result.stdout or '').strip()
        if len(cid)!=64 or any(c not in '0123456789abcdef' for c in cid):
            raise RuntimeError('native_container_id_missing')
        return cid

    async def start(self, force_build):
        if force_build:
            raise RuntimeError('native_runtime_force_build_forbidden')
        if self.native_role == 'verifier' and not EVIDENCE.get(self.native_run_handle,{}).get('main_stopped_before_verifier'):
            raise RuntimeError('native_main_not_stopped_before_verifier')
        await super().start(force_build)
        main_id=await self._container_id('main')
        def inspect():
            return json.loads(subprocess.run(['docker','inspect',main_id],check=True,
                                             capture_output=True,text=True,timeout=15).stdout)[0]
        observation=await asyncio.to_thread(inspect)
        expected_image=self.native_image_ids['main' if self.native_role=='agent' else 'verifier']
        if observation.get('Image') != expected_image:
            raise RuntimeError('native_running_image_differs_from_build')
        structural=validate_main_container(observation)
        if self.native_role == 'agent':
            from native_security import install
            qsim_id=await self._container_id('qsim')
            project=_sanitize_docker_compose_project_name(self.session_id)
            receipt=await asyncio.to_thread(install,project,main_id,qsim_id)
            self.native_security_receipt={'structural':structural,'firewall':receipt}
            from native_compose_observation import main_observation
            observed=await asyncio.to_thread(main_observation,self.native_plan,self.native_build,self.native_task_id,
                main_id,qsim_id,receipt,project,self.native_source_root,self.native_trial_root)
            EVIDENCE.setdefault(self.native_run_handle,{})['runtime_observation']=observed
        else:
            if observation.get('HostConfig',{}).get('NetworkMode')!='none':
                raise RuntimeError('native_verifier_network_not_disabled')
            self.native_security_receipt={'structural':structural,'network_mode':'none'}
            observed=EVIDENCE[self.native_run_handle]['runtime_observation']
            observed['container_inspects']['verifier']=observation
            from native_contract import validate_runtime, runtime_verification
            errors=validate_runtime(self.native_plan,self.native_task_id,observed,
                                    acceptance_scope=self.native_acceptance_scope)
            EVIDENCE[self.native_run_handle]['runtime_verification']=runtime_verification(
                self.native_plan,self.native_task_id,observed)
            if errors:raise RuntimeError('native_runtime_contract_rejected:'+','.join(errors))
        self.native_security_ready=True
        EVIDENCE.setdefault(self.native_run_handle,{})[self.native_role]=self.native_security_receipt

    async def stop_service(self, service):
        if self.native_role=='agent' and service=='main':
            from native_compose_observation import container_inspect
            evidence=EVIDENCE[self.native_run_handle]
            if 'main_before_planned_stop' not in evidence:
                cid=evidence['runtime_observation']['container_inspects']['main']['Id']
                evidence['main_before_planned_stop']=await asyncio.to_thread(container_inspect,cid)
        await super().stop_service(service)
        if self.native_role == 'agent' and service == 'main':
            from native_compose_observation import container_inspect
            observed=EVIDENCE[self.native_run_handle]['runtime_observation']
            current=await asyncio.to_thread(container_inspect,observed['container_inspects']['main']['Id'])
            stopped=current.get('State',{}).get('Running') is False
            EVIDENCE[self.native_run_handle]['main_stopped_before_verifier']=stopped
            if not stopped:raise RuntimeError('native_main_stop_not_confirmed')
            execution=EVIDENCE[self.native_run_handle].get('agent_execution_stop')
            if execution:
                marker={**execution,'schema':'qiqc-native-agent-stop/v1','main_stopped':True,
                        'main_container_id':current['Id'],'completed_at':time.time()}
                target=Path(self.native_trial_root).parent/'agent-stop.json'
                temporary=target.with_suffix('.tmp')
                temporary.write_text(json.dumps(marker,sort_keys=True));temporary.chmod(0o600)
                temporary.replace(target)
                EVIDENCE[self.native_run_handle]['agent_stop_marker']=marker

    async def stop(self, delete):
        # Harbor destroys the verifier environment immediately after verify(),
        # and the agent environment later in trial cleanup. Capture each role
        # before that normal destruction, not after docker compose down.
        from native_compose_observation import container_inspect
        from native_final_containers import SCHEMA
        evidence=EVIDENCE.setdefault(self.native_run_handle,{})
        terminal=evidence.setdefault('final_container_evidence',{
            'schema':SCHEMA,'containers':{},'observed_before_cleanup':{},
            'collected_at':{},'capture_errors':[]})
        roles=('main','qsim') if self.native_role=='agent' else ('verifier',)
        try:
            originals=evidence.get('runtime_observation',{}).get('container_inspects',{})
            for role in roles:
                if role in terminal['containers']:continue
                try:
                    cid=originals[role]['Id']
                    terminal['containers'][role]=await asyncio.to_thread(container_inspect,cid)
                    terminal['observed_before_cleanup'][role]=True
                    terminal['collected_at'][role]=time.time()
                except Exception:
                    terminal['capture_errors'].append(role+'_terminal_inspect_failed')
        finally:
            # The canonical ``harbor run`` CLI owns the Python process that
            # executes both environments.  Persist the same organizer evidence
            # at each role boundary so the thin controller can read it after
            # the CLI exits without recreating Harbor state in another process.
            try:
                target = self.native_control_dir / 'native-evidence.json'
                temporary = target.with_suffix('.tmp')
                temporary.write_text(json.dumps(evidence, sort_keys=True))
                temporary.chmod(0o600)
                temporary.replace(target)
            except Exception:
                pass
            # Evidence failure must not leak resources or prevent cleanup.
            await super().stop(delete)

    async def exec(self, command, *args, **kwargs):
        result=await super().exec(command,*args,**kwargs)
        if self.native_role=='verifier' and '/tests/test.sh' in command and not command.lstrip().startswith('chmod '):
            EVIDENCE.setdefault(self.native_run_handle,{})['verifier_exit']=result.return_code
        return result
