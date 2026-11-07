// Package providerprofile describes only organizer-owned Bearer-authenticated
// OpenAI-compatible JSON/SSE endpoints. It is not a universal provider SDK.
package providerprofile

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"io"
	"net/url"
	"os"
	"path/filepath"
	"strings"
)

type Profile struct {
	Schema            string   `json:"schema"`
	ID                string   `json:"id"`
	BaseURL           string   `json:"base_url"`
	Model             string   `json:"model"`
	Endpoints         []string `json:"endpoints"`
	KeyFile           string   `json:"key_file"`
	QuotaGroup        string   `json:"quota_group"`
	RPM               int      `json:"requests_per_minute"`
	Daily             int      `json:"requests_per_day"`
	AllowPaidFallback bool     `json:"allow_paid_fallback"`
	ParameterPolicy   string   `json:"parameter_policy"`
	Authentication    string   `json:"authentication"`
	Billing           string   `json:"billing"`
}
type Identity struct {
	Schema          string   `json:"schema"`
	ID              string   `json:"id"`
	SHA256          string   `json:"sha256"`
	Model           string   `json:"model"`
	Endpoints       []string `json:"endpoints"`
	QuotaGroup      string   `json:"quota_group"`
	Authentication  string   `json:"authentication"`
	ParameterPolicy string   `json:"parameter_policy"`
	Billing         string   `json:"billing"`
}

func slug(s string) bool {
	if len(s) < 1 || len(s) > 64 {
		return false
	}
	for _, c := range s {
		if !(c >= 'a' && c <= 'z' || c >= '0' && c <= '9' || c == '-' || c == '_') {
			return false
		}
	}
	return true
}
func (p Profile) Validate() error {
	bad := errors.New("unsupported or invalid provider profile")
	if p.Schema != "qiqc-provider-profile/v1" || !slug(p.ID) || !slug(p.QuotaGroup) || p.Authentication != "bearer" || p.ParameterPolicy != "passthrough/v1" || p.AllowPaidFallback || p.RPM < 1 || p.RPM > 100000 || p.Daily < 1 || p.Daily > 10000000 || (p.Billing != "free" && p.Billing != "paid") {
		return bad
	}
	if len(p.Model) < 1 || len(p.Model) > 200 || strings.TrimSpace(p.Model) != p.Model || !filepath.IsAbs(p.KeyFile) {
		return bad
	}
	for _, s := range []string{p.BaseURL, p.Model, p.KeyFile} {
		for _, c := range s {
			if c < 32 || c > 126 {
				return bad
			}
		}
	}
	u, e := url.Parse(p.BaseURL)
	if e != nil || u.Scheme != "https" || u.Hostname() == "" || u.User != nil || u.RawQuery != "" || u.Fragment != "" || u.Opaque != "" || strings.Contains(u.Path, "..") || !publicHost(u.Hostname()) {
		return bad
	}
	if strings.EqualFold(strings.TrimRight(u.Hostname(), "."), "openrouter.ai") && p.Billing == "free" {
		if !strings.HasSuffix(p.Model, ":free") || p.RPM > 20 || p.Daily > 1000 {
			return bad
		}
	}
	if port := u.Port(); port != "" && port != "443" {
		return bad
	}
	if len(p.Endpoints) < 1 || len(p.Endpoints) > 2 {
		return bad
	}
	seen := map[string]bool{}
	for _, v := range p.Endpoints {
		if (v != "responses" && v != "chat/completions") || seen[v] {
			return bad
		}
		seen[v] = true
	}
	return nil
}
func Parse(data []byte) (Profile, Identity, error) {
	var p Profile
	var identity Identity
	if len(data) > 16384 {
		return p, identity, errors.New("provider profile too large")
	}
	dec := json.NewDecoder(bytes.NewReader(data))
	dec.DisallowUnknownFields()
	if dec.Decode(&p) != nil {
		return p, identity, errors.New("invalid provider profile JSON")
	}
	var extra any
	if dec.Decode(&extra) != io.EOF {
		return p, identity, errors.New("invalid provider profile JSON")
	}
	// Exact key presence is important: a missing allow_paid_fallback is not an
	// operator declaration. Duplicate keys are rejected by token counting.
	var raw map[string]json.RawMessage
	if json.Unmarshal(data, &raw) != nil || len(raw) != 13 {
		return p, identity, errors.New("incomplete provider profile")
	}
	for _, v := range raw {
		if bytes.Equal(bytes.TrimSpace(v), []byte("null")) {
			return p, identity, errors.New("null profile value")
		}
	}
	d := json.NewDecoder(bytes.NewReader(data))
	d.Token()
	seen := map[string]bool{}
	for d.More() {
		key, e := d.Token()
		if e != nil {
			return p, identity, e
		}
		k, ok := key.(string)
		if !ok || seen[k] {
			return p, identity, errors.New("duplicate profile key")
		}
		seen[k] = true
		var v any
		if d.Decode(&v) != nil {
			return p, identity, errors.New("invalid profile value")
		}
	}
	if e := p.Validate(); e != nil {
		return p, identity, e
	}
	// Marshal the typed object through a map for lexicographically ordered keys.
	normalized, _ := json.Marshal(p)
	var obj map[string]any
	json.Unmarshal(normalized, &obj)
	var buf bytes.Buffer
	encoder := json.NewEncoder(&buf)
	encoder.SetEscapeHTML(false)
	_ = encoder.Encode(obj)
	canonical := bytes.TrimSuffix(buf.Bytes(), []byte("\n"))
	sum := sha256.Sum256(canonical)
	identity = Identity{"qiqc-provider-profile-identity/v1", p.ID, hex.EncodeToString(sum[:]), p.Model, p.Endpoints, p.QuotaGroup, p.Authentication, p.ParameterPolicy, p.Billing}
	return p, identity, nil
}
func ReadPrivate(path string, max int64, groupReadable bool) ([]byte, error) {
	bad := errors.New("private provider file unavailable")
	if !filepath.IsAbs(path) || filepath.Clean(path) != path {
		return nil, bad
	}
	for current := path; current != "/"; current = filepath.Dir(current) {
		i, e := os.Lstat(current)
		if e != nil || i.Mode()&os.ModeSymlink != 0 {
			return nil, bad
		}
	}
	before, e := os.Lstat(path)
	mask := os.FileMode(0077)
	if groupReadable {
		mask = 0037
	}
	if e != nil || !before.Mode().IsRegular() || before.Size() > max || before.Mode().Perm()&mask != 0 {
		return nil, bad
	}
	f, e := os.Open(path)
	if e != nil {
		return nil, bad
	}
	defer f.Close()
	actual, e := f.Stat()
	if e != nil || !os.SameFile(before, actual) {
		return nil, bad
	}
	b, e := io.ReadAll(io.LimitReader(f, max+1))
	if e != nil || int64(len(b)) > max {
		return nil, bad
	}
	return b, nil
}
func Load(path string) (Profile, Identity, string, error) {
	var p Profile
	var id Identity
	b, e := ReadPrivate(path, 16384, true)
	if e != nil {
		return p, id, "", e
	}
	p, id, e = Parse(b)
	if e != nil {
		return p, id, "", e
	}
	key, e := ReadPrivate(p.KeyFile, 1024, true)
	if e != nil {
		return p, id, "", e
	}
	k := strings.TrimSpace(string(key))
	if len(k) < 12 || len(k) > 512 || strings.ContainsAny(k, "\r\n") {
		return p, id, "", errors.New("invalid provider key reference")
	}
	return p, id, k, nil
}
