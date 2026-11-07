import importlib.util
from pathlib import Path

spec=importlib.util.spec_from_file_location('preflight',Path(__file__).parents[1]/'scripts/preflight.py')
preflight=importlib.util.module_from_spec(spec)
spec.loader.exec_module(preflight)

def test_inventory_excludes_private_data_and_tracks_source(tmp_path):
    src=tmp_path/'platform-go/internal/demo/a.go';src.parent.mkdir(parents=True);src.write_text('package demo')
    env=tmp_path/'.env';env.write_text('SECRET=never-collected')
    first=preflight.source_identity(tmp_path)
    env.write_text('SECRET=changed')
    hidden=tmp_path/'benchmark-assets/hidden/task.txt';hidden.parent.mkdir(parents=True);hidden.write_text('private truth')
    assert preflight.source_identity(tmp_path)==first
    src.write_text('package changed')
    assert preflight.source_identity(tmp_path)['sha256']!=first['sha256']

def test_inventory_rejects_symlink_source(tmp_path):
    private=tmp_path/'.env';private.write_text('private')
    src=tmp_path/'platform-go/internal/demo/secret.go';src.parent.mkdir(parents=True);src.symlink_to(private)
    assert preflight.source_identity(tmp_path)['files']==0

def test_missing_or_failed_tools_are_not_reported_ready(monkeypatch):
    monkeypatch.setattr(preflight.shutil,'which',lambda _: None)
    r=preflight.check()
    assert not r['dependency_ready'] and not r['production_ready']
    assert not any(r['services'].values()) and not any(r['tools'].values())
    assert r['secret_values_read'] is False
