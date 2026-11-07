import io
import json
import stat
import zipfile
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from pathlib import Path

import pytest

from qiqc.core import Engine, Problem, Settings, mean_seconds, standings
from qiqc.archive import inspect_archive, reference_package
from qiqc.storage import ObjectStore


@pytest.fixture
def env(tmp_path):
    e = Engine(Settings(data_dir=tmp_path, scan_docker=False))
    a = e.create_account('alpha@example.test', 'Test-password-123', 'Alpha', must_change=False)
    b = e.create_account('beta@example.test', 'Test-password-456', 'Beta', must_change=False)
    admin = e.create_account('admin@example.test', 'Test-admin-123', 'Organizer', role='admin', must_change=False)
    return e, a, b, admin


def upload(e, a):
    return e.upload(a['id'], reference_package())


def batch(e, a, kind='full', key='first', mode='simulated'):
    v = upload(e, a)
    return e.confirm(a['id'], v['id'], kind, mode, key)


def finish(e, lease, reward=1, seconds='1.0000005'):
    e.complete_task(lease, reward, seconds, 'Safe public trace', provenance='simulated')


def drain(e):
    while lease := e.claim('test-worker'):
        finish(e, lease)


def test_quota_consumed_at_confirmation_and_idempotent_concurrently(env):
    e, a, _, _ = env
    v = upload(e, a)
    with ThreadPoolExecutor(max_workers=12) as pool:
        results = list(pool.map(lambda _: e.confirm(a['id'], v['id'], 'full', 'simulated', 'same-key'), range(20)))
    assert len({r['id'] for r in results}) == 1
    assert e.me(a['id'])['quotas']['full'] == 0
    assert len(e.ledger(a['id'])) == 1


def test_idempotency_cannot_reuse_key_for_different_input(env):
    e, a, _, _ = env
    b = batch(e, a)
    with pytest.raises(Problem, match='幂等'):
        e.confirm(a['id'], b['version_id'], 'public', 'simulated', 'first')


def test_all_batch_types_are_mutually_exclusive(env):
    e, a, _, _ = env
    b = batch(e, a, 'public')
    for kind in ['public', 'full', 'infra']:
        with pytest.raises(Problem, match='批次'):
            e.confirm(a['id'], b['version_id'], kind, 'simulated', 'new-'+kind)
    assert e.me(a['id'])['quotas'] == {'full': 1, 'public': 2, 'infra': 10}


def test_cancel_queued_does_not_refund_and_releases_team(env):
    e, a, _, _ = env
    b = batch(e, a)
    e.cancel(a['id'], b['id'])
    assert e.me(a['id'])['quotas']['full'] == 0
    assert e.get_batch(a['id'], b['id'])['status'] == 'cancelled'
    with pytest.raises(Problem, match='次数已用完'):
        batch(e, a, key='second')
    assert e.me(a['id'])['quotas']['full'] == 0


def test_cancel_running_keeps_active_lock_until_task_finishes(env):
    e, a, _, _ = env
    b = batch(e, a)
    lease = e.claim('worker')
    e.cancel(a['id'], b['id'])
    assert e.get_batch(a['id'], b['id'])['status'] == 'cancelling'
    with pytest.raises(Problem):
        batch(e, a, key='second')
    finish(e, lease)
    assert e.get_batch(a['id'], b['id'])['status'] == 'cancelled'
    assert e.get_batch(a['id'], b['id'])['score'] is None


def test_only_complete_batches_release_score_and_public_traces(env):
    e, a, b, _ = env
    req = batch(e, a)
    lease = e.claim('worker')
    finish(e, lease)
    assert e.get_batch(a['id'], req['id'])['score'] is None
    with pytest.raises(Problem):
        e.trace(a['id'], req['id'], 0)
    drain(e)
    r = e.get_batch(a['id'], req['id'])
    assert r['score']['passed'] == 39
    assert len(r['public_runs']) == 0
    with pytest.raises(Problem):
        e.trace(a['id'], req['id'], 0)
    public = batch(e, a, 'public', key='public-trace')
    drain(e)
    public_view = e.get_batch(a['id'], public['id'])
    assert len(public_view['public_runs']) == 5
    assert 'Safe public trace' in e.trace(a['id'], public['id'], 0)['text']
    with pytest.raises(Problem):
        e.trace(a['id'], public['id'], 5)
    with pytest.raises(Problem):
        e.trace(b['id'], req['id'], 0)
    with pytest.raises(Problem):
        e.get_batch(b['id'], req['id'])


def test_one_public_batch_never_enters_leaderboard(env):
    e, a, _, _ = env
    batch(e, a, 'public')
    drain(e)
    assert e.leaderboard()['teams'] == []


