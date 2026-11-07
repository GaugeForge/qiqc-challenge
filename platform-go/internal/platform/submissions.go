package platform

import (
	"bytes"
	"context"
	"crypto/sha256"
	"database/sql"
	"encoding/hex"
	"encoding/json"
	"errors"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"sync"
	"time"
)

const MaxZip = 20 * 1024 * 1024

// Manifest is the trusted parser's normalized harness.json. Participants may
// write only {"runtime": "codex"}; the parser derives the remaining fields.
type Manifest struct {
	Schema          string   `json:"schema"`
	Name            string   `json:"name"`
	Entrypoint      string   `json:"entrypoint"`
	Runtime         string   `json:"runtime,omitempty"`
	Adapter         string   `json:"adapter,omitempty"`
	RuntimeKind     string   `json:"runtime_kind,omitempty"`
	RuntimePackage  string   `json:"runtime_package,omitempty"`
	RuntimeVersion  string   `json:"runtime_version,omitempty"`
	ModelInterfaces []string `json:"model_interfaces,omitempty"`
	Trajectory      string   `json:"trajectory,omitempty"`
	TaskTransport   string   `json:"task_transport,omitempty"`
}
type Risk struct {
	File        string `json:"file"`
	Rule        string `json:"rule"`
	Line        int    `json:"line,omitempty"`
	Explanation string `json:"explanation,omitempty"`
}

// PublicReviewFinding is the bounded, team-scoped part of an advisory that
// can be shown in the submit portal. Provider output, snippets and
// diagnostics remain staff-only.
type PublicReviewFinding struct {
	Rule        string  `json:"rule"`
	File        string  `json:"file"`
	Confidence  float64 `json:"confidence"`
	Explanation string  `json:"explanation"`
}
type PublicReviewAdvisory struct {
	Status                 string                `json:"status"`
	Outcome                string                `json:"outcome"`
	Summary                string                `json:"summary,omitempty"`
	Findings               []PublicReviewFinding `json:"findings,omitempty"`
	ManualFollowupRequired bool                  `json:"manual_followup_required,omitempty"`
	UncoveredScannerRisks  int                   `json:"uncovered_scanner_risks,omitempty"`
}

// PublicVersionReview contains only review locations and bounded explanations
// for the authenticated team's own immutable version.
type PublicVersionReview struct {
	StaticRisks []Risk                `json:"static_risks,omitempty"`
	AIAdvisory  *PublicReviewAdvisory `json:"ai_advisory,omitempty"`
}
type Report struct {
	RulesetVersion string              `json:"ruleset_version"`
	ParserSHA256   string              `json:"parser_sha256"`
	ScannerImage   string              `json:"scanner_image"`
	Source         string              `json:"source"`
	SourceSHA256   string              `json:"source_sha256"`
	Manifest       Manifest            `json:"manifest"`
	Files          int                 `json:"files"`
	ExpandedBytes  int64               `json:"expanded_bytes"`
	Risks          []Risk              `json:"risks"`
	Snippets       []map[string]string `json:"snippets"`
	AIReview       string              `json:"ai_review"`
	Reference      bool                `json:"reference"`
}
type Version struct {
	ID     string `json:"id"`
	SHA256 string `json:"sha256"`
	// Name is the platform-assigned label "vN" (Nth upload of the team).
	// HarnessName keeps the optional harness.json name stored with the row.
	Name        string               `json:"name"`
	Number      int                  `json:"number"`
	HarnessName string               `json:"harness_name"`
	Status      string               `json:"status"`
	Reason      string               `json:"reason"`
	Created     float64              `json:"created"`
	Reference   bool                 `json:"reference"`
	Files       int                  `json:"files"`
	RiskCount   int                  `json:"risk_count"`
	AIReview    string               `json:"ai_review"`
	Review      *PublicVersionReview `json:"review,omitempty"`
}

