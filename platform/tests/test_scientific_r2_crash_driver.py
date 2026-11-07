"""Offline fixtures only: never touch stage, systemd, model, E2B, or real R2."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import ssl
import subprocess
import sys
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'
sys.path.insert(0, str(SCRIPTS))
import scientific_r2_crash_check as driver


def test_saved_config_exact_restore_on_exception(tmp_path):
    config, backup = tmp_path / 'worker.env', tmp_path / 'backup'
    original = b'# private fixture\r\nR2_SECRET_ACCESS_KEY=do-not-print\n\n'
    config.write_bytes(original)
    config.chmod(0o600)
    before = config.stat()
    saved = driver.SavedConfig(config, backup)
    try:
        saved.replace(b'local-dummy-only\n')
        raise RuntimeError('fixture failure')
    except RuntimeError:
        saved.restore()
    after = config.stat()
    assert config.read_bytes() == original
    assert (before.st_mode, before.st_uid, before.st_gid) == (after.st_mode, after.st_uid, after.st_gid)
    assert not backup.exists()


@pytest.mark.parametrize('kind', ['symlink', 'permissive', 'oversize'])
def test_config_refuses_unsafe_sources(tmp_path, kind):
    config = tmp_path / 'worker.env'
    config.write_bytes(b'x')
    config.chmod(0o600)
    if kind == 'symlink':
        link = tmp_path / 'link'
        link.symlink_to(config)
        config = link
    elif kind == 'permissive':
        config.chmod(0o640)
    else:
        config.write_bytes(b'x' * 65537)
    with pytest.raises(driver.GuardError):
        driver.SavedConfig(config, tmp_path / 'backup')


def test_restore_does_not_clobber_other_operator_and_retains_backup(tmp_path):
    config = tmp_path / 'worker.env'
    config.write_bytes(b'original')
    config.chmod(0o600)
    backup = tmp_path / 'backup'
    saved = driver.SavedConfig(config, backup)
    saved.replace(b'ours')
    config.write_bytes(b'other-operator')
    with pytest.raises(driver.GuardError, match='other_operator'):
        saved.restore()
    assert config.read_bytes() == b'other-operator'
    assert backup.read_bytes() == b'original'


def fixture(tmp_path):
    body = b'complete inert archive fixture; not executable'
    (tmp_path / 'rollout.tar.gz').write_bytes(body)
    row = dict(id='run', batch_id='batch', team_id='team', owner='owner', attempt=1,
               state='uploading', ordinal=0, public=1)
    lease = {k: row[k] for k in ('batch_id', 'team_id', 'owner', 'attempt', 'ordinal', 'public')}
    lease.update(run_id='run', version_id='version', sha256='submission', mode='scientific-smoke',
                 kind='infra', attempt_id='attempt')
    pending = dict(lease=lease, provenance='scientific-smoke', reward=0, seconds='1.000000',
                   bundle_sha256=hashlib.sha256(body).hexdigest())
    (tmp_path / 'pending.json').write_text(json.dumps(pending))
    receipt = dict(run_id='run', attempt=1, mode='scientific-smoke',
                   resources={'main1': 'destroyed', 'qsim1': 'destroyed', 'verifier1': 'destroyed'})
    return row, pending, receipt


def test_pending_ready_uses_actual_normal_sdk_receipt_contract(tmp_path):
    row, p, receipt = fixture(tmp_path)
    # Normal controller resource events have no provider_checked; that field is
    # specific to the separate recovery CLI. Match real Go publication fence.
    result = driver.validate_pending(tmp_path, row, 'version', 'submission', receipt)
    assert result[0] == p and result[1] == p['bundle_sha256'] and len(result[3]) == 3


@pytest.mark.parametrize('change', ['state', 'attempt', 'owner', 'team', 'version', 'sha',
                                    'digest', 'receipt_run', 'resource_active', 'resource_count', 'reward'])
def test_no_kill_readiness_on_incomplete_or_other_archive(tmp_path, change):
    row, pending, receipt = fixture(tmp_path)
    if change == 'state': row['state'] = 'running'
    if change == 'attempt': row['attempt'] = 2
    if change == 'owner': pending['lease']['owner'] = 'foreign'
    if change == 'team': pending['lease']['team_id'] = 'foreign'
    if change == 'version': pending['lease']['version_id'] = 'foreign'
    if change == 'sha': pending['lease']['sha256'] = 'foreign'
    if change == 'digest': (tmp_path / 'rollout.tar.gz').write_bytes(b'truncated')
    if change == 'receipt_run': receipt['run_id'] = 'foreign'
    if change == 'resource_active': receipt['resources']['main1'] = 'created'
    if change == 'resource_count': receipt['resources'].pop('verifier1')
    if change == 'reward': pending['reward'] = True
    (tmp_path / 'pending.json').write_text(json.dumps(pending))
    with pytest.raises(driver.GuardError):
        driver.validate_pending(tmp_path, row, 'version', 'submission', receipt)


def test_archive_symlink_and_read_limit_refused(tmp_path):
    target, link = tmp_path / 'target', tmp_path / 'link'
    target.write_bytes(b'x' * 20)
    link.symlink_to(target)
    with pytest.raises(driver.GuardError): driver.sha_file(link)
    with pytest.raises(driver.GuardError): driver.sha_file(target, 10)


def empty_drill():
    d = object.__new__(driver.R2Drill)
    d.saved = d.fault = d.runtime = d.thread = None
    d.dropin_owned = d.configured = d.restore_restart_needed = False
    d.batch = d.team_id = d.idem = None
    d.report = {}
    return d


def test_failed_preflight_has_no_service_or_api_mutations(monkeypatch):
    d = empty_drill()
    monkeypatch.setattr(driver.base, 'command', lambda *_a, **_k: pytest.fail('unexpected service command'))
    d.api = lambda *_a, **_k: pytest.fail('unexpected API mutation')
    d.cleanup()
    assert d.report['finally_cleanup_completed']


def test_cleanup_uploading_waits_native_completion_without_cancel(monkeypatch):
    d = empty_drill()
    d.batch = 'owned-batch'
    d.args = SimpleNamespace(recovery_timeout=90)
    states = iter([{'state': 'uploading'}, {'state': 'completed'}])
    d.state = lambda: next(states)
    d.wait = lambda _s, predicate, _reason: predicate()
    d.api = lambda *_a, **_k: pytest.fail('resumable upload must not be cancelled')
    d.cleanup()
    assert d.report['finally_cleanup_completed']


def test_bystander_work_aborts_before_any_kill(monkeypatch):
    d = empty_drill()
    d.run = 'owned'
    d.state = lambda: {'state': 'running'}
    d.active = lambda: {'owned', 'foreign'}
    d.args = SimpleNamespace(phase_timeout=90)
    d.wait = lambda _s, predicate, _reason: predicate()
    monkeypatch.setattr(driver.subprocess, 'run', lambda *_a, **_k: pytest.fail('no SIGKILL allowed'))
    with pytest.raises(driver.GuardError, match='other_work'):
        d.crash()


def test_missing_archive_times_out_before_any_kill(monkeypatch):
    d = empty_drill()
    d.run = 'owned'
    d.state = lambda: {'state': 'running'}
    d.active = lambda: {'owned'}
    d.args = SimpleNamespace(phase_timeout=90)
    def wait(_seconds, predicate, reason):
        assert predicate() is False
        raise driver.GuardError(reason)
    d.wait = wait
    monkeypatch.setattr(driver.subprocess, 'run', lambda *_a, **_k: pytest.fail('no SIGKILL allowed'))
    with pytest.raises(driver.GuardError, match='no_kill'):
        d.crash()


def test_cli_requires_explicit_execute(tmp_path, monkeypatch):
    binary = tmp_path / 'reconcile'
    binary.write_text('inert')
    monkeypatch.setattr(sys, 'argv', ['driver', '--reconcile', str(binary)])
    with pytest.raises(driver.GuardError, match='explicit_execution'):
        driver.main()


@pytest.mark.parametrize('query', ['', '?x-id=PutObject'])
def test_real_local_TLS_fault_discards_body_and_never_records_credentials(tmp_path, capsys, query):
    import httpx
    cert, key = tmp_path / 'cert', tmp_path / 'key'
    subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-days', '1',
                    '-subj', '/CN=127.0.0.1', '-addext', 'subjectAltName=IP:127.0.0.1',
                    '-keyout', str(key), '-out', str(cert)],
                   check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    server = driver.FaultServer(cert, key)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    body = b'not-a-real-rollout'
    try:
        with httpx.Client(verify=ssl.create_default_context(cafile=str(cert)), trust_env=False) as client:
            r = client.put(f'https://127.0.0.1:{server.server_port}/drill-only/team/version/batch/run/1.tar.gz' + query,
                           content=body, headers={'Authorization': 'Bearer SHOULD-NOT-APPEAR'})
        assert r.status_code == 503
        assert server.matching('team/version/batch/run/1.tar.gz', hashlib.sha256(body).hexdigest(), len(body)) == 1
        assert server.matching('foreign', hashlib.sha256(body).hexdigest(), len(body)) == 0
        assert 'SHOULD-NOT-APPEAR' not in json.dumps(server.events)
        assert body.decode() not in json.dumps(server.events)
        assert capsys.readouterr() == ('', '')
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)


def test_restore_retries_failed_service_restart_even_after_config_restored(monkeypatch):
    d = empty_drill()
    d.restore_restart_needed = True
    attempts = []
    def restart():
        attempts.append(1)
        if len(attempts) == 1:
            raise driver.GuardError('fixture_restart_failure')
    d.restart_worker = restart
    monkeypatch.setattr(driver.base, 'command', lambda *_a, **_k: '')
    with pytest.raises(driver.GuardError, match='fixture_restart_failure'):
        d.restore_fault()
    assert d.restore_restart_needed
    d.restore_fault()
    assert len(attempts) == 2 and not d.restore_restart_needed
    assert d.report['original_R2_service_restored']


def test_guard_failure_finally_restores_and_reports_failed(tmp_path, monkeypatch):
    d = empty_drill()
    d.stage = 'fixture'
    d.team = d.admin = None
    d.run = d.attempt = None
    d.args = SimpleNamespace(output=tmp_path / 'report.json')
    d.report['status'] = 'failed'
    restored = []
    def setup():
        raise driver.GuardError('fixture_failure_before_cloud')
    d.setup = setup
    d.restore_fault = lambda: restored.append(True)
    assert d.execute() == 1
    assert restored == [True]
    result = json.loads(d.args.output.read_text())
    assert result['status'] == 'failed' and result['failure_reason'] == 'fixture_failure_before_cloud'
    assert result['finally_cleanup_completed']


def test_readback_request_matches_real_readonly_lookup_contract(tmp_path):
    import sqlite3
    sys.path.insert(0, str(SCRIPTS / 'science'))
    from verify_cloud_relay_evidence import lookup_run, EvidenceError
    path = tmp_path / 'platform.db'
    sha = 'b' * 64
    with sqlite3.connect(path) as db:
        db.executescript("""
          CREATE TABLE teams(id TEXT);
          CREATE TABLE versions(id TEXT,team_id TEXT,sha256 TEXT);
          CREATE TABLE batches(id TEXT,status TEXT,mode TEXT,version_id TEXT,team_id TEXT);
          CREATE TABLE runs(id TEXT,batch_id TEXT,state TEXT,object_key TEXT,object_sha TEXT,
                            reward INTEGER,attempt INTEGER,public INTEGER);
        """)
        db.execute('INSERT INTO teams VALUES(?)', ('team',))
        db.execute('INSERT INTO versions VALUES(?,?,?)', ('version', 'team', 'a' * 64))
        db.execute('INSERT INTO batches VALUES(?,?,?,?,?)',
                   ('batch', 'completed', 'scientific-smoke', 'version', 'team'))
        db.execute('INSERT INTO runs VALUES(?,?,?,?,?,?,?,?)',
                   ('run', 'batch', 'completed', 'team/version/batch/run/1.tar.gz', sha, 0, 1, 1))
    request = driver.readback_request('batch', sha)
    index = lookup_run(path, request)
    assert index['sha256'] == sha and index['key'] == 'team/version/batch/run/1.tar.gz'
    for invalid in (None, 'c' * 64):
        with pytest.raises(EvidenceError, match='browser_database_sha_mismatch'):
            lookup_run(path, driver.readback_request('batch', invalid))
    with sqlite3.connect(path) as db:
        assert db.execute('SELECT COUNT(*) FROM runs').fetchone()[0] == 1


@pytest.mark.parametrize('suffix', ['', '?x-id=PutObject'])
def test_fault_path_accepts_only_exact_SDK_put_contract(suffix):
    path = '/drill-only/team/version/batch/run/1.tar.gz'
    assert driver.canonical_put_path(path + suffix) == path


@pytest.mark.parametrize('target', [
    '/drill-only/team/version/batch/run/1.tar.gz?x-id=GetObject',
    '/drill-only/team/version/batch/run/1.tar.gz?x-id=PutObject&x-id=PutObject',
    '/drill-only/team/version/batch/run/1.tar.gz?x-id=PutObject&extra=1',
    '/drill-only/team/version/batch/run/1.tar.gz?x-id=PutObject&',
    '/drill-only/team/version/batch/run/1.tar.gz?x%2Did=PutObject',
    '/drill-only/team/version/batch/run/1.tar.gz#fragment',
    '/foreign/team/version/batch/run/1.tar.gz?x-id=PutObject',
    '/drill-only/team/version/batch/run/01.tar.gz',
    '/drill-only/team/version/batch/run/0.tar.gz',
    '/drill-only/team/version/batch/%72un/1.tar.gz',
    '/drill-only/team/version/../run/1.tar.gz',
    'https://example.com/drill-only/team/version/batch/run/1.tar.gz',
    '//example.com/drill-only/team/version/batch/run/1.tar.gz',
])
def test_fault_path_refuses_other_actions_queries_or_path_changes(target):
    with pytest.raises(driver.GuardError):
        driver.canonical_put_path(target)


def test_actual_go_aws_SDK_put_query_matches_local_TLS_503(tmp_path):
    """Pinned project SDK, dummy static credentials, loopback only, no cloud."""
    cert, key = tmp_path / 'cert.pem', tmp_path / 'key.pem'
    subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-days', '1',
                    '-subj', '/CN=127.0.0.1', '-addext', 'subjectAltName=IP:127.0.0.1',
                    '-keyout', str(key), '-out', str(cert)], check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    source = tmp_path / 'sdk_fixture.go'
    source.write_text(r'''package main
import (
 "bytes"
 "context"
 "errors"
 "net/http"
 "os"
 "time"
 "github.com/aws/aws-sdk-go-v2/aws"
 "github.com/aws/aws-sdk-go-v2/credentials"
 "github.com/aws/aws-sdk-go-v2/service/s3"
 smithyhttp "github.com/aws/smithy-go/transport/http"
)
func main() {
 body := []byte("inert SDK fixture archive bytes")
 client := s3.NewFromConfig(aws.Config{Region:"auto", RetryMaxAttempts:1,
   Credentials:credentials.NewStaticCredentialsProvider("dummy-local-key","dummy-local-secret",""),
   HTTPClient:&http.Client{Timeout:5*time.Second}}, func(o *s3.Options) {
     o.BaseEndpoint=aws.String(os.Args[1]); o.UsePathStyle=true
     o.RequestChecksumCalculation=aws.RequestChecksumCalculationWhenRequired
     o.ResponseChecksumValidation=aws.ResponseChecksumValidationWhenRequired
   })
 _, err := client.PutObject(context.Background(), &s3.PutObjectInput{
   Bucket:aws.String("drill-only"),Key:aws.String("team/version/batch/run/1.tar.gz"),
   Body:bytes.NewReader(body),ContentLength:aws.Int64(int64(len(body))),
   ContentType:aws.String("application/gzip"),IfNoneMatch:aws.String("*"),
   Metadata:map[string]string{"sha256":"fixture"},
 })
 var response *smithyhttp.ResponseError
 if !errors.As(err, &response) || response.HTTPStatusCode()!=503 { os.Exit(2) }
}
''')
    server = driver.FaultServer(cert, key)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        env = dict(os.environ, GOPROXY='off', GOSUMDB='off', SSL_CERT_FILE=str(cert), SSL_CERT_DIR=str(tmp_path))
        result = subprocess.run(['go', 'run', '-mod=readonly', str(source),
                                 f'https://127.0.0.1:{server.server_port}'],
                                cwd=SCRIPTS.parents[1] / 'platform-go', env=env,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60)
        # Failure text belongs only to inert fixture diagnostics; no real key is loaded.
        assert result.returncode == 0, result.stderr.decode()
        body = b'inert SDK fixture archive bytes'
        assert server.matching('team/version/batch/run/1.tar.gz', hashlib.sha256(body).hexdigest(), len(body)) == 1
        assert len(server.events) == 1
        assert server.events[0]['operation_query'] == 'x-id=PutObject'
        assert 'dummy-local-secret' not in json.dumps(server.events)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)
