package platform

import (
	"database/sql"
	"encoding/json"
	"fmt"
	"net/http"
	"strings"
)

func (s *Store) ReviewReport(token, version, ip string) (map[string]any, error) {
	var out map[string]any
	err := s.write(func(tx *sql.Tx) error {
		u, e := s.session(tx, token)
		if e != nil {
			return e
		}
		if u.Role != "admin" || u.MustChange != 0 {
			return problem(403, "Staff account required.")
		}
		var sha, team, state, reason, raw string
		var created float64
		e = tx.QueryRow("SELECT sha256,team_id,status,reason,report,created FROM versions WHERE id=?", version).Scan(&sha, &team, &state, &reason, &raw, &created)
		if e == sql.ErrNoRows {
			return problem(404, "Submission not found.")
		}
		if e != nil {
			return e
		}
		var scanner any
		if json.Unmarshal([]byte(raw), &scanner) != nil {
			return problem(503, "Stored scanner report is invalid.")
		}
		rows, e := tx.Query("SELECT policy_version,state,COALESCE(result,''),COALESCE(report_sha,''),COALESCE(input_sha,''),attempts,created,finished FROM review_advisories WHERE version_id=? AND submission_sha=? ORDER BY created", version, sha)
		if e != nil {
			return e
		}
		reports := []map[string]any{}
		for rows.Next() {
			var policy, st, result, reportSHA, inputSHA string
			var attempts int
			var started float64
			var finished sql.NullFloat64
			if e = rows.Scan(&policy, &st, &result, &reportSHA, &inputSHA, &attempts, &started, &finished); e != nil {
				rows.Close()
				return e
			}
			var parsed any
			if result != "" && json.Unmarshal([]byte(result), &parsed) != nil {
				parsed = map[string]string{"unparsed_output": result}
			}
			reports = append(reports, map[string]any{"policy": policy, "state": st, "result": parsed, "report_sha256": reportSHA, "input_sha256": inputSHA, "attempts": attempts, "created": started, "finished": finished})
		}
		e = rows.Err()
		rows.Close()
		if e != nil {
			return e
		}
		out = map[string]any{"schema": "qiqc-review-report/v1", "version_id": version, "submission_sha256": sha, "team_id": team, "state": state, "reason": reason, "created": created, "scanner": scanner, "reviews": reports}
		encoded, e := json.Marshal(out)
		if e != nil {
			return e
		}
		out["content_sha256"] = shaBytes(encoded)
		return s.audit(tx, u, token, ip, "authenticated-staff", "review_report_download", version, "Download submission review evidence", "{}", `{"report_sha256":"`+shaBytes(encoded)+`"}`)
	})
	return out, err
}
func (s *Store) registerReviewReports(mux *http.ServeMux) {
	mux.HandleFunc("/api/admin/review-reports/", endpointHandler("GET", func(w http.ResponseWriter, r *http.Request) error {
		v := strings.TrimPrefix(r.URL.Path, "/api/admin/review-reports/")
		if v == "" || strings.Contains(v, "/") {
			return problem(404, "Report not found.")
		}
		report, e := s.ReviewReport(cookieToken(r), v, clientIP(r))
		if e != nil {
			return e
		}
		w.Header().Set("Content-Disposition", `attachment; filename="qiqc-review-report.json"`)
		w.Header().Set("X-Content-Type-Options", "nosniff")
		if r.URL.Query().Get("format") == "text" {
			w.Header().Set("Content-Type", "text/plain; charset=utf-8")
			w.Header().Set("Content-Disposition", `attachment; filename="qiqc-review-report.txt"`)
			raw, _ := json.MarshalIndent(report, "", "  ")
			fmt.Fprintf(w, "QIQC submission review report\nUntrusted submitted/model content; do not execute.\nStored provider output is redacted and bounded; absent historical raw output cannot be reconstructed.\n\n%s\n", raw)
		} else {
			respond(w, report)
		}
		return nil
	}))
}
