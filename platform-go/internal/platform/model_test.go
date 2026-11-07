package platform

import (
	"context"
	"database/sql"
	"github.com/gaugeforge/qiqc-platform-go/internal/modelproxy"
	"testing"
	"time"
)

func TestModelRunCapabilityRejectsWrongModelCancelledExpiredAndOldAttempt(t *testing.T) {
	for _, scenario := range []string{"wrong-model", "cancelled", "expired", "old-attempt", "uploading", "lease-expired"} {
		t.Run(scenario, func(t *testing.T) {
			s := testStore(t)
			queueBatch(t, s, 1, "scientific-smoke")
			l := mustClaim(t, s, "worker", 4)
			token, err := s.MintModelToken(*l, "tencent/hy4-preview")
			if err != nil {
				t.Fatal(err)
			}
			model := "tencent/hy4-preview"
			if _, err = s.AuthorizeModel(context.Background(), token, model); err != nil {
				t.Fatal("valid capability rejected", err)
			}
			switch scenario {
			case "wrong-model":
				model = "unapproved-model"
			case "cancelled":
				s.DB.Exec("UPDATE batches SET status='cancelling' WHERE id=?", l.BatchID)
			case "expired":
				s.DB.Exec("UPDATE run_tokens SET expires=0")
			case "old-attempt":
				s.DB.Exec("UPDATE runs SET attempt=attempt+1 WHERE id=?", l.RunID)
			case "uploading":
				s.DB.Exec("UPDATE runs SET state='uploading' WHERE id=?", l.RunID)
			case "lease-expired":
				s.Now = func() time.Time { return time.Now().Add(time.Hour) }
			}
			if _, err = s.AuthorizeModel(context.Background(), token, model); err == nil {
				t.Fatal("capability bypass")
			}
		})
	}
}

func TestModelUsageRetainsUnknownAndPostCancellationConsumption(t *testing.T) {
	s := testStore(t)
	queueBatch(t, s, 1, "scientific-smoke")
	l := mustClaim(t, s, "worker", 4)
	token, err := s.MintModelToken(*l, "tencent/hy4-preview")
	if err != nil {
		t.Fatal(err)
	}
	grant, err := s.AuthorizeModel(context.Background(), token, "tencent/hy4-preview")
	if err != nil {
		t.Fatal(err)
	}
	grant.RequestID = "1234567890abcdef1234567890abcdef"
	grant.RequestCorrelation = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
	s.DB.Exec("UPDATE batches SET status='cancelled' WHERE id=?", l.BatchID)
	if err = s.RecordModelUsage(context.Background(), modelproxy.UsageRecord{Grant: grant, Status: 502, Attempt: 2, ErrorClass: "provider_unavailable"}); err != nil {
		t.Fatal(err)
	}
	var requestID, errorClass, correlation string
	var upstreamAttempt int
	if err = s.DB.QueryRow("SELECT request_id,upstream_attempt,error_class,request_correlation FROM usage").Scan(&requestID, &upstreamAttempt, &errorClass, &correlation); err != nil || requestID != grant.RequestID || correlation != grant.RequestCorrelation || upstreamAttempt != 2 || errorClass != "provider_unavailable" {
		t.Fatal("durable request identity missing", err)
	}
	var input, output sql.NullInt64
	var amount sql.NullFloat64
	if err = s.DB.QueryRow("SELECT input_tokens,output_tokens,amount FROM usage").Scan(&input, &output, &amount); err != nil {
		t.Fatal(err)
	}
	if input.Valid || output.Valid || amount.Valid {
		t.Fatal("unknown consumption represented as zero")
	}
	in, out := int64(100), int64(20)
	if err = s.RecordModelUsage(context.Background(), modelproxy.UsageRecord{Grant: grant, Status: 200, Usage: modelproxy.Usage{InputTokens: &in, OutputTokens: &out}}); err != nil {
		t.Fatal(err)
	}
	var count int
	s.DB.QueryRow("SELECT COUNT(*) FROM usage").Scan(&count)
	if count != 2 {
		t.Fatal("lost consumed usage after cancellation")
	}
}
