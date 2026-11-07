package providerprofile

import (
	"context"
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func fixture() map[string]any {
	return map[string]any{"schema": "qiqc-provider-profile/v1", "id": "free-test", "base_url": "https://openrouter.ai/api/v1", "model": "minimax/minimax-m3.1-flash:free", "endpoints": []string{"responses", "chat/completions"}, "key_file": "/opt/private/provider.key", "quota_group": "shared-account", "requests_per_minute": 10, "requests_per_day": 50, "allow_paid_fallback": false, "parameter_policy": "passthrough/v1", "authentication": "bearer", "billing": "free"}
}

func TestPublicDialFamilyKeepsAddressBoundaryAndSafeErrors(t *testing.T) {
	for _, network := range []string{"tcp", "tcp4", "tcp6"} {
		for _, address := range []string{"127.0.0.1:443", "[::1]:443", "private-secret:443"} {
			if conn, err := DialPublic(context.Background(), network, address); conn != nil || err == nil || err.Error() != "provider address refused" {
				t.Fatal("family selection bypassed public-only address check")
			}
		}
	}
	if _, err := DialPublic(context.Background(), "private-secret", "example.com:443"); err == nil || err.Error() != "provider network refused" {
		t.Fatal("invalid network accepted or exposed")
	}
}
func TestProfileSafetyAndCanonicalIdentity(t *testing.T) {
	raw := fixture()
	b, _ := json.Marshal(raw)
	p, id, e := Parse(b)
	if e != nil {
		t.Fatal(e)
	}
	pretty, _ := json.MarshalIndent(raw, "", "  ")
	_, id2, e := Parse(pretty)
	if e != nil || id.SHA256 != id2.SHA256 {
		t.Fatal("noncanonical identity")
	}
	safe, _ := json.Marshal(id)
	if strings.Contains(string(safe), "key_file") || strings.Contains(string(safe), "base_url") {
		t.Fatal("sensitive locator in identity")
	}
	p.KeyFile = "/opt/private/another.key"
	b, _ = json.Marshal(p)
	_, other, e := Parse(b)
	if e != nil || other.SHA256 == id.SHA256 || other.QuotaGroup != id.QuotaGroup {
		t.Fatal("key reference identity/group")
	}
	for _, change := range []map[string]any{{"base_url": "http://example.com/v1"}, {"base_url": "https://127.0.0.1/v1"}, {"base_url": "https://api.example.com:8080/v1"}, {"base_url": "https://x:secret@api.example.com/v1"}, {"base_url": "https://api.example.com/v1?k=secret"}, {"endpoints": []string{"anthropic/messages"}}, {"authentication": "api-key"}, {"allow_paid_fallback": true}, {"requests_per_minute": 21}, {"model": "paid-model"}, {"unknown": true}, {"allow_paid_fallback": nil}, {"base_url": "https://OPENROUTER.AI./api/v1", "model": "paid-model"}} {
		m := fixture()
		for k, v := range change {
			m[k] = v
		}
		b, _ := json.Marshal(m)
		if _, _, e := Parse(b); e == nil {
			t.Fatal("invalid profile accepted")
		}
	}
	b, _ = json.Marshal(fixture())
	b = append([]byte(`{"id":"duplicate",`), b[1:]...)
	if _, _, e = Parse(b); e == nil {
		t.Fatal("duplicate key accepted")
	}
	m := fixture()
	delete(m, "allow_paid_fallback")
	b, _ = json.Marshal(m)
	if _, _, e = Parse(b); e == nil {
		t.Fatal("missing required declaration accepted")
	}
}
func TestOtherProviderAndLargeTrustedLimits(t *testing.T) {
	m := fixture()
	m["base_url"] = "https://api.example.com/v1"
	m["model"] = "organizer-approved-model"
	m["billing"] = "paid"
	m["requests_per_minute"] = 2000
	m["requests_per_day"] = 100000
	b, _ := json.Marshal(m)
	p, _, e := Parse(b)
	if e != nil || p.RPM != 2000 {
		t.Fatal("generic provider incorrectly subject to free account limit", e)
	}
}
func TestKeyReferencePrivateRead(t *testing.T) {
	d := t.TempDir()
	key := filepath.Join(d, "key")
	os.WriteFile(key, []byte("synthetic-only-key"), 0640)
	m := fixture()
	m["key_file"] = key
	b, _ := json.Marshal(m)
	path := filepath.Join(d, "profile.json")
	os.WriteFile(path, b, 0640)
	_, _, k, e := Load(path)
	if e != nil || k != "synthetic-only-key" {
		t.Fatal("controlled group-readable key failed", e)
	}
	os.Chmod(key, 0644)
	if _, _, _, e = Load(path); e == nil {
		t.Fatal("public key file accepted")
	}
	os.Chmod(key, 0600)
	link := filepath.Join(d, "link")
	os.Symlink(key, link)
	m["key_file"] = link
	b, _ = json.Marshal(m)
	os.WriteFile(path, b, 0600)
	if _, _, _, e = Load(path); e == nil {
		t.Fatal("symlink accepted")
	}
}
func TestPublicDialRefusesLocalAddressBeforeNetwork(t *testing.T) {
	for _, addr := range []string{"127.0.0.1:443", "169.254.169.254:443", "10.1.1.1:443", "localhost:443", "100.64.0.1:443", "192.0.2.1:443", "example.com:80"} {
		if c, e := DialPublic(context.Background(), "tcp", addr); e == nil {
			c.Close()
			t.Fatal("private address accepted")
		}
	}
}

func TestCrossLanguageCanonicalFixture(t *testing.T) {
	b, e := os.ReadFile("../../../platform/test-fixtures/provider-profile-canonical.json")
	if e != nil {
		t.Fatal(e)
	}
	var f struct {
		Profile  json.RawMessage `json:"profile"`
		Identity Identity        `json:"identity"`
	}
	if json.Unmarshal(b, &f) != nil {
		t.Fatal("fixture JSON")
	}
	_, actual, e := Parse(f.Profile)
	if e != nil {
		t.Fatal(e)
	}
	x, _ := json.Marshal(actual)
	y, _ := json.Marshal(f.Identity)
	if string(x) != string(y) {
		t.Fatalf("cross-language identity mismatch: %s != %s", x, y)
	}
}
