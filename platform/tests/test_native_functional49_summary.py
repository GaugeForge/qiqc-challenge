"""Synthetic offline report aggregation; never creates scientific evidence."""
import json
import sys
from pathlib import Path
import pytest
sys.path.insert(0,str(Path(__file__).parents[1]/'scripts'))
from native_functional49_summary import assess,digest,markdown
from native_candidate import identity
from test_native_candidate import config,write_pair


@pytest.fixture
def cohort(tmp_path):
    cfg,_=config(tmp_path);cfg.update(build_plan_sha256='b'*64,build_receipt_sha256='c'*64)
    lanes=[tmp_path/f'lane-{i}' for i in range(3)]
    for lane in lanes:lane.mkdir()
    checker='d'*64
    for n,lane in enumerate(lanes):
        indices=list(range(n,49,3));rows=[]
        for i in indices:
            bp,ep=write_pair(lane,cfg,i)
            b=json.loads(bp.read_bytes());e=json.loads(ep.read_bytes())
            e.update(run_id=f'run-{i}',checker_sha256=checker,source_files_sha256=cfg['source_files_sha256'],model_id=identity(cfg,i)['model_id'],
                     build_input_digests={k:cfg[k] for k in ('build_plan_sha256','build_receipt_sha256')})
            e['model_response_evidence'].update(all_failures_recovered=True,unrecovered_failed_responses=0,
                invalid_request_evidence=0,invalid_planned_cancellations=0,successful_responses=1)
            target=lane/f'task-{i:02d}-attempt-01.json';r2=target.with_name(target.stem+'-r2.json')
            target.write_bytes(bp.read_bytes());r2.write_text(json.dumps(e));bp.unlink();ep.unlink()
            rows.append({'catalog_index':i,'attempt':1,'state':'passed','browser_sha256':digest(target.read_bytes()),'r2_sha256':digest(r2.read_bytes())})
        (lane/'suite.json').write_text(json.dumps({'schema':'qiqc-catalog-browser-suite/v1','status':'passed',
            'selected_indices':indices,'attempts':rows}))
    return cfg,lanes,checker


def test_all49_functional_never_claims_concurrency_or_freeze(cohort):
    result=assess(*cohort)
    assert result['functional_all49'] and result['passed_tasks']==49
    assert not result['concurrent_all49'] and not result['concurrent_all49_verified']
    assert not result['frozen'] and not result['platform_switched']
    assert '49 / 49' in markdown(result)


def test_latest_failed_attempt_cannot_be_hidden_by_older_pass(cohort):
    cfg,lanes,checker=cohort;lane=lanes[0]
    (lane/'task-00-attempt-02.json').write_text('{"status":"failed"}')
    result=assess(cfg,lanes,checker)
    assert result['passed_tasks']==48 and not result['functional_all49']
    assert result['rows'][0]['attempt']==2 and not result['rows'][0]['passed']


def test_latest_started_attempt_without_report_blocks_old_pass(cohort):
    cfg,lanes,checker=cohort;path=lanes[0]/'suite.json';suite=json.loads(path.read_bytes())
    suite['attempts'][0].update(attempt=2,state='browser_running');suite['status']='running';path.write_text(json.dumps(suite))
    result=assess(cfg,lanes,checker)
    assert result['passed_tasks']==48 and result['rows'][0]['attempt']==2


@pytest.mark.parametrize('mutation',['profile','controller','short','model_failure','image','cleanup','checker','digest'])
def test_mixed_or_incomplete_evidence_never_passes(cohort,mutation):
    cfg,lanes,checker=cohort;path=lanes[0]/'task-00-attempt-01-r2.json';e=json.loads(path.read_bytes())
    if mutation=='profile':e['benchmark_release']['provider_profile']={'sha256':'e'*64}
    if mutation=='controller':e['controller_sha256']='e'*64
    if mutation=='short':e['agent_elapsed_seconds']=42
    if mutation=='model_failure':e['model_response_evidence']['unrecovered_failed_responses']=1
    if mutation=='image':e['source_runtime_software_verified']=False
    if mutation=='cleanup':e['terminal_runtime_verified']=False
    if mutation=='checker':e['checker_sha256']='e'*64
    if mutation=='digest':e['browser_report_sha256']='e'*64
    path.write_text(json.dumps(e))
    # Even an updated suite hash cannot excuse a failed underlying contract.
    sp=lanes[0]/'suite.json';s=json.loads(sp.read_bytes());s['attempts'][0]['r2_sha256']=digest(path.read_bytes());sp.write_text(json.dumps(s))
    result=assess(cfg,lanes,checker)
    assert result['passed_tasks']==48 and not result['functional_all49']


def test_same_task_in_two_lanes_is_not_distinct_coverage(cohort):
    cfg,lanes,checker=cohort
    path=lanes[1]/'suite.json';s=json.loads(path.read_bytes());s['selected_indices'].append(0);path.write_text(json.dumps(s))
    result=assess(cfg,lanes,checker)
    assert not result['functional_all49'] and result['errors']==['task_present_in_multiple_lanes:0']


def test_orphan_old_pass_outside_current_manifest_is_not_accepted(cohort):
    cfg,lanes,checker=cohort;path=lanes[0]/'suite.json';s=json.loads(path.read_bytes())
    s['attempts']=[r for r in s['attempts'] if r['catalog_index']!=0];path.write_text(json.dumps(s))
    result=assess(cfg,lanes,checker)
    assert result['passed_tasks']==48 and 'attempt_not_in_current_suite_manifest' in result['rows'][0]['errors']
