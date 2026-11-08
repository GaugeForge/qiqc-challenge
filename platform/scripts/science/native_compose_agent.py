"""Harbor adapter executing the exact uploaded ZIP in the original main container.

This module is installed on the trusted E2B VM host. No scientific code is
imported here. All submitted programs execute through Harbor DockerEnvironment.
"""
from __future__ import annotations
import json
import asyncio
import shlex
import os
import stat
import time
from pathlib import Path
from harbor.agents.base import BaseAgent
from harbor.agents.options import AgentOptions
from native_compose_contract import submission_manifest, model_from_identity, native_codex_endpoint

RUNS = {}


def _ensure_run(run_handle):
    """Load the controller-owned run spec when Harbor was started by the CLI.

    The old runner called ``Trial`` in this interpreter, so the controller
    populated ``RUNS`` directly.  The canonical Harbor entry point is the
    ``harbor run`` CLI, which starts a fresh Python process.  The spec is the
    same authenticated, controller-written input in that process; it contains
    paths and public execution identity, never the model token itself.
    """
    if run_handle in RUNS:
        return RUNS[run_handle]
    spec_path = os.environ.get("QIQC_NATIVE_SPEC")
    if not spec_path:
        raise RuntimeError("native_run_spec_missing")
    path = Path(spec_path)
    if path.is_symlink() or not path.is_file():
        raise RuntimeError("native_run_spec_invalid")
    run = json.loads(path.read_text())
    if run.get("run_id") != run_handle:
        raise RuntimeError("native_run_spec_mismatch")
    token_path = Path(run["scoped_token_file"])
    fd = os.open(token_path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_size > 4096:
            raise RuntimeError("unsafe_scoped_token_file")
        token = os.read(fd, 4097).decode().strip()
    finally:
        os.close(fd)
    if not token.startswith("qr_") or len(token) < 24:
        raise RuntimeError("scoped_run_token_required")
    run["scoped_token"] = token
    run["control_dir"] = Path(run["control_dir"])
    run["submission_zip"] = Path(run["submission_zip"])
    RUNS[run_handle] = run
    return run

# This script is written into main, never executed on the controller/VM host.
EXTRACT = r'''
import hashlib,json,pathlib,stat,zipfile
archive=pathlib.Path('/tmp/qiqc-native-harness.zip')
expected=pathlib.Path('/tmp/qiqc-native-harness.sha256').read_text().strip()
assert hashlib.sha256(archive.read_bytes()).hexdigest()==expected
root=pathlib.Path('/opt/qiqc-submission');root.mkdir(mode=0o700)
with zipfile.ZipFile(archive) as z:
 names=set()
 for e in z.infolist():
  p=pathlib.PurePosixPath(e.filename);mode=e.external_attr>>16
  assert e.filename not in names and not p.is_absolute() and '..' not in p.parts and '\\' not in e.filename
  assert stat.S_IFMT(mode) in (0,stat.S_IFREG,stat.S_IFDIR)
  names.add(e.filename)
 for e in z.infolist():
  p=root/e.filename
  if e.is_dir():p.mkdir(parents=True,exist_ok=True)
  else:
   p.parent.mkdir(parents=True,exist_ok=True)
   with z.open(e) as src,p.open('xb') as dst:
    while chunk:=src.read(1024*1024):dst.write(chunk)
'''


class NativeOptions(AgentOptions):
    run_handle: str


class NativeSubmittedHarness(BaseAgent):
    options_model = NativeOptions

    @staticmethod
    def name():
        return 'qiqc-native-submitted-harness'

    def version(self):
        return 'native-compose-candidate/1'

    async def setup(self, environment):
        run = _ensure_run(self.options.run_handle)
        run['submission_manifest'] = submission_manifest(run['submission_zip'], run['submission_sha256'])
        # NativeDockerEnvironment must complete the actual isolation checks first.
        if not getattr(environment, 'native_security_ready', False):
            raise RuntimeError('native_environment_security_not_verified')
        manifest=run['submission_manifest']
        self.runtime_env = {}
        if manifest['adapter'] in ('deepseek-harness', 'pi-agent'):
            # This code runs on the E2B host. The trusted controller manifest
            # binds the bundle digest; the prepared snapshot holds its bytes.
            # No package installation or public network occurs during a task.
            from native_runtime_bundles import authenticated_bundle
            bundle = authenticated_bundle(manifest['adapter'],
                Path(__file__).with_name('native-runtime-bundles.json'),
                '/opt/qiqc-agent-runtimes')
            await environment.upload_file(bundle, '/tmp/qiqc-native-runtime.tar.gz')
            prepared = await environment.exec(
                'mkdir -p /opt/qiqc-native-runtime && '
                'tar --no-same-owner -xzf /tmp/qiqc-native-runtime.tar.gz -C /opt/qiqc-native-runtime && '
                'rm /tmp/qiqc-native-runtime.tar.gz', timeout_sec=90)
            if prepared.return_code != 0:
                raise RuntimeError('native_runtime_preparation_failed')
            self.runtime_env = {'PATH': '/opt/qiqc-native-runtime/node/bin:'
                '/opt/qiqc-native-runtime/node_modules/.bin:/usr/local/bin:/usr/bin:/bin'}
            if manifest['adapter'] == 'deepseek-harness':
                # @deepseek-ai/dsh-sdk-jsonrpc-server imports this package but
                # does not declare it, so a bundle built from the published DSH
                # package tree can omit it. Apply the pinned organizer overlay
                # before the runtime version check; this is idempotent for
                # bundles that already contain the package.
                protocol = await environment.exec(
                    'test -f /opt/qiqc-native-runtime/node_modules/@deepseek-ai/dsh-sdk-protocol/package.json',
                    timeout_sec=15)
                if protocol.return_code != 0:
                    overlay = Path('/opt/qiqc-controller/dsh-sdk-protocol.tgz')
                    if overlay.is_symlink() or not overlay.is_file():
                        raise RuntimeError('native_dsh_protocol_overlay_missing')
                    await environment.upload_file(overlay, '/tmp/qiqc-dsh-sdk-protocol.tgz')
                    patched = await environment.exec(
                        'rm -rf /tmp/qiqc-dsh-protocol-overlay && '
                        'mkdir -p /tmp/qiqc-dsh-protocol-overlay && '
                        'tar -xzf /tmp/qiqc-dsh-sdk-protocol.tgz -C /tmp/qiqc-dsh-protocol-overlay && '
                        'rm -rf /opt/qiqc-native-runtime/node_modules/@deepseek-ai/dsh-sdk-protocol && '
                        'mv /tmp/qiqc-dsh-protocol-overlay/package '
                        '/opt/qiqc-native-runtime/node_modules/@deepseek-ai/dsh-sdk-protocol && '
                        'test -f /opt/qiqc-native-runtime/node_modules/@deepseek-ai/dsh-sdk-protocol/package.json && '
                        'rm -rf /tmp/qiqc-dsh-protocol-overlay /tmp/qiqc-dsh-sdk-protocol.tgz',
                        timeout_sec=60)
                    if patched.return_code != 0:
                        raise RuntimeError('native_dsh_protocol_overlay_failed')
        # Check the organizer-managed execution snapshot. The manifest accepts a
        # compatibility window, while the E2B snapshot remains immutable until
        # a separately verified runtime bundle is published. No npm/pip
        # installation or user-selected command is run on the controller.
        known={'codex':('codex --version','codex-cli 0.144.4'),
               'deepseek-harness':('dsh --version','0.2.1-rc.2'),
               'pi-agent':('pi --version','1.3.0')}
        if manifest['adapter'] in known:
            command,version=known[manifest['adapter']]
            result=await environment.exec(command,env=self.runtime_env,timeout_sec=15)
            lines=(result.stdout or '').strip().splitlines()
            if result.return_code!=0 or not lines or lines[-1].strip() not in (version,'dsh '+version,'pi '+version):
                raise RuntimeError('declared_agent_runtime_unavailable')
        work = run['control_dir']
        extraction = work / 'extract-submission.py'
        extraction.write_text(EXTRACT)
        digest = work / 'submission.sha256'
        digest.write_text(run['submission_sha256']+'\n')
        await environment.upload_file(run['submission_zip'], '/tmp/qiqc-native-harness.zip')
        await environment.upload_file(digest, '/tmp/qiqc-native-harness.sha256')
        await environment.upload_file(extraction, '/tmp/qiqc-native-extract.py')
        extracted = await environment.exec('python3 /tmp/qiqc-native-extract.py', timeout_sec=60)
        if extracted.return_code != 0:
            raise RuntimeError('native_submission_extraction_failed')

    async def run(self, instruction, environment, context):
        run = _ensure_run(self.options.run_handle)
        model_id = model_from_identity(run['candidate_identity'])
        manifest = run.get('submission_manifest') or submission_manifest(run['submission_zip'], run['submission_sha256'])
        endpoint = native_codex_endpoint(run['candidate_identity']) if manifest['adapter']=='codex' else manifest.get('model_interfaces',['responses'])[0]
        if run['candidate_identity'].get('provider_profile') and endpoint not in run['candidate_identity']['provider_profile']['endpoints']:
            raise RuntimeError('adapter_model_endpoint_not_approved')
        instruction_file = run['control_dir'] / 'instruction.txt'
        instruction_file.write_text(instruction)
        await environment.upload_file(instruction_file, '/tmp/qiqc-native-task.txt')
        # Mirrors Harbor's normal native MCP settings. No task helper/rewritten tools.
        config = ['model_provider = "qiqc"', 'model = '+json.dumps(model_id),
                  'web_search = "disabled"',
                  '[model_providers.qiqc]', 'name = "QIQC scoped gateway"',
                  'base_url = '+json.dumps(run['model_gateway_url']),
                  'env_key = "QIQC_MODEL_TOKEN"', 'wire_api = "responses"',
                  # Original OpenRouter patch sets this provider capability;
                  # Codex itself emits its supported non-namespace tool shape.
                  'namespace_tools = false']
        for server in self.mcp_servers:
            if server.transport != 'streamable-http' or server.url != 'http://qsim:8123/mcp':
                raise RuntimeError('unexpected_original_task_mcp_contract')
            config.extend(['[mcp_servers.'+json.dumps(server.name)+']',
                           'url = '+json.dumps(server.url)])
        config_file = run['control_dir'] / 'codex-config.toml'
        config_file.write_text('\n'.join(config)+'\n')
        prepared = await environment.exec('mkdir -p /tmp/qiqc-native-codex /logs/agent', timeout_sec=15)
        if prepared.return_code != 0:
            raise RuntimeError('native_agent_log_directory_failed')
        await environment.upload_file(config_file, '/tmp/qiqc-native-codex/config.toml')
        context.metadata = {'submission_sha256':run['submission_sha256'],
                            'execution_kind':'uploaded-native-zip',
                            'task_transport':'original-compose-http',
                            'agent_budget_seconds':run['agent_budget_seconds'],
                            'model_id':model_id,
                            'model_endpoint':endpoint,
                            'provider_profile':run['candidate_identity'].get('provider_profile'),
                            'diagnostic_model':run['candidate_identity'].get('diagnostic_model',''),
                            'leaderboard_eligible':False,
                            'native_security':environment.native_security_receipt}
        started = time.monotonic()
        env = {**self.runtime_env, 'CODEX_HOME':'/tmp/qiqc-native-codex',
               'QIQC_TASK_INPUT':'/tmp/qiqc-native-task.txt','QIQC_MODEL':model_id,
               'QIQC_AGENT_TIMEOUT':str(run['agent_budget_seconds']),
               'QIQC_MODEL_PROTOCOL':endpoint,'QIQC_OUTPUT':'/logs/agent/final.txt',
               'QIQC_TRAJECTORY':'/logs/agent/native-cli.jsonl',
               'QIQC_MODEL_TOKEN':run['scoped_token'], 'QIQC_MODEL_URL':run['model_gateway_url'],
               'QIQC_MCP_SERVERS':json.dumps([{'name':server.name,'transport':server.transport,'url':server.url} for server in self.mcp_servers]),
               'NO_PROXY':'qsim,localhost,127.0.0.1,host.docker.internal',
               'no_proxy':'qsim,localhost,127.0.0.1,host.docker.internal'}
        command = ('timeout --signal=TERM --kill-after=5s '+str(run['agent_budget_seconds'])+
                   ' sh '+shlex.quote('/opt/qiqc-submission/'+manifest['entrypoint'])+
                   ' > /logs/agent/native-trajectory.jsonl 2> /logs/agent/native-stderr.txt')
        execution = asyncio.create_task(environment.exec(command, cwd='/app', env=env,
                                        timeout_sec=run['agent_budget_seconds']+20))
        if run['candidate_identity'].get('acceptance_scope') in ('bounded-functional','bounded-five-minute','bounded-three-minute','practice-native','competition-native'):
            from native_acceptance_stop import observe_qsim
            try:
                while not execution.done():
                    await asyncio.wait({execution},timeout=2)
                    if execution.done():break
                    # Readiness remains evidence only. It must never truncate
                    # this round's configured observation interval.
                    await asyncio.to_thread(observe_qsim,run,environment)
            except BaseException:
                execution.cancel()
                await asyncio.gather(execution,return_exceptions=True)
                raise
        result = await execution
        if run['candidate_identity'].get('acceptance_scope') in ('practice-native','competition-native'):
            # Short natural completion can finish before the polling interval.
            # Observe the real service log once more; a zero count stays zero.
            await asyncio.to_thread(observe_qsim,run,environment)
        elapsed = time.monotonic()-started
        exhausted = result.return_code in (124,137) and elapsed >= run['agent_budget_seconds']-1
        stop_reason='budget_exhausted' if exhausted else ('completed' if result.return_code==0 else 'adapter_failure')
        duration_class=(f"budget_{run['agent_budget_seconds']}s_observed" if exhausted else
                        ('natural_completion_before_budget' if result.return_code==0 and elapsed<run['agent_budget_seconds']-1 else
                         ('natural_completion_at_budget' if result.return_code==0 else 'adapter_failure')))
        context.metadata.update(agent_exit=result.return_code, runtime_seconds=elapsed,
                                budget_exhausted=exhausted,
                                agent_stop_reason=stop_reason,execution_duration_class=duration_class,
                                observation_policy=f"observe-{run['agent_budget_seconds']}s-no-readiness-stop/v1")
        from native_compose_environment import EVIDENCE
        EVIDENCE.setdefault(self.options.run_handle,{})['agent_execution_stop']={
            'run_id':run['run_id'],'attempt':run['attempt'],'submission_sha256':run['submission_sha256'],
            'candidate_identity':run['candidate_identity'],'agent_budget_seconds':run['agent_budget_seconds'],
            'agent_exit':result.return_code,'elapsed_seconds':elapsed,'budget_exhausted':exhausted,
            'agent_stop_reason':stop_reason,'execution_duration_class':duration_class,
            'observation_policy':f"observe-{run['agent_budget_seconds']}s-no-readiness-stop/v1"}
        if result.return_code != 0 and not exhausted:
            context.metadata['agent_stop_reason']='adapter_failure'
            raise RuntimeError('uploaded_native_adapter_failed')
        # Returning after a genuine diagnostic cutoff lets Harbor perform the
        # original sidecar artifact collection and separate verifier normally.
