package main

import (
	"net/http/httptest"
	"os"
	"path/filepath"
	"testing"
)

func TestStaticRootsDenySecretsSymlinksAndListings(t *testing.T) {
	root := t.TempDir()
	pub := filepath.Join(root, "public")
	os.Mkdir(pub, 0700)
	os.WriteFile(filepath.Join(root, "secret"), []byte("private"), 0600)
	os.WriteFile(filepath.Join(pub, "index.html"), []byte("public"), 0600)
	os.WriteFile(filepath.Join(pub, "admin.html"), []byte("staff"), 0600)
	os.WriteFile(filepath.Join(pub, ".env"), []byte("private"), 0600)
	os.Symlink(filepath.Join(root, "secret"), filepath.Join(pub, "link.txt"))
	os.Symlink(root, filepath.Join(pub, "linked"))
	h := publicFiles(pub, pub)
	cache := httptest.NewRecorder()
	h.ServeHTTP(cache, httptest.NewRequest("GET", "/", nil))
	if cache.Header().Get("Cache-Control") != "no-store, max-age=0" {
		t.Fatalf("public files must disable browser caches: %q", cache.Header().Get("Cache-Control"))
	}
	for _, path := range []string{"/.env", "/../secret", "/link.txt", "/linked/secret", "/api/missing", "/platform-assets/", "/platform-assets/../secret"} {
		w := httptest.NewRecorder()
		h.ServeHTTP(w, httptest.NewRequest("GET", path, nil))
		if w.Code != 404 {
			t.Errorf("%s: %d", path, w.Code)
		}
	}
	for _, path := range []string{"/", "/submit", "/leaderboard"} {
		w := httptest.NewRecorder()
		h.ServeHTTP(w, httptest.NewRequest("GET", path, nil))
		if w.Code != 200 || w.Body.String() != "public" {
			t.Errorf("%s: %d", path, w.Code)
		}
	}
	for _, path := range []string{"/admin", "/admin/"} {
		w := httptest.NewRecorder()
		h.ServeHTTP(w, httptest.NewRequest("GET", path, nil))
		if w.Code != 200 || w.Body.String() != "staff" {
			t.Errorf("independent staff shell %s: %d", path, w.Code)
		}
	}
}