// DockerScanner runs only the organizer's bounded parser. No submitted program
// is imported, installed or executed. Missing Docker/image/parser fails closed.
func DockerScanner(script, image string) func(string) (Report, error) {
	var once sync.Once
	var imageID string
	var imageErr error
	return func(path string) (Report, error) {
		ctx, cancel := context.WithTimeout(context.Background(), 35*time.Second)
		defer cancel()
		once.Do(func() {
			output, err := exec.CommandContext(ctx, "docker", "image", "inspect", "--format", "{{.Id}}", image).Output()
			imageID = strings.TrimSpace(string(output))
			if err != nil || !validDigest(strings.TrimPrefix(imageID, "sha256:")) || !strings.HasPrefix(imageID, "sha256:") {
				imageErr = errors.New("scanner image unavailable")
			}
		})
		if imageErr != nil {
			return Report{}, problem(503, "隔离检查器镜像不可用；未扣次数")
		}
		parser, err := os.ReadFile(script)
		if err != nil {
			return Report{}, problem(503, "隔离检查器不可用；未扣次数")
		}
		container := "qiqc-go-scanner-" + id()
		defer func() {
			cleanup, stop := context.WithTimeout(context.Background(), 10*time.Second)
			defer stop()
			_ = exec.CommandContext(cleanup, "docker", "rm", "-f", container).Run()
		}()
		cmd := exec.CommandContext(ctx, "docker", "run", "--rm", "--pull=never", "--name", container, "--network", "none", "--read-only", "--cap-drop", "ALL", "--security-opt", "no-new-privileges", "--pids-limit", "64", "--memory", "256m", "--cpus", "0.5", "--user", "65534:65534", "--mount", "type=bind,src="+path+",dst=/incoming/harness.zip,readonly", "--mount", "type=bind,src="+script+",dst=/scanner.py,readonly", imageID, "python3", "/scanner.py", "/incoming/harness.zip")
		var out bytes.Buffer
		cmd.Stdout = &out
		err = cmd.Run()
		var result struct {
			OK        bool   `json:"ok"`
			Report    Report `json:"report"`
			Error     string `json:"error"`
			ErrorKind string `json:"error_kind"`
		}
		if out.Len() > 1024*1024 || json.Unmarshal(out.Bytes(), &result) != nil {
			return Report{}, problem(503, "隔离检查器暂不可用；未扣次数")
		}
		if !result.OK {
			if result.ErrorKind != "invalid_package" {
				return Report{}, problem(503, "隔离检查器暂不可用；未扣次数")
			}
			return Report{}, problem(400, publicScannerError(result.Error))
		}
		if err != nil {
			return Report{}, problem(503, "隔离检查器失败；未扣次数")
		}
		result.Report.ParserSHA256 = shaBytes(parser)
		result.Report.ScannerImage = imageID
		current, readErr := os.ReadFile(script)
		if readErr != nil || !bytes.Equal(parser, current) {
			return Report{}, problem(503, "检查器版本发生变化")
		}
		if !validScannerReport(result.Report) {
			return Report{}, problem(503, "检查器返回无效报告")
		}
		return result.Report, nil
	}
}

// publicScannerError returns the trusted parser's message only when it is one
// of the fixed participant-safe texts. The parser never echoes submitted values.
func publicScannerError(message string) string {
	for _, public := range []string{"Entrypoint file missing", "Manifest too large", "Harness directory is empty", "Entrypoint must be inside adapter/", "Only harness/, adapter/ and harness.json are allowed at the root", "Native CLI and native-http/v1 transport are required", "Supported model interfaces are required", "Adapter entrypoint must be a shell script ending in .sh", "Manifest contains unsupported fields; keys, budgets and task selection are not allowed", "Compressed package exceeds 20 MiB", "Too many archive entries", "Duplicate archive path", "Links and special files are forbidden", "Encrypted archives are unsupported", "Unsupported compression method", "Expanded size or compression ratio exceeds limits", "Unsafe archive path", "Hidden or escaping archive path", "Non-canonical archive path"} {
		if message == public {
			return message
		}
	}
	if strings.HasPrefix(message, "harness.json: ") && len(message) <= 300 {
		printable := true
		for _, r := range message {
			if r < 0x20 || r == 0x7f || r == '<' || r == '>' {
				printable = false
				break
			}
		}
		if printable {
			return message
		}
	}
	return "Submission package failed format checks. Check the ZIP layout and harness.json."
}

