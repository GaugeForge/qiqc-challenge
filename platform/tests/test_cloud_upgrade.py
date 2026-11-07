"""Local upgrade engineering tests: all service commands and account changes are mocked.

Real filesystem/SQLite cases use temporary paths; no stage service or cloud is touched.
"""
import contextlib
import importlib.util
import io
import json
from pathlib import Path
import sqlite3
import stat
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest import mock

ROOT=Path(__file__).resolve().parents[2] / 'platform-go'
def load(name,path):
    spec=importlib.util.spec_from_file_location(name,path)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module);return module
c=load('upgrade',ROOT/'deploy/cloud_stage.py')
v=load('permissions',ROOT/'deploy/verify_permissions.py')

class UpgradeTests(unittest.TestCase):
    def test_bounded_acceptance_slots_render_only_requested_local_worker(self):
        for slots in (1, 2, 4):
            with mock.patch.object(c, 'ACCEPTANCE_SLOTS', slots):
                units = c.service_units('sha256:' + 'a' * 64)
                worker = next(text for name, text in units.items() if 'worker' in name)
                self.assertIn('-slots ' + str(slots) + ' ', worker)

    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='qiqc-upgrade-tests-')
        self.addCleanup(self.temp.cleanup)
        self.base=Path(self.temp.name)
        self.patches=[]
        for name,relative in [('DATA','data'),('RELEASE','current'),('SCIENCE','science'),('CONF','config'),('UNIT_DIR','units'),('BACKUPS','backups')]:
            path=self.base/relative;path.mkdir()
            patch=mock.patch.object(c,name,path);patch.start();self.addCleanup(patch.stop)
        with sqlite3.connect(c.DATA/'platform.db') as db:
            db.executescript("CREATE TABLE runs(state TEXT,error TEXT);CREATE TABLE batches(status TEXT);CREATE TABLE settings(key TEXT PRIMARY KEY,value TEXT);CREATE TABLE review_advisories(state TEXT,lease_until REAL);")
        (c.DATA/'uploads').mkdir();(c.DATA/'uploads/h.zip').write_bytes(b'immutable-test-zip')
        (c.RELEASE/'old').write_text('old-release');(c.SCIENCE/'old').write_text('old-science')
        (c.CONF/'provider.env').write_text('HY_API=synthetic-private-test-key')
        for service in c.SERVICES[:-1]:(c.UNIT_DIR/service).write_text('old unit')
    def sql(self,query,args=()):
        with sqlite3.connect(c.DATA/'platform.db') as db:db.execute(query,args)
    def test_idle_accepts_terminal_and_reconciled(self):
        self.sql("INSERT INTO runs VALUES('completed',NULL)")
        self.sql("INSERT INTO runs VALUES('attention','platform:reconciled')")
        self.sql("INSERT INTO batches VALUES('completed')")
        c.check_idle()
    def test_idle_refuses_each_active_run_and_unknown_resource(self):
        for state,error in [('queued',None),('running',None),('uploading',None),('attention','worker_lost:reconciliation_required')]:
            with self.subTest(state=state):
                self.sql('DELETE FROM runs');self.sql('INSERT INTO runs VALUES(?,?)',(state,error))
                with self.assertRaises(RuntimeError):c.check_idle()
    def test_idle_refuses_cancelling_and_active_review(self):
        self.sql("INSERT INTO batches VALUES('cancelling')")
        with self.assertRaises(RuntimeError):c.check_idle()
        self.sql('DELETE FROM batches')
        self.sql("INSERT INTO review_advisories VALUES('running',?)",(time.time()+60,))
        with self.assertRaises(RuntimeError):c.check_idle()
        self.sql('UPDATE review_advisories SET lease_until=0');c.check_idle()
    def test_idle_refuses_pending_artifacts_and_upload(self):
        for relative in ['cache/a/pending.json','failure-evidence/a/pending.json','cache/a/staged.json','quarantine/incoming-123.zip']:
            path=c.DATA/relative;path.parent.mkdir(parents=True,exist_ok=True);path.write_text('{}')
            with self.assertRaises(RuntimeError):c.check_idle()
            path.unlink()
        c.check_idle()
    def test_snapshot_keeps_wal_rows_zip_and_old_release_private(self):
        source=sqlite3.connect(c.DATA/'platform.db');self.addCleanup(source.close)
        source.execute('PRAGMA journal_mode=WAL');source.execute("INSERT INTO settings VALUES('sentinel','accepted-before-stop')");source.commit()
        backup=c.recovery_snapshot()
        self.assertEqual(stat.S_IMODE(backup.stat().st_mode),0o700)
        self.assertEqual(stat.S_IMODE(c.BACKUPS.stat().st_mode),0o700)
        with sqlite3.connect(backup/'platform.db') as db:
            self.assertEqual(db.execute("SELECT value FROM settings WHERE key='sentinel'").fetchone()[0],'accepted-before-stop')
        self.assertEqual((backup/'uploads/h.zip').read_bytes(),b'immutable-test-zip')
        self.assertEqual((backup/'release/old').read_text(),'old-release')
        self.assertEqual((backup/'science/old').read_text(),'old-science')
        self.assertIn('HY_API=',(backup/'config/provider.env').read_text())
        self.assertTrue((backup/'ownership.json').is_file())
        self.assertTrue((backup/'RESTORE.txt').is_file())
    def test_copy_tree_refuses_links(self):
        source=self.base/'source';source.mkdir();(source/'link').symlink_to('/etc/passwd')
        with self.assertRaises(RuntimeError):c.copy_tree(source,self.base/'copy')
        self.assertFalse((self.base/'copy').exists())
    def test_candidate_copies_real_web_parser_helper_and_three_exact_templates(self):
        candidate=self.base/'candidate';candidate.mkdir()
        def build(args,**kwargs):
            self.assertEqual(args[:2],['/usr/bin/go','build'])
            Path(args[3]).write_bytes(b'test-only-binary')
        with mock.patch.object(c,'command',side_effect=build):
            release,science=c.prepare_candidate(candidate)
        for name in ['site/index.html','web/app.js','archive.py','scanner_helper.py','review-worker','api','worker','gateway','admin']:
            self.assertTrue((release/name).is_file(),name)
        self.assertEqual((release/'archive.py').read_bytes(),(ROOT.parent/'platform/qiqc/archive.py').read_bytes())
        self.assertEqual((release/'scanner_helper.py').read_bytes(),(ROOT/'deploy/scanner_helper.py').read_bytes())
    def test_private_review_key_is_raw_0600_and_other_config_is_separate(self):
        with mock.patch.object(c.pwd,'getpwnam',return_value=SimpleNamespace(pw_uid=0)),mock.patch.object(c.grp,'getgrnam',return_value=SimpleNamespace(gr_gid=0)),mock.patch.object(c.os,'chown'):
            c.private_payload('review.key','test-review','synthetic-review-only-key')
        path=c.CONF/'review.key'
        self.assertEqual(path.read_text(),'synthetic-review-only-key')
        self.assertEqual(stat.S_IMODE(path.stat().st_mode),0o600)
        self.assertNotIn('HY_API=',path.read_text())
        for bad in ['', 'bad\nkey','bad\x00key']:
            with self.assertRaises(ValueError):c.validate_secret(bad)
    def test_units_have_independent_review_identity_and_pinned_parser(self):
        units=c.service_units('sha256:'+'a'*64)
        self.assertEqual(set(units),set(c.SERVICES))
        review=units['qiqc-stage-review.service']
        for expected in ['User=qiqc-stage-review','Group=qiqc-stage-data','--provider https','--timeout 120s',f'--key-file {c.CONF}/review.key','NoNewPrivileges=true','Restart=on-failure','WantedBy=multi-user.target','CapabilityBoundingSet=']:
            self.assertIn(expected,review)
        self.assertNotIn('SupplementaryGroups=qiqc-stage-science',review)
        self.assertNotIn('HY_API=',review)
        self.assertIn(f'-{c.DATA}/uploads',review)
        self.assertIn(f'--parser {c.RELEASE}/archive.py',units['qiqc-stage-scanner.service'])
    def main_context(self,start_error=None,second_idle_error=False):
        stack=contextlib.ExitStack();self.addCleanup(stack.close)
        events=[]
        stack.enter_context(mock.patch.object(c.os,'geteuid',return_value=0))
        secrets={key:'synthetic-'+key for key in ['E2B_API_KEY','HY_API','HY_URL','R2_ENDPOINT','R2_ACCESS_KEY_ID','R2_SECRET_ACCESS_KEY','R2_BUCKET']}
        stack.enter_context(mock.patch.object(c,'envfile',return_value=secrets))
        stack.enter_context(mock.patch.object(c.grp,'getgrnam',return_value=SimpleNamespace(gr_gid=0)))
        stack.enter_context(mock.patch.object(c.pwd,'getpwnam',return_value=SimpleNamespace(pw_uid=12345)))
        stack.enter_context(mock.patch.object(c,'command',side_effect=lambda *a,**k:events.append('command')))
        stack.enter_context(mock.patch.object(c.subprocess,'check_output',side_effect=lambda args,**kwargs:'sha256:'+'a'*64 if args[0]=='/usr/bin/docker' else 'qiqc-stage-data'))
        stack.enter_context(mock.patch.object(c.subprocess,'run',return_value=SimpleNamespace(returncode=0)))
        def idle():
            events.append('idle')
            if second_idle_error and events.count('idle')==2:raise RuntimeError('accepted during build')
        stack.enter_context(mock.patch.object(c,'check_idle',side_effect=idle))
        stack.enter_context(mock.patch.object(c,'stop_services',side_effect=lambda:events.append('stop')))
        def prepare(directory):
            events.append('prepare');a=directory/'r';a.mkdir();b=directory/'s';b.mkdir();return a,b
        stack.enter_context(mock.patch.object(c,'prepare_candidate',side_effect=prepare))
        def backup():events.append('backup');return c.BACKUPS
        stack.enter_context(mock.patch.object(c,'recovery_snapshot',side_effect=backup))
        def install(source,destination,*args):
            events.append('install');old=self.base/('old-'+destination.name);old.mkdir();return old
        stack.enter_context(mock.patch.object(c,'install_tree',side_effect=install))
        stack.enter_context(mock.patch.object(c,'private_config',side_effect=lambda *args:events.append('config')))
        stack.enter_context(mock.patch.object(c,'private_payload',side_effect=lambda *args:events.append('key')))
        # Generated files must also remain temporary, never change repository units.
        fake_repo=self.base/'repo';(fake_repo/'deploy').mkdir(parents=True)
        stack.enter_context(mock.patch.object(c,'REPO',fake_repo))
        def start():
            events.append('start')
            if start_error:raise RuntimeError('injected failed readiness')
        stack.enter_context(mock.patch.object(c,'start_services',side_effect=start))
        return events
    def test_upgrade_order_success_is_coherent(self):
        events=self.main_context()
        with contextlib.redirect_stdout(io.StringIO()):c.main()
        self.assertLess(events.index('prepare'),events.index('stop'))
        self.assertLess(events.index('stop'),events.index('backup'))
        self.assertLess(events.index('backup'),events.index('install'))
        self.assertLess(events.index('key'),events.index('start'))
        self.assertEqual(events.count('idle'),2)
    def test_upgrade_new_work_after_build_holds_everything_stopped(self):
        events=self.main_context(second_idle_error=True)
        with contextlib.redirect_stderr(io.StringIO()),self.assertRaises(RuntimeError):c.main()
        self.assertEqual(events.count('stop'),2)
        self.assertNotIn('install',events);self.assertNotIn('start',events)
    def test_failed_health_holds_stopped_no_automatic_rollback_restart(self):
        events=self.main_context(start_error=True)
        with contextlib.redirect_stderr(io.StringIO()),self.assertRaises(RuntimeError):c.main()
        self.assertEqual(events.count('stop'),2)
        self.assertEqual(events.count('start'),1)
        self.assertEqual(events[-1],'stop')
    def test_start_waits_for_scanner_then_starts_applications_and_enables(self):
        calls=[]
        response=mock.MagicMock();response.__enter__.return_value.status=200
        with mock.patch.object(c,'command',side_effect=lambda args,**kw:calls.append(args)),mock.patch.object(Path,'is_socket',return_value=True),mock.patch.object(c.urllib.request,'urlopen',return_value=response):
            c.start_services()
        starts=[args for args in calls if args[:2]==['systemctl','start']]
        self.assertEqual(starts[0],['systemctl','start','qiqc-stage-scanner.service'])
        self.assertEqual(set(starts[1][2:]),set(c.SERVICES)-{'qiqc-stage-scanner.service'})
        self.assertIn(['systemctl','enable',*c.SERVICES],calls)

