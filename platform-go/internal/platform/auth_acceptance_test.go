package platform

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/gaugeforge/qiqc-platform-go/internal/rollout"
)

// This acceptance uses real HTTP, scrypt and SQLite. The submitted bytes and
// rollout store are explicit local fixtures; no cloud or email is contacted.
func TestM01TwentyTeamHTTPIsolationAndRestart(t *testing.T) {
	const teams = 20
	started := time.Now()
	dbPath := filepath.Join(t.TempDir(), "platform.db")
	s, err := Open(dbPath)
	if err != nil {
		t.Fatal(err)
	}
	defer func() { s.Close() }()
	withScanner(s)
	objects := &rollout.Store{Root: filepath.Join(s.Root, "objects")}
	s.Objects = objects
	users := make([]User, teams)
	tokens := make([]string, teams)
	stale := make([]string, teams)
	bids := make([]string, teams)
	versionIDs := make([]string, teams)
	for i := range users {
		users[i], err = s.CreateAccount(fmt.Sprintf("m01-%02d@example.test", i), fmt.Sprintf("Initial-private-%02d-password", i), fmt.Sprintf("Isolation team %02d", i), "team")
		if err != nil {
			t.Fatal(err)
		}
	}
	startServer := func() *httptest.Server {
		mux := http.NewServeMux()
		s.Register(mux)
		s.RegisterBusiness(mux, http.NotFoundHandler())
		return httptest.NewServer(mux)
	}
	server := startServer()
	defer func() { server.Close() }()
	client := &http.Client{Timeout: 60 * time.Second}
	defer client.CloseIdleConnections()
	request := func(method, path, token string, payload any, want int) ([]byte, *http.Response, error) {
		data, e := json.Marshal(payload)
		if e != nil {
			return nil, nil, e
		}
		req, e := http.NewRequest(method, server.URL+path, strings.NewReader(string(data)))
		if e != nil {
			return nil, nil, e
		}
		req.Header.Set("X-QIQC-Request", "1")
		req.Header.Set("Content-Type", "application/json")
		if token != "" {
			req.AddCookie(&http.Cookie{Name: "qiqc_session", Value: token})
		}
		resp, e := client.Do(req)
		if e != nil {
			return nil, nil, e
		}
		defer resp.Body.Close()
		b, e := io.ReadAll(io.LimitReader(resp.Body, 2<<20))
		if e != nil {
			return nil, nil, e
		}
		if resp.StatusCode != want {
			return nil, nil, fmt.Errorf("%s %s status=%d want=%d", method, path, resp.StatusCode, want)
		}
		return b, resp, nil
	}
	login := func(i int) (string, error) {
		_, r, e := request("POST", "/api/login", "", map[string]string{"email": users[i].Email, "password": fmt.Sprintf("Initial-private-%02d-password", i)}, 200)
		if e != nil {
			return "", e
		}
		for _, c := range r.Cookies() {
			if c.Name == "qiqc_session" {
				return c.Value, nil
			}
		}
		return "", fmt.Errorf("session cookie missing")
	}
	var wg sync.WaitGroup
	start := make(chan struct{})
	for i := range users {
		wg.Add(1)
		go func(i int) {
			defer wg.Done()
			<-start
			token, e := login(i)
			if e != nil {
				t.Error(e)
				return
			}
			tokens[i] = token
			stale[i], e = login(i)
			if e != nil {
				t.Error(e)
				return
			}
			for _, path := range []string{"/api/versions", "/api/batches", "/api/admin"} {
				if _, _, e = request("GET", path, token, nil, 403); e != nil {
					t.Error(e)
					return
				}
			}
			for _, password := range []string{"short", fmt.Sprintf("Initial-private-%02d-password", i)} {
				if _, _, e = request("POST", "/api/password", token, map[string]string{"password": password}, 400); e != nil {
					t.Error(e)
					return
				}
			}
			b, _, e := request("GET", "/api/me", token, nil, 200)
			var before User
			if e != nil || json.Unmarshal(b, &before) != nil || before.MustChange != 1 {
				t.Error("invalid password cleared first-change gate")
				return
			}
			if _, _, e = request("POST", "/api/password", token, map[string]string{"password": fmt.Sprintf("Replacement-private-%02d-password", i)}, 200); e != nil {
				t.Error(e)
				return
			}
			if _, _, e = request("GET", "/api/me", stale[i], nil, 401); e != nil {
				t.Error(e)
				return
			}
			b, _, e = request("GET", "/api/me", token, nil, 200)
			var me User
			if e != nil || json.Unmarshal(b, &me) != nil || me.ID != users[i].ID || me.TeamID != users[i].TeamID || me.MustChange != 0 {
				t.Error("concurrent identity mismatch")
				return
			}
			if _, _, e = request("GET", "/api/admin", token, nil, 403); e != nil {
				t.Error(e)
			}
			if _, _, e = request("POST", "/api/admin/accounts", token, map[string]string{}, 403); e != nil {
				t.Error(e)
			}
		}(i)
	}
	close(start)
	wg.Wait()
	if t.Failed() {
		return
	}
	// Create one accepted, completed local fixture per owner. This makes forbidden
	// reads meaningful: they cannot pass merely because all resources are absent.
	for i := range users {
		v, e := s.Upload(tokens[i], []byte(fmt.Sprintf("M01 owner %02d fixture", i)))
		if e != nil {
			t.Fatal(e)
		}
		if _, e = s.DB.Exec("UPDATE versions SET status='approved' WHERE id=?", v.ID); e != nil {
			t.Fatal(e)
		}
		batch, e := s.Confirm(tokens[i], v.ID, "infra", "simulated", fmt.Sprintf("m01-%d", i))
		if e != nil {
			t.Fatal(e)
		}
		bids[i] = batch["id"].(string)
		versionIDs[i] = v.ID
		l, e := s.Claim("m01-local-fixture", 1)
		if e != nil || l == nil {
			t.Fatal("claim failed", e)
		}
		if e = s.CompleteFixture(context.Background(), *l, 0, "1.0", fmt.Sprintf("private owner %02d", i)); e != nil {
			t.Fatal(e)
		}
	}
	// Close the actual listener and database before opening replacements; no old
	// connection or process-local session cache can satisfy the checks below.
	server.Close()
	if err = s.Close(); err != nil {
		t.Fatal(err)
	}
	s, err = Open(dbPath)
	if err != nil {
		t.Fatal(err)
	}
	s.Objects = objects
	server = startServer()
	for i := range users {
		if _, _, err = request("GET", "/api/me", stale[i], nil, 401); err != nil {
			t.Fatal(err)
		}
		body, _, e := request("GET", "/api/versions", tokens[i], nil, 200)
		if e != nil {
			t.Fatal(e)
		}
		var versions []Version
		if json.Unmarshal(body, &versions) != nil || len(versions) != 1 || versions[0].ID != versionIDs[i] {
			t.Fatal("version ownership after restart")
		}
		if _, _, e = request("GET", "/api/batches/"+bids[i], tokens[i], nil, 200); e != nil {
			t.Fatal(e)
		}
		if _, _, e = request("GET", "/api/batches/"+bids[i]+"/traces/0", tokens[i], nil, 404); e != nil {
			t.Fatal(e)
		}
		me, e := s.Session(tokens[i])
		if e != nil || me.Quotas["infra"] != 9 {
			t.Fatal("accepted request quota changed after restart")
		}
		for j := range users {
			if i != j {
				for _, suffix := range []string{"", "/traces/0"} {
					if _, _, e = request("GET", "/api/batches/"+bids[j]+suffix, tokens[i], nil, 404); e != nil {
						t.Fatal(e)
					}
				}
				if _, _, e = request("POST", "/api/batches/"+bids[j]+"/cancel", tokens[i], nil, 404); e != nil {
					t.Fatal(e)
				}
			}
		}
	}
	t.Logf("M01 teams=%d concurrent_login_and_rotation=passed stale_sessions_revoked=%d cross_team_denials=%d restart_completed_requests=%d elapsed_seconds=%.3f fixture_scanner=true fixture_rollouts=true email_not_tested=true", teams, teams, teams*(teams-1)*3, teams, time.Since(started).Seconds())
}

