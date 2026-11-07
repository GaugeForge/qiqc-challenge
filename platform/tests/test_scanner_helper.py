"""Fixed privileged bridge tests; uploaded programs are never run."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest
from qiqc.archive import reference_package

HELPER_PATH = Path(__file__).resolve().parents[2] / 'platform-go/deploy/scanner_helper.py'
spec = importlib.util.spec_from_file_location('qiqc_scanner_helper_test', HELPER_PATH)
helper = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helper)


def config(tmp_path):
    quarantine=tmp_path/'quarantine';quarantine.mkdir()
    spool=tmp_path/'spool';spool.mkdir()
    parser=Path(__file__).resolve().parents[1]/'qiqc/archive.py'
    return SimpleNamespace(quarantine=quarantine, spool=spool, parser=parser,
                           api_uid=os.getuid(),image='sha256:'+'1'*64)


def test_helper_rejects_paths_and_symlinks(tmp_path):
    cfg=config(tmp_path)
    for name in ['../outside', '/tmp/file', 'incoming-1.zip;id', 'incoming-x.zip']:
        with pytest.raises(ValueError):helper.scan(cfg,name)
    (cfg.quarantine/'incoming-1.zip').symlink_to('/etc/passwd')
    with pytest.raises(OSError):helper.scan(cfg,'incoming-1.zip')


@pytest.mark.parametrize('failure',['crash','timeout','bad-json','success-nonzero'])
def test_helper_failure_cleans_spool_and_fixed_container(tmp_path,monkeypatch,failure):
    cfg=config(tmp_path);(cfg.quarantine/'incoming-1.zip').write_bytes(reference_package())
    calls=[]
    def run(cmd,**kwargs):
        calls.append(cmd)
        if cmd[1]=='rm': return SimpleNamespace(returncode=0)
        if failure=='timeout':raise subprocess.TimeoutExpired(cmd,40)
        if failure=='bad-json':return SimpleNamespace(stdout=b'not json',returncode=0)
        result={'ok':True,'report':{'ruleset_version':'qiqc-archive/2'}}
        if failure=='crash': result={'ok':False,'error_kind':'unavailable'}
        return SimpleNamespace(stdout=json.dumps(result).encode(),returncode=125)
    monkeypatch.setattr(helper.subprocess,'run',run)
    with pytest.raises((ValueError,subprocess.TimeoutExpired)):
        helper.scan(cfg,'incoming-1.zip')
    assert list(cfg.spool.iterdir())==[]
    assert calls[-1][1:3]==['rm','-f']
    cmd=calls[0]
    for required in ['--network','none','--read-only','--cap-drop','ALL','--pull=never','65534:65534']:
        assert required in cmd
    assert cfg.image in cmd


@pytest.mark.skipif(os.environ.get('QIQC_REAL_SCANNER_TEST')!='1',reason='explicit local Docker integration')
def test_real_fixed_docker_parser_provenance_and_crc_rejection(tmp_path):
    cfg=config(tmp_path)
    cfg.image=subprocess.check_output(['docker','image','inspect','--format','{{.Id}}','qiqcbench-agent:latest'],text=True).strip()
    payload=reference_package();(cfg.quarantine/'incoming-1.zip').write_bytes(payload)
    result=helper.scan(cfg,'incoming-1.zip')
    assert result['ok'] is True
    assert result['source_sha256']==hashlib.sha256(payload).hexdigest()
    assert result['report']['parser_sha256']==hashlib.sha256(cfg.parser.read_bytes()).hexdigest()
    assert result['report']['scanner_image']==cfg.image
    assert result['report']['ruleset_version']=='qiqc-archive/2'
    (cfg.quarantine/'incoming-2.zip').write_bytes(b'not a zip')
    result=helper.scan(cfg,'incoming-2.zip')
    assert result['ok'] is False
    assert result['error_kind']=='invalid_package'
    assert list(cfg.spool.iterdir())==[]


def test_busy_transport_reads_valid_request_and_closes_without_reset(tmp_path):
    """Match Go io.ReadAll: a response followed by ECONNRESET is a failure."""
    import socket
    import threading
    with helper.Server(str(tmp_path/'scanner.sock'),helper.Handler) as server:
        server.config=SimpleNamespace(api_uid=os.getuid())
        server.slots=threading.BoundedSemaphore(2)
        server.slots.acquire();server.slots.acquire()
        thread=threading.Thread(target=server.serve_forever,kwargs={'poll_interval':0.01},daemon=True);thread.start()
        try:
            for _ in range(20):
                with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as client:
                    client.settimeout(3);client.connect(server.server_address)
                    client.sendall(b'{"filename":"incoming-123.zip"}\n')
                    chunks=[]
                    while True:
                        chunk=client.recv(4096)
                        if not chunk:break
                        chunks.append(chunk)
                    result=json.loads(b''.join(chunks))
                    assert result['ok'] is False and result['error_kind']=='unavailable'
                    assert 'busy' in result['error']
        finally:
            server.slots.release();server.slots.release();server.shutdown();thread.join(3)


def test_request_absolute_deadline_blocks_slow_trickle(monkeypatch):
    import socket
    import threading
    import time
    monkeypatch.setattr(helper,'REQUEST_READ_SECONDS',0.12)
    left,right=socket.socketpair();stop=threading.Event()
    def trickle():
        while not stop.wait(0.02):
            try:right.sendall(b' ')
            except OSError:break
    thread=threading.Thread(target=trickle,daemon=True);thread.start();started=time.monotonic()
    try:
        with pytest.raises((TimeoutError,socket.timeout)):helper.read_request(left)
        assert time.monotonic()-started<0.5
    finally:stop.set();left.close();right.close();thread.join(1)


def test_request_frame_size_is_bounded():
    import socket
    left,right=socket.socketpair()
    try:
        right.sendall(b'x'*1025)
        with pytest.raises(ValueError):helper.read_request(left)
    finally:left.close();right.close()


def test_parallel_busy_requests_bound_transport_threads_without_scanning(tmp_path,monkeypatch):
    import socket
    import threading
    import concurrent.futures
    import time
    counts={'active':0,'maximum':0};lock=threading.Lock();original=helper.read_request
    def measured(connection):
        with lock:
            counts['active']+=1;counts['maximum']=max(counts['maximum'],counts['active'])
        try:
            time.sleep(0.03)
            return original(connection)
        finally:
            with lock:counts['active']-=1
    monkeypatch.setattr(helper,'read_request',measured)
    monkeypatch.setattr(helper,'scan',lambda *_:pytest.fail('busy connection must not scan'))
    with helper.Server(str(tmp_path/'scanner.sock'),helper.Handler) as server:
        server.config=SimpleNamespace(api_uid=os.getuid());server.slots=threading.BoundedSemaphore(2)
        server.slots.acquire();server.slots.acquire()
        thread=threading.Thread(target=server.serve_forever,kwargs={'poll_interval':0.01},daemon=True);thread.start()
        def client_request(_):
            with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as client:
                client.settimeout(3);client.connect(server.server_address);client.sendall(b'{"filename":"incoming-1.zip"}\n')
                result=[]
                while True:
                    data=client.recv(4096)
                    if not data:break
                    result.append(data)
                return json.loads(b''.join(result))
        try:
            with concurrent.futures.ThreadPoolExecutor(max_workers=12) as pool:
                values=list(pool.map(client_request,range(48)))
            assert all(v['error_kind']=='unavailable' and 'busy' in v['error'] for v in values)
            assert 2<counts['maximum']<=helper.MAX_TRANSPORT_HANDLERS and counts['active']==0
        finally:server.slots.release();server.slots.release();server.shutdown();thread.join(3)
