"""Harbor provider for frozen task contracts using separate E2B role sandboxes.

Compose is validated as source metadata; arbitrary Compose execution is forbidden.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import os
import shlex
import sys
import tarfile
import tempfile
import uuid
from pathlib import Path

import httpx
from e2b_runtime import ROOT, RUNTIME, create, provision, runtime_env, start_qsim, task_contract
from task_release import verify_release, verified_bytes
from harbor.environments.base import BaseEnvironment, ExecResult
from harbor.environments.capabilities import EnvironmentCapabilities
from harbor.models.task.config import NetworkMode
from verify_public import validate_archive

sys.path.insert(0, str(ROOT / 'benchmark-assets/adapters/science-probe'))
from bounded_files import RemoteFileError, read_remote_bytes

FILE_EVIDENCE_LIMIT = 16 * 1024 * 1024
ARCHIVE_EVIDENCE_LIMIT = 64 * 1024 * 1024

VERIFIER_EXITS = {}
RESOURCE_EVENTS = {}
RUN_CHECKS = {}
ROLE_MANIFESTS = {}
ROLE_DEPLOYMENT_CHECKS = {}


class CZEnvironment(BaseEnvironment):
    @staticmethod
    def type():
        return "qiqc-e2b-public-cz"

    @property
    def capabilities(self):
        return EnvironmentCapabilities(
            docker_compose=True,
            disable_internet=True,
            network_allowlist=True,
            network_allowlist_hostnames=True,
            network_allowlist_ipv4_addresses=True,
            dynamic_network_policy=True,
        )

    def _validate_definition(self):
        self.task_id = self.environment_name
        task_contract(self.task_id)
        if not self.trial_paths.trial_dir.resolve().is_relative_to(Path("/dev/shm")):
            raise ValueError(
                "Harbor trial cache must be in /dev/shm and archived to private R2"
            )
        self.is_verifier = "__verifier" in self.session_id
        if not self.is_verifier:
            expected = verified_bytes(RUNTIME, f"harbor_tasks/{self.task_id}/environment/docker-compose.yaml", verify_release(ROOT, RUNTIME))
            actual = (self.environment_dir / "docker-compose.yaml").read_bytes()
            if hashlib.sha256(actual).digest() != hashlib.sha256(expected).digest():
                raise ValueError(
                    "Unsupported compose definition: frozen task service contract required"
                )
        self.main = None
        self.qsim = None
        self.stopped = set()

    def resource_event(self, sb, state, role=None):
        callback = RESOURCE_EVENTS.get(str(self.trial_paths.trial_dir))
        if callback:
            callback(
                {
                    "sandbox_id": sb.sandbox_id,
                    "state": state,
                    **({"role": role} if role else {}),
                }
            )

    def check(self):
        callback = RUN_CHECKS.get(str(self.trial_paths.trial_dir))
        if callback:
            callback()

    async def start(self, force_build=False):
        self.check()
        self.main = await asyncio.to_thread(create)
        self.resource_event(
            self.main, "created", "verifier" if self.is_verifier else "main"
        )
        if self.is_verifier:
            await asyncio.to_thread(provision, self.main, task_id=self.task_id, role="verifier")
            await asyncio.to_thread(
                self.main.commands.run,
                "mkdir -p /tests && cp -a /opt/runtime/harbor_tasks/" + self.task_id + "/tests/. /tests/",
                user="root",
            )
            await asyncio.to_thread(
                self.main.update_network, {"allow_internet_access": False}
            )
        else:
            await asyncio.to_thread(provision, self.main, task_id=self.task_id, role="main")
            self.qsim = await asyncio.to_thread(create)
            self.resource_event(self.qsim, "created", "qsim")
            await asyncio.to_thread(provision, self.qsim, task_id=self.task_id, role="qsim")
            await asyncio.to_thread(
                self.qsim.update_network, {"allow_internet_access": False}
            )
            await asyncio.to_thread(start_qsim, self.qsim, self.task_id)
            self.qsim_url = "https://" + self.qsim.get_host(8123)
            last_health = {"http_status": None, "transport_error_class": None}
            async with httpx.AsyncClient(timeout=3) as client:
                for _ in range(60):
                    try:
                        r = await client.get(
                            self.qsim_url + "/healthz",
                            headers={
                                "e2b-traffic-access-token": self.qsim.traffic_access_token
                            },
                        )
                        last_health["http_status"] = r.status_code
                        if r.status_code == 200:
                            break
                    except httpx.HTTPError as exc:
                        last_health["transport_error_class"] = type(exc).__name__
                    await asyncio.sleep(0.5)
                else:
                    await self._capture_startup_failure(last_health)
                    raise RuntimeError("qsim startup failed")
        manifests = ROLE_MANIFESTS.setdefault(str(self.trial_paths.trial_dir), {})
        for role, sandbox in (("verifier", self.main),) if self.is_verifier else (("main", self.main), ("qsim", self.qsim)):
            raw = await asyncio.to_thread(sandbox.files.read, "/opt/runtime/deployment-manifest.json", user="root")
            manifest = json.loads(raw)
            if manifest["task_id"] != self.task_id or manifest["role"] != role:
                raise ValueError("Role manifest identity mismatch")
            check_script = """import hashlib,json,pathlib,os,stat
