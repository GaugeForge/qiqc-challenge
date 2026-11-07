package platform

import (
	"database/sql"
	"sync"
	"testing"
)

func approved(t *testing.T, s *Store) (string, Version) {
	t.Helper()
	withScanner(s)
	tok := readyUser(t, s, "a@example.test", "team")
	v, err := s.Upload(tok, []byte("reviewed test bytes"))
	if err != nil {
		t.Fatal(err)
	}
	_, err = s.DB.Exec("UPDATE versions SET status='approved' WHERE id=?", v.ID)
	if err != nil {
		t.Fatal(err)
	}
	return tok, v
}
func TestConfirm32ConcurrentExactlyOneLedgerAnd39Tasks(t *testing.T) {
	s := testStore(t)
	tok, v := approved(t, s)
	var wg sync.WaitGroup
	ids := make(chan string, 32)
	for i := 0; i < 32; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			b, err := s.Confirm(tok, v.ID, "full", "simulated", "same-key")
			if err != nil {
				t.Error(err)
				return
			}
			ids <- b["id"].(string)
		}()
	}
	wg.Wait()
	close(ids)
	seen := map[string]bool{}
	for id := range ids {
		seen[id] = true
	}
	if len(seen) != 1 {
		t.Fatalf("batch count %d", len(seen))
	}
	var runs, ledger int
	s.DB.QueryRow("SELECT COUNT(*) FROM runs").Scan(&runs)
	s.DB.QueryRow("SELECT COUNT(*) FROM ledger").Scan(&ledger)
	if runs != 39 || ledger != 1 {
		t.Fatalf("runs=%d ledger=%d", runs, ledger)
	}
	u, _ := s.Session(tok)
	if u.Quotas["full"] != 0 {
		t.Fatal("duplicate debit")
	}
	if _, err := s.Confirm(tok, v.ID, "public", "simulated", "same-key"); status(err) != 409 {
		t.Fatal("key conflict accepted")
	}
	if _, err := s.Confirm(tok, v.ID, "public", "simulated", "other-key"); status(err) != 409 {
		t.Fatal("active batch not exclusive")
	}
}
func TestCancellationClosureAndCrossTeamPrivacy(t *testing.T) {
	s := testStore(t)
	tok, v := approved(t, s)
	other := readyUser(t, s, "b@example.test", "team")
	b, err := s.Confirm(tok, v.ID, "public", "simulated", "request-1")
	if err != nil {
		t.Fatal(err)
	}
	bid := b["id"].(string)
	if _, err = s.GetBatch(other, bid); status(err) != 404 {
		t.Fatal("cross-team read")
	}
	if err = s.Cancel(other, bid); status(err) != 404 {
		t.Fatal("cross-team cancel")
	}
	if err = s.Cancel(tok, bid); err != nil {
		t.Fatal(err)
	}
	after, _ := s.GetBatch(tok, bid)
	if after["status"] != "cancelled" || after["score"] != nil {
		t.Fatal("cancel state")
	}
	u, _ := s.Session(tok)
	if u.Quotas["public"] != 3 {
		t.Fatal("queued cancellation did not return the attempt")
	}
	s.DB.Exec("UPDATE settings SET value='true' WHERE key='competition_closed'")
	if _, err = s.Confirm(tok, v.ID, "public", "simulated", "request-2"); status(err) != 409 {
		t.Fatal("closed accepted")
	}
	again, err := s.Confirm(tok, v.ID, "public", "simulated", "request-1")
	if err != nil || again["id"] != bid {
		t.Fatal("closed lost accepted receipt")
	}
}
func TestRunningCancelRevokesTokensWithoutReleasingUntilCleanup(t *testing.T) {
	s := testStore(t)
	tok, v := approved(t, s)
	b, _ := s.Confirm(tok, v.ID, "infra", "simulated", "request-1")
	bid := b["id"].(string)
	s.DB.Exec("UPDATE runs SET state='running' WHERE batch_id=?", bid)
	s.DB.Exec("INSERT INTO run_tokens SELECT 'hash',id,1,'model',9999999999,0 FROM runs WHERE batch_id=?", bid)
	if err := s.Cancel(tok, bid); err != nil {
		t.Fatal(err)
	}
	after, _ := s.GetBatch(tok, bid)
	if after["status"] != "cancelling" {
		t.Fatal("released early")
	}
	var revoked sql.NullInt64
	s.DB.QueryRow("SELECT revoked FROM run_tokens WHERE hash='hash'").Scan(&revoked)
	if revoked.Int64 != 1 {
		t.Fatal("token not revoked")
	}
}
