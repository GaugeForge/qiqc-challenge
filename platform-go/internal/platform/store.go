// Package platform owns native Go business modules over the shared migration database.
package platform

import (
	"context"
	"crypto/rand"
	"crypto/sha256"
	"database/sql"
	_ "embed"
	"encoding/hex"
	"errors"
	"fmt"
	_ "github.com/mattn/go-sqlite3"
	"net/url"
	"os"
	"path/filepath"
	"time"
)

//go:embed schema.sql
var schema string

type Store struct {
	ClockHealthPath              string
	UploadReserveBytes           uint64
	DiskProbe                    func(string) (DiskSpace, error)
	Objects                      ObjectStore
	Model                        string
	ScienceHashes                map[string]bool
	scientificAgentBudgetSeconds int
	BaselinePath                 string
	Scanner                      func(string) (Report, error)
	DB                           *sql.DB
	Path                         string
	Root                         string
	Now                          func() time.Time
	SecureCookies                bool
	Assistant                    *Assistant
	authSlots                    chan struct{}
	uploadSlots                  chan struct{}
}

type Problem struct {
	Code    int
	Message string
}

func (p *Problem) Error() string             { return p.Message }
func problem(code int, message string) error { return &Problem{code, message} }
func status(err error) int {
	if err == nil {
		return 200
	}
	var p *Problem
	if errors.As(err, &p) {
		return p.Code
	}
	return 503
}

// Open retains a single source of truth shared with the legacy worker. FULL
// synchronous commits, busy timeout and immediate write transactions apply to
// every connection, not just the first one in the pool.
func Open(path string) (*Store, error) {
	reserve, configErr := configuredUploadReserve()
	if configErr != nil {
		return nil, configErr
	}
	absolute, err := filepath.Abs(path)
	if err != nil {
		return nil, err
	}
	root := filepath.Dir(absolute)
	if err = ensureDataDirectory(root); err != nil {
		return nil, err
	}
	if info, e := os.Lstat(absolute); e == nil && info.Mode()&os.ModeSymlink != 0 {
		return nil, errors.New("database symlink refused")
	}
	if err = prepareDatabaseFile(absolute); err != nil {
		return nil, err
	}
	u := url.URL{Scheme: "file", Path: absolute}
	db, err := sql.Open("sqlite3", u.String()+"?_busy_timeout=30000&_foreign_keys=on&_synchronous=FULL&_journal_mode=WAL&_txlock=immediate")
	if err != nil {
		return nil, err
	}
	db.SetMaxOpenConns(8)
	// The Python worker and the Go worker can mutate the same SQLite WAL file
	// from separate processes. Do not retain an idle connection across that
	// boundary: an idle SQLite connection may keep an old schema/data snapshot
	// and make a long-lived Go controller miss a freshly committed Python run.
	db.SetMaxIdleConns(0)
	s := &Store{UploadReserveBytes: reserve, DB: db, Path: absolute, Root: root, Now: time.Now, Model: "gpt-5.6-sol", authSlots: make(chan struct{}, 2), uploadSlots: make(chan struct{}, 4)}
	if _, err = db.Exec(schema); err != nil {
		db.Close()
		return nil, err
	}
	if err = s.ensureScientificTaskSetSchema(); err != nil {
		db.Close()
		return nil, err
	}
	if err = s.ensureScientificBudgetSchema(); err != nil {
		db.Close()
		return nil, err
	}
	if err = s.ensureNativeBudgetSchema(); err != nil {
		db.Close()
		return nil, err
	}
	if err = s.ensureReviewSchema(); err != nil {
		db.Close()
		return nil, err
	}
	// Old Python databases may predate the two additive audit context columns.
	for _, column := range []struct{ table, name, kind string }{
		{"audit", "session_fingerprint", "TEXT"}, {"audit", "ip", "TEXT"},
		{"usage", "request_id", "TEXT"}, {"usage", "request_correlation", "TEXT"}, {"usage", "upstream_attempt", "INTEGER"}, {"usage", "error_class", "TEXT"},
		{"runs", "failure_category", "TEXT"}, {"runs", "failure_phase", "TEXT"}, {"runs", "failure_code", "TEXT"},
	} {
		name := column.name
		rows, e := db.Query("PRAGMA table_info(" + column.table + ")")
		if e != nil {
			db.Close()
			return nil, e
		}
		found := false
		for rows.Next() {
			var cid, nn, pk int
			var col, typ string
			var def any
			if e = rows.Scan(&cid, &col, &typ, &nn, &def, &pk); e != nil {
				rows.Close()
				db.Close()
				return nil, e
			}
			if col == name {
				found = true
			}
		}
		e = rows.Err()
		rows.Close()
		if e != nil {
			db.Close()
			return nil, e
		}
		if !found {
			if _, e = db.Exec("ALTER TABLE " + column.table + " ADD COLUMN " + name + " " + column.kind); e != nil {
				db.Close()
				return nil, e
			}
		}
	}
	if err = prepareDatabaseFile(absolute); err != nil {
		db.Close()
		return nil, err
	}
	return s, nil
}
func (s *Store) Close() error { return s.DB.Close() }
func (s *Store) now() float64 { return float64(s.Now().UnixNano()) / 1e9 }
func id() string {
	var b [16]byte
	if _, err := rand.Read(b[:]); err != nil {
		panic(err)
	}
	return hex.EncodeToString(b[:])
}
func tokenHash(token string) string {
	h := sha256.Sum256([]byte(token))
	return hex.EncodeToString(h[:])
}

type queryer interface {
	Query(string, ...any) (*sql.Rows, error)
	QueryRow(string, ...any) *sql.Row
}

func (s *Store) write(fn func(*sql.Tx) error) error {
	tx, err := s.DB.BeginTx(context.Background(), nil)
	if err != nil {
		return err
	}
	defer tx.Rollback()
	if err = fn(tx); err != nil {
		return err
	}
	return tx.Commit()
}
func (s *Store) audit(tx *sql.Tx, u User, token, ip, actor, action, target, reason, before, after string) error {
	if u.Role != "admin" || u.MustChange != 0 {
		return problem(403, "仅完成改密的管理员可访问")
	}
	if len(actor) == 0 || len([]rune(reason)) < 3 {
		return problem(400, "请输入操作者和至少 3 个字符的操作理由")
	}
	_, err := tx.Exec(`INSERT INTO audit(id,created,user_id,actor,action,object_id,reason,before_json,after_json,session_fingerprint,ip) VALUES(?,?,?,?,?,?,?,?,?,?,?)`, id(), s.now(), u.ID, actor, action, target, reason, before, after, tokenHash(token)[:16], ip)
	return err
}
func rowError(err error) error {
	if errors.Is(err, sql.ErrNoRows) {
		return problem(401, "会话已失效，请重新登录")
	}
	return err
}
func (s *Store) Check() error {
	var n int
	if err := s.DB.QueryRow("SELECT COUNT(*) FROM settings").Scan(&n); err != nil {
		return err
	}
	if n < 2 {
		return fmt.Errorf("missing platform settings")
	}
	return nil
}
