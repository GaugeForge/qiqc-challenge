"""Pure native operational preflight contracts; no live namespace/service use."""
import hashlib
import importlib.util
import json
from pathlib import Path
import pytest

path=Path(__file__).resolve().parents[2]/'platform-go/deploy/verify_native_permissions.py'
spec=importlib.util.spec_from_file_location('native_permission_preflight',path)
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)


def identity():return {'pid':123,'uid':1001,'gid':1002,'groups':[1002,1003], 'mount_namespace':'mnt:[isolated]'}


def props():return {'Id':'qiqc-native-api.service','User':'qiqc-native-api','Group':'qiqc-native-data',
    'ActiveState':'active','NoNewPrivileges':'yes','ProtectHome':'yes','ProtectSystem':'strict',
    'PrivateTmp':'yes','Restart':'on-failure','CapabilityBoundingSet':''}


def test_native_service_not_old_staging_and_real_namespace_required(monkeypatch):
    monkeypatch.setattr(m.os,'readlink',lambda _: 'mnt:[host]')
    assert m.validate_service('api',props(),identity())==[]
    for change in ({'Id':'qiqc-stage-api.service'},{'ActiveState':'inactive'},{'Group':'docker'},
                   {'ProtectHome':'no'},{'CapabilityBoundingSet':'cap_sys_admin'}):
        assert m.validate_service('api',{**props(),**change},identity())
    assert m.validate_service('api',props(),{**identity(),'uid':0})
    assert m.validate_service('api',props(),{**identity(),'mount_namespace':'mnt:[host]'})


def test_probe_uses_actual_process_mount_root_uid_and_groups_without_reading_bytes():
    argv=m.probe_argv(identity(),'read','/private/test.env')
    assert argv[:8]==['nsenter','--target','123','--mount','--root','--wd=/','--','setpriv']
    assert argv[argv.index('--groups')+1]=='1002,1003'
    assert '--no-new-privs' in argv and 'os.read(' not in m.PROBE
    assert 'O_CREAT' not in m.PROBE and 'O_TRUNC' not in m.PROBE
    with pytest.raises(ValueError):m.probe_argv({**identity(),'uid':0},'read','/private')
    with pytest.raises(ValueError):m.probe_argv(identity(),'delete','/private')


def release_fixture(root):
    rid='native-release-'+'a'*64;p=root/'releases'/rid/'release.json';p.parent.mkdir(parents=True);p.write_text('{}')
    pointer={'release_id':rid,'release_path':str(p),'release_sha256':hashlib.sha256(p.read_bytes()).hexdigest()}
    (root/'active-release.json').write_text(json.dumps(pointer));return p,pointer


def test_candidate_phase_is_explicit_without_fabricated_release(tmp_path):
    assert m.release_target(tmp_path) is None


@pytest.mark.parametrize('change',['digest','missing','outside','symlink'])
def test_release_pointer_cannot_mask_missing_or_wrong_release(tmp_path,change):
    p,value=release_fixture(tmp_path)
    assert m.release_target(tmp_path)==p
    if change=='digest':value['release_sha256']='0'*64
    elif change=='missing':p.unlink()
    elif change=='outside':value['release_path']='/tmp/unrelated-release.json'
    elif change=='symlink':p.unlink();p.symlink_to(tmp_path/'active-release.json')
    (tmp_path/'active-release.json').write_text(json.dumps(value))
    with pytest.raises(ValueError):m.release_target(tmp_path)


def test_probe_setup_failure_cannot_masquerade_as_permission_denial():
    from types import SimpleNamespace
    assert m.probe_passed(SimpleNamespace(returncode=13,stdout='{"probe_completed":true,"allowed":false}'),False)
    for output in ('','nsenter denied','{"probe_completed":false,"allowed":false}'):
        assert not m.probe_passed(SimpleNamespace(returncode=13,stdout=output),False)
    assert not m.probe_passed(SimpleNamespace(returncode=0,stdout='{"probe_completed":true,"allowed":true}'),False)


