"""E2B-host OUTPUT guard; does not change scientific containers or task files."""
from __future__ import annotations
import hashlib
import json
import shlex
import subprocess
from pathlib import Path

CHAIN='QIQC_NATIVE_OUT'
RULES=[['-m','conntrack','--ctstate','ESTABLISHED,RELATED','-j','ACCEPT'],
       ['-o','lo','-j','ACCEPT'],['-j','REJECT']]


def _call(run,args,allowed=(0,)):
    result=run(args,check=False,capture_output=True,text=True,timeout=15)
    if result.returncode not in allowed:raise RuntimeError('native_host_output_command_failed')
    return result


def install(run=subprocess.run):
    if run is subprocess.run and Path('/root/QQ-Challenge').exists():
        raise RuntimeError('native_host_guard_e2b_only')
    evidence={}
    for binary in ('iptables','ip6tables'):
        _call(run,[binary,'-w','5','-N',CHAIN],(0,1))
        _call(run,[binary,'-w','5','-F',CHAIN])
        for rule in RULES:_call(run,[binary,'-w','5','-A',CHAIN,*rule])
        for _ in range(8):
            found=_call(run,[binary,'-w','5','-C','OUTPUT','-j',CHAIN],(0,1))
            if found.returncode:break
            _call(run,[binary,'-w','5','-D','OUTPUT','-j',CHAIN])
        _call(run,[binary,'-w','5','-I','OUTPUT','1','-j',CHAIN])
        for rule in RULES:_call(run,[binary,'-w','5','-C',CHAIN,*rule])
        output=_call(run,[binary,'-w','5','-S','OUTPUT']).stdout.strip()
        rules=_call(run,[binary,'-w','5','-S',CHAIN]).stdout.strip()
        evidence[binary]={'output_chain':output,'chain_rules':rules}
    receipt={'schema':'qiqc-native-host-output/v1','families':evidence}
    receipt['host_output_rules_sha256']=hashlib.sha256(json.dumps(evidence,sort_keys=True,separators=(',',':')).encode()).hexdigest()
    errors=verify_receipt(receipt)
    if errors:raise RuntimeError('native_host_output_readback_invalid')
    receipt['host_output_reject_verified']=True
    return receipt


def verify_receipt(receipt):
    errors=[]
    if not isinstance(receipt,dict) or receipt.get('schema')!='qiqc-native-host-output/v1':return ['host_output_schema']
    families=receipt.get('families',{})
    if set(families)!= {'iptables','ip6tables'}:return ['host_output_families']
    digest=hashlib.sha256(json.dumps(families,sort_keys=True,separators=(',',':')).encode()).hexdigest()
    if receipt.get('host_output_rules_sha256')!=digest:errors.append('host_output_digest')
    for binary,data in families.items():
        try:
            output=[shlex.split(row) for row in data['output_chain'].splitlines() if row.startswith('-A ')]
            rows=[shlex.split(row) for row in data['chain_rules'].splitlines() if row.startswith('-A ')]
            if not output or output[0]!=['-A','OUTPUT','-j',CHAIN]:errors.append('host_output_jump_order')
            if len(rows)!=3:errors.append('host_output_rule_count');continue
            first=rows[0]
            if first[:5]!=['-A',CHAIN,'-m','conntrack','--ctstate'] or len(first)!=8 or set(first[5].split(','))!={'ESTABLISHED','RELATED'} or first[6:]!=['-j','ACCEPT']:
                errors.append('host_output_connection_rule')
            if rows[1]!=['-A',CHAIN,'-o','lo','-j','ACCEPT']:errors.append('host_output_loopback_rule')
            third=rows[2]
            allowed=[['-A',CHAIN,'-j','REJECT'],['-A',CHAIN,'-j','REJECT','--reject-with','icmp-port-unreachable'],['-A',CHAIN,'-j','REJECT','--reject-with','icmp6-port-unreachable']]
            if third not in allowed:errors.append('host_output_reject_rule')
        except (KeyError,TypeError,ValueError):errors.append('host_output_malformed')
    return sorted(set(errors))


validate_receipt = verify_receipt


if __name__=='__main__':
    print(json.dumps(install(),sort_keys=True))
