"""Invalid installer combinations must fail before stopping a running service."""
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[3]
spec = importlib.util.spec_from_file_location('native_stage_preflight',ROOT/'competition-platform/platform-go/deploy/native_candidate_stage.py')
stage = importlib.util.module_from_spec(spec)
spec.loader.exec_module(stage)


def case(tmp_path):
    key = tmp_path/'provider.key'
    key.write_text('test-key-not-real-secret-123456')
    key.chmod(0o600)
    value = json.loads((ROOT/'competition-platform/platform/test-fixtures/provider-profile-canonical.json').read_text())['profile']
    value['key_file'] = str(key)
    profile = tmp_path/'profile.json'
    profile.write_text(json.dumps(value));profile.chmod(0o600)
    args = SimpleNamespace(provider_profile=profile,diagnostic_model='minimax/minimax-m3.1-flash:free',diagnostic_rpm=10,diagnostic_daily_cap=50)
    return args,value,key


def snapshot(root):
    return {str(p.relative_to(root)):(p.read_bytes(),p.stat().st_mode) for p in root.rglob('*') if p.is_file() and not p.is_symlink()}


@pytest.mark.parametrize('endpoints',[['responses'],['chat/completions']])
def test_valid_profile_unsupported_stage_combination_never_mutates(tmp_path,monkeypatch,endpoints):
    args,value,_=case(tmp_path);value['endpoints']=endpoints;args.provider_profile.write_text(json.dumps(value))
    before=snapshot(tmp_path)
    monkeypatch.setattr(stage,'command',lambda *a,**k:pytest.fail('service/user command before preflight'))
    monkeypatch.setattr(stage.grp,'getgrnam',lambda *a:pytest.fail('system mutation phase entered'))
    with pytest.raises(SystemExit,match='Profile is valid.*current combination'):
        stage.prepare(args)
    assert snapshot(tmp_path)==before


@pytest.mark.parametrize('fault',['missing','public','symlink','parent_symlink','empty','multiline','too_large','alias','limit'])
def test_invalid_key_and_alias_fail_before_any_installation(tmp_path,monkeypatch,fault):
    args,value,key=case(tmp_path)
    if fault=='missing':key.unlink()
    elif fault=='public':key.chmod(0o644)
    elif fault=='symlink':
        other=tmp_path/'actual.key';key.rename(other);key.symlink_to(other)
    elif fault=='parent_symlink':
        link=tmp_path/'alias';link.symlink_to(tmp_path,target_is_directory=True);value['key_file']=str(link/'provider.key')
    elif fault=='empty':key.write_text('')
    elif fault=='multiline':key.write_text('first-key-long-enough\nsecond-key')
    elif fault=='too_large':key.write_text('a'*1025)
    elif fault=='alias':value['model']='other-free-model:free'
    elif fault=='limit':args.diagnostic_rpm=0
    args.provider_profile.write_text(json.dumps(value));before=snapshot(tmp_path)
    monkeypatch.setattr(stage,'command',lambda *a,**k:pytest.fail('service/user command before preflight'))
    monkeypatch.setattr(stage.grp,'getgrnam',lambda *a:pytest.fail('system mutation phase entered'))
    with pytest.raises(SystemExit):stage.prepare(args)
    assert snapshot(tmp_path)==before


def test_supported_profile_preflight_is_read_only_and_does_not_export_key(tmp_path):
    args,value,key=case(tmp_path);before=snapshot(tmp_path)
    assert stage.preflight(args)==value
    assert key.read_text() not in json.dumps(stage.preflight(args))
    assert snapshot(tmp_path)==before


