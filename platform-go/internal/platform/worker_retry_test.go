package platform

import (
	"testing"
	"time"
)

func TestAutomaticPlatformRetryIsBoundedToOne(t *testing.T) {
	s := testStore(t)
	now := time.Unix(4000, 0)
	s.Now = func() time.Time { return now }
	queueBatch(t, s, 1, "simulated")
	first := mustClaim(t, s, "retry-worker", 1)
	if err := s.Fail(*first, "platform"); err != nil {
		t.Fatal(err)
	}
	if err := s.RetryPlatformFailure(*first, "transient infrastructure"); err != nil {
		t.Fatal(err)
	}
	second := mustClaim(t, s, "retry-worker", 1)
	if second.Attempt != 2 {
		t.Fatalf("retry attempt %d", second.Attempt)
	}
	if err := s.Fail(*second, "platform"); err != nil {
		t.Fatal(err)
	}
	if err := s.RetryPlatformFailure(*second, "second infrastructure failure"); err == nil {
		t.Fatal("unbounded automatic retry")
	}
	var state string
	s.DB.QueryRow("SELECT state FROM runs WHERE id=?", second.RunID).Scan(&state)
	if state != "attention" {
		t.Fatalf("second failure not held for staff: %s", state)
	}
}

func TestModelFailureNeverGetsAutomaticWholeTaskRetry(t *testing.T) {
	s := testStore(t)
	now := time.Unix(5000, 0)
	s.Now = func() time.Time { return now }
	queueBatch(t, s, 1, "simulated")
	lease := mustClaim(t, s, "retry-worker", 1)
	if err := s.Fail(*lease, "model"); err != nil {
		t.Fatal(err)
	}
	if err := s.RetryPlatformFailure(*lease, "model transport failure"); err == nil {
		t.Fatal("model failure retried the whole task")
	}
	var state string
	s.DB.QueryRow("SELECT state FROM runs WHERE id=?", lease.RunID).Scan(&state)
	if state != "attention" {
		t.Fatalf("model failure not held: %s", state)
	}
}
