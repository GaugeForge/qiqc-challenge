package platform

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestStaffEnglishHTTPAndAccessBoundary(t *testing.T) {
	s := testStore(t)
	mux := http.NewServeMux()
	s.Register(mux)
	s.RegisterBusiness(mux, http.NotFoundHandler())
	team := readyUser(t, s, "staff-boundary-team@example.test", "team")
	admin := readyUser(t, s, "staff-boundary-admin@example.test", "admin")
	for _, tc := range []struct {
		token   string
		allowed bool
	}{{"", false}, {team, false}, {admin, true}} {
		r := httptest.NewRequest("GET", "/api/admin", nil)
		r.AddCookie(&http.Cookie{Name: "qiqc_session", Value: tc.token})
		w := httptest.NewRecorder()
		mux.ServeHTTP(w, r)
		if tc.allowed {
			if w.Code != 200 {
				t.Fatal(w.Code, w.Body.String())
			}
			continue
		}
		if w.Code == 200 || strings.Contains(w.Body.String(), "accounts") || strings.Contains(w.Body.String(), "host_status") {
			t.Fatal("admin data exposure", w.Code)
		}
		var d map[string]string
		json.Unmarshal(w.Body.Bytes(), &d)
		if d["error"] == "" || strings.ContainsAny(d["error"], "请仅无权") {
			t.Fatal("English error missing", d)
		}
	}
}

func TestStaffDirectoryAndGlobalCountsBeyondRecentWindow(t *testing.T) {
	s := testStore(t)
	admin := readyUser(t, s, "directory-admin@example.test", "admin")
	token, v := approved(t, s)
	b, err := s.Confirm(token, v.ID, "infra", "simulated", "staff-count-test")
	if err != nil {
		t.Fatal(err)
	}
	s.DB.Exec("UPDATE batches SET status='attention' WHERE id=?", b["id"])
	snap, err := s.AdminSnapshot(admin)
	if err != nil {
		t.Fatal(err)
	}
	counts := snap["counts"].(map[string]any)
	if counts["running_batches"] != int64(0) || counts["attention_batches"] != int64(1) {
		t.Fatal("attention counted as running", counts)
	}
	accounts := snap["accounts"].([]map[string]any)
	if len(accounts) != 2 {
		t.Fatal(accounts)
	}
	for _, a := range accounts {
		if a["id"] == nil || a["email"] == nil || a["must_change"] == nil {
			t.Fatal(a)
		}
		if a["password"] != nil {
			t.Fatal("password leaked")
		}
	}
}

func TestHostStatusStripsUnknownFieldsAndDetectsStaleData(t *testing.T) {
	path := filepath.Join(t.TempDir(), "status.json")
	os.WriteFile(path, []byte(`{"sampled_at":100,"status":"ok","checks":{"http_api":"ok"},"secret":"DO_NOT_LEAK","backup":{"verified":true,"snapshot":"PRIVATE_PATH"}}`), 0600)
	s := readHostStatus(path, 101)
	if !s.Available || s.Stale || !s.Backup.Verified {
		t.Fatal(s)
	}
	raw, _ := json.Marshal(s)
	if strings.Contains(string(raw), "PRIVATE_PATH") || strings.Contains(string(raw), "DO_NOT_LEAK") {
		t.Fatal("projection leak")
	}
	if !readHostStatus(path, 301).Stale {
		t.Fatal("stale monitor reported healthy")
	}
	if readHostStatus(path+"missing", 301).Available {
		t.Fatal("missing monitor reported available")
	}
}

func TestClientIdentityOnlyTrustsExplicitLoopbackProxy(t *testing.T) {
	t.Setenv("QIQC_TRUST_LOOPBACK_PROXY", "1")
	r := httptest.NewRequest("GET", "http://test/", nil)
	r.Header.Set("X-Real-IP", "198.51.100.2")
	r.RemoteAddr = "203.0.113.3:123"
	if clientIP(r) != "203.0.113.3" {
		t.Fatal("external spoof accepted")
	}
	r.RemoteAddr = "127.0.0.1:123"
	if clientIP(r) != "198.51.100.2" {
		t.Fatal("local proxy ignored")
	}
	r.Header.Set("X-Real-IP", "198.51.100.2,203.0.113.3")
	if clientIP(r) != "127.0.0.1" {
		t.Fatal("invalid proxy header accepted")
	}
	t.Setenv("QIQC_TRUST_LOOPBACK_PROXY", "")
	r.Header.Set("X-Real-IP", "198.51.100.2")
	if clientIP(r) != "127.0.0.1" {
		t.Fatal("unconfigured proxy trusted")
	}
}

func TestStaffAccountCreationCannotBeRequestedByTeam(t *testing.T) {
	s := testStore(t)
	team := readyUser(t, s, "role-team@example.test", "team")
	admin := readyUser(t, s, "role-admin@example.test", "admin")
	if _, err := s.IssueAccountWithRole(team, "new-staff@example.test", "New-staff-password-123", "Staff", "admin", "team", "Escalate", "ip"); status(err) != 403 {
		t.Fatal("team created staff", err)
	}
	u, err := s.IssueAccountWithRole(admin, "new-staff@example.test", "New-staff-password-123", "Staff", "admin", "organizer", "Issue individual staff access", "ip")
	if err != nil || u.Role != "admin" || u.MustChange != 1 {
		t.Fatal(u, err)
	}
	token, _, _ := s.Login(u.Email, "New-staff-password-123", "ip")
	if _, err = s.AdminSnapshot(token); status(err) != 403 {
		t.Fatal("initial staff password bypass", err)
	}
	var after string
	if err = s.DB.QueryRow("SELECT after_json FROM audit WHERE action='account_created' AND object_id=?", u.ID).Scan(&after); err != nil || !strings.Contains(after, `"role":"admin"`) {
		t.Fatal(after, err)
	}
}
