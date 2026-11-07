import sys
import subprocess
import os
import time
from pathlib import Path
import pytest
sys.path.insert(0,str(Path(__file__).parents[1]/'scripts'))
from native_recovery_api_probe import bwrap_command,compare_account,compare_batch,MINIMAL_ENV,verify_api_process,stop_group,process_running


def test_isolated_api_command_has_no_live_mount_or_cloud_environment(tmp_path):
    command=bwrap_command(tmp_path,23456)
    assert '--clearenv' in command and '--unshare-user' in command and '--unshare-pid' in command
    assert command.count('--bind')==1 and command[command.index('--bind')+1:command.index('--bind')+3]==[str(tmp_path),'/work']
    assert '-standalone' in command and '127.0.0.1:23456' in command
    assert all('QIQC_' not in key and 'KEY' not in key for key in MINIMAL_ENV)
    assert not any('/opt/qiqc-native-candidate' in value or '/root/QQ-Challenge' in value for value in command)


def test_account_and_quota_mapping_reject_wrong_team():
    expected={'id':'u','team_id':'t','quotas':{'public':2}}
    compare_account(expected,expected)
    with pytest.raises(ValueError,match='account_quota'):compare_account({**expected,'team_id':'another'},expected)
    with pytest.raises(ValueError,match='account_quota'):compare_account({**expected,'quotas':{'public':3}},expected)


@pytest.mark.parametrize('mutation',['version','score','state'])
def test_restored_completed_result_mismatch_is_failure(mutation):
    expected={'id':'b','version_id':'v','status':'completed','passed':0,'mean':'300.100000'}
    actual={'id':'b','version_id':'v','sha256':'a'*64,'status':'completed','score':{'passed':0,'mean_seconds':'300.100000'}}
    compare_batch(actual,expected,'a'*64)
    if mutation=='version':actual['version_id']='other'
    if mutation=='score':actual['score']['passed']=1
    if mutation=='state':actual['status']='testing'
    with pytest.raises(ValueError):compare_batch(actual,expected,'a'*64)


def test_supervisor_alone_is_not_api_evidence(tmp_path):
    (tmp_path/'api').write_text('not the executable')
    with pytest.raises(ValueError,match='process_identity'):
        verify_api_process(os.getpid(),tmp_path)


def test_process_group_cleanup_stops_child(tmp_path):
    child_file=tmp_path/'child'
    code='import subprocess,time,pathlib; p=subprocess.Popen(["/bin/sleep","60"]); pathlib.Path('+repr(str(child_file))+').write_text(str(p.pid)); time.sleep(60)'
    process=subprocess.Popen([sys.executable,'-c',code],start_new_session=True)
    try:
        for _ in range(100):
            if child_file.exists():break
            time.sleep(.01)
        child=int(child_file.read_text())
        assert process_running(child)
        stop_group(process)
        for _ in range(100):
            if not process_running(child):break
            time.sleep(.01)
        assert process.poll() is not None and not process_running(child)
    finally:
        stop_group(process)