class PermissionsTests(unittest.TestCase):
    def context(self,leak=False,disabled=False):
        stack=contextlib.ExitStack();self.addCleanup(stack.close)
        ids={user:1000+i for i,user in enumerate(v.USERS)}
        stack.enter_context(mock.patch.object(v.pwd,'getpwnam',side_effect=lambda user:SimpleNamespace(pw_uid=ids[user])))
        class FakePath(type(Path())):
            def lstat(self):
                return SimpleNamespace(st_mode=stat.S_IFREG|0o600,st_uid=ids[v.FILES[self.name]],st_nlink=1,st_size=70)
            def read_text(self):return 'Name:\ttest\nPPid:\t1\n'
        stack.enter_context(mock.patch.object(v,'Path',FakePath))
        def run(args):
            code=0;out=''
            if args[0]=='id':
                out='qiqc-stage-data'+(' qiqc-stage-science' if args[-1]=='qiqc-stage-worker' else '')
            elif args[:2]==['systemctl','is-enabled']:out='disabled' if disabled else 'enabled'
            elif args[:2]==['systemctl','show']:
                service=args[2]
                props={'User':service,'Group':'qiqc-stage-data','MainPID':'1234','ActiveState':'active','NoNewPrivileges':'yes','ProtectHome':'yes','ProtectSystem':'strict','PrivateTmp':'yes','TimeoutStopUSec':'8min','Restart':'on-failure','CapabilityBoundingSet':'','SupplementaryGroups':'','ExecStart':'review-worker --provider https --key-file /etc/qiqc-staging/review.key --timeout 120s','InaccessiblePaths':'/opt/qiqc-staging/science /run/docker.sock /var/lib/qiqc-staging/data/uploads'}
                out='\n'.join(k+'='+val for k,val in props.items())
            elif args[0]=='runuser':
                user=args[2]
                if args[-1].startswith('/etc/qiqc-staging/'):
                    owner=v.FILES[Path(args[-1]).name];code=0 if owner==user or (leak and user=='qiqc-stage-review') else 1
                elif '/opt/qiqc-staging/science/' in args[-1]:code=0 if user=='qiqc-stage-worker' else 1
                else:code=1
            else:raise AssertionError('unexpected external command')
            return SimpleNamespace(returncode=code,stdout=out)
        stack.enter_context(mock.patch.object(v,'run',side_effect=run))
    def test_verifier_requires_all_four_identities_and_secret_matrix(self):
        self.context();report=v.verify();self.assertEqual(report['status'],'passed');self.assertEqual(len(report['permission_checks']),44)
    def test_verifier_rejects_review_reading_other_service_key(self):
        self.context(leak=True)
        with self.assertRaises(AssertionError):v.verify()
    def test_verifier_rejects_disabled_boot_start(self):
        self.context(disabled=True)
        with self.assertRaises(AssertionError):v.verify()

if __name__=='__main__':unittest.main(verbosity=2)
