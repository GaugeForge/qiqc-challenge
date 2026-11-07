// Package notifications processes durable notification intentions separately
// from competition transactions. It ships no network mail transport.
package notifications

import (
	"context"
	"crypto/rand"
	"crypto/sha256"
	"database/sql"
	"encoding/hex"
	"errors"
	"fmt"
	"net/mail"
	"strings"
	"time"
)

const TemplateVersion = "qiqc-notice/1"
const LocalOnly = "local_only"
const Fake = "fake"

var ErrTemporary = errors.New("temporary notification failure")
var ErrPermanent = errors.New("permanent notification failure")

type Config struct {
	Mode          string
	MaxAttempts   int
	LeaseDuration time.Duration
	SendTimeout   time.Duration
	RetryBase     time.Duration
	RetryMax      time.Duration
	Now           func() time.Time
}

type Message struct {
	Key     string
	To      string
	Event   string
	Subject string
	Text    string
}

// Transport must provide durable idempotence for Key before any real provider
// can be approved. Only the in-memory FakeTransport exists in this revision.
type Transport interface {
	Kind() string
	Send(context.Context, Message) error
}

type Dispatcher struct {
	db        *sql.DB
	config    Config
	transport Transport
}

type Claim struct {
	EventID         string
	Event           string
	TeamID          string
	TemplateVersion string
	Key             string
	Owner           string
	Attempt         int
	LeaseUntil      float64
}

type Statistics struct {
	Mode            string         `json:"mode"`
	Counts          map[string]int `json:"counts"`
	RealMailEnabled bool           `json:"real_mail_enabled"`
}

var templates = map[string]string{
	"account_ready":        "QIQC account ready / 账号已开通",
	"invitation":           "QIQC invitation / 参赛邀请",
	"password_reset":       "QIQC password update / 密码已更新",
	"password_changed":     "QIQC password changed / 密码已修改",
	"review_result":        "QIQC review result / 提交审查结果",
	"evaluation_accepted":  "QIQC evaluation accepted / 评测已受理",
	"evaluation_completed": "QIQC evaluation complete / 评测已完成",
	"platform_incident":    "QIQC service incident / 平台故障通知",
	"platform_recovered":   "QIQC service restored / 平台服务恢复",
	"competition_started":  "QIQC competition started / 比赛开始",
	"competition_closed":   "QIQC competition closed / 比赛结束",
}

func New(db *sql.DB, config Config, transport Transport) (*Dispatcher, error) {
	if db == nil {
		return nil, errors.New("notification database required")
	}
	if config.Mode == "" {
		config.Mode = LocalOnly
	}
	if config.Mode != LocalOnly && config.Mode != Fake {
		return nil, errors.New("real mail transport is not configured or approved")
	}
	if config.Mode == Fake && (transport == nil || transport.Kind() != Fake) {
		return nil, errors.New("explicit fake transport required")
	}
	if config.MaxAttempts == 0 {
		config.MaxAttempts = 4
	}
	if config.MaxAttempts < 1 || config.MaxAttempts > 10 {
		return nil, errors.New("invalid notification attempt limit")
	}
	if config.LeaseDuration == 0 {
		config.LeaseDuration = 30 * time.Second
	}
	if config.SendTimeout == 0 {
		config.SendTimeout = 10 * time.Second
	}
	if config.RetryBase == 0 {
		config.RetryBase = 5 * time.Second
	}
	if config.RetryMax == 0 {
		config.RetryMax = 5 * time.Minute
	}
	if config.LeaseDuration <= 0 || config.SendTimeout <= 0 || config.SendTimeout >= config.LeaseDuration || config.RetryBase <= 0 || config.RetryMax < config.RetryBase {
		return nil, errors.New("invalid notification timing")
	}
	if config.Now == nil {
		config.Now = time.Now
	}
	_, err := db.Exec(`CREATE TABLE IF NOT EXISTS notification_deliveries (
 event_id TEXT NOT NULL REFERENCES outbox(id), template_version TEXT NOT NULL,
 mode TEXT NOT NULL, status TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
 next_attempt REAL NOT NULL DEFAULT 0, owner TEXT NOT NULL DEFAULT '',
 lease_until REAL NOT NULL DEFAULT 0, last_error TEXT NOT NULL DEFAULT '',
 created REAL NOT NULL, updated REAL NOT NULL,
 PRIMARY KEY(event_id,template_version,mode));
 CREATE INDEX IF NOT EXISTS notification_ready ON notification_deliveries(mode,status,next_attempt);`)
	if err != nil {
		return nil, err
	}
	return &Dispatcher{db: db, config: config, transport: transport}, nil
}
func (d *Dispatcher) now() float64 { return float64(d.config.Now().UnixNano()) / 1e9 }
func deliveryKey(event, version string) string {
	sum := sha256.Sum256([]byte(event + "\x00" + version))
	return hex.EncodeToString(sum[:])
}
func ownerID() (string, error) {
	var bytes [16]byte
	_, err := rand.Read(bytes[:])
	return hex.EncodeToString(bytes[:]), err
}

