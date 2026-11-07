import copy,hashlib,json
from types import SimpleNamespace
import pytest
from native_host_network import install,verify_receipt,CHAIN


def fixture():
    families={binary:{'output_chain':'-P OUTPUT ACCEPT\n-A OUTPUT -j '+CHAIN,
        'chain_rules':'-N '+CHAIN+'\n-A '+CHAIN+' -m conntrack --ctstate RELATED,ESTABLISHED -j ACCEPT\n-A '+CHAIN+' -o lo -j ACCEPT\n-A '+CHAIN+' -j REJECT --reject-with '+('icmp-port-unreachable' if binary=='iptables' else 'icmp6-port-unreachable')} for binary in ('iptables','ip6tables')}
    return {'schema':'qiqc-native-host-output/v1','families':families,'host_output_rules_sha256':hashlib.sha256(json.dumps(families,sort_keys=True,separators=(',',':')).encode()).hexdigest()}


def test_install_only_own_chains_and_readback():
    calls=[];value=fixture()
    def run(args,**kwargs):
        calls.append(args)
        if args[3:]==['-C','OUTPUT','-j',CHAIN]:return SimpleNamespace(returncode=1,stdout='')
        stdout=''
        if args[3:]==['-S','OUTPUT']:stdout=value['families'][args[0]]['output_chain']
        if args[3:]==['-S',CHAIN]:stdout=value['families'][args[0]]['chain_rules']
        return SimpleNamespace(returncode=0,stdout=stdout)
    got=install(run)
    assert got['host_output_reject_verified'] is True and verify_receipt(got)==[]
    assert all('-P' not in call for call in calls)
    assert [c[4] for c in calls if '-F' in c]==[CHAIN,CHAIN]


@pytest.mark.parametrize('mutation',[
 lambda x:x.update(host_output_rules_sha256='0'*64),
 lambda x:x['families'].pop('ip6tables'),
 lambda x:x['families']['iptables'].update(output_chain='-A OUTPUT -j ACCEPT\n-A OUTPUT -j '+CHAIN),
 lambda x:x['families']['iptables'].update(chain_rules=x['families']['iptables']['chain_rules'].replace('-j REJECT','-j ACCEPT')),
])
def test_rejects_tampered_or_weakened_readback(mutation):
    x=fixture();mutation(x);assert verify_receipt(x)