def test_restart_preserves_requests_and_ledger(env):
    e, a, _, _ = env
    req = batch(e, a)
    restarted = Engine(e.settings)
    assert restarted.get_batch(a['id'], req['id'])['status'] == 'queued'
    assert restarted.me(a['id'])['quotas']['full'] == 0
    assert restarted.claim('new-worker')['batch_id'] == req['id']


def test_duplicate_stale_completion_cannot_overwrite_result(env):
    e, a, _, _ = env
    batch(e, a, 'infra')
    lease = e.claim('worker')
    finish(e, lease, 1, '8')
    with pytest.raises(Problem):
        finish(e, lease, 0, '0.1')
    assert e.get_batch(a['id'], lease['batch_id'])['score']['passed'] == 1


def test_platform_retry_keeps_quota_and_increments_attempt(env):
    e, a, _, admin = env
    req = batch(e, a, 'infra')
    first = e.claim('worker')
    e.fail_task(first, 'platform', 'MODEL_UNAVAILABLE')
    e.retry_batch(admin['id'], req['id'], 'Test operator', 'Provider restored')
    second = e.claim('worker')
    assert first['run_id'] == second['run_id']
    assert second['attempt'] == first['attempt'] + 1
    assert e.me(a['id'])['quotas']['infra'] == 9
    with pytest.raises(Problem):
        finish(e, first)
    finish(e, second)


def test_modified_reviewed_bytes_cannot_execute(env):
    e, a, _, _ = env
    v = upload(e, a)
    (e.settings.data_dir/'uploads'/f"{v['sha256']}.zip").write_bytes(b'changed')
    with pytest.raises(Problem, match='校验'):
        e.confirm(a['id'], v['id'], 'full', 'simulated', 'bad')
    assert e.me(a['id'])['quotas']['full'] == 1


def test_unknown_package_requires_manual_review_and_no_real_execution(env):
    e, a, _, admin = env
    data = make_zip({'harness.json': json.dumps({'schema':'qiqc-harness/v1-demo','name':'Own harness','entrypoint':'run.sh'}), 'run.sh':'echo hello'})
    v = e.upload(a['id'], data)
    assert v['status'] == 'pending_review'
    with pytest.raises(Problem):
        e.confirm(a['id'], v['id'], 'infra', 'simulated', 'before-review')
    e.review(admin['id'], v['id'], 'approve', 'Reviewer', 'Reviewed source for local simulation')
    with pytest.raises(Problem, match='参考'):
        e.confirm(a['id'], v['id'], 'infra', 'codex', 'arbitrary-code')
    assert e.me(a['id'])['quotas']['infra'] == 10


def test_admin_actions_require_reason_and_are_audited(env):
    e, a, _, admin = env
    with pytest.raises(Problem):
        e.adjust_quota(admin['id'], a['team_id'], 'full', 1, 'Operator', '')
    e.adjust_quota(admin['id'], a['team_id'], 'full', 1, 'Operator', 'Manual allowance decision')
    assert e.me(a['id'])['quotas']['full'] == 2
    audit = e.admin_snapshot(admin['id'])['audit']
    assert audit[0]['reason'] == 'Manual allowance decision'
    assert audit[0]['actor'] == 'Operator'
    with pytest.raises(Problem):
        e.admin_snapshot(a['id'])


def test_pause_does_not_discard_queue(env):
    e, a, _, admin = env
    batch(e, a)
    e.set_pause(admin['id'], True, 'Operator', 'Storage maintenance')
    assert e.claim('worker') is None
    e.set_pause(admin['id'], False, 'Operator', 'Storage restored')
    assert e.claim('worker') is not None


def test_scheduler_rotates_teams_and_enforces_global_slots(env):
    e, a, b, _ = env
    e.settings.slots = 2
    one, two = batch(e, a), batch(e, b)
    x, y = e.claim('worker'), e.claim('worker')
    assert {x['batch_id'], y['batch_id']} == {one['id'], two['id']}
    assert e.claim('worker') is None


def test_one_appeal_per_team(env):
    e, a, _, _ = env
    e.appeal(a['id'], 'Please review my evaluation.')
    with pytest.raises(Problem):
        e.appeal(a['id'], 'A second appeal.')


def test_mean_uses_decimal_rounding_once():
    assert mean_seconds(['0.0000004', '0.0000005']) == '0.000000'
    assert mean_seconds(['1.0000005']*49) == '1.000001'
    with pytest.raises(ValueError):
        mean_seconds(['NaN'])


