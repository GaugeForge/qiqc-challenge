import asyncio
import copy
import sys
from pathlib import Path
import pytest
sys.path.insert(0,str(Path(__file__).parents[1]/'scripts/science'))
from native_final_containers import SCHEMA,validate_final_containers


def fixture():
    containers={r:{'Id':str(i)*64,'Image':'sha256:'+str(i)*64,'RestartCount':0,
        'State':{'Running':r!='main','Status':'exited' if r=='main' else 'running',
                 'OOMKilled':False,'Dead':False,'Restarting':False,'Error':'','ExitCode':0}}
        for i,r in enumerate(('main','qsim','verifier'),1)}
    terminal={'schema':SCHEMA,'containers':containers,
        'observed_before_cleanup':{r:True for r in containers},
        'collected_at':{r:1000 for r in containers},'capture_errors':[]}
    before=copy.deepcopy(containers['main']);before['State'].update(Running=True,Status='running')
    return terminal,{'container_inspects':copy.deepcopy(containers)},{'main_stopped_before_verifier':True,'main_before_planned_stop':before}


def test_terminal_state_not_cleanup_exit():
    terminal,startup,security=fixture()
    assert validate_final_containers(terminal,startup,security,0)==[]
    assert validate_final_containers(terminal,startup,security,1)==[]


@pytest.mark.parametrize('mutation,expected',[
 ('qsim_oom','qsim_terminal_oom_or_missing'),('qsim_exit','qsim_terminal_unexpected_exit'),
 ('restart','qsim_terminal_restarted_or_missing'),('verifier_missing','verifier_terminal_identity_mismatch'),
 ('main_live','main_terminal_not_stopped'),('main_unplanned','main_terminal_stop_not_planned'),
 ('main_crashed','main_exited_before_planned_stop'),
 ('identity','qsim_terminal_identity_mismatch'),('after_cleanup','qsim_terminal_not_before_cleanup'),
 ('verifier_error','terminal_verifier_process_not_completed')])
def test_late_runtime_failures_not_hidden_by_startup(mutation,expected):
    terminal,startup,security=fixture();exit_code=0
    if mutation=='qsim_oom':terminal['containers']['qsim']['State']['OOMKilled']=True
    if mutation=='qsim_exit':terminal['containers']['qsim']['State'].update(Running=False,Status='exited')
    if mutation=='restart':terminal['containers']['qsim']['RestartCount']=1
    if mutation=='verifier_missing':terminal['containers'].pop('verifier')
    if mutation=='main_live':terminal['containers']['main']['State']['Running']=True
    if mutation=='main_unplanned':security['main_stopped_before_verifier']=False
    if mutation=='main_crashed':security['main_before_planned_stop']['State']['Running']=False
    if mutation=='identity':terminal['containers']['qsim']['Id']='f'*64
    if mutation=='after_cleanup':terminal['observed_before_cleanup']['qsim']=False
    if mutation=='verifier_error':exit_code=2
    assert expected in validate_final_containers(terminal,startup,security,exit_code)


@pytest.mark.parametrize('inspect_fails',[False,True])
def test_capture_happens_before_cleanup_and_cleanup_survives_failure(monkeypatch,inspect_fails):
    import native_compose_environment as module
    import native_compose_observation as observation
    terminal,startup,security=fixture();events=[]
    env=object.__new__(module.NativeDockerEnvironment)
    env.native_role='agent';env.native_run_handle='terminal-fixture'
    module.EVIDENCE['terminal-fixture']={'runtime_observation':startup,**security}
    def inspect(cid):
        events.append('inspect:'+cid)
        if inspect_fails:raise RuntimeError('fixture error')
        return next(v for v in terminal['containers'].values() if v['Id']==cid)
    async def stop(self,delete):events.append('cleanup')
    monkeypatch.setattr(observation,'container_inspect',inspect)
    monkeypatch.setattr(module.DockerEnvironment,'stop',stop)
    try:
        asyncio.run(env.stop(True))
        assert events[-1]=='cleanup' and len(events)==3
        evidence=module.EVIDENCE['terminal-fixture']['final_container_evidence']
        assert bool(evidence['capture_errors']) is inspect_fails
        assert set(evidence['containers'])== (set() if inspect_fails else {'main','qsim'})
    finally:module.EVIDENCE.pop('terminal-fixture',None)
