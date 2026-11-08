package platform

import (
	"context"
	"testing"
	"time"
)

func TestSiblingModelCapabilitySurvivesBatchAttentionAndUsesTeamRoute(t *testing.T) {
	s := testStore(t)
	now := time.Unix(3000, 0)
	s.Now = func() time.Time { return now }
	queueBatch(t, s, 2, "simulated")
	first := mustClaim(t, s, "attention-worker", 2)
	second := mustClaim(t, s, "attention-worker", 2)
	token, err := s.MintModelToken(*second, "fixture-model")
	if err != nil {
		t.Fatal(err)
	}
	if err := s.Fail(*first, "platform"); err != nil {
		t.Fatal(err)
	}
	var batchState string
	s.DB.QueryRow("SELECT status FROM batches WHERE id=?", second.BatchID).Scan(&batchState)
	if batchState != "attention" {
		t.Fatalf("fixture batch state %s", batchState)
	}
	grant, err := s.AuthorizeModel(context.Background(), token, "fixture-model")
	if err != nil {
		t.Fatalf("running sibling lost model capability: %v", err)
	}
	if grant.RouteGroup != "a" && grant.RouteGroup != "b" {
		t.Fatalf("missing stable team route: %#v", grant)
	}
	if err := s.ReserveModelRequest(context.Background(), grant); err != nil {
		t.Fatalf("running sibling lost model admission: %v", err)
	}
}
