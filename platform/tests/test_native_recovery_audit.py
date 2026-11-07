import hashlib
import json
import sqlite3
import sys
from pathlib import Path
import pytest
sys.path.insert(0,str(Path(__file__).parents[1]/'scripts'))
from native_recovery_audit import check_release,copy_checked,db_check
from test_native_release import fixture,freeze


def release_fixture(fixture,tmp_path):
    cfg,*_=fixture;path=freeze(fixture);record=json.loads(path.read_bytes());rid=record['release_id']
    digest=hashlib.sha256(path.read_bytes()).hexdigest()
    pointer=tmp_path/'pointer.json';pointer.write_text(json.dumps({'release_path':str(path),'release_id':rid,'release_sha256':digest}))
    settings={'scientific_active_release':rid,'native_release:'+rid:json.dumps({'release_id':rid,'candidate_id':cfg['candidate_id'],'release_sha256':digest})}
    dest=tmp_path/'restored';dest.mkdir()
    return cfg,path,pointer,settings,dest


def test_actual_release_103_attachments_restored_offline(fixture,tmp_path):
    cfg,path,pointer,settings,dest=release_fixture(fixture,tmp_path)
    result=check_release(cfg,settings,pointer,dest)
    assert result['attachments_verified']==103
    assert (dest/'release/release.json').read_bytes()==path.read_bytes()
    assert (dest/'active-release.original.json').read_bytes()==pointer.read_bytes()


@pytest.mark.parametrize('mutation',['missing_attachment','wrong_binding','pointer_db','registration','pointer_digest'])
def test_recovery_rejects_missing_or_wrong_release(fixture,tmp_path,mutation):
    cfg,path,pointer,settings,dest=release_fixture(fixture,tmp_path)
    if mutation=='missing_attachment':(path.parent/'evidence/00-r2.json').unlink()
    if mutation=='wrong_binding':cfg={**cfg,'template_id':'other'}
    if mutation=='pointer_db':settings['scientific_active_release']='other'
    if mutation=='registration':settings['native_release:'+settings['scientific_active_release']]='{}'
    if mutation=='pointer_digest':
        value=json.loads(pointer.read_text());value['release_sha256']='0'*64;pointer.write_text(json.dumps(value))
    with pytest.raises(ValueError):check_release(cfg,settings,pointer,dest)


def test_active_registration_cannot_lose_pointer(tmp_path):
    with pytest.raises(ValueError,match='pointer_missing'):
        check_release({}, {'scientific_active_release':'registered'},tmp_path/'absent',tmp_path)


def test_upload_wrong_digest_rejected(tmp_path):
    source=tmp_path/'harness.zip';source.write_bytes(b'fixture')
    with pytest.raises(ValueError,match='hash_mismatch'):copy_checked(source,tmp_path/'restore.zip','0'*64)
    assert not (tmp_path/'restore.zip').exists()


def test_orphan_key_detected_without_declared_foreign_keys(tmp_path):
    db=tmp_path/'platform.db'
    with sqlite3.connect(db) as c:
        c.executescript('CREATE TABLE teams(id TEXT); CREATE TABLE versions(id TEXT,team_id TEXT,sha256 TEXT);INSERT INTO versions VALUES("v","missing","x");')
    with pytest.raises(ValueError,match='restore_relation_version_team_failed'):db_check(db,{})


@pytest.mark.parametrize('redirect',['source_parent','source_file','target_parent','target_file'])
def test_copy_rejects_links_without_touching_outside_file(tmp_path,redirect):
    source=tmp_path/'source';source.mkdir();(source/'input').write_bytes(b'fixture')
    outside=tmp_path/'outside';outside.mkdir();victim=outside/'victim';victim.write_bytes(b'untouched')
    source_file=source/'input';target=tmp_path/'destination'/'output'
    if redirect=='source_parent':
        link=tmp_path/'source-link';link.symlink_to(source,target_is_directory=True);source_file=link/'input'
    elif redirect=='source_file':
        link=source/'input-link';link.symlink_to(source_file);source_file=link
    elif redirect=='target_parent':
        target.parent.symlink_to(outside,target_is_directory=True);target=target.parent/'victim'
    else:
        target.parent.mkdir();target.symlink_to(victim)
    with pytest.raises(ValueError,match='restore_path_symlink'):copy_checked(source_file,target)
    assert victim.read_bytes()==b'untouched'


def test_copy_never_overwrites_existing_destination(tmp_path):
    source=tmp_path/'source';source.write_bytes(b'new')
    target=tmp_path/'target';target.write_bytes(b'existing')
    with pytest.raises(FileExistsError):copy_checked(source,target)
    assert target.read_bytes()==b'existing'


def test_error_report_never_echoes_arbitrary_exception_payload():
    from native_recovery_audit import safe_error
    assert safe_error(ValueError('restore_input_hash_mismatch'))=='restore_input_hash_mismatch'
    assert safe_error(ValueError('invalid password_hash=private-authentication-row'))=='recovery_operation_failed'
    assert safe_error(ValueError('restore_failed\nprivate-data'))=='recovery_operation_failed'
    assert safe_error(OSError('sensitive-path'))=='recovery_operation_failed'
