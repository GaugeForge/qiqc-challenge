package platform

// Only the test binary contains these loopback controls and synthetic services.
import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"sync/atomic"
	"testing"
	"time"
)

func TestFinalBrowserFixture(t *testing.T) {
	ready := os.Getenv("QIQC_BROWSER_FIXTURE_READY")
	if ready == "" {
		t.Skip("explicit loopback browser acceptance")
	}
	t.Setenv("QIQC_COMPETITION_PORTAL", "1")
	s := testStore(t)
	nativeCatalogFixture(t, s)
	c := scheduledCalendar()
	start := c.StartsAt.Add(-24 * time.Hour)
	end := c.QuietStart()
	c.InfraStartsAt = &start
	c.InfraEndsAt = &end
	installCalendar(t, s, c)
	var clock atomic.Int64
	clock.Store(start.Unix())
	s.Now = func() time.Time { return time.Unix(clock.Load(), 0) }
	account(t, s, "browser-team@example.test", "team")
	account(t, s, "browser-staff@example.test", "admin")
	root, e := filepath.Abs("../../..")
	if e != nil {
		t.Fatal(e)
	}
	s.Scanner = DockerScanner(filepath.Join(root, "platform/qiqc/archive.py"), "qiqcbench-agent:latest")
	reviewer := automaticReviewWorker(t, cleanAutomaticAdvice, nil)
	mux := http.NewServeMux()
	s.Register(mux)
	s.RegisterBusiness(mux, http.NotFoundHandler())
	web := filepath.Join(root, "platform/web")
	mux.Handle("/platform-assets/", http.StripPrefix("/platform-assets/", http.FileServer(http.Dir(web))))
	for _, route := range []string{"/submit", "/leaderboard"} {
		mux.HandleFunc(route, func(w http.ResponseWriter, r *http.Request) { http.ServeFile(w, r, filepath.Join(web, "index.html")) })
	}
	mux.HandleFunc("/admin", func(w http.ResponseWriter, r *http.Request) { http.ServeFile(w, r, filepath.Join(web, "admin.html")) })
	mux.Handle("/", http.FileServer(http.Dir(filepath.Join(root, "dist"))))
	stop := make(chan struct{})
	nonce := id()
	var done atomic.Bool
	mux.HandleFunc("/__fixture", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != "POST" || r.Header.Get("Authorization") != "Bearer "+nonce {
			http.NotFound(w, r)
			return
		}
		switch r.URL.Query().Get("phase") {
		case "quiet":
			clock.Store(c.QuietStart().Unix())
		case "competition":
			clock.Store(c.StartsAt.Unix())
		case "closed":
			clock.Store(c.EndsAt.Unix())
		case "stop":
			if done.CompareAndSwap(false, true) {
				close(stop)
			}
		default:
			http.Error(w, "invalid phase", 400)
			return
		}
		// Advancing days must not turn the boundary test into a session-expiry test.
		s.DB.Exec("UPDATE sessions SET expires=?", float64(c.AppealClosesAt.Unix()+86400))
		respond(w, map[string]bool{"ok": true})
	})
	server := httptest.NewServer(mux)
	defer server.Close()
	raw, _ := json.Marshal(map[string]string{"url": server.URL, "nonce": nonce})
	if e = os.WriteFile(ready, raw, 0600); e != nil {
		t.Fatal(e)
	}
	ticker := time.NewTicker(150 * time.Millisecond)
	defer ticker.Stop()
	timeout := time.NewTimer(8 * time.Minute)
	defer timeout.Stop()
	for {
		select {
		case <-stop:
			return
		case <-timeout.C:
			t.Fatal("browser fixture timeout")
		case <-ticker.C:
			if _, e = s.ProcessReviewOnce(context.Background(), reviewer, "local-fixture"); e != nil {
				t.Fatal(e)
			}
			if e = s.ProcessSubmissionIntents(); e != nil {
				t.Fatal(e)
			}
			var n int
			if e = s.DB.QueryRow("SELECT count(*) FROM runs WHERE state='queued'").Scan(&n); e != nil {
				t.Fatal(e)
			}
			if n > 0 {
				finishFixtureBatch(t, s, n, 0)
			}
		}
	}
}
