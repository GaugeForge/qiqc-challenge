"""Trusted candidate configuration; imports no science or Docker runtime."""
from __future__ import annotations
import hashlib
import json
import re
import stat
import zipfile
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit

SCHEMA = 'qiqc-native-compose-run/v1'
DEFAULT_MODEL = 'tencent/hy4-preview'
DIAGNOSTIC_MODEL = 'minimax/minimax-m3.1-flash:free'


def agent_budget_from_identity(identity):
    """Legacy candidates mean 300 seconds; shorter observations are explicit."""
    budget = identity.get('agent_budget_seconds', 300)
    scope = identity.get('acceptance_scope', 'full-runtime')
    if scope in ('competition-native','practice-native'):
        limits=identity.get('native_limits') or {}
        if (type(budget) is not int or not 0<budget<=604800 or type(limits.get('agent_seconds')) is not int
            or budget>limits['agent_seconds'] or scope=='competition-native' and budget!=limits['agent_seconds']
            or not re.fullmatch('[a-f0-9]{64}',str(limits.get('source_sha256','')))):
            raise ValueError('native_agent_budget_invalid')
        return budget
    if type(budget) is not int or budget not in (180, 300):
        raise ValueError('native_agent_budget_invalid')
    if (budget == 180) != (scope == 'bounded-three-minute'):
        raise ValueError('native_agent_budget_scope_mismatch')
    return budget


def model_from_identity(identity):
    """One explicit model per candidate; legacy identities retain Hy4 semantics."""
    if not isinstance(identity, dict):raise ValueError('native_model_identity_invalid')
    if identity.get('provider_profile') is not None:
        from native_provider_profile import validate_profile_identity
        profile=validate_profile_identity(identity['provider_profile'])
        if identity.get('model_id')!=profile['model']:
            raise ValueError('native_profile_model_identity_mismatch')
        diagnostic=identity.get('diagnostic_model','')
        if diagnostic not in ('', DIAGNOSTIC_MODEL) or (diagnostic and
                (profile['model']!=diagnostic or profile['billing']!='free')):
            raise ValueError('native_diagnostic_model_invalid')
        return profile['model']
    if 'model_id' not in identity and 'diagnostic_model' not in identity:
        return DEFAULT_MODEL
    diagnostic=identity.get('diagnostic_model')
    if diagnostic not in ('', DIAGNOSTIC_MODEL):raise ValueError('native_diagnostic_model_invalid')
    expected=diagnostic or DEFAULT_MODEL
    if identity.get('model_id')!=expected:raise ValueError('native_model_identity_mismatch')
    return expected


def native_codex_endpoint(identity):
    """This submitted Codex version has only been integrated with Responses."""
    model_from_identity(identity)
    if identity.get('provider_profile') is not None:
        if 'responses' not in identity['provider_profile']['endpoints']:
            raise ValueError('native_codex_responses_endpoint_not_approved')
    return 'responses'


