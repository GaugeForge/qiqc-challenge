package platform

import (
	"context"
	"errors"
	"github.com/gaugeforge/qiqc-platform-go/internal/rollout"
	"os"
	"path/filepath"
	"testing"
	"time"
)

type brokenObjects struct {
	*rollout.Store
	broken bool
}

func (b *brokenObjects) PutFile(c context.Context, k, p string) (rollout.Info, error) {
	if b.broken {
		return rollout.Info{}, errors.New("injected storage outage")
	}
	return b.Store.PutFile(c, k, p)
}
func TestGoCompleteUploadOutageRestartWithoutRerunOrDebit(t *testing.T) {
	s := testStore(t)
	tok, v := approved(t, s)
	objects := &brokenObjects{&rollout.Store{Root: filepath.Join(s.Root, "objects")}, true}
	s.Objects = objects
	b, err := s.Confirm(tok, v.ID, "infra", "simulated", "request-1")
	if err != nil {
		t.Fatal(err)
	}
	l, err := s.Claim("worker", 4)
	if err != nil {
		t.Fatal(err)
	}
	if err = s.CompleteFixture(context.Background(), *l, 0, "1.0000005", "safe public view"); err == nil {
		t.Fatal("outage hidden")
	}
	cache := filepath.Join(s.Root, "cache", l.RunID, "1", "rollout.tar.gz")
	sha, _, err := rollout.Digest(cache)
	if err != nil {
		t.Fatal(err)
	}
	restarted, err := Open(s.Path)
	if err != nil {
		t.Fatal(err)
	}
	defer restarted.Close()
	objects.broken = false
	restarted.Objects = objects
	restarted.Now = func() time.Time { return time.Now().Add(5 * time.Minute) }
	count, err := restarted.ResumeUploads(context.Background())
	if err != nil || count != 1 {
		t.Fatalf("resume %d %v", count, err)
	}
	after, err := restarted.GetBatch(tok, b["id"].(string))
	if err != nil || after["status"] != "completed" {
		t.Fatal("not completed", err)
	}
	u, _ := restarted.Session(tok)
	if u.Quotas["infra"] != 9 {
		t.Fatal("duplicate quota")
	}
	var attempts int
	var remoteSHA string
	s.DB.QueryRow("SELECT COUNT(*) FROM attempts").Scan(&attempts)
	s.DB.QueryRow("SELECT object_sha FROM runs WHERE id=?", l.RunID).Scan(&remoteSHA)
	if attempts != 1 || sha != remoteSHA {
		t.Fatal("rerun or changed evidence")
	}
	if _, err = os.Stat(cache); !os.IsNotExist(err) {
		t.Fatal("cache not cleaned")
	}
	if _, err = restarted.Trace(context.Background(), tok, b["id"].(string), 0); status(err) != 404 {
		t.Fatal("infra trace must remain hidden", err)
	}
	if _, err = restarted.Trace(context.Background(), tok, b["id"].(string), 5); status(err) != 404 {
		t.Fatal("hidden trace")
	}
	if err = restarted.CompleteFixture(context.Background(), *l, 1, "1", "fake"); status(err) != 409 {
		t.Fatal("stale completion")
	}
	var notifications int
	s.DB.QueryRow("SELECT COUNT(*) FROM outbox WHERE event='evaluation_completed'").Scan(&notifications)
	if notifications != 1 {
		t.Fatal("duplicate completion notification")
	}
}