// Ingest never reads outbox.payload: credentials, hidden task content and
// participant-supplied strings cannot enter notification rendering.
func (d *Dispatcher) Ingest(ctx context.Context, limit int) (int, error) {
	if limit < 1 || limit > 1000 {
		return 0, errors.New("invalid notification ingestion limit")
	}
	status := "pending"
	if d.config.Mode == LocalOnly {
		status = LocalOnly
	}
	result, err := d.db.ExecContext(ctx, `INSERT OR IGNORE INTO notification_deliveries(event_id,template_version,mode,status,created,updated)
 SELECT o.id,?,?,?, ?,? FROM outbox o WHERE NOT EXISTS
 (SELECT 1 FROM notification_deliveries n WHERE n.event_id=o.id AND n.template_version=? AND n.mode=?)
 ORDER BY o.created,o.id LIMIT ?`, TemplateVersion, d.config.Mode, status, d.now(), d.now(), TemplateVersion, d.config.Mode, limit)
	if err != nil {
		return 0, err
	}
	count, err := result.RowsAffected()
	return int(count), err
}

func (d *Dispatcher) backoff(attempt int) time.Duration {
	value := d.config.RetryBase
	for i := 1; i < attempt; i++ {
		if value >= d.config.RetryMax/2 {
			return d.config.RetryMax
		}
		value *= 2
	}
	if value > d.config.RetryMax {
		return d.config.RetryMax
	}
	return value
}

// Recover fences expired owners. A later retry reuses the provider key because
// the prior sender may have succeeded just before its process disappeared.
func (d *Dispatcher) Recover(ctx context.Context) (int, error) {
	if d.config.Mode == LocalOnly {
		return 0, nil
	}
	rows, err := d.db.QueryContext(ctx, `SELECT event_id,attempts FROM notification_deliveries WHERE mode=? AND status='sending' AND lease_until<=? LIMIT 1000`, d.config.Mode, d.now())
	if err != nil {
		return 0, err
	}
	type expired struct {
		id      string
		attempt int
	}
	all := []expired{}
	for rows.Next() {
		var item expired
		if err = rows.Scan(&item.id, &item.attempt); err != nil {
			rows.Close()
			return 0, err
		}
		all = append(all, item)
	}
	err = rows.Err()
	rows.Close()
	if err != nil {
		return 0, err
	}
	count := 0
	for _, item := range all {
		status := "retry"
		if item.attempt >= d.config.MaxAttempts {
			status = "dead_letter"
		}
		result, e := d.db.ExecContext(ctx, `UPDATE notification_deliveries SET status=?,owner='',lease_until=0,next_attempt=?,last_error='lease_expired',updated=?
 WHERE event_id=? AND template_version=? AND mode=? AND status='sending' AND lease_until<=?`, status, d.now()+d.backoff(item.attempt).Seconds(), d.now(), item.id, TemplateVersion, d.config.Mode, d.now())
		if e != nil {
			return count, e
		}
		n, _ := result.RowsAffected()
		count += int(n)
	}
	return count, nil
}

