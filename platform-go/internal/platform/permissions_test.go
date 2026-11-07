package platform

import (
	"os"
	"path/filepath"
	"testing"
)

func TestPermissionModesPrivateDefaultAndExplicitShared(t *testing.T) {
	for _, shared := range []string{"", "1", "true"} {
		t.Run("shared_"+shared, func(t *testing.T) {
			t.Setenv("QIQC_SHARED_DATA", shared)
			s := testStore(t)
			_, v := approved(t, s)
			dbMode, zipMode, dirMode := os.FileMode(0600), os.FileMode(0600), os.FileMode(0700)
			if shared == "1" {
				dbMode = 0660
				zipMode = 0640
				dirMode = 0770 | os.ModeSetgid
			}
			for path, want := range map[string]os.FileMode{s.Path: dbMode, filepath.Join(s.Root, "uploads", v.SHA256+".zip"): zipMode} {
				info, err := os.Stat(path)
				if err != nil || info.Mode().Perm() != want {
					t.Fatalf("mode %s: %v %v", path, info, err)
				}
			}
			for _, path := range []string{s.Root, filepath.Join(s.Root, "uploads"), filepath.Join(s.Root, "quarantine")} {
				info, err := os.Stat(path)
				if err != nil || info.Mode()&(os.ModePerm|os.ModeSetgid) != dirMode {
					t.Fatalf("directory mode %s: %v %v", path, info, err)
				}
			}
			if _, err := s.DB.Exec("PRAGMA journal_mode=WAL"); err != nil {
				t.Fatal(err)
			}
			if _, err := s.DB.Exec("INSERT OR REPLACE INTO settings VALUES('permissions_test','true')"); err != nil {
				t.Fatal(err)
			}
			for _, suffix := range []string{"-wal", "-shm"} {
				if info, err := os.Stat(s.Path + suffix); err == nil && info.Mode().Perm() != dbMode {
					t.Fatalf("sidecar %s %v want %v", suffix, info.Mode().Perm(), dbMode)
				}
			}
		})
	}
}
func TestSharedModeStillRejectsDatabaseAndUploadDirectoryLinks(t *testing.T) {
	t.Setenv("QIQC_SHARED_DATA", "1")
	root := t.TempDir()
	outside := filepath.Join(t.TempDir(), "outside.db")
	os.WriteFile(outside, []byte("untouched"), 0600)
	os.Symlink(outside, filepath.Join(root, "platform.db"))
	if s, err := Open(filepath.Join(root, "platform.db")); err == nil {
		s.Close()
		t.Fatal("database link accepted")
	}
	s := testStore(t)
	os.Symlink(t.TempDir(), filepath.Join(s.Root, "uploads"))
	withScanner(s)
	tok := readyUser(t, s, "permissions@example.test", "team")
	if _, err := s.Upload(tok, []byte("fixture")); err == nil {
		t.Fatal("upload directory link accepted")
	}
	data, _ := os.ReadFile(outside)
	if string(data) != "untouched" {
		t.Fatal("outside target altered")
	}
}

func TestOperatorMissingSharedSettingCannotBreakRunningServicePermissions(t *testing.T) {
	t.Setenv("QIQC_SHARED_DATA", "1")
	root := t.TempDir()
	path := filepath.Join(root, "platform.db")
	shared, err := Open(path)
	if err != nil {
		t.Fatal(err)
	}
	defer shared.Close()
	t.Setenv("QIQC_SHARED_DATA", "")
	if accidental, err := Open(path); err == nil {
		accidental.Close()
		t.Fatal("missing shared configuration silently accepted")
	}
	dir, _ := os.Stat(root)
	file, _ := os.Stat(path)
	if dir.Mode()&(os.ModePerm|os.ModeSetgid) != 0770|os.ModeSetgid || file.Mode().Perm() != 0660 {
		t.Fatal("operator command changed live permissions")
	}
	if err = shared.Check(); err != nil {
		t.Fatal("running database lost availability", err)
	}
}