def file_sha256(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def _harness_normalizer():
    """Load the organizer archive parser by path (it is also mounted standalone
    into the isolated scanner, so it cannot be a package-relative import)."""
    import importlib.util
    path = Path(__file__).resolve().with_name('harness_archive.py')
    if not path.is_file():
        path = Path(__file__).resolve().parents[2] / 'qiqc' / 'archive.py'
    spec = importlib.util.spec_from_file_location('qiqc_trusted_archive', path)
    if spec is None or spec.loader is None:
        raise ValueError('harness_normalizer_unavailable')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.normalize_manifest


def submission_manifest(path, expected_sha256):
    if file_sha256(path) != expected_sha256:
        raise ValueError('submission_digest_mismatch')
    with zipfile.ZipFile(path) as archive:
        entries = archive.infolist()
        if len(entries) > 1000 or sum(e.file_size for e in entries) > 100*1024*1024:
            raise ValueError("submission_expansion_limit")
        names = set()
        for entry in entries:
            name = entry.filename
            mode = entry.external_attr >> 16
            if (not name or name in names or '\\' in name or PurePosixPath(name).is_absolute()
                or '..' in PurePosixPath(name).parts or '\x00' in name
                or stat.S_IFMT(mode) not in (0, stat.S_IFREG, stat.S_IFDIR)):
                raise ValueError('unsafe_submission_entry')
            names.add(name)
        info = archive.getinfo('harness.json')
        if info.file_size > 32768:
            raise ValueError('manifest_too_large')
        manifest = json.loads(archive.read(info))
        if isinstance(manifest, dict) and manifest.get('schema') == 'qiqc-harness/v1-demo':
            # Historical organizer candidates keep their original full contract.
            if (not isinstance(manifest.get('adapter'), str)
                or not re.fullmatch('[a-zA-Z0-9_-]{1,64}', manifest['adapter'])
                or manifest.get('runtime_kind') != 'native_cli'
                or manifest.get('task_transport') != 'native-http/v1'
                or not isinstance(manifest.get('entrypoint'), str)
                or manifest['entrypoint'] not in names
                or archive.getinfo(manifest['entrypoint']).is_dir()):
                raise ValueError('native_submission_contract_required')
            interfaces = manifest.get("model_interfaces", ["responses"])
            if (not isinstance(interfaces, list) or not interfaces
                or any(v not in ("responses", "chat/completions") for v in interfaces)):
                raise ValueError("unsupported_model_interface")
            return manifest
        # Minimal or legacy full-form qiqc-harness/v1: the same trusted
        # normalizer as the upload scanner derives adapter, interfaces, etc.
        try:
            manifest = _harness_normalizer()(manifest)
        except ValueError as error:
            raise ValueError('native_submission_contract_required') from error
        if (manifest['entrypoint'] not in names or archive.getinfo(manifest['entrypoint']).is_dir()
            or any(n!='harness.json' and PurePosixPath(n).parts[0] not in ('harness','adapter') for n in names)):
            raise ValueError('invalid_v1_submission_layout')
        return manifest


def validate_spec(value):
    expected = {'schema','source_root','task_id','run_id','submission_zip','submission_sha256',
                'output_dir','agent_budget_seconds','model_gateway_url','scoped_token_file','compose_overlays','candidate_identity','attempt'}
    if not isinstance(value, dict) or set(value) != expected or value['schema'] != SCHEMA:
        raise ValueError('invalid_native_spec')
    if not isinstance(value['candidate_identity'],dict) or not value['candidate_identity']:
        raise ValueError('candidate_identity_required')
    model_from_identity(value['candidate_identity'])
    if value['candidate_identity'].get('acceptance_scope','full-runtime') not in ('full-runtime','bounded-functional','bounded-five-minute','bounded-three-minute','competition-native','practice-native'):
        raise ValueError('invalid_acceptance_scope')
    if type(value['attempt']) is not int or value['attempt']<1:
        raise ValueError('native_attempt_required')
    for name in ('task_id', 'run_id'):
        if not isinstance(value[name], str) or not re.fullmatch('[a-zA-Z0-9_-]{1,100}', value[name]):
            raise ValueError('invalid_native_identifier')
    if (type(value['agent_budget_seconds']) is not int
            or value['agent_budget_seconds'] != agent_budget_from_identity(value['candidate_identity'])):
        raise ValueError('native_agent_budget_binding_mismatch')
    if not isinstance(value['submission_sha256'], str) or not re.fullmatch('[a-f0-9]{64}',value['submission_sha256']):
        raise ValueError('invalid_submission_digest')
    if not isinstance(value['compose_overlays'],list):
        raise ValueError('invalid_compose_overlays')
    for path in [value[k] for k in ('source_root','submission_zip','output_dir','scoped_token_file')] + value['compose_overlays']:
        if not isinstance(path,str) or not Path(path).is_absolute() or '..' in Path(path).parts:
            raise ValueError('absolute_trusted_path_required')
    url=urlsplit(value['model_gateway_url'])
    if (url.scheme!='http' or url.hostname!='host.docker.internal' or url.port!=8181
        or url.path!='/v1' or url.query or url.fragment or url.username or url.password):
        raise ValueError('fixed_native_model_gateway_required')
    return dict(value)


def validate_main_container(observation):
    """Necessary structural checks, not a complete network/isolation acceptance."""
    host=observation.get('HostConfig', {})
    if host.get('Privileged') or host.get('PidMode')=='host' or host.get('NetworkMode')=='host':
        raise ValueError('unsafe_main_container_namespace')
    if any(cap.upper() in {'ALL','SYS_ADMIN','NET_ADMIN'} for cap in (host.get('CapAdd') or [])):
        raise ValueError('unsafe_main_container_capability')
    for mount in observation.get('Mounts', []):
        source=str(mount.get('Source','')); dest=str(mount.get('Destination',''))
        if ('docker.sock' in source or 'docker.sock' in dest or source=='/'
            or dest in ('/var/run','/run','/app/configs','/opt/runtime','/src')):
            raise ValueError('unsafe_main_container_mount')
        if dest=='/qsim_logs' or (dest=='/qsim_logs/public_job_results' and mount.get('RW')):
            raise ValueError('private_or_writable_qsim_mount')
    return {'container_image_id':observation.get('Image'),
            'container_id':observation.get('Id'), 'structural_checks_passed':True,
            'network_negative_checks_complete':False}
