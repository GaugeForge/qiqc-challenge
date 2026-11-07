"""Controller transport fault tests; no provider, Docker, or scientific execution."""
import base64
import io
import json
from types import SimpleNamespace

import pytest
from e2b.exceptions import FileNotFoundException
import native_model_mailbox as mailbox
from bounded_files import RemoteFileError, read_remote_bytes_retry


class Files:
    def __init__(self, *, rename_failures=(), read_failures=(), entry=None):
        self.payload=json.dumps({'id':'a'*32,'path':'/v1/responses',
            'body':{'model':mailbox.MODEL,'input':'fixture'}}).encode()
        self.rename_failures=list(rename_failures)
        self.read_failures=list(read_failures)
        self.entry=entry
        self.writes=[];self.renames=[];self.reads=0;self.stats=0

    def get_info(self, path, **kwargs):
        self.stats+=1
        return self.entry or SimpleNamespace(type='file',symlink_target=None,size=len(self.payload))

    def read(self, path, **kwargs):
        self.reads+=1
        if self.read_failures:raise self.read_failures.pop(0)
        return io.BytesIO(self.payload)

    def write(self, path, data, **kwargs):
        self.writes.append((path,data))

    def rename(self, source, dest, **kwargs):
        self.renames.append((source,dest))
        if self.rename_failures:raise self.rename_failures.pop(0)


def test_rename_transient_repeats_identical_delivery_not_provider(monkeypatch):
    files=Files(rename_failures=[TimeoutError(),TimeoutError()])
    calls=[];events=[];seen={};delays=[]
    actual=mailbox.deliver_response
    monkeypatch.setattr(mailbox,'deliver_response',lambda fs,data:actual(fs,data,delay=delays.append))
    response={'status':200,'type':'application/json','body':b'{"object":"response","status":"completed","output":[]}'}
    def provider(endpoint,body,token):calls.append((endpoint,body,token));return response
    assert mailbox.poll_model_mailbox(SimpleNamespace(files=files),seen,events,provider,'fixture-token')
    assert len(calls)==len(events)==1
    assert len(files.writes)==len(files.renames)==3
    assert len({data for path,data in files.writes})==1
    delivered=json.loads(files.writes[-1][1])
    assert base64.b64decode(delivered['body'])==response['body']
    assert delays==[0.25,0.5]
    assert not mailbox.poll_model_mailbox(SimpleNamespace(files=files),seen,events,provider,'fixture-token')
    assert len(calls)==1


@pytest.mark.parametrize('error',[PermissionError(),ValueError(),FileNotFoundException('missing')])
def test_permanent_delivery_failure_not_retried(error):
    files=Files(rename_failures=[error]);delays=[]
    with pytest.raises(mailbox.ProbeError,match='mailbox write failed'):
        mailbox.deliver_response(files,'same response',delay=delays.append)
    assert len(files.writes)==len(files.renames)==1
    assert not delays


def test_delivery_retry_exhaustion_is_bounded_and_fails():
    files=Files(rename_failures=[TimeoutError() for _ in range(4)])
    with pytest.raises(mailbox.ProbeError):mailbox.deliver_response(files,'response',delay=lambda _:None)
    assert len(files.writes)==len(files.renames)==3


@pytest.mark.parametrize('missing_ok',[True,False])
def test_stat_to_stream_missing_race_is_not_transfer_retry(missing_ok):
    files=Files(read_failures=[FileNotFoundException('removed after stat')]);delays=[]
    if missing_ok:
        assert read_remote_bytes_retry(files,'mailbox',1024,missing_ok=True,delay=delays.append) is None
    else:
        with pytest.raises(RemoteFileError,match='Remote file missing'):
            read_remote_bytes_retry(files,'mailbox',1024,delay=delays.append)
    assert files.reads==files.stats==1 and not delays


def test_transient_read_retries_bounded_same_file():
    files=Files(read_failures=[TimeoutError(),TimeoutError()]);delays=[]
    assert read_remote_bytes_retry(files,'mailbox',1024,delay=delays.append)==files.payload
    assert files.reads==files.stats==3 and delays==[0.25,0.5]


@pytest.mark.parametrize('entry',[
    SimpleNamespace(type='file',symlink_target='/private',size=1),
    SimpleNamespace(type='directory',symlink_target=None,size=1),
    SimpleNamespace(type='file',symlink_target=None,size=2048),
])
def test_read_policy_refusal_never_retries(entry):
    files=Files(entry=entry);delays=[]
    with pytest.raises(RemoteFileError):read_remote_bytes_retry(files,'mailbox',1024,delay=delays.append)
    assert files.stats==1 and files.reads==0 and not delays


