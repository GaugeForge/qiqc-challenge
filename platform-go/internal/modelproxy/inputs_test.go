package modelproxy

import (
	"net/http"
	"testing"
)

func TestRunCannotUseProviderAsExternalNetworkOrCrossRunFileProxy(t *testing.T) {
	cfg, _, _ := config(t, func(w http.ResponseWriter, r *http.Request) { t.Fatal("forbidden request reached provider") })
	proxy := build(t, cfg)
	for _, fragment := range []string{
		`"plugins":[{"id":"web"}]`, `"provider":{"only":["unapproved"]}`, `"models":["other-model"]`,
		`"web_search_options":{}`, `"background":true`,
		`"messages":[{"content":[{"type":"image_url","image_url":{"url":"https://receiver.test/private"}}]}]`,
		`"input":[{"content":[{"type":"input_file","file_url":"https://receiver.test/private"}]}]`,
		`"input":[{"content":[{"type":"input_file","file_id":"other-team-object"}]}]`,
	} {
		out := perform(proxy, "/v1/responses", `{"model":"hy4-test",`+fragment+`}`)
		if out.Code != 400 {
			t.Fatalf("%s returned %d", fragment, out.Code)
		}
	}
	if remoteInput(map[string]any{"text": "Read the docs at https://docs.example.test"}) {
		t.Fatal("inert text rejected")
	}
	if remoteInput(map[string]any{"image_url": map[string]any{"url": "data:image/png;base64,AA=="}}) {
		t.Fatal("inline content rejected")
	}
}