@pytest.mark.parametrize('role,allowed',[('api',False),('worker',False),('gateway',True),('review',True)])
def test_profile_key_matrix_metadata_only_and_actual_namespace_probe(tmp_path,role,allowed,monkeypatch):
    import os
    conf=tmp_path/'config';conf.mkdir();profile=conf/'provider-profile.json';profile.touch()
    key=conf/'profile.key';key.touch();key.chmod(0o640)
    # Synthetic file is never read; forbid accidental content inspection.
    monkeypatch.setattr(Path,'read_bytes',lambda self:pytest.fail('secret bytes read'))
    probes,errors=m.profile_key_probes(tmp_path,role,key.stat().st_gid)
    assert errors==[]
    assert probes==[('read',key,allowed,'credential_open_close:profile.key')]
    argv=m.probe_argv(identity(),probes[0][0],probes[0][1])
    assert str(key)==argv[-1] and '--mount' in argv and 'os.read(' not in m.PROBE


@pytest.mark.parametrize('mutation',['worldread','wronggroup','symlink','missingprofile','missingkey'])
def test_profile_key_unsafe_metadata_or_partial_deployment_rejected(tmp_path,mutation):
    conf=tmp_path/'config';conf.mkdir();profile=conf/'provider-profile.json';profile.touch()
    key=conf/'profile.key';key.touch();key.chmod(0o640);gid=key.stat().st_gid
    if mutation=='worldread':key.chmod(0o644)
    if mutation=='wronggroup':gid+=10000
    if mutation=='symlink':key.unlink();key.symlink_to(profile)
    if mutation=='missingprofile':profile.unlink()
    if mutation=='missingkey':key.unlink()
    _,errors=m.profile_key_probes(tmp_path,'api',gid)
    assert errors


def test_profile_key_absent_legacy_deployment_remains_explicit(tmp_path):
    assert m.profile_key_probes(tmp_path,'api',123)==([],[])


@pytest.mark.parametrize('role', ['api', 'worker', 'gateway', 'review'])
def test_active_and_historical_source_audits_all_probed_without_payload_reads(tmp_path, monkeypatch, role):
    current = tmp_path / ('source-audit-' + 'a' * 64 + '.json')
    previous = tmp_path / 'source-audit.json'
    current.write_text('private audit'); previous.write_text('old private audit')
    candidate = tmp_path / 'candidate.json'
    candidate.write_text(json.dumps({'source_audit': str(current)}))
    read_text = Path.read_text
    def safe_read(path, *args, **kwargs):
        assert path == candidate, 'source audit payload was read'
        return read_text(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'read_text', safe_read)
    monkeypatch.setattr(Path, 'read_bytes', lambda *a, **k: pytest.fail('audit payload was read'))
    probes = m.source_audit_probes(tmp_path, role)
    assert probes == [
        ('read', current, role == 'worker', 'source_audit_open_close'),
        ('read', previous, False, 'retained_source_audit_open_close:source-audit.json')]


@pytest.mark.parametrize('fault', ['missing', 'outside', 'symlink', 'retained_symlink'])
def test_source_audit_probe_requires_real_host_positive_controls(tmp_path, fault):
    current = tmp_path / 'source-audit-current.json'
    current.touch()
    configured = current
    if fault == 'missing': current.unlink()
    if fault == 'outside': configured = tmp_path.parent / 'source-audit-outside.json'
    if fault == 'symlink':
        current.unlink(); current.symlink_to(tmp_path / 'candidate.json')
    if fault == 'retained_symlink': (tmp_path / 'source-audit-old.json').symlink_to(current)
    (tmp_path / 'candidate.json').write_text(json.dumps({'source_audit': str(configured)}))
    with pytest.raises(ValueError): m.source_audit_probes(tmp_path, 'api')
