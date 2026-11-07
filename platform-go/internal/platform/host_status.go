package platform

import (
	"encoding/json"
	"io"
	"os"
)

// A root-owned monitor publishes this bounded, sanitized projection. The API
// cannot read monitor secrets, backups, systemd environments or execute commands.
type HostStatus struct {
	SampledAt        float64           `json:"sampled_at"`
	Status           string            `json:"status"`
	Checks           map[string]string `json:"checks"`
	ExternalDelivery bool              `json:"external_delivery"`
	Backup           struct {
		Verified     bool    `json:"verified"`
		CreatedAt    float64 `json:"created_at"`
		FilesChecked int     `json:"files_checked"`
		ZIPsChecked  int     `json:"zips_checked"`
		Offsite      bool    `json:"offsite"`
	} `json:"backup"`
	Reboot struct {
		Passed    bool    `json:"passed"`
		CheckedAt float64 `json:"checked_at"`
	} `json:"reboot"`
	Available bool `json:"available"`
	Stale     bool `json:"stale"`
}

func readHostStatus(path string, now float64) HostStatus {
	var result HostStatus
	f, err := os.Open(path)
	if err != nil {
		return result
	}
	defer f.Close()
	if json.NewDecoder(io.LimitReader(f, 32768)).Decode(&result) != nil {
		return HostStatus{}
	}
	result.Available = result.SampledAt > 0
	result.Stale = !result.Available || now-result.SampledAt > 180 || result.SampledAt > now+30
	return result
}
