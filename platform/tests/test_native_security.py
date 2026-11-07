"""Firewall composition and fail-closed orchestration without touching host rules."""
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
sys.path.insert(0,str(Path(__file__).parents[1]/'scripts/science'))
import native_security as security

PROJECT='acceptance_42'
MAIN='a'*64
QSIM='b'*64


def fake_runner(*,failed_probe=None,bad_cap=False):
    commands=[];net=security.network_spec(PROJECT)
    def run(argv,**kwargs):
        commands.append(argv);out='';code=0
        if argv[:3]==['docker','network','inspect']:
            out=json.dumps([{'Internal':True,'EnableIPv6':False,'Options':{'com.docker.network.bridge.name':net['bridge']}}])
        elif argv[:3]==['docker','container','inspect']:
            role='main' if argv[-1]==MAIN else 'qsim'
            out=json.dumps([{'HostConfig':{'Privileged':False,'CapDrop':[] if bad_cap else ['NET_RAW','NET_ADMIN','SYS_ADMIN'],
                                          'SecurityOpt':['no-new-privileges:true'],'Dns':['127.0.0.1']},
                'NetworkSettings':{'Networks':{net['network']:{'IPAddress':net[role+'_ip']}}}}])
        elif argv[0]=='iptables' and '-C' in argv and argv[argv.index('-C')+1] in ['INPUT','FORWARD']:
            code=1 if not any(a[0]=='iptables' and '-I' in a for a in commands) else 0
        elif argv[0]=='iptables' and '-S' in argv:
            out='-N '+argv[-1]+'\n'
        elif argv[:2]==['docker','exec']:
            if argv[3]=='python':
                allowed=argv[2]==MAIN and repr(net['gateway']) in argv[-1] and ',8181)' in argv[-1]
                code=0 if allowed else 7
                if failed_probe=='egress' and '1.1.1.1' in argv[-1]:code=0
            elif argv[3]=='curl':out='200'
            elif argv[3] in ['test','touch']:code=1
        return SimpleNamespace(returncode=code,stdout=out,stderr='')
    return run,commands


def test_overlay_preserves_original_task_files_and_limits_namespaces():
    n=security.network_spec(PROJECT);o=security.compose_overlay(PROJECT)
    assert o['networks']['default']['internal'] is True
    assert len(n['bridge'])<=15
    assert o['services']['main']['extra_hosts']==['host.docker.internal:'+n['gateway']]
    assert o['services']['main']['networks']['default']['ipv4_address']==n['main_ip']
    assert 'volumes' not in o['services']['main']
    assert security.verifier_overlay()['services']['main']['network_mode']=='none'
    assert 'NET_RAW' in o['services']['main']['cap_drop']


def test_firewall_only_grants_main_host_model_port_and_science_service():
    n=security.network_spec(PROJECT);rules=security.firewall_rules(PROJECT)
    accepts=[x for x in rules['rules'] if x[-1]=='ACCEPT']
    assert len(accepts)==3
    host=accepts[0]
    assert host[host.index('--dport')+1]=='8181' and host[host.index('-s')+1]==n['main_ip']
    assert not any('0.0.0.0/0' in a for a in rules['rules'])
    assert len(rules['jumps'])==3


def test_install_gates_every_probe_before_returning_success():
    run,commands=fake_runner()
    receipt=security.install(PROJECT,MAIN,QSIM,run)
    assert receipt['passed'] is True
    assert receipt['e2b_outer_egress_denial_verified_by_this_check'] is False
    assert receipt['probes']['main_private_host_port']['exit_code']==7
    assert receipt['probes']['qsim_model_relay_denied']['exit_code']==7
    assert any(a[:3]==['sysctl','-w','net.bridge.bridge-nf-call-iptables=1'] for a in commands)
    first_probe=next(i for i,a in enumerate(commands) if a[:2]==['docker','exec'])
    assert any('-I' in a for a in commands[:first_probe])
    assert not security.validate_receipt(receipt)
    del receipt['probes']['main_external_dns']
    assert security.validate_receipt(receipt)==['security_probe_missing_or_failed']


def test_egress_or_missing_capability_drop_blocks_harness():
    run,_=fake_runner(failed_probe='egress')
    with pytest.raises(security.NativeSecurityError,match='network_probe_failed'):
        security.install(PROJECT,MAIN,QSIM,run)
    run,_=fake_runner(bad_cap=True)
    with pytest.raises(security.NativeSecurityError,match='capability_drop_missing'):
        security.install(PROJECT,MAIN,QSIM,run)


def test_never_run_firewall_on_workspace_host_and_reject_shell_fragments():
    with pytest.raises(security.NativeSecurityError,match='must_run_in_e2b'):
        security.install(PROJECT,MAIN,QSIM)
    with pytest.raises(security.NativeSecurityError,match='invalid_compose_project'):
        security.compose_overlay('x; echo unsafe')
