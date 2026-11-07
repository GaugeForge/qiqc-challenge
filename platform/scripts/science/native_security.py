"""Competition-only overlay and host firewall for original Compose in E2B.

Source task files remain unchanged. Network restriction intentionally overrides
the upstream development allow_internet policy. No cloud or Docker action occurs
on import. install() belongs on the trusted E2B host, before any Harness starts.
"""
from __future__ import annotations

import datetime
import hashlib
import json
import re
import socket
import subprocess
from pathlib import Path


class NativeSecurityError(RuntimeError):
    pass


def network_spec(project):
    if not isinstance(project,str) or not re.fullmatch(r'[a-z0-9][a-z0-9_-]{0,62}',project):
        raise NativeSecurityError('invalid_compose_project')
    digest=hashlib.sha256(project.encode()).hexdigest()
    prefix='10.253.'+str(1+int(digest[:2],16)%253)
    return {'project':project,'network':project+'_default','bridge':'qc'+digest[:12],
            'subnet':prefix+'.0/24','gateway':prefix+'.1','main_ip':prefix+'.2',
            'qsim_ip':prefix+'.3','relay_port':8181,'input_chain':'QC'+digest[:10]+'I',
            'forward_chain':'QC'+digest[:10]+'F'}


def compose_overlay(project):
    net=network_spec(project)
    common={'cap_drop':['NET_RAW','NET_ADMIN','SYS_ADMIN'],
            'security_opt':['no-new-privileges:true'],
            'dns':['127.0.0.1'], 'dns_opt':['timeout:1','attempts:1'],
            'sysctls':{'net.ipv6.conf.all.disable_ipv6':'1','net.ipv6.conf.default.disable_ipv6':'1'}}
    return {'services':{
        'main':{**common,'networks':{'default':{'ipv4_address':net['main_ip']}},
                'extra_hosts':['host.docker.internal:'+net['gateway']]},
        'qsim':{**common,'networks':{'default':{'ipv4_address':net['qsim_ip']}}}},
        'networks':{'default':{'internal':True,'enable_ipv6':False,'driver':'bridge',
                   'driver_opts':{'com.docker.network.bridge.name':net['bridge']},
                   'ipam':{'config':[{'subnet':net['subnet'],'gateway':net['gateway']}]}}}}


def verifier_overlay():
    return {'services':{'main':{'network_mode':'none','cap_drop':['NET_RAW','NET_ADMIN','SYS_ADMIN'],
                                'security_opt':['no-new-privileges:true']}}}


def firewall_rules(project):
    """Token arrays only; no shell interpolation or mutable global policy."""
    n=network_spec(project);ic,fc=n['input_chain'],n['forward_chain']
    return {
        'chains':[ic,fc],
        'rules':[
            ['-A',ic,'-s',n['main_ip'],'-d',n['gateway'],'-p','tcp','--dport','8181','-j','ACCEPT'],
            ['-A',ic,'-j','DROP'],
            ['-A',fc,'-s',n['main_ip'],'-d',n['qsim_ip'],'-p','tcp','--dport','8123','-j','ACCEPT'],
            ['-A',fc,'-s',n['qsim_ip'],'-d',n['main_ip'],'-p','tcp','--sport','8123','-m','conntrack','--ctstate','ESTABLISHED','-j','ACCEPT'],
            ['-A',fc,'-j','DROP']],
        'jumps':[['INPUT','-i',n['bridge'],'-j',ic],
                 ['FORWARD','-i',n['bridge'],'-j',fc],
                 ['FORWARD','-o',n['bridge'],'-j',fc]],
    }


def _run(run,argv,*,allowed=(0,),timeout=30):
    value=run(argv,capture_output=True,text=True,timeout=timeout,check=False)
    if value.returncode not in allowed:
        raise NativeSecurityError('trusted_security_command_failed')
    return value


def _inspect(run,kind,identifier):
    value=json.loads(_run(run,['docker',kind,'inspect',identifier]).stdout)
    if not isinstance(value,list) or len(value)!=1 or not isinstance(value[0],dict):
        raise NativeSecurityError('invalid_docker_inspection')
    return value[0]


