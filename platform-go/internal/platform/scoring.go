package platform

import (
	"encoding/json"
	"errors"
	"fmt"
	"math/big"
	"os"
	"regexp"
	"sort"
)

var decimal = regexp.MustCompile(`^[0-9]+(\.[0-9]+)?([eE][+-]?[0-9]{1,3})?$`)

func durationRat(v string) (*big.Rat, error) {
	if len(v) > 128 || !decimal.MatchString(v) {
		return nil, errors.New("invalid duration")
	}
	r, ok := new(big.Rat).SetString(v)
	if !ok || r.Sign() < 0 {
		return nil, errors.New("invalid duration")
	}
	return r, nil
}
func MeanSeconds(values []string) (string, error) {
	if len(values) == 0 {
		return "", errors.New("missing durations")
	}
	sum := new(big.Rat)
	for _, v := range values {
		r, err := durationRat(v)
		if err != nil {
			return "", err
		}
		sum.Add(sum, r)
	}
	sum.Quo(sum, big.NewRat(int64(len(values)), 1))
	sum.Mul(sum, big.NewRat(1000000, 1))
	q, rem := new(big.Int), new(big.Int)
	q.QuoRem(sum.Num(), sum.Denom(), rem)
	if new(big.Int).Mul(rem, big.NewInt(2)).Cmp(sum.Denom()) >= 0 {
		q.Add(q, big.NewInt(1))
	}
	whole, fraction := new(big.Int), new(big.Int)
	whole.QuoRem(q, big.NewInt(1000000), fraction)
	return fmt.Sprintf("%s.%06d", whole.String(), fraction.Int64()), nil
}

type Standing struct {
	ID         string  `json:"id"`
	TeamID     string  `json:"team_id"`
	Team       string  `json:"team"`
	Passed     int     `json:"passed"`
	Mean       string  `json:"mean"`
	AcceptedAt float64 `json:"accepted_at"`
	SHA256     string  `json:"sha256"`
	Rank       int     `json:"rank"`
	Challenger bool    `json:"challenger"`
	Tie        bool    `json:"tie"`
}

func Rank(rows []Standing, baselinePass int, baselineMean string) ([]Standing, error) {
	base, err := durationRat(baselineMean)
	if err != nil {
		return nil, err
	}
	means := map[string]*big.Rat{}
	best := map[string]Standing{}
	challenger := ""
	var first *Standing
	less := func(a, b Standing) bool {
		if a.Passed != b.Passed {
			return a.Passed > b.Passed
		}
		c := means[a.ID].Cmp(means[b.ID])
		if c != 0 {
			return c < 0
		}
		if a.AcceptedAt != b.AcceptedAt {
			return a.AcceptedAt < b.AcceptedAt
		}
		return a.ID < b.ID
	}
	for _, row := range rows {
		mean, e := MeanSeconds([]string{row.Mean})
		if e != nil {
			return nil, e
		}
		row.Mean = mean
		r, _ := durationRat(mean)
		means[row.ID] = r
		if prev, ok := best[row.TeamID]; !ok || less(row, prev) {
			best[row.TeamID] = row
		}
		if row.Passed > baselinePass || (row.Passed == baselinePass && r.Cmp(base) < 0) {
			if first == nil || row.AcceptedAt < first.AcceptedAt || (row.AcceptedAt == first.AcceptedAt && row.ID < first.ID) {
				c := row
				first = &c
				challenger = row.TeamID
			}
		}
	}
	result := make([]Standing, 0, len(best))
	counts := map[string]int{}
	for _, row := range best {
		result = append(result, row)
		counts[fmt.Sprintf("%d:%s", row.Passed, row.Mean)]++
	}
	sort.Slice(result, func(i, j int) bool { return less(result[i], result[j]) })
	rank := 0
	for i := range result {
		if i == 0 || result[i].Passed != result[i-1].Passed || result[i].Mean != result[i-1].Mean {
			rank = i + 1
		}
		result[i].Rank = rank
		result[i].Challenger = result[i].TeamID == challenger
		result[i].Tie = counts[fmt.Sprintf("%d:%s", result[i].Passed, result[i].Mean)] > 1
	}
	return result, nil
}
func (s *Store) Leaderboard() (map[string]any, error) {
	if CompetitionPortal() {
		return s.ScientificLeaderboard()
	}
	baseline := map[string]any{"label": "Codex + Astra", "passed": float64(38), "mean": "30.000000", "simulated": true, "replacement_required": true}
	if s.BaselinePath != "" {
		if data, err := os.ReadFile(s.BaselinePath); err == nil && len(data) < 131072 {
			var parsed map[string]any
			if json.Unmarshal(data, &parsed) == nil {
				if _, ok := parsed["passed"].(float64); ok {
					baseline = parsed
				}
			}
		}
	}
	mean, _ := baseline["mean_runtime_seconds"].(string)
	if mean == "" {
		mean, _ = baseline["mean"].(string)
	}
	if mean == "" {
		mean = "30.000000"
	}
	baseline["mean"] = mean
	rows, err := s.DB.Query(`SELECT b.id,b.team_id,t.name,b.passed,b.mean,b.accepted_at,v.sha256 FROM batches b JOIN teams t ON t.id=b.team_id JOIN versions v ON v.id=b.version_id WHERE b.kind='full' AND b.mode='simulated' AND b.status='completed' AND b.invalid=0`)
	if err != nil {
		return nil, err
	}
	items := []Standing{}
	for rows.Next() {
		var r Standing
		if err = rows.Scan(&r.ID, &r.TeamID, &r.Team, &r.Passed, &r.Mean, &r.AcceptedAt, &r.SHA256); err != nil {
			rows.Close()
			return nil, err
		}
		items = append(items, r)
	}
	err = rows.Err()
	rows.Close()
	if err != nil {
		return nil, err
	}
	ranked, err := Rank(items, int(baseline["passed"].(float64)), mean)
	if err != nil {
		return nil, err
	}
	var queued, testing int
	if err = s.DB.QueryRow("SELECT COUNT(*) FROM batches WHERE status='queued'").Scan(&queued); err != nil {
		return nil, err
	}
	if err = s.DB.QueryRow("SELECT COUNT(*) FROM batches WHERE status IN ('testing','attention','cancelling')").Scan(&testing); err != nil {
		return nil, err
	}
	return map[string]any{"teams": ranked, "baseline": baseline, "queued_batches": queued, "testing_batches": testing, "mode": "simulated", "provisional": true}, nil
}
