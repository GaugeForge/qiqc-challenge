package platform

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestSharedStoreRejectsImplicitPrivateReopenWithoutMutatingPermissions(t *testing.T) {
	t.Setenv("QIQC_SHARED_DATA", "1")
	path := filepath.Join(t.TempDir(), "data", "platform.db")
	first, err := Open(path)
	if err != nil {
		t.Fatal(err)
	}
	if _, err = first.DB.Exec("INSERT INTO settings VALUES('shared_permission_sentinel','retained')"); err != nil {
		t.Fatal(err)
	}
	if err = first.Close(); err != nil {
		t.Fatal(err)
	}
	for _, value := range []string{"", "true", "0"} {
		t.Setenv("QIQC_SHARED_DATA", value)
		rejected, err := Open(path)
		if err == nil {
			rejected.Close()
			t.Fatalf("implicit private reopen accepted: %q", value)
		}
		if !strings.Contains(err.Error(), "refusing permission downgrade") {
			t.Fatalf("wrong rejection: %v", err)
		}
		dir, err := os.Stat(filepath.Dir(path))
		if err != nil {
			t.Fatal(err)
		}
		db, err := os.Stat(path)
		if err != nil {
			t.Fatal(err)
		}
		if dir.Mode()&(os.ModePerm|os.ModeSetgid) != 0770|os.ModeSetgid || db.Mode().Perm() != 0660 {
			t.Fatalf("failed maintenance open mutated sharing: %v %v", dir.Mode(), db.Mode())
		}
	}
	t.Setenv("QIQC_SHARED_DATA", "1")
	reopened, err := Open(path)
	if err != nil {
		t.Fatal(err)
	}
	defer reopened.Close()
	var value string
	if err = reopened.DB.QueryRow("SELECT value FROM settings WHERE key='shared_permission_sentinel'").Scan(&value); err != nil || value != "retained" {
		t.Fatalf("shared data lost: %q %v", value, err)
	}
}
