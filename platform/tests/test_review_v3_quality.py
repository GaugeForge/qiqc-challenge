"""Synthetic scoring proof only: no model calls, fixture execution, or credentials."""
import copy
import importlib.util
import json
from pathlib import Path
import pytest
import shutil
import hashlib

BASE=Path(__file__).parent/'review_blindtest/v3'
spec=importlib.util.spec_from_file_location('review_v3',BASE/'evaluate.py')
evaluate=importlib.util.module_from_spec(spec);spec.loader.exec_module(evaluate)


@pytest.fixture(autouse=True)
def synthetic_scoring_contract(tmp_path, monkeypatch):
    # Exercise scoring against a newly hashed local fixture. This is NOT a
    # repair, replacement, or re-signing of historical live-review evidence.
    folder=tmp_path/'synthetic-corpus';folder.mkdir()
    for name in ('fixtures.json','expected-labels.json','evaluate.py','frozen-manifest.json'):
        shutil.copyfile(BASE/name,folder/name)
    manifest=json.loads((folder/'frozen-manifest.json').read_text())
    manifest['files']={str((folder/name).relative_to(tmp_path)):hashlib.sha256((folder/name).read_bytes()).hexdigest()
                       for name in ('fixtures.json','expected-labels.json','evaluate.py')}
    (folder/'frozen-manifest.json').write_text(json.dumps(manifest))
    monkeypatch.setattr(evaluate,'HERE',folder);monkeypatch.setattr(evaluate,'ROOT',tmp_path)
    monkeypatch.setattr(__import__(__name__,fromlist=['BASE']),'BASE',folder)


def sample(tmp_path):
    labels=json.loads((BASE/'expected-labels.json').read_text())
    rows=[]
    for item in labels['fixtures']:
        rows.append({'fixture':item['id'],'version_status':'pending_review','ledger_rows':0,'admin_visible':True,
                     'submission_sha256':'a'*64,'scanner_rules':[],
                     'advice':{'policy_version':evaluate.POLICY,'provider_version':evaluate.PROVIDER,'status':'manual_required',
                               'sha256':'a'*64,'outcome':'advice_ready','findings':[{'rule':r} for r in item['expected_rules']]}})
    report={'mode':'v3-independent-frozen-quality-real-docker-openrouter','provider_calls':24,
            'freeze_manifest_sha256':evaluate.digest(BASE/'frozen-manifest.json'),'fixtures':rows,
            'system_invariants_passed':True,'temporary_resources_cleaned':True}
    path=tmp_path/'synthetic-report.json';path.write_text(json.dumps(report));return path,report


def test_exact_synthetic_scores_cannot_be_security_signoff(tmp_path):
    path,_=sample(tmp_path);result=evaluate.score(path)
    assert result['micro']['precision']==result['micro']['recall']==1
    assert result['system_invariants_passed'] is True and result['formal_security_signoff'] is False
    assert result['benign_cases']['total']==5
    assert all(v['support']>0 for v in result['per_category'].values())


@pytest.mark.parametrize('fault',['unknown_case','duplicate','budget','policy','provider','mode','freeze','unknown_rule','missing_checkpoint'])
def test_untrusted_or_mismatched_report_rejected(tmp_path,fault):
    path,r=sample(tmp_path)
    if fault=='unknown_case':r['fixtures'][0]['fixture']='extra'
    if fault=='duplicate':r['fixtures'][1]=copy.deepcopy(r['fixtures'][0])
    if fault=='budget':r['provider_calls']=25
    if fault in ('policy','provider'):r['fixtures'][0]['advice'][fault+'_version']='old'
    if fault=='mode':r['mode']='mock'
    if fault=='freeze':r['freeze_manifest_sha256']='0'*64
    if fault=='unknown_rule':r['fixtures'][0]['advice']['findings']=[{'rule':'approve'}]
    if fault=='missing_checkpoint':r['fixtures'].pop()
    path.write_text(json.dumps(r))
    with pytest.raises(ValueError):evaluate.score(path)


def test_fallback_is_missed_detection_not_safe(tmp_path):
    path,r=sample(tmp_path);r['fixtures'][8]['advice']['outcome']='timeout';path.write_text(json.dumps(r))
    value=evaluate.score(path)
    assert value['manual_fallback_count']==1 and value['micro']['fn']==3
    assert value['cases'][8]['predicted']==[]


@pytest.mark.parametrize('fault',['quota','approved','admin','cleanup','binding'])
def test_system_evidence_not_trusted_from_top_level_boolean(tmp_path,fault):
    path,r=sample(tmp_path)
    if fault=='quota':r['fixtures'][0]['ledger_rows']=1
    if fault=='approved':r['fixtures'][0]['advice']['status']='approved'
    if fault=='admin':r['fixtures'][0]['admin_visible']=False
    if fault=='cleanup':r['temporary_resources_cleaned']=False
    if fault=='binding':r['fixtures'][0]['advice']['sha256']='b'*64
    path.write_text(json.dumps(r));assert evaluate.score(path)['system_invariants_passed'] is False


def test_benign_authorized_relay_false_alert_counted(tmp_path):
    path,r=sample(tmp_path);r['fixtures'][2]['advice']['findings']=[{'rule':'credential_access'}]
    path.write_text(json.dumps(r));value=evaluate.score(path)
    assert value['benign_cases']['false_alert_cases']==1 and value['micro']['fp']==1


def test_partial_run_remains_incomplete(tmp_path):
    path,r=sample(tmp_path);r['fixtures']=r['fixtures'][:2];r['provider_calls']=2
    path.write_text(json.dumps(r));value=evaluate.score(path)
    assert value['complete'] is False and value['system_invariants_passed'] is False
    assert value['outcomes']['not_run']==22
