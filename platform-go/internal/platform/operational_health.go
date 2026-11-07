package platform

import (
	"path/filepath"
)

// OperationalHealth contains counters and bounded alert codes only. It never
// reads archive contents, credentials, provider error bodies or task text.
func (s *Store) operationalHealth(q queryer) (map[string]any, error) {
	disk, diskErr := s.UploadDiskCapacity()
	state := "ok"
	alerts := []string{}
	if diskErr != nil {
		state = "unavailable"
		alerts = append(alerts, "disk_probe_failed")
	} else if !disk.Admitting {
		state = "low"
		alerts = append(alerts, "upload_disk_reserve")
	}
	counts := map[string]int{}
	for name, query := range map[string]string{
		"queued_tasks":          "SELECT COUNT(*) FROM runs WHERE state='queued'",
		"uploading_tasks":       "SELECT COUNT(*) FROM runs WHERE state='uploading'",
		"remote_reconciliation": "SELECT COUNT(*) FROM runs r JOIN batches b ON b.id=r.batch_id WHERE r.state='attention' AND r.error='worker_lost:reconciliation_required' AND b.mode!='simulated'",
		"pending_reviews":       "SELECT COUNT(*) FROM versions WHERE status='pending_review'",
	} {
		var n int
		if err := q.QueryRow(query).Scan(&n); err != nil {
			return nil, err
		}
		counts[name] = n
	}
	var expired, limited int
	if err := q.QueryRow("SELECT COUNT(*) FROM runs WHERE state='running' AND lease_until<=?", s.now()).Scan(&expired); err != nil {
		return nil, err
	}
	if err := q.QueryRow("SELECT COUNT(*) FROM usage WHERE status=429 AND created>=?", s.now()-900).Scan(&limited); err != nil {
		return nil, err
	}
	counts["expired_leases"] = expired
	counts["model_429_last_15m"] = limited
	pending, err := filepath.Glob(filepath.Join(s.Root, "failure-evidence", "*", "*", "pending.json"))
	if err != nil {
		return nil, err
	}
	counts["failed_rollout_pending"] = len(pending)
	if counts["uploading_tasks"]+len(pending) > 0 {
		alerts = append(alerts, "rollout_upload_backlog")
	}
	if counts["remote_reconciliation"] > 0 {
		alerts = append(alerts, "remote_cleanup_unconfirmed")
	}
	if expired > 0 {
		alerts = append(alerts, "worker_lease_expired")
	}
	if limited > 0 {
		alerts = append(alerts, "model_rate_limited")
	}
	return map[string]any{"sampled_at": s.now(), "disk_status": state, "upload_disk": disk, "counts": counts, "alerts": alerts, "delivery": "dashboard_only"}, nil
}
