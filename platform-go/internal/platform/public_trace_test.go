package platform

import (
	"archive/tar"
	"compress/gzip"
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func writeTraceArchive(t *testing.T, view map[string]any, files map[string][]byte) string {
	t.Helper()
	path := filepath.Join(t.TempDir(), "trace.tar.gz")
	f, err := os.Create(path)
	if err != nil {
		t.Fatal(err)
	}
	gz := gzip.NewWriter(f)
	tw := tar.NewWriter(gz)
	if view != nil {
		raw, _ := json.Marshal(view)
		files["public-view.txt"] = raw
	}
	for name, data := range files {
		if err := tw.WriteHeader(&tar.Header{Name: name, Mode: 0600, Size: int64(len(data))}); err != nil {
			t.Fatal(err)
		}
		if _, err := tw.Write(data); err != nil {
			t.Fatal(err)
		}
	}
	if err := tw.Close(); err != nil {
		t.Fatal(err)
	}
	if err := gz.Close(); err != nil {
		t.Fatal(err)
	}
	if err := f.Close(); err != nil {
		t.Fatal(err)
	}
	return path
}

func decodeTraceView(t *testing.T, raw []byte) map[string]any {
	t.Helper()
	var view map[string]any
	if err := json.Unmarshal(raw, &view); err != nil {
		t.Fatal(err, string(raw))
	}
	return view
}

func TestPublicTraceRecoversCommandsAndTruncatedOutput(t *testing.T) {
	taskID := "hidden_task_xyz"
	cli := strings.Join([]string{
		`{"type":"item.completed","item":{"type":"agent_message","text":"investigating hidden_task_xyz"}}`,
		`{"type":"item.completed","item":{"type":"command_execution","command":"echo hidden_task_xyz","aggregated_output":"stdout hidden_task_xyz\nsk-abcdefghijklmnopqrstuv","exit_code":0,"status":"completed"}}`,
		`{"type":"item.completed","item":{"type":"mcp_tool_call","server":"qsim","tool":"run","arguments":{"secret":"hidden_task_xyz"},"result":"ok","status":"completed"}}`,
	}, "\n") + "\n"
	path := writeTraceArchive(t, map[string]any{
		"mode": "scientific", "reward": 0, "runtime_seconds": 12.5,
		"trajectory_available": true, "trajectory_excerpt": "",
	}, map[string][]byte{"agent/native-cli.jsonl": []byte(cli)})
	raw, err := publicTraceViewFromFile(path, taskID, "Public task 3")
	if err != nil {
		t.Fatal(err)
	}
	view := decodeTraceView(t, raw)
	excerpt, _ := view["trajectory_excerpt"].(string)
	if view["trajectory_available"] != true || !strings.Contains(excerpt, "command_execution") || !strings.Contains(excerpt, `"command":"echo Public task 3"`) || !strings.Contains(excerpt, "stdout Public task 3") || !strings.Contains(excerpt, "[REDACTED]") {
		t.Fatalf("missing recovered command/output: %s", excerpt)
	}
	if strings.Contains(excerpt, taskID) || strings.Contains(excerpt, "sk-abcdefghijklmnopqrstuv") {
		t.Fatalf("task id or secret leaked: %s", excerpt)
	}
}

func TestPublicTraceFormatsFromOtherHarnesses(t *testing.T) {
	taskID := "hidden_task_xyz"
	aaa := strings.Join([]string{
		`{"type":"text","text":"using hidden_task_xyz"}`,
		`{"type":"tool_call","tool":"bash","input":"echo hidden_task_xyz"}`,
		`{"type":"tool_result","status":"completed","result":"stdout hidden_task_xyz"}`,
	}, "\n") + "\n"
	deepseekMessage := map[string]any{"method": "session.event", "params": map[string]any{"event": map[string]any{"type": "tool/call", "data": map[string]any{"name": "bash", "arguments": `{"command":"echo hidden_task_xyz"}`}}}}
	deepseekResult := map[string]any{"method": "session.event", "params": map[string]any{"event": map[string]any{"type": "tool/result", "data": map[string]any{"callId": "call-1", "message": map[string]any{"role": "tool", "content": []any{map[string]any{"type": "text", "text": "stdout hidden_task_xyz"}}}}}}}
	msg1, _ := json.Marshal(deepseekMessage)
	msg2, _ := json.Marshal(deepseekResult)
	deepseek := strings.Join([]string{
		`{"kind":"run/start"}`,
		`{"kind":"run/end","status":"completed"}`,
		`{"direction":"from-harness","message":` + string(mustJSON(t, string(msg1))) + `}`,
		`{"direction":"from-harness","message":` + string(mustJSON(t, string(msg2))) + `}`,
	}, "\n") + "\n"
	zquanta := `{"ts":1,"elapsed_s":0.1,"type":"harness_end","data":{"status":"completed","task_id":"hidden_task_xyz"}}` + "\n"
	for name, cli := range map[string]string{"aaa": aaa, "deepseek": deepseek, "zquanta": zquanta} {
		t.Run(name, func(t *testing.T) {
			path := writeTraceArchive(t, map[string]any{"trajectory_available": true, "trajectory_excerpt": ""}, map[string][]byte{"agent/native-cli.jsonl": []byte(cli)})
			raw, err := publicTraceViewFromFile(path, taskID, "Public task 2")
			if err != nil {
				t.Fatal(err)
			}
			view := decodeTraceView(t, raw)
			excerpt, _ := view["trajectory_excerpt"].(string)
			if view["trajectory_available"] != true || excerpt == "" || !strings.Contains(excerpt, "Public task 2") || strings.Contains(excerpt, taskID) {
				t.Fatalf("format not filtered: %s", excerpt)
			}
			if name != "zquanta" && !strings.Contains(excerpt, "stdout Public task 2") {
				t.Fatalf("command output missing: %s", excerpt)
			}
		})
	}
}

func mustJSON(t *testing.T, value string) []byte {
	t.Helper()
	raw, err := json.Marshal(value)
	if err != nil {
		t.Fatal(err)
	}
	return raw
}

func TestPublicTraceFallsBackToRetainedResultSummary(t *testing.T) {
	path := writeTraceArchive(t, map[string]any{
		"mode": "scientific", "reward": 0, "runtime_seconds": 10,
		"agent_stop_reason": "completed", "trajectory_available": true, "trajectory_excerpt": "",
	}, map[string][]byte{"agent/final.txt": []byte("final answer summary")})
	raw, err := publicTraceViewFromFile(path, "hidden_task", "Public task 1")
	if err != nil {
		t.Fatal(err)
	}
	view := decodeTraceView(t, raw)
	excerpt, _ := view["trajectory_excerpt"].(string)
	if view["trajectory_available"] != false || !strings.Contains(excerpt, "No retained Agent event stream") || !strings.Contains(excerpt, "final answer summary") {
		t.Fatalf("missing retained result summary: %s", excerpt)
	}
}

func TestPublicTraceTransportLimitAndTruncation(t *testing.T) {
	output := strings.Repeat("x", 300*1024)
	cli := `{"type":"item.completed","item":{"type":"command_execution","command":"cat big","aggregated_output":"` + output + `","exit_code":0,"status":"completed"}}` + "\n"
	path := writeTraceArchive(t, map[string]any{"trajectory_available": true, "trajectory_excerpt": ""}, map[string][]byte{"agent/native-cli.jsonl": []byte(cli)})
	raw, err := publicTraceViewFromFile(path, "", "Public task 1")
	if err != nil {
		t.Fatal(err)
	}
	if len(raw) > maxPublicTraceWire {
		t.Fatalf("wire trace too large: %d", len(raw))
	}
	view := decodeTraceView(t, raw)
	excerpt, _ := view["trajectory_excerpt"].(string)
	if len(excerpt) >= len(output) || view["trajectory_truncated"] != true {
		t.Fatalf("trace not bounded: %d", len(excerpt))
	}
}
