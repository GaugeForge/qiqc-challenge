package rollout

import (
	"context"
	"os"
	"path/filepath"
	"testing"
)

func TestSharedLocalRolloutPermissionsAndSymlinkRefusal(t *testing.T) {
	for _, shared := range []string{"", "1"} {
		t.Run("shared_"+shared, func(t *testing.T) {
			t.Setenv("QIQC_SHARED_DATA", shared)
			root := t.TempDir()
			source := filepath.Join(root, "source")
			os.WriteFile(source, []byte("synthetic rollout"), 0600)
			store := &Store{Root: filepath.Join(root, "objects")}
			info, err := store.PutFile(context.Background(), "team/run/1.tar.gz", source)
			if err != nil {
				t.Fatal(err)
			}
			want := os.FileMode(0600)
			dirMode := os.FileMode(0700)
			if shared == "1" {
				want = 0640
				dirMode = 0770 | os.ModeSetgid
			}
			for _, path := range []string{store.Root, filepath.Join(store.Root, "team"), filepath.Join(store.Root, "team/run")} {
				st, err := os.Stat(path)
				if err != nil || st.Mode()&(os.ModePerm|os.ModeSetgid) != dirMode {
					t.Fatal("directory permission", st, err)
				}
			}
			st, err := os.Stat(filepath.Join(store.Root, "team/run/1.tar.gz"))
			if err != nil || st.Mode().Perm() != want {
				t.Fatal("object permission", st, err)
			}
			if err = store.WithFile(context.Background(), info.Key, info.SHA256, func(string) error { return nil }); err != nil {
				t.Fatal(err)
			}
			os.Symlink(t.TempDir(), filepath.Join(store.Root, "linked"))
			if _, err = store.PutFile(context.Background(), "linked/1.tar.gz", source); err == nil {
				t.Fatal("object directory link accepted")
			}
		})
	}
}