def test_best_whole_batch_ties_and_challenger_accepted_time():
    rows = [
        dict(id='a1',team_id='a',team='Alpha',passed=40,mean='10.000000',accepted_at=2,sha256='a'),
        dict(id='a2',team_id='a',team='Alpha',passed=41,mean='20.000000',accepted_at=4,sha256='b'),
        dict(id='b1',team_id='b',team='Beta',passed=41,mean='20.000000',accepted_at=1,sha256='c'),
    ]
    result = standings(rows, {'passed':39,'mean':'10.000000'})
    assert {r['id'] for r in result} == {'a2','b1'}
    assert all(r['tie'] for r in result)
    assert [r['team_id'] for r in result if r['challenger']] == ['b']


def make_zip(files):
    f=io.BytesIO()
    with zipfile.ZipFile(f,'w',zipfile.ZIP_DEFLATED) as z:
        for name,content in files.items(): z.writestr(name,content)
    return f.getvalue()


@pytest.mark.parametrize('name',['../escape','/absolute','a/../../b','C:/file','a\\b'])
def test_reject_unsafe_archive_paths(name):
    with pytest.raises(ValueError): inspect_archive(make_zip({name:'bad'}))


def test_reject_duplicate_symlink_bomb_and_invalid_manifest():
    f=io.BytesIO()
    with zipfile.ZipFile(f,'w') as z:
        zi=zipfile.ZipInfo('link'); zi.create_system=3; zi.external_attr=(stat.S_IFLNK|0o777)<<16
        z.writestr(zi,'/etc/passwd')
    with pytest.raises(ValueError): inspect_archive(f.getvalue())
    f=io.BytesIO()
    with zipfile.ZipFile(f,'w') as z:
        z.writestr('same','one'); z.writestr('same','two')
    with pytest.raises(ValueError): inspect_archive(f.getvalue())
    with pytest.raises(ValueError): inspect_archive(make_zip({'large':'a'*1_000_000}))
    with pytest.raises(ValueError): inspect_archive(make_zip({'harness.json':'{}'}))
    assert inspect_archive(reference_package())['manifest']['schema'] == 'qiqc-harness/v1-demo'


def test_object_store_integrity_and_no_overwrite(tmp_path):
    store=ObjectStore(tmp_path/'objects')
    source=tmp_path/'source'; source.write_bytes(b'original')
    first=store.put_file('team/run/attempt.tar.gz',source)
    assert store.put_file('team/run/attempt.tar.gz',source) == first
    assert source.exists()
    source.write_bytes(b'different')
    with pytest.raises(ValueError): store.put_file('team/run/attempt.tar.gz',source)
    with pytest.raises(ValueError): store.put_file('../outside',source)


def test_backup_restores_accepted_request(env,tmp_path):
    e,a,_,_=env
    req=batch(e,a)
    backup=e.backup(tmp_path/'backup')
    restored_dir=tmp_path/'restored'
    Engine.restore(backup,restored_dir)
    restored=Engine(Settings(data_dir=restored_dir,scan_docker=False))
    assert restored.get_batch(a['id'],req['id'])['status']=='queued'
    assert restored.me(a['id'])['quotas']['full']==0
    assert restored.claim('worker') is not None


def test_storage_outage_retries_upload_without_rerunning_or_charging(env,monkeypatch):
    from qiqc.worker import resume_uploads
    e,a,_,_=env
    b=batch(e,a,'infra'); lease=e.claim('worker')
    original=e.store.put_file
    def outage(*args): raise OSError('Storage unavailable')
    monkeypatch.setattr(e.store,'put_file',outage)
    with pytest.raises(OSError): finish(e,lease)
    cache=e.root/'cache'/lease['run_id']/str(lease['attempt'])
    assert (cache/'rollout.tar.gz').exists()
    assert e.get_batch(a['id'],b['id'])['score'] is None
    monkeypatch.setattr(e.store,'put_file',original)
    resume_uploads(e)
    assert e.get_batch(a['id'],b['id'])['status']=='completed'
    assert e.me(a['id'])['quotas']['infra']==9
    assert not cache.exists()
    assert len(e.ledger(a['id']))==1


def test_expired_simulated_lease_is_recovered_and_old_completion_rejected(env):
    e,a,_,_=env
    batch(e,a,'infra'); old=e.claim('dead-worker')
    e.clock=lambda:time_now+120
    time_now=__import__('time').time()
    assert e.recover()==1
    new=e.claim('new-worker')
    assert new['attempt']==2
    with pytest.raises(Problem): finish(e,old)
    finish(e,new)


def test_cancelled_expired_lease_must_not_deadlock_team(env):
    e,a,_,_=env
    b=batch(e,a,'infra'); lease=e.claim('dead-worker'); e.cancel(a['id'],b['id'])
    now=__import__('time').time(); e.clock=lambda:now+120
    e.recover()
    assert e.get_batch(a['id'],b['id'])['status']=='cancelled'


