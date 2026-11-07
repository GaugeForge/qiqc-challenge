package platform

import (
	"context"
	"database/sql"
	"encoding/json"
	"strings"
	"testing"

	"github.com/gaugeforge/qiqc-platform-go/internal/notifications"
)

func TestNotificationSnapshotIsLocalOnlyAndContainsNoRecipientPayload(t *testing.T) {
	s := testStore(t)
	var before map[string]any
	if err := s.write(func(tx *sql.Tx) error { var err error; before, err = snapshotNotificationDelivery(tx); return err }); err != nil {
		t.Fatal(err)
	}
	if before["real_mail_enabled"] != false || before["default_mode"] != "local_only" {
		t.Fatal(before)
	}
	if _, err := s.DB.Exec("INSERT INTO outbox(id,event,payload,created) VALUES('notice','account_ready','private@example.test TOP_SECRET',1)"); err != nil {
		t.Fatal(err)
	}
	d, err := notifications.New(s.DB, notifications.Config{}, nil)
	if err != nil {
		t.Fatal(err)
	}
	if err = d.Cycle(context.Background()); err != nil {
		t.Fatal(err)
	}
	var after map[string]any
	if err = s.write(func(tx *sql.Tx) error { var e error; after, e = snapshotNotificationDelivery(tx); return e }); err != nil {
		t.Fatal(err)
	}
	bytes, err := json.Marshal(after)
	if err != nil {
		t.Fatal(err)
	}
	if strings.Contains(string(bytes), "example.test") || strings.Contains(string(bytes), "SECRET") || !strings.Contains(string(bytes), `"count":1`) {
		t.Fatal("notification summary leaked private data or lost count")
	}
}