// versionNumberSQL numbers a team's versions by upload order (1, 2, 3, ...).
// It is computed on read, so stored historical rows are never rewritten.
func versionNumberSQL(alias string) string {
	return "(SELECT COUNT(*) FROM versions o WHERE o.team_id=" + alias + ".team_id AND (o.created<" + alias + ".created OR (o.created=" + alias + ".created AND o.id<=" + alias + ".id)))"
}

// VersionLabel is the team-facing version name.
func VersionLabel(number int) string { return "v" + strconv.Itoa(number) }

func validDigest(value string) bool {
	if len(value) != 64 {
		return false
	}
	_, err := hex.DecodeString(value)
	return err == nil
}
func validScannerReport(r Report) bool {
	return (r.Manifest.Schema == "qiqc-harness/v1-demo" || r.Manifest.Schema == "qiqc-harness/v1") && r.Files >= 1 && r.Files <= 1000 && r.RulesetVersion == "qiqc-archive/2" && validDigest(r.ParserSHA256) && strings.HasPrefix(r.ScannerImage, "sha256:") && validDigest(strings.TrimPrefix(r.ScannerImage, "sha256:"))
}
func shaBytes(b []byte) string { h := sha256.Sum256(b); return hex.EncodeToString(h[:]) }
func (s *Store) CheckVersion(sha string) error {
	if len(sha) != 64 {
		return problem(500, "无效版本摘要")
	}
	if _, err := hex.DecodeString(sha); err != nil {
		return problem(500, "无效版本摘要")
	}
	path := filepath.Join(s.Root, "uploads", sha+".zip")
	info, err := os.Lstat(path)
	if err != nil || !info.Mode().IsRegular() || info.Size() > MaxZip {
		return problem(500, "版本文件缺失或无效")
	}
	b, err := os.ReadFile(path)
	if err != nil {
		return err
	}
	if shaBytes(b) != sha {
		return problem(500, "版本字节校验不一致，已停止受理")
	}
	return nil
}
func versionRow(row interface{ Scan(...any) error }) (Version, error) {
	var v Version
	var raw string
	err := row.Scan(&v.ID, &v.SHA256, &v.Number, &v.HarnessName, &v.Status, &v.Reason, &v.Created, &raw)
	if err != nil {
		return v, err
	}
	v.Name = VersionLabel(v.Number)
	var r Report
	if err = json.Unmarshal([]byte(raw), &r); err != nil {
		return v, err
	}
	v.Reference = r.Reference
	v.Files = r.Files
	v.RiskCount = len(r.Risks)
	v.AIReview = r.AIReview
	return v, nil
}