root=pathlib.Path('/opt/runtime')
raw=(root/'deployment-manifest.json').read_bytes()
manifest=json.loads(raw)
for item in manifest['files']:
 p=root/item['path']
 if p.is_symlink() or not p.is_file(): raise ValueError('invalid role file')
 if hashlib.sha256(p.read_bytes()).hexdigest()!=item['sha256']: raise ValueError('role byte mismatch')
print(json.dumps({'files_verified':len(manifest['files']),'manifest_sha256':hashlib.sha256(raw).hexdigest()}))
"""
            checked = await asyncio.to_thread(sandbox.commands.run, "python -c " + shlex.quote(check_script), user="root", timeout=30)
            result = json.loads(checked.stdout)
            if result["files_verified"] != len(manifest["files"]) or result["manifest_sha256"] != hashlib.sha256(raw.encode() if isinstance(raw, str) else raw).hexdigest():
                raise ValueError("Role deployed bytes mismatch")
            manifests[role] = manifest
            ROLE_DEPLOYMENT_CHECKS.setdefault(str(self.trial_paths.trial_dir), {})[role] = result
        self.check()
        await self.exec(
            "mkdir -p /logs/agent /logs/verifier /logs/artifacts /tests /qsim_logs /home/user",
            user="root",
        )

    async def _capture_startup_failure(self, health):
        """Keep bounded startup evidence in the private failed rollout only.

        Trial storage is tmpfs, the failure archive is private R2, and failed
        rollout objects have no contestant download route. Never include log
        text in exceptions, browser reports, or operational receipt strings.
        """
        target = self.trial_paths.trial_dir / "artifacts"
        target.mkdir(parents=True, exist_ok=True)
        report = {"task_id": self.task_id, "role": "qsim", "health": health,
                  "private_log_collected": False}
        try:
            await self._download_file(self.qsim, "/tmp/qsim.log", target / "private-qsim-startup.log")
            report["private_log_collected"] = True
        except Exception as exc:
            report["collection_error_class"] = type(exc).__name__
        (target / "qsim-startup-diagnostics.json").write_text(json.dumps(report, sort_keys=True) + "\n")

    async def _apply_network_policy(self, network_policy):
        if not self.main:
            return
        if self.is_verifier or network_policy.network_mode == NetworkMode.NO_NETWORK:
            await asyncio.to_thread(
                self.main.update_network, {"allow_internet_access": False}
            )
        elif network_policy.network_mode == NetworkMode.ALLOWLIST:
            await asyncio.to_thread(
                self.main.update_network,
                {
                    "allow_out": list(network_policy.allowed_hosts),
                    "deny_out": ["0.0.0.0/0"],
                },
            )
        # Agent setup is public for pinned npm installation. execute_main applies
        # the qsim-only policy before loading the frozen official submission.

    def _service(self, service=None):
        if service in (None, "main"):
            return self.main
        if service == "qsim" and self.qsim:
            return self.qsim
        raise ValueError("Unsupported service")

    async def _exec(self, sb, command, cwd=None, env=None, timeout_sec=None, user=None):
        effective = {
            **(self._merge_env(env) or {}),
            **(runtime_env(self.task_id, role="verifier") if self.is_verifier else {}),
        }
        # The Hamiltonian verifier image explicitly runs as uid 10001. Keep
        # provisioning/artifact transfer privileged, execute only its scorer
        # under the original verifier identity.
        execution_user = str(user or self.default_user or "root")
        if self.is_verifier and self.task_id == "time_budgeted_hamlearn_10q" and "/tests/test.sh" in command:
            # Harbor's trusted artifact ferry can restore root-owned directory
            # metadata after provisioning. Repair only output ownership after
            # that transfer; private truth retains its original 0400 mode.
            prepare_outputs = """import os,pathlib,stat
