-- Compatible with the Python Demo schema; additive migrations only.

            CREATE TABLE IF NOT EXISTS teams(id TEXT PRIMARY KEY,name TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS users(id TEXT PRIMARY KEY,email TEXT UNIQUE NOT NULL,password TEXT NOT NULL,team_id TEXT REFERENCES teams(id),role TEXT NOT NULL,must_change INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS sessions(hash TEXT PRIMARY KEY,user_id TEXT REFERENCES users(id),expires REAL,ip TEXT);
            CREATE TABLE IF NOT EXISTS login_attempts(identity TEXT,created REAL);
            CREATE TABLE IF NOT EXISTS versions(id TEXT PRIMARY KEY,team_id TEXT REFERENCES teams(id),sha256 TEXT NOT NULL,name TEXT,status TEXT,report TEXT,reason TEXT,created REAL,UNIQUE(team_id,sha256));
            CREATE TABLE IF NOT EXISTS batches(id TEXT PRIMARY KEY,team_id TEXT REFERENCES teams(id),version_id TEXT REFERENCES versions(id),kind TEXT,mode TEXT,idem TEXT,status TEXT,accepted_at REAL,last_dispatch REAL DEFAULT 0,passed INTEGER,mean TEXT,invalid INTEGER DEFAULT 0,UNIQUE(team_id,idem));
            CREATE UNIQUE INDEX IF NOT EXISTS one_active_batch ON batches(team_id) WHERE status IN ('queued','testing','attention','cancelling');
            CREATE TABLE IF NOT EXISTS runs(id TEXT PRIMARY KEY,batch_id TEXT REFERENCES batches(id),ordinal INTEGER,public INTEGER,state TEXT,attempt INTEGER DEFAULT 0,owner TEXT,lease_until REAL,reward INTEGER,seconds TEXT,object_key TEXT,object_sha TEXT,error TEXT,failure_category TEXT,failure_phase TEXT,failure_code TEXT,UNIQUE(batch_id,ordinal));
            CREATE TABLE IF NOT EXISTS attempts(id TEXT PRIMARY KEY,run_id TEXT REFERENCES runs(id),number INTEGER,state TEXT,started REAL,finished REAL,error TEXT,UNIQUE(run_id,number));
            CREATE TABLE IF NOT EXISTS ledger(id TEXT PRIMARY KEY,team_id TEXT REFERENCES teams(id),kind TEXT,delta INTEGER,action TEXT,batch_id TEXT REFERENCES batches(id),reason TEXT,created REAL,UNIQUE(batch_id,action));
            CREATE TABLE IF NOT EXISTS audit(id TEXT PRIMARY KEY,created REAL,user_id TEXT,actor TEXT,action TEXT,object_id TEXT,reason TEXT,before_json TEXT,after_json TEXT,session_fingerprint TEXT,ip TEXT);
            CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY,value TEXT);
            INSERT OR IGNORE INTO settings VALUES('paused','false');
            INSERT OR IGNORE INTO settings VALUES('competition_closed','false');
            CREATE TABLE IF NOT EXISTS appeals(id TEXT PRIMARY KEY,team_id TEXT UNIQUE REFERENCES teams(id),body TEXT,status TEXT,created REAL,response TEXT);
            CREATE TABLE IF NOT EXISTS outbox(id TEXT PRIMARY KEY,event TEXT,team_id TEXT,payload TEXT,created REAL,status TEXT DEFAULT 'local_only');
            CREATE TABLE IF NOT EXISTS run_tokens(hash TEXT PRIMARY KEY,run_id TEXT,attempt INTEGER,model TEXT,expires REAL,revoked INTEGER DEFAULT 0);
            CREATE TABLE IF NOT EXISTS usage(id TEXT PRIMARY KEY,run_id TEXT,attempt INTEGER,model TEXT,created REAL,status INTEGER,input_tokens INTEGER,output_tokens INTEGER,cached_tokens INTEGER,amount REAL,price_version TEXT,source TEXT);

CREATE TABLE IF NOT EXISTS admin_operations(user_id TEXT NOT NULL REFERENCES users(id),operation_key TEXT NOT NULL,request_hash TEXT NOT NULL,created REAL NOT NULL,PRIMARY KEY(user_id,operation_key));

CREATE TABLE IF NOT EXISTS batch_policies(batch_id TEXT PRIMARY KEY REFERENCES batches(id),policy_sha256 TEXT NOT NULL,policy_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS calendar_versions(version TEXT PRIMARY KEY,sha256 TEXT NOT NULL,policy_json TEXT NOT NULL);

-- M11 local announcements/support. Published notices and thread messages are append-only.
CREATE TABLE IF NOT EXISTS announcements(id TEXT PRIMARY KEY,version TEXT NOT NULL UNIQUE,title TEXT NOT NULL,body TEXT NOT NULL,scope TEXT NOT NULL CHECK(scope IN ('public','teams','team')),team_id TEXT REFERENCES teams(id),prd_version TEXT NOT NULL,published REAL NOT NULL,created_by TEXT NOT NULL REFERENCES users(id));
CREATE INDEX IF NOT EXISTS announcements_visible ON announcements(scope,team_id,published);
CREATE TABLE IF NOT EXISTS support_tickets(id TEXT PRIMARY KEY,team_id TEXT NOT NULL REFERENCES teams(id),subject TEXT NOT NULL,batch_id TEXT REFERENCES batches(id),submission_sha TEXT,event_id TEXT REFERENCES outbox(id),status TEXT NOT NULL CHECK(status IN ('open','resolved','closed')),created REAL NOT NULL,updated REAL NOT NULL);
CREATE INDEX IF NOT EXISTS support_team_history ON support_tickets(team_id,updated);
CREATE TABLE IF NOT EXISTS support_messages(id TEXT PRIMARY KEY,ticket_id TEXT NOT NULL REFERENCES support_tickets(id),sequence INTEGER NOT NULL,author_id TEXT NOT NULL REFERENCES users(id),author_role TEXT NOT NULL CHECK(author_role IN ('team','admin')),body TEXT NOT NULL,created REAL NOT NULL,UNIQUE(ticket_id,sequence));
CREATE TABLE IF NOT EXISTS communication_operations(user_id TEXT NOT NULL REFERENCES users(id),operation_key TEXT NOT NULL,kind TEXT NOT NULL,request_hash TEXT NOT NULL,object_id TEXT NOT NULL,created REAL NOT NULL,PRIMARY KEY(user_id,operation_key));

-- Private operator acceptance selection. Immutable version and batch snapshots.
CREATE TABLE IF NOT EXISTS scientific_targets(version_id TEXT PRIMARY KEY REFERENCES versions(id),submission_sha256 TEXT NOT NULL,task_index INTEGER NOT NULL CHECK(task_index BETWEEN 0 AND 48),release_id TEXT NOT NULL,agent_budget_seconds INTEGER NOT NULL CHECK(agent_budget_seconds IN (180,300)),created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS batch_scientific_targets(batch_id TEXT PRIMARY KEY REFERENCES batches(id),submission_sha256 TEXT NOT NULL,task_index INTEGER NOT NULL CHECK(task_index BETWEEN 0 AND 48),release_id TEXT NOT NULL,agent_budget_seconds INTEGER NOT NULL CHECK(agent_budget_seconds IN (180,300)));

CREATE TABLE IF NOT EXISTS event_phase(version TEXT PRIMARY KEY,rank INTEGER NOT NULL,phase TEXT NOT NULL,observed REAL NOT NULL);

CREATE TABLE IF NOT EXISTS submission_intents(id TEXT PRIMARY KEY,user_id TEXT NOT NULL REFERENCES users(id),team_id TEXT NOT NULL REFERENCES teams(id),version_id TEXT NOT NULL REFERENCES versions(id),sha256 TEXT NOT NULL,kind TEXT NOT NULL,mode TEXT NOT NULL,phase TEXT NOT NULL,idem TEXT NOT NULL,state TEXT NOT NULL,reason TEXT NOT NULL DEFAULT '',batch_id TEXT REFERENCES batches(id),created REAL NOT NULL,updated REAL NOT NULL,UNIQUE(team_id,idem));
