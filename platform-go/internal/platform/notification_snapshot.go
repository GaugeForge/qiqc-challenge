package platform

// Notification delivery summaries contain no recipient, payload or provider
// error. Outbox intent records retain their original local_only status.
func snapshotNotificationDelivery(tx queryer) (map[string]any, error) {
	result := map[string]any{"default_mode": "local_only", "real_mail_enabled": false, "counts": []map[string]any{}}
	var exists int
	if err := tx.QueryRow("SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='notification_deliveries'").Scan(&exists); err != nil {
		return nil, err
	}
	if exists == 0 {
		return result, nil
	}
	rows, err := tx.Query("SELECT mode,status,COUNT(*) FROM notification_deliveries GROUP BY mode,status ORDER BY mode,status")
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	counts := []map[string]any{}
	for rows.Next() {
		var mode, status string
		var n int
		if err = rows.Scan(&mode, &status, &n); err != nil {
			return nil, err
		}
		counts = append(counts, map[string]any{"mode": mode, "status": status, "count": n})
	}
	result["counts"] = counts
	return result, rows.Err()
}
