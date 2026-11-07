package platform

import (
	"net/url"
	"strconv"
	"strings"
)

// Staff history is bounded per response, but every record remains reachable.
// Filters are bound values, never SQL fragments supplied by the browser.
type snapshotOptions struct {
	pages                                              map[string]int
	versionQuery, versionState, batchQuery, batchState string
	from, until                                        int64
}

func parseSnapshotOptions(q url.Values) (snapshotOptions, error) {
	o := snapshotOptions{pages: map[string]int{}}
	for _, name := range []string{"versions", "batches", "usage", "audit"} {
		raw := q.Get(name + "_page")
		if raw == "" {
			continue
		}
		n, err := strconv.Atoi(raw)
		if err != nil || n < 0 || n > 1000000 {
			return o, problem(400, "Invalid history page.")
		}
		o.pages[name] = n
	}
	o.versionQuery = strings.TrimSpace(q.Get("version_query"))
	o.batchQuery = strings.TrimSpace(q.Get("batch_query"))
	if len(o.versionQuery) > 200 || len(o.batchQuery) > 200 {
		return o, problem(400, "History search is limited to 200 characters.")
	}
	o.versionState = q.Get("version_state")
	o.batchState = q.Get("batch_state")
	if !oneOf(o.versionState, "", "pending_review", "approved", "rejected", "cancelled") || !oneOf(o.batchState, "", "queued", "testing", "attention", "completed", "cancelled", "cancelling") {
		return o, problem(400, "Invalid history status.")
	}
	for key, target := range map[string]*int64{"version_from": &o.from, "version_until": &o.until} {
		if raw := q.Get(key); raw != "" {
			n, e := strconv.ParseInt(raw, 10, 64)
			if e != nil || n < 0 {
				return o, problem(400, "Invalid history date.")
			}
			*target = n
		}
	}
	if o.until > 0 && o.from > o.until {
		return o, problem(400, "The start date must be before the end date.")
	}
	return o, nil
}

func oneOf(value string, values ...string) bool {
	for _, v := range values {
		if value == v {
			return true
		}
	}
	return false
}

func snapshotPage(tx queryer, table, fields, order string, where []string, args []any, page int) ([]map[string]any, map[string]any, error) {
	clause := ""
	if len(where) > 0 {
		clause = " WHERE " + strings.Join(where, " AND ")
	}
	var total int
	if err := tx.QueryRow("SELECT COUNT(*) FROM "+table+clause, args...).Scan(&total); err != nil {
		return nil, nil, err
	}
	const size = 100
	pages := (total + size - 1) / size
	if pages < 1 {
		pages = 1
	}
	if page >= pages {
		page = pages - 1
	}
	params := append(append([]any{}, args...), size, page*size)
	rows, err := snapshotRows(tx, "SELECT "+fields+" FROM "+table+clause+" ORDER BY "+order+" LIMIT ? OFFSET ?", params...)
	return rows, map[string]any{"page": page, "pages": pages, "total": total, "page_size": size}, err
}

func (o snapshotOptions) versionFilters() ([]string, []any) {
	where, args := []string{}, []any{}
	if o.versionQuery != "" {
		where = append(where, "(instr(lower(t.name || ' v' || "+versionNumberSQL("v")+" || ' ' || v.name || ' ' || v.sha256 || ' ' || v.id),lower(?))>0)")
		args = append(args, o.versionQuery)
	}
	if o.versionState != "" {
		where = append(where, "v.status=?")
		args = append(args, o.versionState)
	}
	if o.from > 0 {
		where = append(where, "v.created>=?")
		args = append(args, o.from)
	}
	if o.until > 0 {
		where = append(where, "v.created<=?")
		args = append(args, o.until)
	}
	return where, args
}
func (o snapshotOptions) batchFilters() ([]string, []any) {
	where, args := []string{}, []any{}
	if o.batchQuery != "" {
		where = append(where, "instr(lower(t.name || ' ' || b.id),lower(?))>0")
		args = append(args, o.batchQuery)
	}
	if o.batchState != "" {
		where = append(where, "b.status=?")
		args = append(args, o.batchState)
	}
	return where, args
}