func TestM01PasswordReuseCannotBypassFirstChange(t *testing.T) {
	s := testStore(t)
	u := account(t, s, "reuse@example.test", "team")
	token, _, err := s.Login(u.Email, "Initial-password-123", "local")
	if err != nil {
		t.Fatal(err)
	}
	if err = s.ChangePassword(token, "Initial-password-123"); status(err) != 400 {
		t.Fatal("unchanged initial password accepted")
	}
	me, err := s.Session(token)
	if err != nil || me.MustChange != 1 {
		t.Fatal("first change gate cleared")
	}
	if err = s.ChangePassword(token, "Replacement-password-123"); err != nil {
		t.Fatal(err)
	}
	if err = s.ChangePassword(token, "Replacement-password-123"); status(err) != 400 {
		t.Fatal("unchanged replacement password accepted")
	}
}

func TestM01AccountAndIPLimitsPersistAndSessionsExpire(t *testing.T) {
	s := testStore(t)
	account(t, s, "limited@example.test", "team")
	for i := 0; i < 20; i++ {
		if _, _, err := s.Login("limited@example.test", "incorrect", fmt.Sprintf("203.0.113.%d", i)); status(err) != 401 {
			t.Fatal("account failure counting", i, status(err))
		}
	}
	if _, _, err := s.Login("limited@example.test", "Initial-password-123", "new-address"); status(err) != 429 {
		t.Fatal("distributed account limit bypassed")
	}
	for i := 0; i < 60; i++ {
		if _, _, err := s.Login(fmt.Sprintf("unknown-%d@example.test", i), "incorrect", "one-origin"); status(err) != 401 {
			t.Fatal("IP failure counting", i, status(err))
		}
	}
	if _, _, err := s.Login("another@example.test", "incorrect", "one-origin"); status(err) != 429 {
		t.Fatal("many-account IP limit bypassed")
	}
	path := s.Path
	if err := s.Close(); err != nil {
		t.Fatal(err)
	}
	reopened, err := Open(path)
	if err != nil {
		t.Fatal(err)
	}
	defer reopened.Close()
	if _, _, err = reopened.Login("limited@example.test", "Initial-password-123", "fresh-address"); status(err) != 429 {
		t.Fatal("account limit lost on restart")
	}
	if _, _, err = reopened.Login("fresh@example.test", "incorrect", "one-origin"); status(err) != 429 {
		t.Fatal("IP limit lost on restart")
	}
	reopened.Now = func() time.Time { return time.Now().Add(16 * time.Minute) }
	token, _, err := reopened.Login("limited@example.test", "Initial-password-123", "fresh-address")
	if err != nil {
		t.Fatal("limit did not expire", err)
	}
	reopened.Now = func() time.Time { return time.Now().Add(13 * time.Hour) }
	if _, err = reopened.Session(token); status(err) != 401 {
		t.Fatal("expired session accepted")
	}
}

