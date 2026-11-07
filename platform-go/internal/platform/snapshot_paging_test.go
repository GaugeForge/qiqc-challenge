package platform

import (
	"database/sql"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"net/url"
	"strings"
	"testing"
)

func TestStaffHistoryAllRecordsReachableAndSearchable(t *testing.T) {
	s := testStore(t)
	admin := readyUser(t, s, "history-staff@example.test", "admin")
	u, e := s.Session(admin)
	if e != nil {
		t.Fatal(e)
	}
	e = s.write(func(tx *sql.Tx) error {
		for i := 0; i < 205; i++ {
			key := fmt.Sprintf("history-%03d", i)
			for _, q := range []struct {
				query string
				args  []any
			}{
				{"INSERT INTO versions(id,team_id,sha256,name,status,report,reason,created) VALUES(?,?,?,?,'approved','{}','',?)", []any{key, u.TeamID, key, key, float64(i + 1)}},
				{"INSERT INTO batches(id,team_id,version_id,kind,mode,idem,status,accepted_at) VALUES(?,?,?,'infra','simulated',?,'completed',?)", []any{key, u.TeamID, key, key, float64(i + 1)}},
				{"INSERT INTO audit(id,created,actor,action,reason,before_json,after_json) VALUES(?,?,'staff','fixture','history','{}','{}')", []any{key, float64(i + 1)}},
				{"INSERT INTO usage(id,model,created,status,source) VALUES(?,'fixture',?,200,'fixture')", []any{key, float64(i + 1)}},
			} {
				if _, e := tx.Exec(q.query, q.args...); e != nil {
					return e
				}
			}
		}
		return nil
	})
	if e != nil {
		t.Fatal(e)
	}
	for _, section := range []string{"versions", "batches", "usage", "audit"} {
		seen := map[string]bool{}
		for page := 0; page < 3; page++ {
			result, e := s.adminSnapshot(admin, snapshotOptions{pages: map[string]int{section: page}})
			if e != nil {
				t.Fatal(e)
			}
			key := section
			if section == "usage" {
				key = "usage_rows"
			}
			for _, row := range result[key].([]map[string]any) {
				id := row["id"].(string)
				if seen[id] {
					t.Fatalf("duplicate %s: %s", section, id)
				}
				seen[id] = true
			}
		}
		if len(seen) != 205 {
			t.Fatalf("%s: reached %d/205", section, len(seen))
		}
	}
	options, e := parseSnapshotOptions(url.Values{"version_query": {"history-000"}, "batch_query": {"history-000"}, "versions_page": {"99"}})
	if e != nil {
		t.Fatal(e)
	}
	result, e := s.adminSnapshot(admin, options)
	if e != nil {
		t.Fatal(e)
	}
	for _, key := range []string{"versions", "batches"} {
		rows := result[key].([]map[string]any)
		if len(rows) != 1 || rows[0]["id"] != "history-000" {
			t.Fatalf("oldest record not found: %s", key)
		}
	}
	options.versionQuery = "' OR 1=1 --"
	result, e = s.adminSnapshot(admin, options)
	if e != nil || len(result["versions"].([]map[string]any)) != 0 {
		t.Fatal("search not literal", e)
	}
	options.versionQuery = ""
	options.from = 20
	options.until = 30
	result, e = s.adminSnapshot(admin, options)
	if e != nil || len(result["versions"].([]map[string]any)) != 11 {
		t.Fatal("date range lost", e)
	}
}

func TestStaffHistoryHTTPValidationAndAuthorization(t *testing.T) {
	s := testStore(t)
	admin := readyUser(t, s, "history-http-admin@example.test", "admin")
	team := readyUser(t, s, "history-http-team@example.test", "team")
	mux := http.NewServeMux()
	s.RegisterBusiness(mux, http.NotFoundHandler())
	for _, tc := range []struct {
		query, token string
		status       int
	}{
		{"?batches_page=-1", admin, 400}, {"?versions_page=1000001", admin, 400},
		{"?version_state=unknown", admin, 400}, {"?version_from=20&version_until=10", admin, 400},
		{"?audit_page=1", team, 403}, {"?audit_page=1", "", 401}, {"?audit_page=1", admin, 200},
	} {
		r := httptest.NewRequest("GET", "/api/admin"+tc.query, nil)
		r.AddCookie(&http.Cookie{Name: "qiqc_session", Value: tc.token})
		w := httptest.NewRecorder()
		mux.ServeHTTP(w, r)
		if w.Code != tc.status {
			t.Fatalf("%s: %d expected %d", tc.query, w.Code, tc.status)
		}
	}
}

func TestStaffExecutionPolicyShowsFormalReadinessWithoutPrivatePaths(t *testing.T) {
	s := testStore(t)
	admin := readyUser(t, s, "policy-staff@example.test", "admin")
	snapshot, e := s.AdminSnapshot(admin)
	if e != nil {
		t.Fatal(e)
	}
	if snapshot["execution_policy"].(map[string]any)["formal_configured"] != false {
		t.Fatal("missing configuration reported ready")
	}
	raw, e := json.Marshal(FormalExecution{ProfilePath: "/private/test-profile.json", ProfileSHA256: strings.Repeat("a", 64), Model: "fixture-model", GatewayURL: "http://127.0.0.1:18789", MaxRequestsPerTask: 1000})
	if e != nil {
		t.Fatal(e)
	}
	if _, e = s.DB.Exec("INSERT INTO settings(key,value) VALUES('formal_execution',?)", string(raw)); e != nil {
		t.Fatal(e)
	}
	snapshot, e = s.AdminSnapshot(admin)
	if e != nil {
		t.Fatal(e)
	}
	p := snapshot["execution_policy"].(map[string]any)
	if p["formal_configured"] != true || p["formal_model"] != "fixture-model" || p["formal_max_requests_per_task"] != 1000 {
		t.Fatal("policy display does not match configured execution", p)
	}
	for _, key := range []string{"profile_path", "gateway_url", "profile_sha256"} {
		if _, ok := p[key]; ok {
			t.Fatal("unnecessary private configuration field", key)
		}
	}
}