def test_native_catalog_limits_supported_before_installation(tmp_path):
    args, value, _ = case(tmp_path)
    catalog = {'schema': 'qiqc-scientific-task-set/v2', 'release_id': 'native-candidate-' + 'a' * 64,
               'agent_budget_seconds': 300, 'infra_task_index': 0,
               'tasks': [{'task_index': i, 'task_id': f'task_{i:02}', 'public': i < 5,
                          'limits': {'agent_seconds': 10800, 'setup_seconds': 300,
                                     'build_seconds': 600, 'verifier_seconds': 4200,
                                     'cpus': 2, 'memory_mb': 4096, 'source_sha256': 'b' * 64}}
                         for i in range(49)]}
    path = tmp_path / 'native-catalog.json'
    path.write_text(json.dumps(catalog))
    args.science_task_set = path
    before = snapshot(tmp_path)
    assert stage.preflight(args) == value
    assert snapshot(tmp_path) == before
    catalog['tasks'][3]['limits']['agent_seconds'] = 0
    path.write_text(json.dumps(catalog))
    with pytest.raises(SystemExit, match='Scientific task set'):
        stage.preflight(args)


def test_event_configuration_commands_are_api_only(tmp_path):
    from native_candidate import commands
    from test_native_candidate import config
    value, _ = config(tmp_path)
    value['event_calendar_path'] = str(tmp_path / 'calendar.json')
    value['formal_execution_path'] = str(tmp_path / 'formal.json')
    result = commands(value)
    assert result['api'][-4:] == ['-event-calendar', value['event_calendar_path'],
                                  '-formal-execution', value['formal_execution_path']]
    for role in ('worker', 'gateway', 'review'):
        assert '-event-calendar' not in result[role]
        assert '-formal-execution' not in result[role]


@pytest.mark.parametrize('family', ['auto', 'ipv4', 'ipv6'])
def test_family_preflight_preserves_profile_and_does_not_mutate(tmp_path, family):
    args, value, _ = case(tmp_path)
    args.upstream_ip_family = family
    before = snapshot(tmp_path)
    assert stage.preflight(args) == value
    assert snapshot(tmp_path) == before


@pytest.mark.parametrize('family', ['', 'private-secret', 'ipv4\nprivate-secret', None])
def test_invalid_family_fails_before_installation_without_echo(tmp_path, monkeypatch, family):
    args, _, _ = case(tmp_path)
    args.upstream_ip_family = family
    before = snapshot(tmp_path)
    monkeypatch.setattr(stage, 'command', lambda *a, **k: pytest.fail('mutation before preflight'))
    monkeypatch.setattr(stage.grp, 'getgrnam', lambda *a: pytest.fail('mutation before preflight'))
    with pytest.raises(SystemExit) as error:
        stage.prepare(args)
    assert str(error.value) == 'Invalid upstream IP family'
    assert snapshot(tmp_path) == before


@pytest.mark.parametrize('fault', ['count', 'budget', 'public', 'infra', 'symlink', 'oversize'])
def test_task_set_rejected_before_service_mutation(tmp_path, monkeypatch, fault):
    args, _, _ = case(tmp_path)
    path = tmp_path/'tasks.json'
    value = {'schema':'qiqc-scientific-task-set/v1','release_id':'native-candidate-'+'a'*64,
             'agent_budget_seconds':300,'infra_task_index':0,
             'tasks':[{'task_index':i,'task_id':f'task_{i:02d}','public':i<5} for i in range(49)]}
    if fault == 'count':value['tasks'].pop()
    if fault == 'budget':value['agent_budget_seconds']=180
    if fault == 'public':value['tasks'][0]['public']='true'
    if fault == 'infra':value['infra_task_index']=48
    path.write_text(json.dumps(value))
    if fault == 'oversize':path.write_bytes(b' '*65537)
    if fault == 'symlink':
        other = tmp_path/'actual.json';path.rename(other);path.symlink_to(other)
    args.science_task_set=path
    before=snapshot(tmp_path)
    monkeypatch.setattr(stage,'command',lambda *a,**k:pytest.fail('mutation before preflight'))
    monkeypatch.setattr(stage.grp,'getgrnam',lambda *a:pytest.fail('mutation before preflight'))
    with pytest.raises(SystemExit,match='Scientific task set'):stage.prepare(args)
    assert snapshot(tmp_path)==before


