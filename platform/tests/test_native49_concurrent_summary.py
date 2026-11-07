"""Synthetic metadata only: no cloud access or scientific execution."""
import datetime
import json
from pathlib import Path
import sys
import subprocess

import pytest

sys.path.insert(0,str(Path(__file__).parents[1]/'scripts'))
from native49_concurrent_summary import assess,digest,markdown
from native_candidate import identity
from test_native_candidate import config,write_pair


@pytest.fixture
def cohort(tmp_path):
    candidate,_=config(tmp_path)
    candidate.update(agent_budget_seconds=180,build_plan_sha256='b'*64,build_receipt_sha256='c'*64)
    directory=tmp_path/'reports';directory.mkdir();checker='d'*64
    start=datetime.datetime(2026,10,21,tzinfo=datetime.timezone.utc)
    for index in range(49):
        browser,evidence=write_pair(directory,candidate,index)
        value=json.loads(evidence.read_text())
        begin=start+datetime.timedelta(seconds=index)
        value.update(run_id=f'run-{index}',checker_sha256=checker,source_files_sha256=candidate['source_files_sha256'],
            model_id=identity(candidate,index)['model_id'],build_input_digests={key:candidate[key] for key in ('build_plan_sha256','build_receipt_sha256')},
            acceptance_scope='bounded-three-minute',agent_budget_seconds=180,agent_elapsed_seconds=180.2,
            observation_policy='observe-180s-no-readiness-stop/v1',execution_duration_class='budget_180s_observed',
            agent_execution_started_at=begin.isoformat(),agent_execution_finished_at=(begin+datetime.timedelta(seconds=180.2)).isoformat())
        value['model_response_evidence'].update(all_failures_recovered=True,unrecovered_failed_responses=0,
            invalid_request_evidence=0,invalid_planned_cancellations=0,successful_responses=1)
        browser.rename(directory/f'task-{index:02d}-attempt-01.json')
        target=directory/f'task-{index:02d}-attempt-01-r2.json';target.write_text(json.dumps(value));evidence.unlink()
    return candidate,directory,checker


def mutate(cohort,index,change):
    path=cohort[1]/f'task-{index:02d}-attempt-01-r2.json'
    value=json.loads(path.read_text());change(value);path.write_text(json.dumps(value))


def test_actual_all49_overlap_and_180s_acceptance(cohort):
    result=assess(*cohort)
    assert result['passed_indices']==list(range(49))
    assert result['all49_end_to_end_passed'] and result['all49_agent_overlap_verified']
    assert result['agent_start_span_seconds']==48
    assert result['observed_peak_agent_concurrency']==49
    assert result['all49_common_agent_window']['overlap_seconds']==pytest.approx(132.2)
    assert not result['peak_is_lower_bound']
    assert not result['frozen'] and not result['platform_switched']
    assert '49 / 49' in markdown(result)
    for row in result['rows']:
        assert row['browser_sha256']==digest((cohort[1]/row['browser_report']).read_bytes())
        assert row['r2_sha256']==digest((cohort[1]/row['r2_report']).read_bytes())


def test_queued_49_is_not_49_simultaneous_agents(cohort):
    for index in range(49):
        def shift(value):
            start=datetime.datetime(2026,10,21,tzinfo=datetime.timezone.utc)+datetime.timedelta(seconds=index*200)
            value.update(agent_execution_started_at=start.isoformat(),agent_execution_finished_at=(start+datetime.timedelta(seconds=180.2)).isoformat())
        mutate(cohort,index,shift)
    result=assess(*cohort)
    assert result['all49_end_to_end_passed']
    assert result['observed_peak_agent_concurrency']==1
    assert not result['all49_agent_overlap_verified']
    assert result['status']=='incomplete'


def test_touching_intervals_do_not_overlap(cohort):
    for index in range(49):
        def shift(value):
            start=datetime.datetime(2026,10,21,tzinfo=datetime.timezone.utc)+datetime.timedelta(seconds=180.2 if index==48 else 0)
            value.update(agent_execution_started_at=start.isoformat(),agent_execution_finished_at=(start+datetime.timedelta(seconds=180.2)).isoformat())
        mutate(cohort,index,shift)
    result=assess(*cohort)
    assert result['observed_peak_agent_concurrency']==48
    assert not result['all49_agent_overlap_verified']


def test_failure_without_harbor_time_is_missing_not_fabricated(cohort):
    def change(value):
        value.update(status='failed',failure_code='candidate_qsim_not_called')
        value.pop('agent_execution_started_at');value.pop('agent_execution_finished_at')
    mutate(cohort,7,change)
    result=assess(*cohort)
    assert result['failed_indices']==[7]
    assert result['timing_missing_indices']==[7]
    assert result['peak_is_lower_bound']
    assert result['observed_peak_agent_concurrency']==48
    assert result['rows'][7]['failure_category']=='qsim_evidence'
    assert not result['all49_agent_overlap_verified']


def test_failure_with_bound_real_time_can_still_prove_overlap(cohort):
    mutate(cohort,8,lambda v:v.update(status='failed',failure_code='candidate_model_unrecovered'))
    result=assess(*cohort)
    assert result['failed_indices']==[8]
    assert result['all49_agent_overlap_verified']
    assert not result['all49_end_to_end_passed']
    assert result['status']=='incomplete'