def test_archive_total_deadline_does_not_reset_on_each_chunk():
    import contextlib
    from types import SimpleNamespace
    from bounded_files import RemoteFileError, read_remote_bytes
    now = [0]
    def chunks():
        for moment in (2, 9, 11):
            now[0] = moment
            yield b'a'
    class Files:
        def get_info(self, *args, **kwargs):
            now[0] = 1
            return SimpleNamespace(type='file', size=3, symlink_target=None)
        def read(self, *args, **kwargs):
            assert kwargs['request_timeout'] == 9
            return contextlib.nullcontext(chunks())
    with pytest.raises(RemoteFileError, match='total deadline exceeded'):
        read_remote_bytes(Files(), '/host/archive', 64, request_timeout=10,
                          deadline=10, clock=lambda: now[0])


@pytest.mark.parametrize('model',[mailbox.MODEL,mailbox.DIAGNOSTIC_MODEL])
def test_explicit_model_is_forwarded_unmodified_and_recorded(model):
    files=Files();request=json.loads(files.payload);request['body']['model']=model
    request['body'].update(max_output_tokens=1234,reasoning={'effort':'low'})
    files.payload=json.dumps(request).encode();calls=[];events=[]
    def provider(endpoint,body,token):
        calls.append(body.copy())
        return {'status':200,'type':'application/json','body':b'{"object":"response","status":"completed","output":[]}'}
    assert mailbox.poll_model_mailbox(SimpleNamespace(files=files),{},events,provider,'fixture',expected_model=model)
    assert calls==[request['body']]
    assert events[0]['model_id']==model
    assert events[0]['adapter_request_sha256']==events[0]['forwarded_request_sha256']


@pytest.mark.parametrize('change',[
    lambda req:req['body'].update(model=mailbox.MODEL),
    lambda req:req['body'].update(models=[mailbox.DIAGNOSTIC_MODEL,mailbox.MODEL]),
    lambda req:req['body'].update(fallback_models=[mailbox.MODEL]),
    lambda req:req['body'].update(route='fallback'),
])
def test_diagnostic_model_mismatch_or_fallback_never_calls_provider(change):
    files=Files();request=json.loads(files.payload);request['body']['model']=mailbox.DIAGNOSTIC_MODEL
    change(request);files.payload=json.dumps(request).encode()
    def forbidden(*args):pytest.fail('mismatched model reached provider')
    with pytest.raises(mailbox.ProbeError,match='policy'):
        mailbox.poll_model_mailbox(SimpleNamespace(files=files),{},[],forbidden,'fixture',expected_model=mailbox.DIAGNOSTIC_MODEL)
    assert not files.writes


@pytest.mark.parametrize('endpoint',['responses','chat/completions'])
def test_mailbox_approved_provider_profile_permits_only_bound_model_and_endpoint(endpoint):
    from native_provider_profile import profile_identity
    full={'schema':'qiqc-provider-profile/v1','id':'approved','model':'approved-provider/science',
        'base_url':'https://provider.example/v1','endpoints':[endpoint],'key_file':'/private/key',
        'quota_group':'approved','requests_per_minute':20,'requests_per_day':50,'allow_paid_fallback':False,
        'parameter_policy':'passthrough/v1','authentication':'bearer','billing':'paid'}
    profile=profile_identity(full);files=Files();request=json.loads(files.payload)
    request.update(path='/v1/'+endpoint);request['body']['model']=full['model'];files.payload=json.dumps(request).encode()
    events=[];calls=[]
    def provider(path,body,token):
        calls.append(path)
        return {'status':200,'type':'application/json','body':b'{"object":"response","status":"completed","output":[]}'}
    assert mailbox.poll_model_mailbox(SimpleNamespace(files=files),{},events,provider,'fixture',
        expected_model=full['model'],provider_profile=profile)
    assert calls==['/v1/'+endpoint] and events[0]['provider_profile']==profile
    assert 'key_file' not in json.dumps(events) and 'provider.example' not in json.dumps(events)
    request['path']='/v1/'+('responses' if endpoint=='chat/completions' else 'chat/completions')
    files.payload=json.dumps(request).encode()
    with pytest.raises(mailbox.ProbeError,match='policy'):
        mailbox.poll_model_mailbox(SimpleNamespace(files=files),{},[],provider,'fixture',
            expected_model=full['model'],provider_profile=profile)
    assert len(calls)==1


def test_arbitrary_model_without_trusted_profile_is_rejected_before_file_read():
    files=Files()
    with pytest.raises(mailbox.ProbeError,match='policy'):
        mailbox.poll_model_mailbox(SimpleNamespace(files=files),{},[],lambda *args:None,'fixture',expected_model='unapproved/model')
    assert files.stats==0
