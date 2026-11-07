package platform

import (
	"strings"
	"testing"
)

// Version names shown to teams come from upload order, not from harness.json.
func TestVersionLabelsFollowPerTeamUploadOrder(t *testing.T) {
	s := testStore(t)
	withScanner(s)
	tok := readyUser(t, s, "label-a@example.test", "team")
	other := readyUser(t, s, "label-b@example.test", "team")
	first, err := s.Upload(tok, []byte("first package bytes"))
	if err != nil {
		t.Fatal(err)
	}
	if _, err = s.Upload(other, []byte("other team package")); err != nil {
		t.Fatal(err)
	}
	second, err := s.Upload(tok, []byte("second package bytes"))
	if err != nil {
		t.Fatal(err)
	}
	if first.Name != "v1" || first.Number != 1 || second.Name != "v2" || second.Number != 2 {
		t.Fatalf("labels %q/%d %q/%d", first.Name, first.Number, second.Name, second.Number)
	}
	if first.HarnessName != "test" {
		t.Fatalf("stored harness.json name lost: %q", first.HarnessName)
	}
	again, err := s.Upload(tok, []byte("first package bytes"))
	if err != nil || again.ID != first.ID || again.Name != "v1" {
		t.Fatalf("duplicate upload relabelled: %+v %v", again, err)
	}
	vs, err := s.Versions(tok)
	if err != nil || len(vs) != 2 || vs[0].Name != "v2" || vs[1].Name != "v1" {
		t.Fatalf("listing labels: %+v %v", vs, err)
	}
	others, err := s.Versions(other)
	if err != nil || len(others) != 1 || others[0].Name != "v1" {
		t.Fatalf("other team numbering: %+v %v", others, err)
	}
	// The stored name column (historical data) is never rewritten.
	var stored string
	if err = s.DB.QueryRow("SELECT name FROM versions WHERE id=?", second.ID).Scan(&stored); err != nil || stored != "test" {
		t.Fatalf("stored row changed: %q %v", stored, err)
	}
	view, err := batchView(s.DB, batch{VersionID: second.ID, Kind: "public", Mode: "simulated", Status: "queued"})
	if err != nil {
		t.Fatal(err)
	}
	if view["name"] != "v2" || view["harness_name"] != "test" {
		t.Fatalf("batch view label: %v / %v", view["name"], view["harness_name"])
	}
}

func TestPublicScannerErrorsOnlyPassSafeManifestMessages(t *testing.T) {
	generic := "Submission package failed format checks. Check the ZIP layout and harness.json."
	for in, want := range map[string]string{
		`harness.json: "runtime" is required and must be one of codex, deepseek or pi`: `harness.json: "runtime" is required and must be one of codex, deepseek or pi`,
		"Entrypoint file missing":                   "Entrypoint file missing",
		"harness.json: <script>":                    generic,
		"harness.json: line\nbreak":                 generic,
		"harness.json: " + strings.Repeat("x", 300): generic,
		"Traceback (most recent call last)":         generic,
		"":                                          generic,
	} {
		if got := publicScannerError(in); got != want {
			t.Errorf("publicScannerError(%q) = %q, want %q", in, got, want)
		}
	}
}