@pytest.mark.parametrize('key,value',[
    ('controller_sha256','f'*64),('source_files_sha256','f'*64),('browser_report_sha256','f'*64),
    ('checker_sha256','f'*64),('agent_budget_seconds',300),('acceptance_scope','bounded-five-minute'),
    ('agent_budget_seconds',180.0),('archive_sha256','f'*64),('submission_sha256','f'*64),
    ('build_input_digests',{}),('run_id',[]),
    ('catalog_index',48),('schema','other'),('agent_execution_started_at','2026-10-21T00:00:00'),
    ('agent_elapsed_seconds',12),('harbor_agent_timing_verified',False),
])
def test_wrong_identity_digest_budget_or_timing_rejects(cohort,key,value):
    mutate(cohort,0,lambda v:v.update({key:value}))
    result=assess(*cohort)
    assert result['failed_indices']==[0]
    assert result['timing_missing_indices']==[0]
    assert not result['all49_agent_overlap_verified']


def test_changed_browser_bytes_break_recorded_digest(cohort):
    path=cohort[1]/'task-00-attempt-01.json';path.write_text(path.read_text()+' ')
    result=assess(*cohort)
    assert result['failed_indices']==[0] and result['timing_missing_indices']==[0]


def test_candidate_300_default_cannot_be_called_180(cohort):
    config,directory,checker=cohort;config.pop('agent_budget_seconds')
    with pytest.raises(ValueError,match='180_second_candidate'):assess(config,directory,checker)


def test_newer_prepared_attempt_prevents_old_pass_selection(cohort):
    (cohort[1]/'task-00-attempt-02-prepared.json').write_text('{"status":"browser_queued_waiting_barrier"}')
    result=assess(*cohort)
    assert result['pending_indices']==[0]
    assert result['timing_missing_indices']==[0]
    assert result['rows'][0]['attempt']==2


def test_duplicate_run_cannot_prove_distinct_agents(cohort):
    mutate(cohort,1,lambda v:v.update(run_id='run-0'))
    result=assess(*cohort)
    assert result['failed_indices']==[0,1]
    assert result['timing_missing_indices']==[0,1]
    assert result['observed_peak_agent_concurrency']==47


def test_failure_text_is_not_echoed(cohort):
    mutate(cohort,2,lambda v:v.update(status='failed',failure_code='PRIVATE_TASK_CONTENT_OR_CREDENTIAL'))
    result=assess(*cohort)
    assert 'PRIVATE_TASK_CONTENT_OR_CREDENTIAL' not in json.dumps(result)
    assert result['rows'][2]['failure_category']=='other_or_unclassified'


def test_empty_directory_reports_all_missing(cohort):
    for path in cohort[1].iterdir():path.unlink()
    result=assess(*cohort)
    assert result['pending_indices']==list(range(49))
    assert result['timing_missing_indices']==list(range(49))
    assert result['observed_peak_agent_concurrency']==0
    assert result['agent_start_span_seconds'] is None


def test_duplicate_flat_and_subdirectory_attempt_refused(cohort):
    root=cohort[1];sub=root/'task-00';sub.mkdir()
    (sub/'task-00-attempt-01.json').write_bytes((root/'task-00-attempt-01.json').read_bytes())
    with pytest.raises(ValueError,match='duplicate_attempt_locations'):assess(*cohort)


def test_malformed_model_evidence_cannot_leave_timing_claim_behind(cohort):
    mutate(cohort,0,lambda v:v.update(model_response_evidence=['bad']))
    result=assess(*cohort)
    assert result['failed_indices']==[0]
    assert result['timing_missing_indices']==[0]


def test_cli_imports_deployed_controller_before_workspace_inventory(tmp_path):
    root=tmp_path/'deployed';science=root/'competition-platform/platform/scripts/science';science.mkdir(parents=True)
    (science/'native_candidate.py').write_text('def agent_budget_seconds(c): return 180\ndef identity(c,i): return {}\ndef load_candidate(p): raise RuntimeError("DEPLOYED_CANDIDATE_BOUND")\n')
    (science/'native_controller.py').write_text('def verify(c): return None\n')
    (science/'native_release_record.py').write_text('def agent_window(v): return []\ndef concurrency_evidence(v): return {}\ndef read_regular(p,*args): return b"{}"\ndef acceptance_evidence_errors(v): return []\n')
    (science/'native_freeze_gate.py').write_text('def runtime_evidence_errors(v): return []\n')
    candidate=tmp_path/'candidate.json';candidate.write_text(json.dumps({'project_root':str(root)}))
    command=[sys.executable,str(Path(__file__).parents[1]/'scripts/native49_concurrent_summary.py'),
             '--candidate',str(candidate),'--output-dir',str(tmp_path),'--output',str(tmp_path.parent/'unwritten.json')]
    result=subprocess.run(command,capture_output=True,text=True,check=False)
    assert result.returncode != 0
    assert 'DEPLOYED_CANDIDATE_BOUND' in result.stderr
    assert 'mixed_science_dependency_roots' not in result.stderr
