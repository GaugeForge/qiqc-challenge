import json
import httpx
import pytest
from fastapi.testclient import TestClient
from qiqc.core import Engine, Settings
from qiqc.archive import reference_package
from qiqc import gateway

@pytest.fixture
def setup(tmp_path,monkeypatch):
    settings=Settings(data_dir=tmp_path,scan_docker=False)
    e=Engine(settings)
    a=e.create_account('gateway@example.test','Gateway-test-password','Gateway team',must_change=False)
    v=e.upload(a['id'],reference_package())
    e.confirm(a['id'],v['id'],'infra','simulated','gateway-test')
    lease=e.claim('gateway-worker'); token=e.mint_token(lease)
    monkeypatch.setattr(gateway,'credentials',lambda:{'OPENAI_API_KEY':'test-master-secret','OPENAI_BASE_URL':'https://provider.invalid/v1'})
    return e,token


def test_streaming_proxy_accounts_usage_without_exposing_key(setup,monkeypatch):
    e,token=setup; captured=[]
    def upstream(request):
        captured.append(request)
        event={'type':'response.completed','response':{'usage':{'input_tokens':30,'output_tokens':7}}}
        return httpx.Response(200,content=('data: '+json.dumps(event)+'\n\n').encode(),headers={'Content-Type':'text/event-stream'})
    original=httpx.AsyncClient
    monkeypatch.setattr(gateway.httpx,'AsyncClient',lambda **kw:original(transport=httpx.MockTransport(upstream),**kw))
    client=TestClient(gateway.factory(e.settings))
    for _ in range(4):
        r=client.post('/v1/responses',json={'model':e.settings.model,'stream':True},headers={'Authorization':'Bearer '+token})
        assert r.status_code==200 and 'test-master-secret' not in r.text
    assert len(captured)==4
    assert all(r.headers['authorization']=='Bearer test-master-secret' for r in captured)
    with e.db() as c:
        rows=c.execute('SELECT * FROM usage').fetchall()
        assert len(rows)==4 and all(r['input_tokens']==30 and r['output_tokens']==7 and r['status']==200 for r in rows)


def test_proxy_rejects_wrong_model_and_unapproved_endpoints(setup):
    e,token=setup; client=TestClient(gateway.factory(e.settings))
    assert client.post('/v1/responses',json={'model':'other'},headers={'Authorization':'Bearer '+token}).status_code==403
    assert client.post('/v1/responses',json={'model':e.settings.model}).status_code==403
    assert client.post('/v1/files',json={}).status_code==404


def test_chat_completions_protocol_is_forwarded_and_usage_accounted(setup, monkeypatch):
    e, token = setup
    captured = []

    def upstream(request):
        captured.append(request)
        return httpx.Response(200, json={
            'id': 'chatcmpl-demo',
            'choices': [{'message': {'role': 'assistant', 'content': 'ok'}, 'finish_reason': 'stop'}],
            'usage': {'prompt_tokens': 11, 'completion_tokens': 5, 'total_tokens': 16},
        })

    original = httpx.AsyncClient
    monkeypatch.setattr(gateway.httpx, 'AsyncClient', lambda **kw: original(transport=httpx.MockTransport(upstream), **kw))
    client = TestClient(gateway.factory(e.settings))
    response = client.post('/v1/chat/completions', json={'model': e.settings.model, 'messages': [{'role': 'user', 'content': 'hi'}]}, headers={'Authorization': 'Bearer ' + token})
    assert response.status_code == 200
    assert response.json()['choices'][0]['message']['content'] == 'ok'
    assert len(captured) == 1
    assert captured[0].url.path == '/v1/chat/completions'
    assert captured[0].headers['authorization'] == 'Bearer test-master-secret'
    with e.db() as conn:
        row = conn.execute('SELECT input_tokens,output_tokens,status FROM usage ORDER BY created DESC LIMIT 1').fetchone()
        assert tuple(row) == (11, 5, 200)
