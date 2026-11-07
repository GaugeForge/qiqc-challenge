import importlib.util
from pathlib import Path
import pytest

spec=importlib.util.spec_from_file_location('result_read',Path(__file__).parents[1]/'scripts/ordinary_result_browser_read.py')
import sys
sys.path.insert(0,str(Path(__file__).parents[1]/'scripts'))
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)

@pytest.fixture
def original():return dict(batch_id='b',submission_sha256='a'*64,agent_budget_seconds=180)

@pytest.fixture
def batch():return dict(id='b',status='completed',mode='scientific',bounded_scientific=True,leaderboard_eligible=False,sha256='a'*64,agent_budget_seconds=180,score=dict(total=1),public_runs=[dict(ordinal=0,label='公开任务 1')])

def test_corrected_label_preserves_result(batch,original):
    assert m.validate_result(batch,original)['label']=='公开任务 1'

@pytest.mark.parametrize('label',['Mock task','MOCK','模拟任务','',None])
def test_old_label_rejected(batch,original,label):
    batch['public_runs'][0]['label']=label
    with pytest.raises(ValueError):m.validate_result(batch,original)

@pytest.mark.parametrize('key,value',[('mode','simulated'),('status','testing'),('leaderboard_eligible',True),('sha256','changed'),('agent_budget_seconds',300)])
def test_same_batch_identity_required(batch,original,key,value):
    batch[key]=value
    with pytest.raises(ValueError):m.validate_result(batch,original)
