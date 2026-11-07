from __future__ import annotations
import hashlib
import hmac
import io
import json
import math
import os
import secrets
import shutil
import sqlite3
import subprocess
import tarfile
import time
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

from .archive import MAX_ZIP, inspect_archive, reference_package
from .storage import ObjectStore, digest

START=1794096000.0  # 2026-11-08 00:00 UTC
END=1794315600.0    # 2026-11-10 13:00 UTC
APPEAL_END=END+86400
KINDS={'full':39,'public':5,'infra':1}
ACTIVE=('queued','testing','attention','cancelling')
AUDIT_CONTEXT=ContextVar('audit_context',default={})

class Problem(Exception):
    def __init__(self,message,status=409): self.message=message; self.status=status; super().__init__(message)

@dataclass
class Settings:
    data_dir: Path=field(default_factory=lambda:Path(os.environ.get('QIQC_DATA','/root/QQ-Challenge/runtime/legacy-platform-data')))
    scan_docker: bool=True
    demo_mode: bool=True
    slots: int=4
    simulated_delay: float=0.4
    secure_cookies: bool=False
    model: str='gpt-5.6-sol'


def mean_seconds(values):
    nums=[Decimal(str(x)) for x in values]
    if not nums or any(not x.is_finite() or x<0 for x in nums): raise ValueError('Invalid durations')
    return str((sum(nums)/len(nums)).quantize(Decimal('0.000001'),rounding=ROUND_HALF_UP))


def standings(rows,baseline):
    best={}
    key=lambda x:(-x['passed'],Decimal(x['mean']),x['accepted_at'],x['id'])
    qualifying=[]
    for row in rows:
        row=dict(row)
        if row['team_id'] not in best or key(row)<key(best[row['team_id']]): best[row['team_id']]=row
        if (-row['passed'],Decimal(row['mean']))<(-baseline['passed'],Decimal(baseline['mean'])):
            qualifying.append(row)
    challenger=min(qualifying,key=lambda x:(x['accepted_at'],x['id']))['team_id'] if qualifying else None
    result=sorted(best.values(),key=key)
    prev=None; rank=0
    for idx,row in enumerate(result):
        score=(row['passed'],row['mean'])
        if score!=prev: rank=idx+1
        row.update(rank=rank,challenger=row['team_id']==challenger,tie=sum((r['passed'],r['mean'])==score for r in result)>1)
        prev=score
    return result


def password_hash(password):
    salt=secrets.token_bytes(16)
    value=hashlib.scrypt(password.encode(),salt=salt,n=16384,r=8,p=1).hex()
    return salt.hex()+':'+value


def password_matches(password,stored):
    salt,value=stored.split(':')
    return hmac.compare_digest(hashlib.scrypt(password.encode(),salt=bytes.fromhex(salt),n=16384,r=8,p=1).hex(),value)


def uid(): return uuid.uuid4().hex

def token_hash(token): return hashlib.sha256(token.encode()).hexdigest()

def rowdict(row): return dict(row) if row is not None else None