// publicVersionReview reads the frozen scanner report and the latest bounded
// advisory without exposing source snippets or provider diagnostics. The
// caller has already constrained the version query to the authenticated team.
func (s *Store) publicVersionReview(versionID, sha, raw string) (*PublicVersionReview, error) {
	var report Report
	if err := json.Unmarshal([]byte(raw), &report); err != nil {
		return nil, err
	}
	out := &PublicVersionReview{StaticRisks: append([]Risk(nil), report.Risks...)}
	reportSHA := shaBytes([]byte(raw))
	var result, state string
	err := s.DB.QueryRow(`SELECT result,state FROM review_advisories
WHERE version_id=? AND submission_sha=? AND report_sha=?
ORDER BY created DESC,finished DESC LIMIT 1`, versionID, sha, reportSHA).Scan(&result, &state)
	if errors.Is(err, sql.ErrNoRows) {
		out.AIAdvisory = &PublicReviewAdvisory{Status: "manual_required", Outcome: "not_requested"}
		return out, nil
	}
	if err != nil {
		return nil, err
	}
	if state != "complete" {
		out.AIAdvisory = &PublicReviewAdvisory{Status: "manual_required", Outcome: state}
		return out, nil
	}
	var advice struct {
		Status                 string                `json:"status"`
		Outcome                string                `json:"outcome"`
		Summary                string                `json:"summary"`
		Findings               []PublicReviewFinding `json:"findings"`
		ManualFollowupRequired bool                  `json:"manual_followup_required"`
		UncoveredScannerRisks  int                   `json:"uncovered_scanner_risks"`
	}
	if err := json.Unmarshal([]byte(result), &advice); err != nil {
		out.AIAdvisory = &PublicReviewAdvisory{Status: "manual_required", Outcome: "invalid_stored_advice"}
		return out, nil
	}
	out.AIAdvisory = &PublicReviewAdvisory{
		Status: advice.Status, Outcome: advice.Outcome, Summary: advice.Summary,
		Findings: advice.Findings, ManualFollowupRequired: advice.ManualFollowupRequired,
		UncoveredScannerRisks: advice.UncoveredScannerRisks,
	}
	return out, nil
}

// versionFields must be selected FROM versions without an alias.
var versionFields = "id,sha256," + versionNumberSQL("versions") + ",name,status,reason,created,report"

