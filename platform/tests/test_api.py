import pytest
from fastapi.testclient import TestClient
from qiqc.app import create_app
from qiqc.core import Settings

@pytest.fixture
def client(tmp_path):
    app=create_app(Settings(data_dir=tmp_path,scan_docker=False))
    e=app.state.engine
    e.create_account('captain@example.test','Initial-password-123','Team',must_change=True)
    return TestClient(app)


def test_first_login_and_password_rotation(client):
    r=client.post('/api/login',json={'email':'captain@example.test','password':'Initial-password-123'})
    assert r.status_code==200
    assert 'httponly' in r.headers['set-cookie'].lower()
    assert client.get('/api/versions').status_code==403
    assert client.post('/api/password',json={'password':'Replacement-password-123'},headers={'X-QIQC-Request':'1'}).status_code==200
    assert client.get('/api/versions').status_code==200
    assert client.post('/api/logout',headers={'X-QIQC-Request':'1'}).status_code==200
    assert client.get('/api/me').status_code==401


def test_cross_origin_mutation_and_anonymous_admin_rejected(client):
    assert client.get('/api/admin').status_code==401
    assert client.post('/api/login',json={'email':'captain@example.test','password':'Initial-password-123'},headers={'Origin':'https://evil.example'}).status_code==403
    assert client.post('/api/login',json={'email':'captain@example.test','password':'wrong'}).status_code==401


def test_public_payload_does_not_leak_private_data(client):
    for path in ['/api/config','/api/leaderboard']:
        r=client.get(path)
        assert r.status_code==200
        assert 'captain@example.test' not in r.text
        assert 'password' not in r.text.lower()
    assert client.get('/.env').status_code==404
    assert client.get('/platform.db').status_code==404


def test_login_rate_limit(client):
    statuses=[client.post('/api/login',json={'email':'captain@example.test','password':'wrong'}).status_code for _ in range(9)]
    assert 429 in statuses
