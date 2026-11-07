package platform

import (
	"bytes"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"
)

func testStore(t *testing.T) *Store {
	t.Helper()
	s, err := Open(filepath.Join(t.TempDir(), "platform.db"))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { s.Close() })
	return s
}
func account(t *testing.T, s *Store, email, role string) User {
	t.Helper()
	u, err := s.CreateAccount(email, "Initial-password-123", "Test team", role)
	if err != nil {
		t.Fatal(err)
	}
	return u
}
func call(t *testing.T, h http.Handler, method, path, token, body string) *httptest.ResponseRecorder {
	t.Helper()
	req := httptest.NewRequest(method, path, strings.NewReader(body))
	req.RemoteAddr = "127.0.0.1:12345"
	req.Header.Set("X-QIQC-Request", "1")
	if token != "" {
		req.AddCookie(&http.Cookie{Name: "qiqc_session", Value: token})
	}
	res := httptest.NewRecorder()
	h.ServeHTTP(res, req)
	return res
}
func TestPasswordCompatibilityAndMalformedHashes(t *testing.T) {
	hash, err := hashPassword("量子密码-test-123456")
	if err != nil || !passwordMatches("量子密码-test-123456", hash) || passwordMatches("wrong", hash) {
		t.Fatal("password round trip")
	}
	for _, bad := range []string{"", ":", "not-hex:not-hex", "00:00"} {
		if passwordMatches("anything", bad) {
			t.Fatal("malformed password accepted")
		}
	}
}
func TestSessionPasswordRotationPersistsAndRevokesOthers(t *testing.T) {
	s := testStore(t)
	u := account(t, s, "captain@example.test", "team")
	a, _, err := s.Login("CAPTAIN@example.test", "Initial-password-123", "127.0.0.1")
	if err != nil {
		t.Fatal(err)
	}
	b, _, err := s.Login(u.Email, "Initial-password-123", "127.0.0.1")
	if err != nil {
		t.Fatal(err)
	}
	if err = s.ChangePassword(a, "Replacement-password-123"); err != nil {
		t.Fatal(err)
	}
	me, err := s.Session(a)
	if err != nil || me.MustChange != 0 {
		t.Fatalf("session: %v", err)
	}
	if _, err = s.Session(b); err == nil {
		t.Fatal("old session not revoked")
	}
	if _, _, err = s.Login(u.Email, "Initial-password-123", "127.0.0.1"); err == nil {
		t.Fatal("old password accepted")
	}
	reopened, err := Open(s.Path)
	if err != nil {
		t.Fatal(err)
	}
	defer reopened.Close()
	if _, err = reopened.Session(a); err != nil {
		t.Fatal("session lost on restart")
	}
	if err = s.Logout(a); err != nil {
		t.Fatal(err)
	}
	if _, err = reopened.Session(a); err == nil {
		t.Fatal("logout not persistent")
	}
}
func TestRateLimitPersistsAcrossRestartAndExpires(t *testing.T) {
	s := testStore(t)
	account(t, s, "captain@example.test", "team")
	for i := 0; i < 6; i++ {
		if _, _, err := s.Login("captain@example.test", "wrong", "127.0.0.1"); status(err) != 401 {
			t.Fatalf("attempt %d status %d", i, status(err))
		}
	}
	if _, _, err := s.Login("captain@example.test", "Initial-password-123", "127.0.0.1"); status(err) != 429 {
		t.Fatal("rate limit bypass")
	}
	reopened, err := Open(s.Path)
	if err != nil {
		t.Fatal(err)
	}
	defer reopened.Close()
	if _, _, err = reopened.Login("captain@example.test", "Initial-password-123", "127.0.0.1"); status(err) != 429 {
		t.Fatal("restart bypass")
	}
	reopened.Now = func() time.Time { return time.Now().Add(16 * time.Minute) }
	if _, _, err = reopened.Login("captain@example.test", "Initial-password-123", "127.0.0.1"); err != nil {
		t.Fatal(err)
	}
}
func TestHTTPAuthCSRFBodyLimitAndNoSecretFields(t *testing.T) {
	s := testStore(t)
	account(t, s, "captain@example.test", "team")
	mux := http.NewServeMux()
	s.Register(mux)
	req := httptest.NewRequest("POST", "/api/login", bytes.NewBufferString(`{"email":"captain@example.test","password":"Initial-password-123"}`))
	req.Header.Set("Origin", "https://evil.example")
	res := httptest.NewRecorder()
	mux.ServeHTTP(res, req)
	if res.Code != 403 {
		t.Fatal("cross-origin login accepted")
	}
	for _, body := range []string{`[]`, `null`, `{} {}`, strings.Repeat("x", 65537)} {
		r := call(t, mux, "POST", "/api/login", "", body)
		if r.Code != 400 && r.Code != 413 {
			t.Fatalf("invalid body status %d", r.Code)
		}
	}
	r := call(t, mux, "POST", "/api/login", "", `{"email":"captain@example.test","password":"Initial-password-123"}`)
	if r.Code != 200 {
		t.Fatalf("login %d %s", r.Code, r.Body)
	}
	if strings.Contains(r.Body.String(), "password") || strings.Contains(r.Body.String(), "Initial-") {
		t.Fatal("secret field leaked")
	}
	c := r.Result().Cookies()[0]
	if !c.HttpOnly || c.SameSite != http.SameSiteStrictMode {
		t.Fatal("unsafe cookie")
	}
	if r := call(t, mux, "GET", "/api/me", c.Value, ""); r.Code != 200 {
		t.Fatal(r.Body.String())
	}
	if r := call(t, mux, "GET", "/api/login", "", ""); r.Code != 405 {
		t.Fatal("method not enforced")
	}
}
func TestAdminResetIsAtomicAuditedAndRevokesSessions(t *testing.T) {
	s := testStore(t)
	u := account(t, s, "captain@example.test", "team")
	a := account(t, s, "admin@example.test", "admin")
	tok, _, _ := s.Login(u.Email, "Initial-password-123", "127.0.0.1")
	at, _, _ := s.Login(a.Email, "Initial-password-123", "127.0.0.1")
	if err := s.ChangePassword(at, "Admin-replacement-123"); err != nil {
		t.Fatal(err)
	}
	if err := s.ResetPassword(tok, u.ID, "New-password-12345", "operator", "identity verified", "127.0.0.1"); status(err) != 403 {
		t.Fatal("nonadmin reset")
	}
	if err := s.ResetPassword(at, u.ID, "New-password-12345", "operator", "", "127.0.0.1"); status(err) != 400 {
		t.Fatal("missing reason accepted")
	}
	if err := s.ResetPassword(at, u.ID, "New-password-12345", "operator", "identity verified", "127.0.0.1"); err != nil {
		t.Fatal(err)
	}
	if _, err := s.Session(tok); err == nil {
		t.Fatal("reset kept session")
	}
	_, updated, err := s.Login(u.Email, "New-password-12345", "127.0.0.1")
	if err != nil || updated.MustChange != 0 {
		t.Fatal("team reset should set a final password")
	}
	if err := s.ResetPassword(at, a.ID, "Staff-reset-password-1", "operator", "identity verified", "127.0.0.1"); err == nil {
		if _, staff, err := s.Login(a.Email, "Staff-reset-password-1", "127.0.0.2"); err != nil || staff.MustChange != 1 {
			t.Fatal("staff reset must still force a change")
		}
	}
	var before, after string
	if err := s.DB.QueryRow("SELECT before_json,after_json FROM audit WHERE action='password_reset'").Scan(&before, &after); err != nil {
		t.Fatal(err)
	}
	if strings.Contains(before+after, "password-123") {
		t.Fatal("password in audit")
	}
}
func TestParallelLoginDifferentTeams(t *testing.T) {
	s := testStore(t)
	for i := 0; i < 20; i++ {
		account(t, s, strings.Repeat("a", i+1)+"@example.test", "team")
	}
	var wg sync.WaitGroup
	for i := 0; i < 20; i++ {
		wg.Add(1)
		go func(i int) {
			defer wg.Done()
			tok, _, err := s.Login(strings.Repeat("a", i+1)+"@example.test", "Initial-password-123", "127.0.0.1")
			if err != nil {
				t.Error(err)
				return
			}
			if _, err = s.Session(tok); err != nil {
				t.Error(err)
			}
		}(i)
	}
	wg.Wait()
	var n int
	s.DB.QueryRow("SELECT COUNT(*) FROM sessions").Scan(&n)
	if n != 20 {
		t.Fatalf("sessions %d", n)
	}
}
func TestJSONUserContract(t *testing.T) {
	s := testStore(t)
	u := account(t, s, "captain@example.test", "team")
	b, _ := json.Marshal(u)
	var m map[string]any
	json.Unmarshal(b, &m)
	for _, k := range []string{"id", "email", "team_id", "team", "role", "must_change", "quotas"} {
		if _, ok := m[k]; !ok {
			t.Fatal(k)
		}
	}
}