class Engine:
    def __init__(self,settings=None):
        self.settings=settings or Settings()
        self.settings.data_dir=Path(self.settings.data_dir).resolve()
        self.root=self.settings.data_dir
        self.root.mkdir(parents=True,exist_ok=True,mode=0o700)
        os.chmod(self.root,0o700)
        for name in ['uploads','quarantine','cache','jobs','backups']:
            (self.root/name).mkdir(exist_ok=True,mode=0o700)
        self.db_path=self.root/'platform.db'
        self.store=ObjectStore(self.root/'objects')
        self.clock=time.time
        self.init_db()

    @contextmanager
    def db(self,write=False):
        conn=sqlite3.connect(self.db_path,timeout=30,isolation_level=None)
        conn.row_factory=sqlite3.Row
        conn.execute('PRAGMA foreign_keys=ON')
        conn.execute('PRAGMA synchronous=FULL')
        try:
            if write: conn.execute('BEGIN IMMEDIATE')
            yield conn
            if write: conn.commit()
        except BaseException:
            if write: conn.rollback()
            raise
        finally: conn.close()

    def init_db(self):
        with self.db() as c:
            c.executescript('''
            CREATE TABLE IF NOT EXISTS teams(id TEXT PRIMARY KEY,name TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS users(id TEXT PRIMARY KEY,email TEXT UNIQUE NOT NULL,password TEXT NOT NULL,team_id TEXT REFERENCES teams(id),role TEXT NOT NULL,must_change INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS sessions(hash TEXT PRIMARY KEY,user_id TEXT REFERENCES users(id),expires REAL,ip TEXT);
            CREATE TABLE IF NOT EXISTS login_attempts(identity TEXT,created REAL);
            CREATE TABLE IF NOT EXISTS versions(id TEXT PRIMARY KEY,team_id TEXT REFERENCES teams(id),sha256 TEXT NOT NULL,name TEXT,status TEXT,report TEXT,reason TEXT,created REAL,UNIQUE(team_id,sha256));
            CREATE TABLE IF NOT EXISTS batches(id TEXT PRIMARY KEY,team_id TEXT REFERENCES teams(id),version_id TEXT REFERENCES versions(id),kind TEXT,mode TEXT,idem TEXT,status TEXT,accepted_at REAL,last_dispatch REAL DEFAULT 0,passed INTEGER,mean TEXT,invalid INTEGER DEFAULT 0,UNIQUE(team_id,idem));
            CREATE UNIQUE INDEX IF NOT EXISTS one_active_batch ON batches(team_id) WHERE status IN ('queued','testing','attention','cancelling');
            CREATE TABLE IF NOT EXISTS runs(id TEXT PRIMARY KEY,batch_id TEXT REFERENCES batches(id),ordinal INTEGER,public INTEGER,state TEXT,attempt INTEGER DEFAULT 0,owner TEXT,lease_until REAL,reward INTEGER,seconds TEXT,object_key TEXT,object_sha TEXT,error TEXT,UNIQUE(batch_id,ordinal));
            CREATE TABLE IF NOT EXISTS attempts(id TEXT PRIMARY KEY,run_id TEXT REFERENCES runs(id),number INTEGER,state TEXT,started REAL,finished REAL,error TEXT,UNIQUE(run_id,number));
            CREATE TABLE IF NOT EXISTS ledger(id TEXT PRIMARY KEY,team_id TEXT REFERENCES teams(id),kind TEXT,delta INTEGER,action TEXT,batch_id TEXT REFERENCES batches(id),reason TEXT,created REAL,UNIQUE(batch_id,action));
            CREATE TABLE IF NOT EXISTS audit(id TEXT PRIMARY KEY,created REAL,user_id TEXT,actor TEXT,action TEXT,object_id TEXT,reason TEXT,before_json TEXT,after_json TEXT);
            CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY,value TEXT);
            INSERT OR IGNORE INTO settings VALUES('paused','false');
            INSERT OR IGNORE INTO settings VALUES('competition_closed','false');
            CREATE TABLE IF NOT EXISTS appeals(id TEXT PRIMARY KEY,team_id TEXT UNIQUE REFERENCES teams(id),body TEXT,status TEXT,created REAL,response TEXT);
            CREATE TABLE IF NOT EXISTS outbox(id TEXT PRIMARY KEY,event TEXT,team_id TEXT,payload TEXT,created REAL,status TEXT DEFAULT 'local_only');
            CREATE TABLE IF NOT EXISTS run_tokens(hash TEXT PRIMARY KEY,run_id TEXT,attempt INTEGER,model TEXT,expires REAL,revoked INTEGER DEFAULT 0);
            CREATE TABLE IF NOT EXISTS usage(id TEXT PRIMARY KEY,run_id TEXT,attempt INTEGER,model TEXT,created REAL,status INTEGER,input_tokens INTEGER,output_tokens INTEGER,cached_tokens INTEGER,amount REAL,price_version TEXT,source TEXT);
            ''')
            c.execute('BEGIN IMMEDIATE')
            columns={r[1] for r in c.execute('PRAGMA table_info(audit)')}
            for column in ['session_fingerprint','ip']:
                if column not in columns: c.execute('ALTER TABLE audit ADD COLUMN '+column+' TEXT')
            c.commit()

    def create_account(self,email,password,team,role='team',must_change=True):
        if len(password)<12: raise Problem('密码至少 12 个字符',400)
        with self.db(True) as c:
            existing=c.execute('SELECT id,team_id FROM users WHERE email=?',(email.lower(),)).fetchone()
            if existing: return dict(existing)
            tid=uid(); account=uid()
            c.execute('INSERT INTO teams VALUES(?,?)',(tid,team))
            c.execute('INSERT INTO users VALUES(?,?,?,?,?,?)',(account,email.lower(),password_hash(password),tid,role,int(must_change)))
            return {'id':account,'team_id':tid}

    def user(self,c,user_id):
        r=c.execute('SELECT u.*,t.name team FROM users u JOIN teams t ON t.id=u.team_id WHERE u.id=?',(user_id,)).fetchone()
        if not r: raise Problem('请先登录',401)
        return r

    def me(self,user_id):
        with self.db() as c:
            u=self.user(c,user_id)
            return {k:u[k] for k in ['id','email','team_id','team','role','must_change']}|{'quotas':self.quotas(c,u['team_id'])}

    def quotas(self,c,team_id):
        result={'full':1,'public':3,'infra':10}
        for r in c.execute('SELECT kind,SUM(delta) n FROM ledger WHERE team_id=? GROUP BY kind',(team_id,)):
            result[r['kind']]+=r['n']
        return result

    def ledger(self,user_id):
        with self.db() as c:
            u=self.user(c,user_id)
            return [dict(r) for r in c.execute('SELECT * FROM ledger WHERE team_id=? ORDER BY created',(u['team_id'],))]

    def login(self,email,password,ip):
        identity=ip+':'+email.lower(); now=self.clock()
        with self.db(True) as c:
            c.execute('DELETE FROM login_attempts WHERE created<?',(now-900,))
            if c.execute('SELECT COUNT(*) FROM login_attempts WHERE identity=?',(identity,)).fetchone()[0]>=6:
                raise Problem('尝试次数过多，请 15 分钟后重试',429)
            u=c.execute('SELECT * FROM users WHERE email=?',(email.lower(),)).fetchone()
            valid=bool(u and password_matches(password,u['password']))
            if not valid:
                c.execute('INSERT INTO login_attempts VALUES(?,?)',(identity,now))
            else:
                token=secrets.token_urlsafe(32)
                c.execute('INSERT INTO sessions VALUES(?,?,?,?)',(token_hash(token),u['id'],now+43200,ip))
                c.execute('DELETE FROM login_attempts WHERE identity=?',(identity,))
        if not valid: raise Problem('邮箱或密码不正确',401)
        return token,self.me(u['id'])

    def session(self,token):
        if not token: raise Problem('请先登录',401)
        with self.db() as c:
            r=c.execute('SELECT user_id FROM sessions WHERE hash=? AND expires>?',(token_hash(token),self.clock())).fetchone()
            if not r: raise Problem('会话已失效，请重新登录',401)
            return self.me(r['user_id'])

    def logout(self,token):
        with self.db(True) as c: c.execute('DELETE FROM sessions WHERE hash=?',(token_hash(token),))

    def change_password(self,user_id,password,token):
        if not 12<=len(password)<=200: raise Problem('密码长度为 12–200 个字符',400)
        with self.db(True) as c:
            c.execute('UPDATE users SET password=?,must_change=0 WHERE id=?',(password_hash(password),user_id))
            c.execute('DELETE FROM sessions WHERE user_id=? AND hash!=?',(user_id,token_hash(token)))

    def upload(self,user_id,data):
        if len(data)>MAX_ZIP: raise Problem('压缩包不能超过 20 MiB',413)
        with self.db() as c:
            u=self.user(c,user_id)
            if u['role']!='team': raise Problem('请使用队伍账号上传',403)
            if c.execute('SELECT COUNT(*) FROM versions WHERE team_id=?',(u['team_id'],)).fetchone()[0]>=25:
                raise Problem('本地 Demo 每队最多保留 25 个版本',429)
        sha=hashlib.sha256(data).hexdigest()
        canonical=self.root/'uploads'/f'{sha}.zip'
        path=self.root/'quarantine'/f'{uid()}.zip'
        # Each upload has a private staging inode. Concurrent inspections cannot
        # delete or read another request's partially written canonical file.
        fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o644)
        with os.fdopen(fd,'wb') as f: f.write(data); f.flush(); os.fsync(f.fileno())
        path.chmod(0o644)  # parent is 0700; container receives only this file read-only
        try:
            if self.settings.scan_docker:
                script=Path(__file__).with_name('archive.py').resolve()
                result=subprocess.run(['docker','run','--rm','--network','none','--read-only','--cap-drop','ALL','--security-opt','no-new-privileges','--pids-limit','64','--memory','256m','--cpus','0.5','--user','65534:65534','--mount',f'type=bind,src={path},dst=/incoming/harness.zip,readonly','--mount',f'type=bind,src={script},dst=/scanner.py,readonly','qiqcbench-agent:latest','python3','/scanner.py','/incoming/harness.zip'],capture_output=True,timeout=30)
                try: parsed=json.loads(result.stdout)
                except Exception: raise Problem('隔离检查器暂不可用，请联系管理员；未扣次数',503)
                if not parsed.get('ok'): raise ValueError(parsed.get('error','Archive rejected'))
                report=parsed['report']
            else: report=inspect_archive(data)
            try: os.link(path,canonical)
            except FileExistsError:
                if digest(canonical)!=sha: raise Problem('已保存版本校验失败',500)
            directory=os.open(canonical.parent,os.O_DIRECTORY)
            try: os.fsync(directory)
            finally: os.close(directory)
        except (ValueError,zipfile_error()) as exc:
            raise Problem('提交格式检查失败：'+str(exc)[:250],400)
        except subprocess.TimeoutExpired:
            raise Problem('隔离检查超时；未扣次数',503)
        finally:
            path.unlink(missing_ok=True)
        state='approved' if report['reference'] else 'pending_review'
        reason='固定可信 Codex 参考包；真实执行仅限 mock 题。' if report['reference'] else 'AI 审查未连接，需管理员人工复核。'
        with self.db(True) as c:
            existing=c.execute('SELECT * FROM versions WHERE team_id=? AND sha256=?',(u['team_id'],sha)).fetchone()
            if existing: return self.version_view(existing)
            vid=uid()
            c.execute('INSERT INTO versions VALUES(?,?,?,?,?,?,?,?)',(vid,u['team_id'],sha,report['manifest']['name'],state,json.dumps(report,ensure_ascii=False),reason,self.clock()))
            return self.version_view(c.execute('SELECT * FROM versions WHERE id=?',(vid,)).fetchone())

    def version_view(self,r):
        report=json.loads(r['report'])
        return {k:r[k] for k in ['id','sha256','name','status','reason','created']}|{'reference':report['reference'],'files':report['files'],'risk_count':len(report['risks']),'ai_review':report['ai_review']}

    def versions(self,user_id):
        with self.db() as c:
            u=self.user(c,user_id)
            return [self.version_view(r) for r in c.execute('SELECT * FROM versions WHERE team_id=? ORDER BY created DESC',(u['team_id'],))]

    def confirm(self,user_id,version_id,kind,mode,idem):
        if kind not in KINDS or mode not in ['simulated','codex'] or not 1<=len(idem)<=120: raise Problem('无效评测参数',400)
        now=self.clock()
        with self.db(True) as c:
            u=self.user(c,user_id)
            if u['role']!='team' or u['must_change']: raise Problem('请先使用队伍账号完成改密',403)
            old=c.execute('SELECT * FROM batches WHERE team_id=? AND idem=?',(u['team_id'],idem)).fetchone()
            if old:
                if (old['version_id'],old['kind'],old['mode'])!=(version_id,kind,mode): raise Problem('幂等键已绑定其他请求')
                return self.batch_view(c,old)
            if self.settings.demo_mode and c.execute("SELECT value FROM settings WHERE key='competition_closed'").fetchone()[0] == 'true': raise Problem('Demo 比赛已结束，不再受理新评测')
            if not self.settings.demo_mode and not START<=now<END: raise Problem('当前不在评测受理时间内')
            v=c.execute('SELECT * FROM versions WHERE id=? AND team_id=?',(version_id,u['team_id'])).fetchone()
            if not v: raise Problem('版本不存在',404)
            if v['status']!='approved': raise Problem('该版本尚未通过审查')
            if digest(self.root/'uploads'/f"{v['sha256']}.zip")!=v['sha256']: raise Problem('版本字节校验不一致，已停止受理',500)
            if mode=='codex' and (kind!='infra' or not json.loads(v['report'])['reference']): raise Problem('真实本地执行仅支持固定 Codex 参考包的 infra mock 题')
            if c.execute("SELECT 1 FROM batches WHERE team_id=? AND status IN ('queued','testing','attention','cancelling')",(u['team_id'],)).fetchone(): raise Problem('本队已有未结束批次，请等待或取消')
            if self.quotas(c,u['team_id'])[kind]<=0: raise Problem('该类型评测次数已用完')
            bid=uid()
            c.execute('INSERT INTO batches(id,team_id,version_id,kind,mode,idem,status,accepted_at) VALUES(?,?,?,?,?,?,?,?)',(bid,u['team_id'],version_id,kind,mode,idem,'queued',now))
            for i in range(KINDS[kind]):
                c.execute('INSERT INTO runs(id,batch_id,ordinal,public,state) VALUES(?,?,?,?,?)',(uid(),bid,i,int(i<5),'queued'))
            c.execute('INSERT INTO ledger VALUES(?,?,?,?,?,?,?,?)',(uid(),u['team_id'],kind,-1,'accepted',bid,'确认评测；取消不退次',now))
            self.notify(c,'evaluation_accepted',u['team_id'],{'batch_id':bid,'kind':kind})
            return self.batch_view(c,c.execute('SELECT * FROM batches WHERE id=?',(bid,)).fetchone())

    def batch_view(self,c,r):
        v=c.execute('SELECT sha256,name FROM versions WHERE id=?',(r['version_id'],)).fetchone()
        public_status='testing' if r['status']=='attention' else r['status']
        result={k:r[k] for k in ['id','version_id','kind','mode','accepted_at','invalid']}
        result.update(status=public_status,sha256=v['sha256'],name=v['name'],task_count=KINDS[r['kind']],score=None,public_runs=[])
        if r['kind']=='public':
            # Display metadata for the participant workspace. Trace access is
            # still authorized by team and batch ID in trace().
            result['public_test_number']=c.execute(
                "SELECT count(*) FROM batches WHERE team_id=? AND kind='public' AND (accepted_at<? OR (accepted_at=? AND id<=?))",
                (r['team_id'],r['accepted_at'],r['accepted_at'],r['id'])).fetchone()[0]
        if r['status']=='queued':
            # Number of task dispatches scheduled before the first task of this batch
            ahead=c.execute("SELECT COUNT(*) FROM batches b WHERE b.id!=? AND b.status IN ('queued','testing') AND EXISTS(SELECT 1 FROM runs x WHERE x.batch_id=b.id AND x.state='queued') AND (b.last_dispatch<? OR (b.last_dispatch=? AND b.accepted_at<?))",(r['id'],r['last_dispatch'],r['last_dispatch'],r['accepted_at'])).fetchone()[0]
            result['tasks_ahead']=ahead
        if r['status']=='completed':
            result['score']={'passed':r['passed'],'total':KINDS[r['kind']],'mean_seconds':r['mean']}
            if r['kind']=='public':
                result['public_runs']=[{'ordinal':x['ordinal'],'label':f"公开任务 {x['ordinal']+1}"} for x in c.execute('SELECT ordinal FROM runs WHERE batch_id=? AND public=1 ORDER BY ordinal',(r['id'],))]
        return result

    def get_batch(self,user_id,bid):
        with self.db() as c:
            u=self.user(c,user_id)
            r=c.execute('SELECT * FROM batches WHERE id=? AND team_id=?',(bid,u['team_id'])).fetchone()
            if not r: raise Problem('评测不存在',404)
            return self.batch_view(c,r)

    def batches(self,user_id):
        with self.db() as c:
            u=self.user(c,user_id)
            return [self.batch_view(c,r) for r in c.execute('SELECT * FROM batches WHERE team_id=? ORDER BY accepted_at DESC',(u['team_id'],))]

    def cancel(self,user_id,bid):
        with self.db(True) as c:
            u=self.user(c,user_id)
            r=c.execute('SELECT * FROM batches WHERE id=? AND team_id=?',(bid,u['team_id'])).fetchone()
            if not r: raise Problem('评测不存在',404)
            if r['status'] not in ACTIVE: return
            c.execute("UPDATE runs SET state='cancelled' WHERE batch_id=? AND state IN ('queued','attention')",(bid,))
            running=c.execute("SELECT COUNT(*) FROM runs WHERE batch_id=? AND state IN ('running','uploading')",(bid,)).fetchone()[0]
            c.execute('UPDATE batches SET status=? WHERE id=?',('cancelling' if running else 'cancelled',bid))
            c.execute('UPDATE run_tokens SET revoked=1 WHERE run_id IN (SELECT id FROM runs WHERE batch_id=?)',(bid,))

    def claim(self,owner):
        now=self.clock()
        with self.db(True) as c:
            if json.loads(c.execute("SELECT value FROM settings WHERE key='paused'").fetchone()[0]): return None
            if c.execute("SELECT COUNT(*) FROM runs WHERE state IN ('running','uploading')").fetchone()[0]>=self.settings.slots: return None
            r=c.execute("SELECT r.*,b.kind,b.mode,b.team_id,b.version_id,v.sha256 FROM runs r JOIN batches b ON b.id=r.batch_id JOIN versions v ON v.id=b.version_id WHERE r.state='queued' AND b.status IN ('queued','testing') ORDER BY b.last_dispatch,b.accepted_at,r.ordinal LIMIT 1").fetchone()
            if not r: return None
            attempt=r['attempt']+1; aid=uid()
            c.execute("UPDATE runs SET state='running',attempt=?,owner=?,lease_until=?,error=NULL WHERE id=?",(attempt,owner,now+60,r['id']))
            c.execute('INSERT INTO attempts VALUES(?,?,?,?,?,?,?)',(aid,r['id'],attempt,'running',now,None,None))
            c.execute("UPDATE batches SET status='testing',last_dispatch=? WHERE id=?",(now,r['batch_id']))
            return dict(r)|{'run_id':r['id'],'attempt':attempt,'attempt_id':aid,'owner':owner}

    def heartbeat(self,lease):
        with self.db(True) as c:
            c.execute("UPDATE runs SET lease_until=? WHERE id=? AND attempt=? AND owner=? AND state IN ('running','uploading')",(self.clock()+60,lease['run_id'],lease['attempt'],lease['owner']))

    def check_lease(self,c,lease):
        r=c.execute('SELECT * FROM runs WHERE id=?',(lease['run_id'],)).fetchone()
        if not r or r['attempt']!=lease['attempt'] or r['owner']!=lease['owner'] or r['state'] not in ['running','uploading']:
            raise Problem('旧执行代次或重复完成，拒绝写入')
        return r

    def complete_task(self,lease,reward,seconds,trace,provenance='simulated',source_dir=None):
        if reward not in [0,1]: raise ValueError('Verifier reward must be binary')
        mean_seconds([seconds])
        with self.db() as c: self.check_lease(c,lease)
        cache=self.root/'cache'/lease['run_id']/str(lease['attempt'])
        cache.mkdir(parents=True,exist_ok=True,mode=0o700)
        bundle=cache/'rollout.tar.gz'
        if not bundle.exists():
            temp=cache/'rollout.tmp'
            with tarfile.open(temp,'w:gz') as tar:
                if source_dir:
                    for p in sorted(Path(source_dir).rglob('*')):
                        if p.is_symlink(): raise ValueError('Rollout contains symbolic link')
                        if p.is_file(): tar.add(p,arcname='harbor/'+str(p.relative_to(source_dir)),recursive=False)
                metadata=json.dumps({'provenance':provenance,'reward':reward,'seconds':str(seconds),'run_id':lease['run_id'],'attempt':lease['attempt']}).encode()
                for name,data in [('platform-result.json',metadata),('public-view.txt',trace.encode())]:
                    item=tarfile.TarInfo(name); item.size=len(data); tar.addfile(item,io.BytesIO(data))
            os.replace(temp,bundle)
        pending={'lease':lease,'reward':reward,'seconds':str(seconds),'provenance':provenance}
        (cache/'pending.json').write_text(json.dumps(pending))
        with self.db(True) as c:
            self.check_lease(c,lease)
            c.execute("UPDATE runs SET state='uploading' WHERE id=?",(lease['run_id'],))
        key=f"{lease['team_id']}/{lease['version_id']}/{lease['batch_id']}/{lease['run_id']}/{lease['attempt']}.tar.gz"
        obj=self.store.put_file(key,bundle)
        with self.db(True) as c:
            self.check_lease(c,lease)
            status=c.execute('SELECT status FROM batches WHERE id=?',(lease['batch_id'],)).fetchone()[0]
            state='cancelled' if status=='cancelling' else 'completed'
            c.execute('UPDATE runs SET state=?,reward=?,seconds=?,object_key=?,object_sha=?,lease_until=NULL WHERE id=?',(state,reward,str(seconds),key,obj['sha256'],lease['run_id']))
            c.execute('UPDATE attempts SET state=?,finished=? WHERE run_id=? AND number=?',(state,self.clock(),lease['run_id'],lease['attempt']))
            c.execute('UPDATE run_tokens SET revoked=1 WHERE run_id=?',(lease['run_id'],))
            self.finalize(c,lease['batch_id'])
        shutil.rmtree(cache)

    def finalize(self,c,bid):
        batch=c.execute('SELECT * FROM batches WHERE id=?',(bid,)).fetchone()
        rows=c.execute('SELECT * FROM runs WHERE batch_id=?',(bid,)).fetchall()
        if batch['status']=='cancelling':
            if all(r['state'] not in ['running','uploading'] for r in rows): c.execute("UPDATE batches SET status='cancelled' WHERE id=?",(bid,))
        elif len(rows)==KINDS[batch['kind']] and all(r['state']=='completed' and r['object_key'] and r['reward'] in [0,1] for r in rows):
            c.execute("UPDATE batches SET status='completed',passed=?,mean=? WHERE id=?",(sum(r['reward'] for r in rows),mean_seconds([r['seconds'] for r in rows]),bid))
            self.notify(c,'evaluation_completed',batch['team_id'],{'batch_id':bid,'mode':batch['mode']})

    def fail_task(self,lease,category,error):
        with self.db(True) as c:
            self.check_lease(c,lease)
            c.execute("UPDATE runs SET state='attention',error=?,lease_until=NULL WHERE id=?",(f'{category}:{error}'[:300],lease['run_id']))
            c.execute("UPDATE attempts SET state='failed',finished=?,error=? WHERE run_id=? AND number=?",(self.clock(),error[:300],lease['run_id'],lease['attempt']))
            c.execute('UPDATE run_tokens SET revoked=1 WHERE run_id=?',(lease['run_id'],))
            b=c.execute('SELECT status FROM batches WHERE id=?',(lease['batch_id'],)).fetchone()
            if b[0]=='cancelling':
                c.execute("UPDATE runs SET state='cancelled' WHERE id=?",(lease['run_id'],)); self.finalize(c,lease['batch_id'])
            else: c.execute("UPDATE batches SET status='attention' WHERE id=?",(lease['batch_id'],))

    def recover(self):
        # Uploading attempts with a complete bundle are resumed by the worker, not rerun.
        with self.db(True) as c:
            rows=c.execute("SELECT r.*,b.mode,b.status batch_status FROM runs r JOIN batches b ON b.id=r.batch_id WHERE r.state='running' AND r.lease_until<?",(self.clock(),)).fetchall()
            for r in rows:
                state=('cancelled' if r['batch_status']=='cancelling' else 'queued') if r['mode']=='simulated' else 'attention'
                c.execute('UPDATE runs SET state=?,owner=NULL,error=? WHERE id=?',(state,'worker_lost:reconciliation_required',r['id']))
                c.execute("UPDATE attempts SET state='interrupted',finished=? WHERE run_id=? AND number=?",(self.clock(),r['id'],r['attempt']))
                if state=='attention': c.execute("UPDATE batches SET status='attention' WHERE id=?",(r['batch_id'],))
                elif state=='cancelled': self.finalize(c,r['batch_id'])
                c.execute('UPDATE run_tokens SET revoked=1 WHERE run_id=?',(r['id'],))
        return len(rows)

    def trace(self,user_id,bid,ordinal):
        with self.db() as c:
            u=self.user(c,user_id)
            b=c.execute('SELECT * FROM batches WHERE id=? AND team_id=?',(bid,u['team_id'])).fetchone()
            if not b or b['status']!='completed': raise Problem('整批完成后才能读取本队公开轨迹',404)
            r=c.execute("SELECT * FROM runs WHERE batch_id=? AND ordinal=? AND public=1 AND (SELECT kind FROM batches WHERE id=?)='public'",(bid,ordinal,bid)).fetchone()
            if not r: raise Problem('轨迹不可访问',404)
            with self.store.local_path(r['object_key']) as path:
                if digest(path)!=r['object_sha']: raise Problem('证据完整性检查失败',503)
                with tarfile.open(path,'r:gz') as t:
                    member=t.getmember('public-view.txt')
                    if not member.isfile() or member.size>500000: raise Problem('轨迹视图过大',503)
                    text=t.extractfile(member).read().decode('utf-8',errors='replace')
            return {'text':text,'mode':b['mode'],'ordinal':ordinal,'filtered':True}

    def is_closed(self):
        with self.db() as c:
            row=c.execute("SELECT value FROM settings WHERE key='competition_closed'").fetchone()
            return bool(row and row[0] == 'true')

    def leaderboard(self):
        baseline_path=Path(os.environ.get('QIQC_BASELINE','/root/QQ-Challenge/benchmark-assets/baseline-mock.json'))
        try:
            baseline=json.loads(baseline_path.read_text())
            baseline['mean']=baseline.get('mean_runtime_seconds',baseline.get('mean','30.000000'))
        except (OSError,ValueError,TypeError):
            baseline={'label':'Codex + Astra','passed':38,'mean':'30.000000','simulated':True,'replacement_required':True}
        with self.db() as c:
            rows=[dict(r) for r in c.execute("SELECT b.id,b.team_id,t.name team,b.passed,b.mean,b.accepted_at,v.sha256 FROM batches b JOIN teams t ON t.id=b.team_id JOIN versions v ON v.id=b.version_id WHERE b.kind='full' AND b.mode='simulated' AND b.status='completed' AND b.invalid=0")]
            counts={r['status']:r['n'] for r in c.execute('SELECT status,COUNT(*) n FROM batches GROUP BY status')}
        return {'teams':standings(rows,baseline),'baseline':baseline,'queued_batches':counts.get('queued',0),'testing_batches':sum(counts.get(x,0) for x in ['testing','attention','cancelling']),'mode':'simulated','provisional':True}

    def admin(self,c,user_id):
        u=self.user(c,user_id)
        if u['role']!='admin': raise Problem('仅管理员可访问',403)
        return u

    def record(self,c,user_id,actor,action,obj,reason,before=None,after=None):
        self.admin(c,user_id)
        if not actor.strip() or len(reason.strip())<3: raise Problem('请输入操作者和至少 3 个字符的操作理由',400)
        c.execute('INSERT INTO audit VALUES(?,?,?,?,?,?,?,?,?,?,?)',(uid(),self.clock(),user_id,actor[:80],action,obj,reason[:1000],json.dumps(before,ensure_ascii=False),json.dumps(after,ensure_ascii=False),AUDIT_CONTEXT.get().get('session'),AUDIT_CONTEXT.get().get('ip')))

    def review(self,user_id,vid,decision,actor,reason):
        if decision not in ['approve','reject']: raise Problem('无效复核决定',400)
        with self.db(True) as c:
            self.admin(c,user_id)
            v=c.execute('SELECT * FROM versions WHERE id=?',(vid,)).fetchone()
            if not v: raise Problem('版本不存在',404)
            if v['status']!='pending_review': raise Problem('该版本已有审查结论')
            state='approved' if decision=='approve' else 'rejected'
            self.record(c,user_id,actor,'review',vid,reason,v['status'],state)
            c.execute('UPDATE versions SET status=?,reason=? WHERE id=?',(state,reason[:1000],vid))

    def adjust_quota(self,user_id,team_id,kind,delta,actor,reason):
        if kind not in KINDS or type(delta)!=int or not -20<=delta<=20 or not delta: raise Problem('无效额度调整',400)
        with self.db(True) as c:
            self.admin(c,user_id)
            if not c.execute('SELECT 1 FROM teams WHERE id=?',(team_id,)).fetchone(): raise Problem('队伍不存在',404)
            before=self.quotas(c,team_id)[kind]
            if before+delta<0: raise Problem('剩余次数不能为负')
            self.record(c,user_id,actor,'quota_adjust',team_id,reason,before,before+delta)
            c.execute('INSERT INTO ledger VALUES(?,?,?,?,?,?,?,?)',(uid(),team_id,kind,delta,'adjustment',None,reason,self.clock()))

    def close_demo(self,user_id,actor,reason):
        with self.db(True) as c:
            self.admin(c,user_id)
            before=c.execute("SELECT value FROM settings WHERE key='competition_closed'").fetchone()[0]
            if before == 'true':
                raise Problem('Demo 比赛已经结束')
            self.record(c,user_id,actor,'competition_close','competition',reason,False,True)
            c.execute("UPDATE settings SET value='true' WHERE key='competition_closed'")

    def set_pause(self,user_id,paused,actor,reason):
        with self.db(True) as c:
            before=c.execute("SELECT value FROM settings WHERE key='paused'").fetchone()[0]
            self.record(c,user_id,actor,'queue_pause','queue',reason,json.loads(before),bool(paused))
            c.execute("UPDATE settings SET value=? WHERE key='paused'",(json.dumps(bool(paused)),))

    def retry_batch(self,user_id,bid,actor,reason):
        with self.db(True) as c:
            self.admin(c,user_id)
            b=c.execute('SELECT * FROM batches WHERE id=?',(bid,)).fetchone()
            if not b or b['status']!='attention': raise Problem('只有待处理平台故障可重试')
            self.record(c,user_id,actor,'retry',bid,reason,'attention','testing')
            c.execute("UPDATE runs SET state='queued',owner=NULL WHERE batch_id=? AND state='attention'",(bid,))
            c.execute("UPDATE batches SET status='testing' WHERE id=?",(bid,))

    def invalidate(self,user_id,bid,actor,reason):
        with self.db(True) as c:
            self.admin(c,user_id)
            b=c.execute('SELECT * FROM batches WHERE id=?',(bid,)).fetchone()
            if not b or b['status']!='completed': raise Problem('只能对已完成成绩作废')
            self.record(c,user_id,actor,'invalidate_score',bid,reason,b['invalid'],1)
            c.execute('UPDATE batches SET invalid=1 WHERE id=?',(bid,))

    def appeal(self,user_id,body):
        if not 10<=len(body)<=4000: raise Problem('申诉内容应为 10–4000 字符',400)
        if not self.settings.demo_mode and self.clock()>APPEAL_END: raise Problem('申诉已截止')
        with self.db(True) as c:
            u=self.user(c,user_id)
            if c.execute('SELECT 1 FROM appeals WHERE team_id=?',(u['team_id'],)).fetchone(): raise Problem('每队只有一次申诉机会')
            c.execute('INSERT INTO appeals VALUES(?,?,?,?,?,?)',(uid(),u['team_id'],body,'open',self.clock(),None))

    def answer_appeal(self,user_id,appeal_id,actor,reason):
        with self.db(True) as c:
            self.admin(c,user_id)
            a=c.execute('SELECT * FROM appeals WHERE id=?',(appeal_id,)).fetchone()
            if not a: raise Problem('申诉不存在',404)
            self.record(c,user_id,actor,'appeal_response',appeal_id,reason,a['status'],'answered')
            c.execute("UPDATE appeals SET status='answered',response=? WHERE id=?",(reason,appeal_id))

    def notify(self,c,event,team_id,payload):
        c.execute('INSERT INTO outbox(id,event,team_id,payload,created) VALUES(?,?,?,?,?)',(uid(),event,team_id,json.dumps(payload),self.clock()))

    def admin_snapshot(self,user_id):
        with self.db() as c:
            self.admin(c,user_id)
            versions=[dict(r) for r in c.execute('SELECT v.*,t.name team FROM versions v JOIN teams t ON t.id=v.team_id ORDER BY v.created DESC LIMIT 100')]
            for v in versions: v['report']=json.loads(v['report'])
            batches=[dict(r) for r in c.execute('SELECT b.*,t.name team FROM batches b JOIN teams t ON t.id=b.team_id ORDER BY b.accepted_at DESC LIMIT 100')]
            for b in batches:
                b['run_states']={r['state']:r['n'] for r in c.execute('SELECT state,COUNT(*) n FROM runs WHERE batch_id=? GROUP BY state',(b['id'],))}
                b['errors']=[dict(r) for r in c.execute('SELECT ordinal,attempt,error FROM runs WHERE batch_id=? AND error IS NOT NULL',(b['id'],))]
            totals=dict(c.execute('SELECT COUNT(*) calls,COALESCE(SUM(input_tokens),0) input_tokens,COALESCE(SUM(output_tokens),0) output_tokens,SUM(amount) known_amount,SUM(CASE WHEN amount IS NULL THEN 1 ELSE 0 END) unpriced FROM usage').fetchone())
            teams=[dict(r)|{'quotas':self.quotas(c,r['id'])} for r in c.execute("SELECT t.* FROM teams t JOIN users u ON u.team_id=t.id WHERE u.role='team'")]
            return {'versions':versions,'batches':batches,'teams':teams,'paused':json.loads(c.execute("SELECT value FROM settings WHERE key='paused'").fetchone()[0]),'slots':self.settings.slots,'usage':totals,'usage_rows':[dict(r) for r in c.execute('SELECT * FROM usage ORDER BY created DESC LIMIT 100')],'audit':[dict(r) for r in c.execute('SELECT * FROM audit ORDER BY created DESC LIMIT 100')],'appeals':[dict(r) for r in c.execute('SELECT a.*,t.name team FROM appeals a JOIN teams t ON t.id=a.team_id ORDER BY created DESC')],'outbox':[dict(r) for r in c.execute('SELECT * FROM outbox ORDER BY created DESC LIMIT 25')],'disk_free_bytes':shutil.disk_usage(self.root).free,'storage':self.store.provider,'e2b':'configured' if os.environ.get('E2B_API_KEY') else 'not_configured','ai_review':'manual_fallback','model':self.settings.model,'real_cloud_verified':self.store.r2,'pricing':self.pricing(c)}

    def mint_token(self,lease):
        token='qr_'+secrets.token_urlsafe(32)
        with self.db(True) as c:
            self.check_lease(c,lease)
            c.execute('INSERT INTO run_tokens VALUES(?,?,?,?,?,0)',(token_hash(token),lease['run_id'],lease['attempt'],self.settings.model,self.clock()+600))
        return token

    def authorize_model(self,token,model):
        with self.db() as c:
            r=c.execute("SELECT t.* FROM run_tokens t JOIN runs r ON r.id=t.run_id JOIN batches b ON b.id=r.batch_id WHERE t.hash=? AND t.revoked=0 AND t.expires>? AND t.attempt=r.attempt AND r.state='running' AND b.status='testing'",(token_hash(token),self.clock())).fetchone()
            if not r or r['model']!=model: raise Problem('Invalid run capability',403)
            if c.execute('SELECT COUNT(*) FROM usage WHERE run_id=? AND attempt=?',(r['run_id'],r['attempt'])).fetchone()[0]>=100: raise Problem('Run request limit exceeded',429)
            return dict(r)

    def pricing(self,c):
        r=c.execute("SELECT value FROM settings WHERE key='pricing'").fetchone()
        return json.loads(r[0]) if r else None

    def estimated_amount(self,pricing,inp,out,cached):
        if not pricing or inp is None or out is None: return None
        cached=max(0,min(inp,cached or 0))
        return float((Decimal(str(inp-cached))*Decimal(str(pricing['input_per_million']))+Decimal(str(cached))*Decimal(str(pricing['cached_per_million']))+Decimal(str(out))*Decimal(str(pricing['output_per_million'])))/1000000)

    def set_pricing(self,user_id,rates,actor,reason):
        keys=['input_per_million','cached_per_million','output_per_million']
        if not isinstance(rates,dict) or any(type(rates.get(k)) not in [int,float] or not math.isfinite(rates[k]) or not 0<=rates[k]<=1000000 for k in keys):
            raise Problem('请输入非负、有限的每百万 token 美元单价',400)
        pricing={k:rates[k] for k in keys}|{'version':uid(),'model':self.settings.model,'currency':'USD','basis':'operator-estimate','created':self.clock()}
        with self.db(True) as c:
            before=self.pricing(c)
            self.record(c,user_id,actor,'pricing_update',self.settings.model,reason,before,pricing)
            c.execute("INSERT OR REPLACE INTO settings VALUES('pricing',?)",(json.dumps(pricing),))
            for r in c.execute('SELECT * FROM usage WHERE amount IS NULL AND model=?',(self.settings.model,)).fetchall():
                amount=self.estimated_amount(pricing,r['input_tokens'],r['output_tokens'],r['cached_tokens'])
                if amount is not None: c.execute('UPDATE usage SET amount=?,price_version=? WHERE id=?',(amount,pricing['version'],r['id']))

    def record_usage(self,grant,status,usage=None):
        usage=usage or {}
        inp=usage.get('input_tokens',usage.get('prompt_tokens'))
        out=usage.get('output_tokens',usage.get('completion_tokens'))
        cached=(usage.get('input_tokens_details') or usage.get('prompt_tokens_details') or {}).get('cached_tokens')
        with self.db(True) as c:
            pricing=self.pricing(c)
            amount=self.estimated_amount(pricing,inp,out,cached)
            c.execute('INSERT INTO usage VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',(uid(),grant['run_id'],grant['attempt'],grant['model'],self.clock(),status,inp,out,cached,amount,pricing['version'] if amount is not None else None,'real-model-gateway'))

    def backup(self,destination):
        destination=Path(destination); destination.mkdir(parents=True,exist_ok=False,mode=0o700)
        with sqlite3.connect(self.db_path) as source,sqlite3.connect(destination/'platform.db') as target: source.backup(target)
        shutil.copytree(self.root/'uploads',destination/'uploads')
        # Demo substitute objects are included only for local restore testing. No production rollout backup to VPS.
        shutil.copytree(self.root/'objects',destination/'objects')
        (destination/'manifest.json').write_text(json.dumps({'mode':'local-demo-restore-only','created':self.clock(),'db_sha':digest(destination/'platform.db')}))
        return destination

    @staticmethod
    def restore(backup,destination):
        backup=Path(backup); destination=Path(destination)
        if destination.exists(): raise ValueError('Restore destination must not exist')
        if digest(backup/'platform.db')!=json.loads((backup/'manifest.json').read_text())['db_sha']: raise ValueError('Backup integrity failed')
        shutil.copytree(backup,destination)


def zipfile_error():
    import zipfile
    return zipfile.BadZipFile
