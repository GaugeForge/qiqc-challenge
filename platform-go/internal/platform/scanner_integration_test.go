package platform

import (
	"archive/zip"
	"bytes"
	"errors"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestDockerScannerFixedImageAndStoredProvenance(t *testing.T) {
	if os.Getenv("QIQC_REAL_SCANNER_TEST") != "1" {
		t.Skip("explicit local Docker integration")
	}
	parser, err := filepath.Abs("../../../platform/qiqc/archive.py")
	if err != nil {
		t.Fatal(err)
	}
	s := testStore(t)
	s.Scanner = DockerScanner(parser, "qiqcbench-agent:latest")
	token := readyUser(t, s, "real-scanner@example.test", "team")
	var buf bytes.Buffer
	writer := zip.NewWriter(&buf)
	for name, body := range map[string]string{"harness.json": `{"schema":"qiqc-harness/v1-demo","name":"M02 integration","entrypoint":"adapter.sh"}`, "adapter.sh": "#!/bin/sh\nexit 0\n"} {
		f, e := writer.Create(name)
		if e != nil {
			t.Fatal(e)
		}
		if _, e = f.Write([]byte(body)); e != nil {
			t.Fatal(e)
		}
	}
	if err = writer.Close(); err != nil {
		t.Fatal(err)
	}
	v, err := s.Upload(token, buf.Bytes())
	if err != nil {
		t.Fatal(err)
	}
	if v.Status != "pending_review" {
		t.Fatal("unreviewed package automatically approved")
	}
	var raw string
	if err = s.DB.QueryRow("SELECT report FROM versions WHERE id=?", v.ID).Scan(&raw); err != nil {
		t.Fatal(err)
	}
	if !bytes.Contains([]byte(raw), []byte(`"ruleset_version":"qiqc-archive/2"`)) || !bytes.Contains([]byte(raw), []byte(`"scanner_image":"sha256:`)) {
		t.Fatal("missing immutable provenance")
	}
	if _, err = s.Upload(token, []byte("bad zip")); status(err) != 400 {
		t.Fatal(err)
	}
	var n int
	s.DB.QueryRow("SELECT COUNT(*) FROM ledger").Scan(&n)
	if n != 0 {
		t.Fatal("scanner debited quota")
	}
}

func TestDockerScannerMinimalHarnessManifest(t *testing.T) {
	if os.Getenv("QIQC_REAL_SCANNER_TEST") != "1" {
		t.Skip("explicit local Docker integration")
	}
	parser, err := filepath.Abs("../../../platform/qiqc/archive.py")
	if err != nil {
		t.Fatal(err)
	}
	s := testStore(t)
	s.Scanner = DockerScanner(parser, "qiqcbench-agent:latest")
	token := readyUser(t, s, "minimal-scanner@example.test", "team")
	pack := func(manifest string) []byte {
		var buf bytes.Buffer
		writer := zip.NewWriter(&buf)
		for _, file := range [][2]string{{"harness.json", manifest}, {"adapter/run.sh", "#!/bin/sh\nexit 0\n"}, {"harness/agent.py", "print(1)\n"}} {
			f, e := writer.Create(file[0])
			if e != nil {
				t.Fatal(e)
			}
			if _, e = f.Write([]byte(file[1])); e != nil {
				t.Fatal(e)
			}
		}
		if e := writer.Close(); e != nil {
			t.Fatal(e)
		}
		return buf.Bytes()
	}
	v, err := s.Upload(token, pack(`{"runtime":"pi"}`))
	if err != nil {
		t.Fatal(err)
	}
	if v.Name != "v1" || v.HarnessName != "Pi Agent harness" {
		t.Fatalf("label %q harness name %q", v.Name, v.HarnessName)
	}
	var raw string
	if err = s.DB.QueryRow("SELECT report FROM versions WHERE id=?", v.ID).Scan(&raw); err != nil {
		t.Fatal(err)
	}
	for _, want := range []string{`"runtime":"pi"`, `"adapter":"pi-agent"`, `"runtime_version":"1.5.0"`, `"task_transport":"native-http/v1"`} {
		if !bytes.Contains([]byte(raw), []byte(want)) {
			t.Fatalf("derived field %s missing from %s", want, raw)
		}
	}
	_, err = s.Upload(token, pack(`{"runtime":"codex","runtime_version":"9.9.9"}`))
	var p *Problem
	if !errors.As(err, &p) || p.Code != 400 || !strings.Contains(p.Message, `"runtime_version" is not a supported codex version`) {
		t.Fatalf("contradiction not reported clearly: %v", err)
	}
}