func (s *Store) Versions(token string) ([]Version, error) {
	u, err := s.Session(token)
	if err != nil {
		return nil, err
	}
	if u.MustChange != 0 {
		return nil, problem(403, "请先修改初始密码")
	}
	rows, err := s.DB.Query("SELECT "+versionFields+" FROM versions WHERE team_id=? ORDER BY created DESC", u.TeamID)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	out := []Version{}
	for rows.Next() {
		v, err := versionRow(rows)
		if err != nil {
			return nil, err
		}
		var raw string
		if err := s.DB.QueryRow("SELECT report FROM versions WHERE id=? AND team_id=?", v.ID, u.TeamID).Scan(&raw); err != nil {
			return nil, err
		}
		v.Review, err = s.publicVersionReview(v.ID, v.SHA256, raw)
		if err != nil {
			return nil, err
		}
		out = append(out, v)
	}
	return out, rows.Err()
}
func (s *Store) Upload(token string, data []byte) (Version, error) {
	var result Version
	u, err := s.Session(token)
	if err != nil {
		return result, err
	}
	if u.Role != "team" || u.MustChange != 0 {
		return result, problem(403, "请先使用队伍账号完成改密")
	}
	if err = s.CheckUploadPhase(); err != nil {
		return result, err
	}
	if len(data) > MaxZip {
		return result, problem(413, "压缩包不能超过 20 MiB")
	}
	if s.Scanner == nil {
		return result, problem(503, "隔离检查器未配置；未扣次数")
	}
	if err = s.CheckUploadDisk(); err != nil {
		return result, err
	}
	for _, name := range []string{"uploads", "quarantine"} {
		if err = ensureDataDirectory(filepath.Join(s.Root, name)); err != nil {
			return result, err
		}
	}
	file, err := os.CreateTemp(filepath.Join(s.Root, "quarantine"), "incoming-*.zip")
	if err != nil {
		return result, err
	}
	path := file.Name()
	defer os.Remove(path)
	if _, err = file.Write(data); err == nil {
		err = file.Sync()
	}
	closeErr := file.Close()
	if err != nil {
		return result, err
	}
	if closeErr != nil {
		return result, closeErr
	}
	if err = os.Chmod(path, 0644); err != nil {
		return result, err
	} // private parent; scanner sees one read-only bind
	report, err := s.Scanner(path)
	if err != nil {
		return result, err
	}
	sha := shaBytes(data)
	report.Source = "authenticated_team_upload"
	report.SourceSHA256 = sha
	raw, err := json.Marshal(report)
	if err != nil {
		return result, err
	}
	state, reason := "pending_review", "Submission frozen; awaiting automated security review. No evaluation attempt charged."
	err = s.write(func(tx *sql.Tx) error {
		current, err := s.session(tx, token)
		if err != nil {
			return err
		}
		if current.MustChange != 0 || current.Role != "team" {
			return problem(403, "请先完成改密")
		}
		old, err := versionRow(tx.QueryRow("SELECT "+versionFields+" FROM versions WHERE team_id=? AND sha256=?", u.TeamID, sha))
		if err == nil {
			if err = s.CheckVersion(sha); err != nil {
				return err
			}
			result = old
			return nil
		}
		if !errors.Is(err, sql.ErrNoRows) {
			return err
		}
		var n int
		if err = tx.QueryRow("SELECT COUNT(*) FROM versions WHERE team_id=?", u.TeamID).Scan(&n); err != nil {
			return err
		}
		if n >= 25 {
			return problem(429, "Each team may retain at most 25 submission versions.")
		}
		canonical := filepath.Join(s.Root, "uploads", sha+".zip")
		if err = os.Link(path, canonical); err != nil && !os.IsExist(err) {
			return err
		}
		if err = s.CheckVersion(sha); err != nil {
			return err
		}
		if err = dataFilePermissions(canonical, packageMode()); err != nil {
			return err
		}
		dir, err := os.Open(filepath.Dir(canonical))
		if err != nil {
			return err
		}
		err = dir.Sync()
		dir.Close()
		if err != nil {
			return err
		}

		vid := id()
		if _, err = tx.Exec("INSERT INTO versions(id,team_id,sha256,name,status,report,reason,created) VALUES(?,?,?,?,?,?,?,?)", vid, u.TeamID, sha, report.Manifest.Name, state, string(raw), reason, s.now()); err != nil {
			return err
		}
		result, err = versionRow(tx.QueryRow("SELECT "+versionFields+" FROM versions WHERE id=?", vid))
		return err
	})
	return result, err
}
func (s *Store) Review(token, vid, decision, actor, reason, ip string) error {
	if decision != "approve" && decision != "reject" {
		return problem(400, "无效复核决定")
	}
	actor = strings.TrimSpace(actor)
	reason = strings.TrimSpace(reason)
	return s.write(func(tx *sql.Tx) error {
		u, err := s.session(tx, token)
		if err != nil {
			return err
		}
		if u.Role != "admin" || u.MustChange != 0 {
			return problem(403, "仅管理员可访问")
		}
		var state, sha string
		if err = tx.QueryRow("SELECT status,sha256 FROM versions WHERE id=?", vid).Scan(&state, &sha); errors.Is(err, sql.ErrNoRows) {
			return problem(404, "版本不存在")
		} else if err != nil {
			return err
		}
		if state != "pending_review" {
			return problem(409, "该版本已有审查结论")
		}
		if err = s.CheckVersion(sha); err != nil {
			return err
		}
		next := "rejected"
		if decision == "approve" {
			next = "approved"
		}
		before, _ := json.Marshal(state)
		after, _ := json.Marshal(next)
		if err = s.audit(tx, u, token, ip, actor, "review", vid, reason, string(before), string(after)); err != nil {
			return err
		}
		_, err = tx.Exec("UPDATE versions SET status=?,reason=? WHERE id=?", next, reason, vid)
		return err
	})
}

// ReadPackage bounds memory before the scanner is invoked.
func ReadPackage(r io.Reader) ([]byte, error) {
	b, err := io.ReadAll(io.LimitReader(r, MaxZip+1))
	if err != nil {
		return nil, err
	}
	if len(b) > MaxZip {
		return nil, problem(413, "压缩包不能超过 20 MiB")
	}
	return b, nil
}