func TestM01LegacyPythonHashAndSecureCookieContract(t *testing.T) {
	// Generated with the existing Python core.py contract: hashlib.scrypt over
	// UTF-8, n=16384, r=8, p=1, dklen=64. This is a public test vector only.
	const vector = "000102030405060708090a0b0c0d0e0f:8ebc1ced806a6d0098d70e8bb9eeb6008515cb25a8817a8a9227c0929af80ec9dc540a8c5d6835e01d3829fd813027e17e6bb7bdeb1364b31dcaa31ab08590ce"
	if !passwordMatches("M01-test-vector-量子", vector) || passwordMatches("wrong", vector) {
		t.Fatal("legacy Python scrypt incompatibility")
	}
	s := testStore(t)
	u := account(t, s, "secure@example.test", "team")
	if _, err := s.DB.Exec("UPDATE users SET password=? WHERE id=?", vector, u.ID); err != nil {
		t.Fatal(err)
	}
	s.SecureCookies = true
	mux := http.NewServeMux()
	s.Register(mux)
	r := call(t, mux, "POST", "/api/login", "", `{"email":"secure@example.test","password":"M01-test-vector-量子"}`)
	if r.Code != 200 {
		t.Fatal("legacy password HTTP login", r.Code)
	}
	cookies := r.Result().Cookies()
	if len(cookies) != 1 || !cookies[0].Secure || !cookies[0].HttpOnly || cookies[0].SameSite != http.SameSiteStrictMode || cookies[0].Path != "/" {
		t.Fatal("production cookie attributes missing")
	}
}