def test_model_capability_is_scoped_and_revoked_at_completion(env):
    e,a,_,_=env
    batch(e,a,'infra'); lease=e.claim('worker'); token=e.mint_token(lease)
    grant=e.authorize_model(token,e.settings.model)
    with pytest.raises(Problem): e.authorize_model(token,'unapproved-model')
    with pytest.raises(Problem): e.authorize_model('wrong',e.settings.model)
    e.record_usage(grant,200,{'input_tokens':100,'output_tokens':20})
    with e.db() as c:
        row=c.execute('SELECT * FROM usage').fetchone()
        assert row['run_id']==lease['run_id'] and row['amount'] is None
    finish(e,lease)
    with pytest.raises(Problem): e.authorize_model(token,e.settings.model)


def test_submission_deadline_and_appeal_deadline_in_utc(env):
    from datetime import datetime,timezone
    from qiqc.core import START,END,APPEAL_END
    assert datetime.fromtimestamp(START,timezone.utc).isoformat()=='2026-11-08T00:00:00+00:00'
    assert datetime.fromtimestamp(END,timezone.utc).isoformat()=='2026-11-10T13:00:00+00:00'
    e,a,_,_=env; v=upload(e,a); e.settings.demo_mode=False
    e.clock=lambda:END
    with pytest.raises(Problem): e.confirm(a['id'],v['id'],'full','simulated','after-end')
    assert e.me(a['id'])['quotas']['full']==1
    e.clock=lambda:END-1
    accepted=e.confirm(a['id'],v['id'],'infra','simulated','before-end')
    e.clock=lambda:END+100
    drain(e)
    assert e.get_batch(a['id'],accepted['id'])['status']=='completed'
    e.clock=lambda:APPEAL_END+1
    with pytest.raises(Problem): e.appeal(a['id'],'Late appeal text here')


def test_bad_manifest_entrypoint_case_is_rejected():
    data=make_zip({'harness.json':json.dumps({'schema':'qiqc-harness/v1-demo','name':'Case mismatch','entrypoint':'RUN.sh'}),'run.sh':'echo test'})
    with pytest.raises(ValueError): inspect_archive(data)


def test_pricing_is_audited_estimate_and_reprices_unknown_usage(env):
    e,a,_,admin=env
    batch(e,a,'infra'); lease=e.claim('worker'); grant=e.authorize_model(e.mint_token(lease),e.settings.model)
    e.record_usage(grant,200,{'input_tokens':1000,'output_tokens':100,'input_tokens_details':{'cached_tokens':200}})
    e.set_pricing(admin['id'],{'input_per_million':2,'cached_per_million':1,'output_per_million':10},'Operator','Demo price estimate for billing workflow')
    summary=e.admin_snapshot(admin['id'])
    assert summary['usage']['known_amount']==pytest.approx(.0028)
    assert summary['usage']['unpriced']==0
    assert summary['usage_rows'][0]['price_version']
    assert summary['audit'][0]['action']=='pricing_update'


def test_rejected_archive_does_not_accumulate_unbounded_private_files(env):
    e,a,_,_=env
    before=set((e.root/'uploads').glob('*'))
    with pytest.raises(Problem):e.upload(a['id'],b'not an archive')
    assert set((e.root/'uploads').glob('*'))==before


def test_concurrent_identical_uploads_share_complete_immutable_bytes(env):
    e,a,_,_=env
    with ThreadPoolExecutor(max_workers=8) as pool:
        versions=list(pool.map(lambda _:upload(e,a),range(12)))
    assert len({v['id'] for v in versions})==1
    import hashlib
    path=e.root/'uploads'/f"{versions[0]['sha256']}.zip"
    assert hashlib.sha256(path.read_bytes()).hexdigest()==versions[0]['sha256']
    assert list((e.root/'quarantine').iterdir())==[]


def test_outbox_acceptance_and_completion_are_deduplicated_by_batch(env):
    e, a, _, _ = env
    b = batch(e, a, kind='infra', key='outbox')
    with e.db() as conn:
        rows = conn.execute("SELECT event,payload,status FROM outbox WHERE team_id=? ORDER BY created", (a['team_id'],)).fetchall()
        assert [(row['event'], row['status']) for row in rows] == [('evaluation_accepted', 'local_only')]
    lease = e.claim('outbox-worker')
    finish(e, lease)
    with e.db() as conn:
        rows = conn.execute("SELECT event,payload,status FROM outbox WHERE team_id=? ORDER BY created", (a['team_id'],)).fetchall()
        assert [(row['event'], row['status']) for row in rows] == [('evaluation_accepted', 'local_only'), ('evaluation_completed', 'local_only')]
        assert all(json.loads(row['payload'])['batch_id'] == b['id'] for row in rows)