for name in ('/logs/artifacts','/logs/verifier'):
 p=pathlib.Path(name)
 if p.is_symlink(): raise ValueError('linked verifier output')
 p.mkdir(parents=True,exist_ok=True)
 for item in [p,*p.rglob('*')]:
  if item.is_symlink(): raise ValueError('linked verifier output entry')
  os.chown(item,10001,-1,follow_symlinks=False)
private=pathlib.Path('/verifier/private/hidden_scorer.yaml')
s=private.lstat()
if not stat.S_ISREG(s.st_mode) or s.st_uid!=10001 or stat.S_IMODE(s.st_mode)!=0o400: raise ValueError('verifier private authority changed')
"""
            await asyncio.to_thread(sb.commands.run, "python -c " + shlex.quote(prepare_outputs), user="root", timeout=30)
            execution_user = "verifier"
        try:
            result = await asyncio.to_thread(
                sb.commands.run,
                command,
                cwd=cwd,
                envs=effective,
                timeout=timeout_sec or 120,
                user=execution_user,
            )
            if self.is_verifier and "/tests/test.sh" in command:
                VERIFIER_EXITS[str(self.trial_paths.trial_dir)] = result.exit_code
            return ExecResult(
                stdout=result.stdout, stderr=result.stderr, return_code=result.exit_code
            )
        except Exception as exc:
            if hasattr(exc, "exit_code"):
                if self.is_verifier and "/tests/test.sh" in command:
                    VERIFIER_EXITS[str(self.trial_paths.trial_dir)] = exc.exit_code
                return ExecResult(
                    stdout=getattr(exc, "stdout", ""),
                    stderr=getattr(exc, "stderr", ""),
                    return_code=exc.exit_code,
                )
            raise

    async def exec(self, command, cwd=None, env=None, timeout_sec=None, user=None):
        return await self._exec(self.main, command, cwd, env, timeout_sec, user)

    async def service_exec(
        self, command, *, service=None, cwd=None, env=None, timeout_sec=None, user=None
    ):
        return await self._exec(
            self._service(service), command, cwd, env, timeout_sec, user
        )

    async def upload_file(self, source_path, target_path):
        await asyncio.to_thread(
            self.main.files.write,
            target_path,
            Path(source_path).read_bytes(),
            user="root",
        )

    async def upload_dir(self, source_dir, target_dir):
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w:gz") as tf:
            for p in Path(source_dir).rglob("*"):
                if p.is_symlink():
                    raise ValueError("Symlink upload forbidden")
                if p.is_file():
                    tf.add(p, arcname=str(p.relative_to(source_dir)), recursive=False)
        data = buffer.getvalue()
        validate_archive(data)
        remote = "/root/hb-upload-" + uuid.uuid4().hex + ".tgz"
        await asyncio.to_thread(self.main.files.write, remote, data, user="root")
        result = await self.exec(
            "mkdir -p "
            + shlex.quote(target_dir)
            + " && tar -xzf "
            + shlex.quote(remote)
            + " -C "
            + shlex.quote(target_dir)
            + " && rm -f "
            + shlex.quote(remote),
            user="root",
        )
        if result.return_code:
            raise RuntimeError("Harbor directory upload failed")

    def organizer_artifact_source(self, sandbox, source_path):
        # The original task requests this scorer from qsim's full Compose tree.
        # Our stricter qsim role excludes private scoring inputs. Preserve the
        # declared artifact using the exact frozen organizer-owned input, with
        # explicit provenance; never make it readable by qsim or main.
        relative = "configs/task_materials/modexp_ccz2t_spacetime_ftqc/hidden/hidden_scorer.yaml"
        if (getattr(self, "task_id", None) == "modexp_ccz2t_spacetime_ftqc"
            and sandbox is getattr(self, "qsim", None)
            and source_path == "/app/" + relative):
            return relative
        return None

    async def _download_file(self, sb, source_path, target_path):
        target = Path(target_path)
        if not target.resolve().is_relative_to(Path("/dev/shm")):
            raise ValueError("Harbor evidence cache must remain in tmpfs")
        target.parent.mkdir(parents=True, exist_ok=True)
        override = self.organizer_artifact_source(sb, source_path)
        if override is not None:
            from task_release import verify_release, verified_bytes, identity
            release = verify_release(ROOT, RUNTIME)
            data = verified_bytes(RUNTIME, override, release)
            if len(data) > FILE_EVIDENCE_LIMIT:
                raise ValueError("Organizer artifact exceeds evidence budget")
            provenance_path = self.trial_paths.trial_dir / "artifacts/organizer-input-provenance.json"
            provenance_path.parent.mkdir(parents=True, exist_ok=True)
            provenance = json.loads(provenance_path.read_text()) if provenance_path.exists() else []
            provenance.append({"declared_service":"qsim", "declared_source":source_path,
                "actual_source_kind":"organizer_frozen_verifier_input", "frozen_path":override,
                "destination":str(target.relative_to(self.trial_paths.trial_dir)),
                "sha256":hashlib.sha256(data).hexdigest(), "benchmark_release":identity(release,self.task_id)})
            provenance_path.write_text(json.dumps(provenance,sort_keys=True) + "\n")
        else:
            try:
                data = await asyncio.to_thread(
                    read_remote_bytes, sb.files, source_path, FILE_EVIDENCE_LIMIT, user="root"
                )
            except RemoteFileError:
                raise ValueError("Harbor evidence file transfer rejected") from None
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(dir=target.parent, prefix='.evidence-', delete=False) as stream:
                temporary = Path(stream.name)
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, target)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    async def download_file(self, source_path, target_path):
        await self._download_file(self.main, source_path, target_path)

    async def service_download_file(self, source_path, target_path, *, service=None):
        await self._download_file(self._service(service), source_path, target_path)

    async def _download_dir(self, sb, source_dir, target_dir):
        target = Path(target_dir)
        if not target.resolve().is_relative_to(Path("/dev/shm")):
            raise ValueError("Harbor evidence cache must remain in tmpfs")
        remote = "/root/hb-download-" + uuid.uuid4().hex + ".tgz"
        result = await self._exec(
            sb,
            "tar -czf " + shlex.quote(remote) + " -C " + shlex.quote(source_dir) + " .",
            user="root",
        )
        if result.return_code:
            raise RuntimeError("Harbor directory collection failed")
        try:
            data = await asyncio.to_thread(
                read_remote_bytes, sb.files, remote, ARCHIVE_EVIDENCE_LIMIT, user="root"
            )
        except RemoteFileError:
            raise ValueError("Harbor evidence archive transfer rejected") from None
        validate_archive(data)
        target.mkdir(parents=True, exist_ok=True)
        with tarfile.open(fileobj=io.BytesIO(data)) as tf:
            tf.extractall(target, filter="data")
        await self._exec(sb, "rm -f " + shlex.quote(remote), user="root")

    async def download_dir(self, source_dir, target_dir):
        await self._download_dir(self.main, source_dir, target_dir)

    async def service_download_dir(self, source_dir, target_dir, *, service=None):
        await self._download_dir(self._service(service), source_dir, target_dir)

    async def stop_service(self, service):
        if service not in self.stopped:
            await asyncio.to_thread(self._service(service).kill)
            self.resource_event(self._service(service), "destroyed")
            self.stopped.add(service)

    async def stop(self, delete=True):
        errors = []
        for name, sb in [("main", self.main), ("qsim", self.qsim)]:
            if sb and name not in self.stopped:
                try:
                    await asyncio.to_thread(sb.kill)
                    self.resource_event(sb, "destroyed")
                    self.stopped.add(name)
                except Exception as exc:  # noqa: BLE001 — aggregate cleanup errors and raise
                    self.resource_event(sb, "cleanup_unconfirmed")
                    errors.append(type(exc).__name__)
        if errors:
            raise RuntimeError("Sandbox cleanup failed: " + ",".join(errors))