def transition_case(tmp_path, monkeypatch):
    import hashlib
    from native_candidate import candidate_id_for_audit
    import verify_restored_benchmark
    destination = tmp_path / "deployment"
    destination.mkdir()
    (destination / "candidate.json").write_text("old candidate untouched")
    old_audit = {"source_files_sha256": "a" * 64, "source_commit": "b" * 40,
                 "competition_task_ids": [f"task_{i:02}" for i in range(49)]}
    old_path = destination / "source-audit.json"
    old_path.write_text(json.dumps(old_audit))
    previous = {"candidate_id": "native-candidate-" + "a" * 64,
                "source_audit": str(old_path), "source_files_sha256": "a" * 64,
                "source_audit_sha256": hashlib.sha256(old_path.read_bytes()).hexdigest(),
                "template_id": "old-template"}
    new_audit = {**old_audit, "competition_task_ids": [f"task_{i:02}" for i in range(1, 50)]}
    new_path = tmp_path / "paper-audit.json"
    new_path.write_text(json.dumps(new_audit))
    next_id = candidate_id_for_audit(new_audit, hashlib.sha256(new_path.read_bytes()).hexdigest())
    catalog = tmp_path / "paper-catalog.json"
    catalog.write_text(json.dumps({"release_id": next_id,
        "tasks": [{"task_id": t} for t in new_audit["competition_task_ids"]]}))
    monkeypatch.setattr(stage, "DEST", destination)
    monkeypatch.setattr(stage, "load_candidate", lambda *a, **k: (previous, old_audit))
    monkeypatch.setattr(verify_restored_benchmark, "verify", lambda *a: None)
    args = SimpleNamespace(source_audit=new_path, science_task_set=catalog,
        template_id="old-template", replace_drained_candidate_roster=True)
    return args, previous, next_id


def test_roster_transition_is_read_only_and_preserves_historical_audit(tmp_path, monkeypatch):
    args, previous, next_id = transition_case(tmp_path, monkeypatch)
    before = snapshot(tmp_path)
    result = stage.source_transition(args)
    assert result["changed"] and result["candidate_id"] == next_id
    assert result["previous"] == previous
    assert snapshot(tmp_path) == before


@pytest.mark.parametrize("fault", ["missing_explicit", "wrong_catalog", "missing_catalog", "frozen", "new_source", "template"])
def test_roster_transition_rejects_before_mutation(tmp_path, monkeypatch, fault):
    args, previous, _ = transition_case(tmp_path, monkeypatch)
    if fault == "missing_explicit": args.replace_drained_candidate_roster = False
    if fault == "wrong_catalog": args.science_task_set.write_text('{"release_id":"wrong","tasks":[]}')
    if fault == "missing_catalog": args.science_task_set = None
    if fault == "frozen": (stage.DEST / "active-release.json").write_text("{}")
    if fault == "new_source": previous["source_files_sha256"] = "c" * 64
    if fault == "template": args.template_id = "changed-template"
    before = snapshot(tmp_path)
    with pytest.raises(SystemExit): stage.source_transition(args)
    assert snapshot(tmp_path) == before


def test_omitted_source_audit_preserves_legacy_identity(tmp_path, monkeypatch):
    args, previous, _ = transition_case(tmp_path, monkeypatch)
    args.source_audit = None
    args.science_task_set = None
    args.replace_drained_candidate_roster = False
    result = stage.source_transition(args)
    assert not result["changed"]
    assert result["candidate_id"] == previous["candidate_id"]
    assert result["identity_schema"] is None


def test_source_audit_mount_denials_cover_current_and_every_retained_selection(tmp_path):
    current = tmp_path / ('source-audit-' + 'b' * 64 + '.json')
    retained = [tmp_path / 'source-audit.json',
                tmp_path / ('source-audit-' + 'a' * 64 + '.json')]
    for path in retained:
        path.write_text('private selection metadata')
    # The current destination can be reserved before its file is created.
    assert set(stage.source_audit_denied_paths(tmp_path, current).split()) == {
        '-' + str(path) for path in [current, *retained]}
    with pytest.raises(ValueError):
        stage.source_audit_denied_paths(tmp_path, tmp_path.parent / 'unrelated.json')
