package platform

import (
	"database/sql"
	"errors"
	"strings"
)

// SQLite cannot alter a CHECK constraint. Rebuild only the two leaf target
// tables in one transaction; copy every frozen binding without changing it.
func (s *Store) ensureScientificBudgetSchema() error {
	return s.write(func(tx *sql.Tx) error {
		for _, table := range []string{"scientific_targets", "batch_scientific_targets"} {
			var existing string
			if err := tx.QueryRow("SELECT sql FROM sqlite_master WHERE type='table' AND name=?", table).Scan(&existing); err != nil {
				return err
			}
			normalized := strings.Join(strings.Fields(strings.ToLower(existing)), "")
			if strings.Contains(normalized, "check(agent_budget_secondsin(180,300))") {
				continue
			}
			if !strings.Contains(normalized, "check(agent_budget_seconds=300)") {
				return errors.New("unrecognized scientific budget schema")
			}
			prefix := "CREATE TABLE IF NOT EXISTS " + table + "("
			start := strings.Index(schema, prefix)
			if start < 0 {
				return errors.New("scientific budget schema missing")
			}
			end := strings.Index(schema[start:], ";")
			if end < 0 {
				return errors.New("scientific budget schema invalid")
			}
			definition := strings.Replace(schema[start:start+end+1], prefix, "CREATE TABLE "+table+"_budget_upgrade(", 1)
			if _, err := tx.Exec(definition); err != nil {
				return err
			}
			if _, err := tx.Exec("INSERT INTO " + table + "_budget_upgrade SELECT * FROM " + table); err != nil {
				return err
			}
			if _, err := tx.Exec("DROP TABLE " + table); err != nil {
				return err
			}
			if _, err := tx.Exec("ALTER TABLE " + table + "_budget_upgrade RENAME TO " + table); err != nil {
				return err
			}
		}
		return nil
	})
}
