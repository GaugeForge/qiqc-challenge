package notifications

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"
	_ "github.com/mattn/go-sqlite3"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"
)

func testDB(t *testing.T) *sql.DB {
	t.Helper()
	db, err := sql.Open("sqlite3", filepath.Join(t.TempDir(), "notice.db")+"?_busy_timeout=30000&_txlock=immediate&_foreign_keys=on")
	if err != nil {
		t.Fatal(err)
	}
	db.SetMaxOpenConns(16)
	t.Cleanup(func() { db.Close() })
	_, err = db.Exec(`CREATE TABLE outbox(id TEXT PRIMARY KEY,event TEXT,team_id TEXT,payload TEXT,status TEXT DEFAULT 'local_only',created REAL);CREATE TABLE users(id TEXT PRIMARY KEY,email TEXT,team_id TEXT,role TEXT);CREATE TABLE ledger(id TEXT PRIMARY KEY,delta INTEGER);CREATE TABLE batches(id TEXT PRIMARY KEY,status TEXT);`)
	if err != nil {
		t.Fatal(err)
	}
	return db
}
func event(t *testing.T, db *sql.DB, team, id, kind string) {
	t.Helper()
	if _, err := db.Exec("INSERT OR IGNORE INTO users VALUES(?,?,?,'team')", team, team+"@example.test", team); err != nil {
		t.Fatal(err)
	}
	if _, err := db.Exec("INSERT INTO outbox(id,event,team_id,payload,created) VALUES(?,?,?,?,'1')", id, kind, team, `{"password":"TOP_SECRET","hidden_task":"DO_NOT_SEND","other_email":"victim@example.test"}`); err != nil {
		t.Fatal(err)
	}
}
func newFake(t *testing.T, db *sql.DB, now *time.Time, sink *FakeTransport) *Dispatcher {
	t.Helper()
	d, err := New(db, Config{Mode: Fake, Now: func() time.Time { return *now }}, sink)
	if err != nil {
		t.Fatal(err)
	}
	return d
}
func scalar(t *testing.T, db *sql.DB, q string, args ...any) string {
	t.Helper()
	var value string
	if err := db.QueryRow(q, args...).Scan(&value); err != nil {
		t.Fatal(err)
	}
	return value
}
func TestDisabledDefaultNeverCallsTransportAndDeduplicatesIntent(t *testing.T) {
	db := testDB(t)
	event(t, db, "one", "event-1", "account_ready")
	sink := &FakeTransport{BeforeSend: func(Message) error { panic("local_only attempted send") }}
	d, err := New(db, Config{}, sink)
	if err != nil {
		t.Fatal(err)
	}
	for i := 0; i < 3; i++ {
		if err = d.Cycle(context.Background()); err != nil {
			t.Fatal(err)
		}
	}
	stats, err := d.Statistics(context.Background())
	if err != nil || stats.Counts[LocalOnly] != 1 || stats.RealMailEnabled || sink.AttemptCount() != 0 {
		t.Fatal(stats, err)
	}
	if scalar(t, db, "SELECT status FROM outbox") != LocalOnly {
		t.Fatal("legacy status mutated")
	}
	for _, mode := range []string{"smtp", "production", "live"} {
		if _, err = New(db, Config{Mode: mode}, sink); err == nil {
			t.Fatal("real transport accepted")
		}
	}
}
func TestFakeDeliveryUsesOnlyOneTeamRecipientAndSafeVersionedTemplate(t *testing.T) {
	db := testDB(t)
	event(t, db, "one", "event-1", "evaluation_completed")
	event(t, db, "two", "event-2", "evaluation_completed")
	now := time.Unix(1000, 0)
	sink := &FakeTransport{}
	d := newFake(t, db, &now, sink)
	if err := d.Cycle(context.Background()); err != nil {
		t.Fatal(err)
	}
	messages := sink.Accepted()
	if len(messages) != 2 {
		t.Fatal("missing fake delivery")
	}
	for _, m := range messages {
		raw, _ := json.Marshal(m)
		for _, secret := range []string{"TOP_SECRET", "DO_NOT_SEND", "victim@example.test"} {
			if strings.Contains(string(raw), secret) {
				t.Fatal("private payload leaked")
			}
		}
		other := "one@example.test"
		if m.To == other {
			other = "two@example.test"
		}
		if strings.Contains(string(raw), other) {
			t.Fatal("other recipient leaked")
		}
		if !strings.Contains(m.Text, "UTC+8") || !strings.Contains(m.Text, "请登录") {
			t.Fatal("template incomplete")
		}
	}
	if scalar(t, db, "SELECT COUNT(*) FROM notification_deliveries WHERE status='fake_delivered'") != "2" {
		t.Fatal("fake mislabeled")
	}
}
func TestRetryBackoffDeadLetterDoesNotRollBackScoreOrQuota(t *testing.T) {
	db := testDB(t)
	event(t, db, "one", "event-1", "evaluation_completed")
	if _, err := db.Exec("INSERT INTO ledger VALUES('charged',-1); INSERT INTO batches VALUES('batch','completed')"); err != nil {
		t.Fatal(err)
	}
	now := time.Unix(1000, 0)
	sink := &FakeTransport{BeforeSend: func(Message) error { return errors.New("provider SECRET and victim@example.test") }}
	d := newFake(t, db, &now, sink)
	if err := d.Cycle(context.Background()); err != nil {
		t.Fatal(err)
	}
	for _, delay := range []time.Duration{5 * time.Second, 10 * time.Second, 20 * time.Second} {
		if worked, err := d.DispatchOne(context.Background()); err != nil || worked {
			t.Fatal("backoff ignored", err)
		}
		now = now.Add(delay)
		if worked, err := d.DispatchOne(context.Background()); err != nil || !worked {
			t.Fatal(err)
		}
	}
	if sink.AttemptCount() != 4 || scalar(t, db, "SELECT status FROM notification_deliveries") != "dead_letter" {
		t.Fatal("unbounded retry")
	}
	now = now.Add(time.Hour)
	if worked, err := d.DispatchOne(context.Background()); err != nil || worked {
		t.Fatal("dead letter resent")
	}
	if scalar(t, db, "SELECT last_error FROM notification_deliveries") != "temporary" {
		t.Fatal("raw error stored")
	}
	if scalar(t, db, "SELECT delta FROM ledger") != "-1" || scalar(t, db, "SELECT status FROM batches") != "completed" {
		t.Fatal("mail altered business state")
	}
}
func TestRestartAfterProviderAcceptFencesOldWorkerAndReusesDeliveryKey(t *testing.T) {
	db := testDB(t)
	event(t, db, "one", "event-1", "evaluation_accepted")
	now := time.Unix(1000, 0)
	sink := &FakeTransport{}
	d := newFake(t, db, &now, sink)
	ctx := context.Background()
	if _, err := d.Ingest(ctx, 100); err != nil {
		t.Fatal(err)
	}
	old, err := d.Claim(ctx)
	if err != nil || old == nil {
		t.Fatal(err)
	}
	message, err := d.message(ctx, *old)
	if err != nil {
		t.Fatal(err)
	}
	if err = sink.Send(ctx, message); err != nil {
		t.Fatal(err)
	}
	now = now.Add(31 * time.Second)
	restarted := newFake(t, db, &now, sink)
	if count, err := restarted.Recover(ctx); err != nil || count != 1 {
		t.Fatal(count, err)
	}
	if accepted, err := d.Finish(ctx, *old, nil); err != nil || accepted {
		t.Fatal("stale write accepted")
	}
	now = now.Add(5 * time.Second)
	current, err := restarted.Claim(ctx)
	if err != nil || current == nil {
		t.Fatal(err)
	}
	if current.Key != old.Key || current.Owner == old.Owner || current.Attempt != 2 {
		t.Fatal("broken retry identity")
	}
	message, err = restarted.message(ctx, *current)
	if err != nil {
		t.Fatal(err)
	}
	if err = sink.Send(ctx, message); err != nil {
		t.Fatal(err)
	}
	if accepted, err := restarted.Finish(ctx, *current, nil); err != nil || !accepted {
		t.Fatal(err)
	}
	if len(sink.Accepted()) != 1 || sink.AttemptCount() != 2 {
		t.Fatal("deduplication failed")
	}
}
func TestMalformedRecipientUnknownEventAndPermanentBounceArePrivateDeadLetters(t *testing.T) {
	for _, kind := range []string{"recipient", "event", "bounce", "panic"} {
		t.Run(kind, func(t *testing.T) {
			db := testDB(t)
			eventName := "account_ready"
			if kind == "event" {
				eventName = "unknown"
			}
			event(t, db, "one", "event-1", eventName)
			if kind == "recipient" {
				db.Exec("UPDATE users SET email='one@example.test\r\nBcc: victim@example.test'")
			}
			sink := &FakeTransport{BeforeSend: func(Message) error {
				if kind == "panic" {
					panic("secret details")
				}
				return fmt.Errorf("private bounce: %w", ErrPermanent)
			}}
			now := time.Unix(1000, 0)
			d := newFake(t, db, &now, sink)
			if err := d.Cycle(context.Background()); err != nil {
				t.Fatal(err)
			}
			want := "dead_letter"
			if kind == "panic" {
				want = "retry"
			}
			if scalar(t, db, "SELECT status FROM notification_deliveries") != want {
				t.Fatal("bad failure state")
			}
			last := scalar(t, db, "SELECT last_error FROM notification_deliveries")
			if last != "permanent" && last != "temporary" {
				t.Fatal("private error stored")
			}
		})
	}
}
func TestTwoHundredTeamInvitationAndStartBurst(t *testing.T) {
	db := testDB(t)
	for i := 0; i < 200; i++ {
		team := fmt.Sprintf("team-%03d", i)
		event(t, db, team, "invite-"+team, "invitation")
		event(t, db, team, "start-"+team, "competition_started")
	}
	now := time.Unix(1000, 0)
	sink := &FakeTransport{}
	d := newFake(t, db, &now, sink)
	ctx := context.Background()
	started := time.Now()
	var wg sync.WaitGroup
	errs := make(chan error, 8)
	for i := 0; i < 8; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			if _, err := d.Ingest(ctx, 1000); err != nil {
				errs <- err
				return
			}
			for {
				worked, err := d.DispatchOne(ctx)
				if err != nil {
					errs <- err
					return
				}
				if !worked {
					return
				}
			}
		}()
	}
	wg.Wait()
	close(errs)
	for err := range errs {
		t.Fatal(err)
	}
	stats, err := d.Statistics(ctx)
	if err != nil || stats.Counts["fake_delivered"] != 400 || len(sink.Accepted()) != 400 || sink.AttemptCount() != 400 {
		t.Fatal(stats, err, sink.AttemptCount())
	}
	perRecipient := map[string]int{}
	for _, m := range sink.Accepted() {
		perRecipient[m.To]++
	}
	if len(perRecipient) != 200 {
		t.Fatal("recipient missing")
	}
	for _, n := range perRecipient {
		if n != 2 {
			t.Fatal("duplicate recipient notice")
		}
	}
	t.Logf("200 teams, 400 fake notices, 8 dispatchers, %s; no network transport", time.Since(started))
}