def _container_check(container,net,role):
    host=container.get('HostConfig',{})
    if host.get('Privileged') is not False or host.get('PidMode')=='host' or host.get('NetworkMode')=='host':
        raise NativeSecurityError('unsafe_native_container_namespace')
    if set(host.get('CapAdd') or []) & {'ALL','NET_ADMIN','SYS_ADMIN','NET_RAW'}:
        raise NativeSecurityError('unsafe_native_container_capability')
    dropped={v.removeprefix('CAP_') for v in host.get('CapDrop') or []}
    if not {'NET_RAW','NET_ADMIN','SYS_ADMIN'}<=dropped:
        raise NativeSecurityError('required_native_capability_drop_missing')
    if not any(v in {'no-new-privileges','no-new-privileges:true'} for v in host.get('SecurityOpt') or []):
        raise NativeSecurityError('native_no_new_privileges_missing')
    if host.get('Dns')!=['127.0.0.1']:
        raise NativeSecurityError('native_external_dns_not_disabled')
    networks=container.get('NetworkSettings',{}).get('Networks',{})
    if set(networks)!={net['network']} or networks[net['network']].get('IPAddress')!=net[role+'_ip']:
        raise NativeSecurityError('native_network_attachment_mismatch')
    if networks[net['network']].get('GlobalIPv6Address'):
        raise NativeSecurityError('unexpected_native_ipv6_address')
    for mount in container.get('Mounts',[]):
        src,dst=str(mount.get('Source','')),str(mount.get('Destination',''))
        if 'docker.sock' in src+dst or src=='/' or dst in {'/var/run','/run','/proc','/sys','/dev'}:
            raise NativeSecurityError('native_host_control_mount')


def _connect_code(host,port):
    # Success is 0; controlled socket failure exactly 7. Docker/executable errors
    # use other codes and must not masquerade as successful negative checks.
    return ('import socket,sys\ntry:\n s=socket.create_connection(('+repr(host)+','+str(port)+'),1.5);s.close()\n'
            'except OSError: sys.exit(7)\nsys.exit(0)')


def install(project,main_id,qsim_id,run=subprocess.run):
    """Install and prove restrictions on an existing E2B-host Compose run.

    Returns a private receipt. Raises before Harness setup on any failed check.
    Network deny-all at the E2B boundary is a separate required outer-host gate.
    """
    if run is subprocess.run and Path('/root/QQ-Challenge').exists():
        raise NativeSecurityError('native_firewall_must_run_in_e2b')
    if any(not re.fullmatch(r'[a-f0-9]{12,64}',v) for v in [main_id,qsim_id]):
        raise NativeSecurityError('invalid_native_container_id')
    net=network_spec(project)
    actual_network=_inspect(run,'network',net['network'])
    if actual_network.get('Internal') is not True or actual_network.get('EnableIPv6') is not False or actual_network.get('Options',{}).get('com.docker.network.bridge.name')!=net['bridge']:
        raise NativeSecurityError('native_network_not_isolated')
    main=_inspect(run,'container',main_id);qsim=_inspect(run,'container',qsim_id)
    _container_check(main,net,'main');_container_check(qsim,net,'qsim')
    # Force bridge traffic through FORWARD; otherwise same-bridge packets may
    # bypass iptables even though host INPUT is protected.
    _run(run,['sysctl','-w','net.bridge.bridge-nf-call-iptables=1'])
    settings=firewall_rules(project)
    for chain in settings['chains']:
        value=_run(run,['iptables','-w','5','-N',chain],allowed=(0,1))
        if value.returncode:
            _run(run,['iptables','-w','5','-S',chain])
        _run(run,['iptables','-w','5','-F',chain])
    for rule in settings['rules']:
        _run(run,['iptables','-w','5',*rule])
    for jump in settings['jumps']:
        # Remove previous identical hooks, then insert before all Docker ACCEPT
        # paths. Keep changes scoped to this unique run bridge and own chains.
        for _ in range(4):
            value=_run(run,['iptables','-w','5','-C',*jump],allowed=(0,1))
            if value.returncode:break
            _run(run,['iptables','-w','5','-D',*jump])
        _run(run,['iptables','-w','5','-I',jump[0],'1',*jump[1:]])
    for rule in settings['rules']:
        _run(run,['iptables','-w','5','-C',*rule[1:]])
    for jump in settings['jumps']:
        _run(run,['iptables','-w','5','-C',*jump])
    probes={}
    # A listening private host canary proves denial, instead of accepting
    # connection-refused from a port that never had a service.
    with socket.socket() as canary:
        canary.bind(('0.0.0.0',0));canary.listen(4)
        private_port=canary.getsockname()[1]
        cases=[('main_model_relay',main_id,net['gateway'],8181,0),
               ('main_private_host_port',main_id,net['gateway'],private_port,7),
               ('qsim_model_relay_denied',qsim_id,net['gateway'],8181,7),
               ('qsim_private_host_port',qsim_id,net['gateway'],private_port,7),
               ('main_external_egress',main_id,'1.1.1.1',443,7),
               ('main_metadata_route',main_id,'169.254.169.254',80,7)]
        for name,container,host,port,expected in cases:
            result=_run(run,['docker','exec',container,'python','-c',_connect_code(host,port)],allowed=(0,7))
            probes[name]={'exit_code':result.returncode,'expected_exit_code':expected}
            if result.returncode!=expected:
                raise NativeSecurityError('native_network_probe_failed')
    result=_run(run,['docker','exec',main_id,'curl','--silent','--output','/dev/null','--write-out','%{http_code}','--max-time','5','http://qsim:8123/health'])
    if result.stdout.strip()!='200':
        raise NativeSecurityError('native_qsim_route_not_ready')
    probes['qsim_http']={'status':200}
    # Docker's embedded resolver still serves Compose aliases, but its external
    # upstream is explicitly container-loopback (no DNS service). Do not allow
    # arbitrary query names to become an outbound data channel via host DNS.
    dns_probe='import socket,sys\ntry: socket.getaddrinfo("example.com",443)\nexcept OSError: sys.exit(7)\nsys.exit(0)'
    result=_run(run,['docker','exec',main_id,'python','-c',dns_probe],allowed=(0,7),timeout=10)
    probes['main_external_dns']={'exit_code':result.returncode,'expected_exit_code':7}
    if result.returncode!=7:
        raise NativeSecurityError('native_external_dns_probe_failed')
    for path in ['/var/run/docker.sock','/app/configs','/app/qiqcbench_configs','/qsim_logs/experiment_log.jsonl','/qsim_logs/final_answer.json','/qsim_logs/private','/verifier/private']:
        result=_run(run,['docker','exec',main_id,'test','-r',path],allowed=(0,1))
        probes['private_read:'+path]={'exit_code':result.returncode,'expected_exit_code':1}
        if result.returncode!=1:
            raise NativeSecurityError('native_private_material_readable')
    result=_run(run,['docker','exec',main_id,'touch','/qsim_logs/public_job_results/.qiqc-readonly-probe'],allowed=(0,1))
    probes['public_volume_write']={'exit_code':result.returncode,'expected_exit_code':1}
    if result.returncode!=1:
        raise NativeSecurityError('native_public_volume_writable')
    saved='\n'.join(_run(run,['iptables','-w','5','-S',chain]).stdout for chain in settings['chains'])
    return {'schema':'qiqc-native-security/v1','passed':True,'project':project,'network_spec':net,
            'iptables_sha256':hashlib.sha256(saved.encode()).hexdigest(),'iptables_readback':saved,'iptables_rules':settings,
            'probes':probes,'checked_at':datetime.datetime.now(datetime.timezone.utc).isoformat(),
            'e2b_outer_egress_denial_verified_by_this_check':False,
            'policy_difference':'Competition restricts upstream development internet policy; source files unchanged'}