func (d *Dispatcher) Claim(ctx context.Context) (*Claim, error) {
	if d.config.Mode == LocalOnly {
		return nil, nil
	}
	owner, err := ownerID()
	if err != nil {
		return nil, err
	}
	tx, err := d.db.BeginTx(ctx, nil)
	if err != nil {
		return nil, err
	}
	defer tx.Rollback()
	c := Claim{Owner: owner, TemplateVersion: TemplateVersion}
	err = tx.QueryRowContext(ctx, `SELECT n.event_id,COALESCE(o.event,''),COALESCE(o.team_id,''),n.attempts FROM notification_deliveries n JOIN outbox o ON o.id=n.event_id
 WHERE n.mode=? AND n.template_version=? AND n.status IN ('pending','retry') AND n.next_attempt<=? AND n.attempts<? ORDER BY n.created,n.event_id LIMIT 1`, d.config.Mode, TemplateVersion, d.now(), d.config.MaxAttempts).Scan(&c.EventID, &c.Event, &c.TeamID, &c.Attempt)
	if errors.Is(err, sql.ErrNoRows) {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	c.Attempt++
	c.LeaseUntil = d.now() + d.config.LeaseDuration.Seconds()
	c.Key = deliveryKey(c.EventID, TemplateVersion)
	result, err := tx.ExecContext(ctx, `UPDATE notification_deliveries SET status='sending',attempts=?,owner=?,lease_until=?,updated=?
 WHERE event_id=? AND template_version=? AND mode=? AND status IN ('pending','retry') AND attempts=?`, c.Attempt, c.Owner, c.LeaseUntil, d.now(), c.EventID, TemplateVersion, d.config.Mode, c.Attempt-1)
	if err != nil {
		return nil, err
	}
	n, _ := result.RowsAffected()
	if n != 1 {
		return nil, nil
	}
	if err = tx.Commit(); err != nil {
		return nil, err
	}
	return &c, nil
}

func (d *Dispatcher) message(ctx context.Context, c Claim) (Message, error) {
	subject, ok := templates[c.Event]
	if !ok {
		return Message{}, ErrPermanent
	}
	rows, err := d.db.QueryContext(ctx, "SELECT email FROM users WHERE team_id=? AND role='team'", c.TeamID)
	if err != nil {
		return Message{}, err
	}
	defer rows.Close()
	recipients := []string{}
	for rows.Next() {
		var email string
		if err = rows.Scan(&email); err != nil {
			return Message{}, err
		}
		recipients = append(recipients, email)
	}
	if err = rows.Err(); err != nil {
		return Message{}, err
	}
	if len(recipients) != 1 {
		return Message{}, ErrPermanent
	}
	email := recipients[0]
	parsed, err := mail.ParseAddress(email)
	if err != nil || parsed.Address != email || strings.ContainsAny(email, "\r\n") {
		return Message{}, ErrPermanent
	}
	return Message{Key: c.Key, To: email, Event: c.Event, Subject: subject,
		Text: subject + "\nPlease sign in to the QIQC platform to view your team's details.\n请登录 QIQC 平台查看本队详情。\nTime zone / 时区: Asia/Singapore (UTC+8).\nThis notice contains no password or task data. 本通知不含密码或任务数据。"}, nil
}

func safeSend(ctx context.Context, transport Transport, message Message) (err error) {
	defer func() {
		if recover() != nil {
			err = ErrTemporary
		}
	}()
	return transport.Send(ctx, message)
}

func (d *Dispatcher) Finish(ctx context.Context, c Claim, sendErr error) (bool, error) {
	status, category, next := "fake_delivered", "", float64(0)
	if sendErr != nil {
		status, category = "retry", "temporary"
		if errors.Is(sendErr, ErrPermanent) {
			status, category = "dead_letter", "permanent"
		} else if c.Attempt >= d.config.MaxAttempts {
			status = "dead_letter"
		}
		next = d.now() + d.backoff(c.Attempt).Seconds()
	}
	result, err := d.db.ExecContext(ctx, `UPDATE notification_deliveries SET status=?,last_error=?,next_attempt=?,owner='',lease_until=0,updated=?
 WHERE event_id=? AND template_version=? AND mode=? AND status='sending' AND owner=? AND attempts=? AND lease_until>?`, status, category, next, d.now(), c.EventID, c.TemplateVersion, d.config.Mode, c.Owner, c.Attempt, d.now())
	if err != nil {
		return false, err
	}
	n, err := result.RowsAffected()
	return n == 1, err
}

// DispatchOne uses a distinct transaction for delivery, so provider failure can
// never undo the event, batch, score or quota transaction that created it.
func (d *Dispatcher) DispatchOne(ctx context.Context) (bool, error) {
	c, err := d.Claim(ctx)
	if err != nil || c == nil {
		return false, err
	}
	message, sendErr := d.message(ctx, *c)
	if sendErr == nil {
		sendCtx, cancel := context.WithTimeout(ctx, d.config.SendTimeout)
		sendErr = safeSend(sendCtx, d.transport, message)
		cancel()
	}
	finished, err := d.Finish(ctx, *c, sendErr)
	if err != nil {
		return true, err
	}
	if !finished {
		return true, fmt.Errorf("notification lease expired before completion")
	}
	return true, nil
}

func (d *Dispatcher) Statistics(ctx context.Context) (Statistics, error) {
	result := Statistics{Mode: d.config.Mode, Counts: map[string]int{}, RealMailEnabled: false}
	rows, err := d.db.QueryContext(ctx, "SELECT status,COUNT(*) FROM notification_deliveries WHERE mode=? GROUP BY status", d.config.Mode)
	if err != nil {
		return result, err
	}
	defer rows.Close()
	for rows.Next() {
		var status string
		var count int
		if err = rows.Scan(&status, &count); err != nil {
			return result, err
		}
		result.Counts[status] = count
	}
	return result, rows.Err()
}
