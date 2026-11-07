package modelproxy

import (
	"encoding/json"
	"net/http"
	"testing"
)

func TestNamespacesPermitLocalToolsButNeverNestedProviderTools(t *testing.T) {
	for _, kind := range []string{"function", "custom", "web_search", "mcp", "computer_use_preview"} {
		t.Run(kind, func(t *testing.T) {
			called := false
			cfg, _, _ := config(t, func(w http.ResponseWriter, r *http.Request) {
				called = true
				w.Header().Set("Content-Type", "application/json")
				w.Write([]byte(`{"output":[]}`))
			})
			body := `{"model":"hy4-test","tools":[{"type":"namespace","name":"functions","tools":[{"type":"` + kind + `","name":"local_tool"}]}]}`
			response := perform(build(t, cfg), "/v1/responses", body)
			allowed := kind == "function" || kind == "custom"
			if allowed && (response.Code != 200 || !called) {
				t.Fatal("safe namespace rejected", response.Code)
			}
			if !allowed && (response.Code != 400 || called) {
				t.Fatal("nested provider tool escaped", response.Code)
			}
		})
	}
	for _, raw := range []string{`[{"type":"namespace","name":"bad"}]`, `[{"type":"namespace","name":"","tools":[]}]`, `null`, `{}`} {
		var value any
		json.Unmarshal([]byte(raw), &value)
		n := 0
		if allowedTools(value, []string{"function", "custom"}, 0, &n) {
			t.Fatal("malformed namespace accepted")
		}
	}
	value := any([]any{map[string]any{"type": "function", "name": "local"}})
	for i := 0; i < 6; i++ {
		value = []any{map[string]any{"type": "namespace", "name": "deep", "tools": value}}
	}
	n := 0
	if allowedTools(value, []string{"function"}, 0, &n) {
		t.Fatal("unbounded namespace nesting")
	}
}