def validate_receipt(receipt):
    """Independent private-rollout check; outer E2B deny-all is checked separately."""
    errors=[]
    try:
        expected=network_spec(receipt.get('project'))
    except (NativeSecurityError,AttributeError):
        return ['security_receipt_invalid_project']
    if receipt.get('schema')!='qiqc-native-security/v1' or receipt.get('passed') is not True:
        errors.append('security_receipt_not_passed')
    if receipt.get('network_spec')!=expected or receipt.get('iptables_rules')!=firewall_rules(receipt['project']):
        errors.append('security_rule_contract_mismatch')
    raw=receipt.get('iptables_readback')
    if not isinstance(raw,str) or not raw.strip() or hashlib.sha256(raw.encode()).hexdigest()!=receipt.get('iptables_sha256'):
        errors.append('security_rule_readback_missing_or_changed')
    probes=receipt.get('probes',{})
    codes={'main_model_relay':0,'main_private_host_port':7,'qsim_model_relay_denied':7,
           'qsim_private_host_port':7,'main_external_egress':7,'main_metadata_route':7,
           'main_external_dns':7,'public_volume_write':1}
    for path in ['/var/run/docker.sock','/app/configs','/app/qiqcbench_configs','/qsim_logs/experiment_log.jsonl','/qsim_logs/final_answer.json','/qsim_logs/private','/verifier/private']:
        codes['private_read:'+path]=1
    if any(probes.get(name,{}).get('exit_code')!=code or probes.get(name,{}).get('expected_exit_code')!=code for name,code in codes.items()):
        errors.append('security_probe_missing_or_failed')
    if probes.get('qsim_http',{}).get('status')!=200:
        errors.append('security_qsim_route_missing_or_failed')
    return errors
