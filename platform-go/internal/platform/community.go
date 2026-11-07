package platform

import (
	"database/sql"
	"encoding/json"
	"errors"
	"strings"
	"time"
	"unicode/utf8"
)

const communityPageSize = 50
const maxOpenSupportTickets = 20
const maxSupportMessages = 100

type AnnouncementInput struct {
	Version    string `json:"version"`
	Title      string `json:"title"`
	Body       string `json:"body"`
	Scope      string `json:"scope"`
	TeamID     string `json:"team_id,omitempty"`
	PRDVersion string `json:"prd_version"`
	Actor      string `json:"actor"`
	Reason     string `json:"reason"`
	Idem       string `json:"idempotency_key"`
}
type Announcement struct {
	ID              string  `json:"id"`
	Version         string  `json:"version"`
	Title           string  `json:"title"`
	Body            string  `json:"body"`
	Scope           string  `json:"scope"`
	TeamID          string  `json:"team_id,omitempty"`
	PRDVersion      string  `json:"prd_version"`
	Published       float64 `json:"published"`
	PublishedAtUTC8 string  `json:"published_at_utc8"`
	TimeZone        string  `json:"time_zone"`
}
type SupportInput struct {
	Subject       string `json:"subject"`
	Body          string `json:"body"`
	BatchID       string `json:"batch_id,omitempty"`
	SubmissionSHA string `json:"submission_sha,omitempty"`
	EventID       string `json:"event_id,omitempty"`
	Idem          string `json:"idempotency_key"`
}
type SupportReply struct {
	Body   string `json:"body"`
	Status string `json:"status,omitempty"`
	Actor  string `json:"actor,omitempty"`
	Reason string `json:"reason,omitempty"`
	Idem   string `json:"idempotency_key"`
}
type SupportMessage struct {
	ID         string  `json:"id"`
	Sequence   int     `json:"sequence"`
	AuthorRole string  `json:"author_role"`
	Body       string  `json:"body"`
	Created    float64 `json:"created"`
}
type SupportTicket struct {
	ID            string           `json:"id"`
	TeamID        string           `json:"team_id"`
	Team          string           `json:"team"`
	Subject       string           `json:"subject"`
	BatchID       string           `json:"batch_id,omitempty"`
	SubmissionSHA string           `json:"submission_sha,omitempty"`
	EventID       string           `json:"event_id,omitempty"`
	Status        string           `json:"status"`
	Created       float64          `json:"created"`
	Updated       float64          `json:"updated"`
	Messages      []SupportMessage `json:"messages,omitempty"`
}

func communityText(text string, min, max int) bool {
	return utf8.ValidString(text) && !strings.ContainsRune(text, 0) && utf8.RuneCountInString(text) >= min && utf8.RuneCountInString(text) <= max
}
func communityAuditInput(actor, reason, idem string) error {
	if !communityText(actor, 1, 120) || !communityText(reason, 3, 1000) || !communityText(idem, 1, 120) {
		return problem(400, "请填写操作者、操作理由和有效请求标识")
	}
	return nil
}
func communityPage(page int) error {
	if page < 0 || page > 10000 {
		return problem(400, "无效分页参数")
	}
	return nil
}
func communitySession(s *Store, q queryer, token string) (User, error) {
	u, err := s.session(q, token)
	if err != nil {
		return u, err
	}
	if u.MustChange != 0 {
		return u, problem(403, "请先修改初始密码")
	}
	if u.Role != "team" && u.Role != "admin" {
		return u, problem(403, "无权访问")
	}
	return u, nil
}
func operationReplay(tx *sql.Tx, u User, key, kind string, payload any) (string, error) {
	if !communityText(key, 1, 120) {
		return "", problem(400, "缺少有效请求标识")
	}
	raw, err := json.Marshal(payload)
	if err != nil {
		return "", err
	}
	hash := shaBytes(raw)
	var existingKind, existingHash, objectID string
	err = tx.QueryRow("SELECT kind,request_hash,object_id FROM communication_operations WHERE user_id=? AND operation_key=?", u.ID, key).Scan(&existingKind, &existingHash, &objectID)
	if err == nil {
		if existingKind != kind || existingHash != hash {
			return "", problem(409, "请求标识已用于不同内容")
		}
		return objectID, nil
	}
	if !errors.Is(err, sql.ErrNoRows) {
		return "", err
	}
	return "", nil
}
func recordOperation(tx *sql.Tx, u User, key, kind string, payload any, objectID string, now float64) error {
	raw, err := json.Marshal(payload)
	if err != nil {
		return err
	}
	_, err = tx.Exec("INSERT INTO communication_operations(user_id,operation_key,kind,request_hash,object_id,created) VALUES(?,?,?,?,?,?)", u.ID, key, kind, shaBytes(raw), objectID, now)
	return err
}
func announcementRow(row interface{ Scan(...any) error }) (Announcement, error) {
	var a Announcement
	err := row.Scan(&a.ID, &a.Version, &a.Title, &a.Body, &a.Scope, &a.TeamID, &a.PRDVersion, &a.Published)
	if err == nil {
		a.TimeZone = "Asia/Singapore"
		a.PublishedAtUTC8 = time.Unix(int64(a.Published), 0).In(time.FixedZone(a.TimeZone, 8*3600)).Format(time.RFC3339)
	}
	return a, err
}

const announcementFields = "id,version,title,body,scope,COALESCE(team_id,''),prd_version,published"

func (s *Store) PublishAnnouncement(token string, in AnnouncementInput, ip string) (Announcement, error) {
	var result Announcement
	in.Version = strings.TrimSpace(in.Version)
	in.Title = strings.TrimSpace(in.Title)
	in.Body = strings.TrimSpace(in.Body)
	in.Actor = strings.TrimSpace(in.Actor)
	in.Reason = strings.TrimSpace(in.Reason)
	config, err := s.Config()
	if err != nil {
		return result, err
	}
	err = s.write(func(tx *sql.Tx) error {
		u, err := communitySession(s, tx, token)
		if err != nil {
			return err
		}
		if u.Role != "admin" {
			return problem(403, "仅管理员可发布公告")
		}
		if err = communityAuditInput(in.Actor, in.Reason, in.Idem); err != nil {
			return err
		}
		if !communityText(in.Version, 1, 80) || !communityText(in.Title, 1, 120) || !communityText(in.Body, 3, 8000) {
			return problem(400, "公告版本、标题或内容长度无效")
		}
		// Config() is the public rule authority; its current rule version is static
		// in this release. Read before accepting any new public statement.
		if in.PRDVersion != config["rule_version"] {
			return problem(409, "公告必须引用当前PRD规则版本")
		}
		if in.Scope != "public" && in.Scope != "teams" && in.Scope != "team" {
			return problem(400, "无效公告范围")
		}
		if in.Scope != "team" && in.TeamID != "" {
			return problem(400, "公开或全队公告不能关联单队资料")
		}
		if in.Scope == "team" {
			var exists int
			if err = tx.QueryRow("SELECT COUNT(*) FROM teams WHERE id=?", in.TeamID).Scan(&exists); err != nil {
				return err
			}
			if exists != 1 {
				return problem(404, "目标不可用")
			}
		}
		previous, err := operationReplay(tx, u, in.Idem, "announcement_publish", in)
		if err != nil {
			return err
		}
		if previous != "" {
			result, err = announcementRow(tx.QueryRow("SELECT "+announcementFields+" FROM announcements WHERE id=?", previous))
			return err
		}
		var count int
		if err = tx.QueryRow("SELECT COUNT(*) FROM announcements WHERE version=?", in.Version).Scan(&count); err != nil {
			return err
		}
		if count != 0 {
			return problem(409, "公告版本已发布；修改内容请使用新版本")
		}
		aid, now := id(), s.now()
		var target any
		if in.TeamID != "" {
			target = in.TeamID
		}
		if _, err = tx.Exec("INSERT INTO announcements(id,version,title,body,scope,team_id,prd_version,published,created_by) VALUES(?,?,?,?,?,?,?,?,?)", aid, in.Version, in.Title, in.Body, in.Scope, target, in.PRDVersion, now, u.ID); err != nil {
			return err
		}
		after, _ := json.Marshal(map[string]string{"version": in.Version, "scope": in.Scope, "prd_version": in.PRDVersion, "content_sha256": shaBytes([]byte(in.Title + "\x00" + in.Body))})
		if err = s.audit(tx, u, token, ip, in.Actor, "announcement_publish", aid, in.Reason, "null", string(after)); err != nil {
			return err
		}
		if err = recordOperation(tx, u, in.Idem, "announcement_publish", in, aid, now); err != nil {
			return err
		}
		result, err = announcementRow(tx.QueryRow("SELECT "+announcementFields+" FROM announcements WHERE id=?", aid))
		return err
	})
	return result, err
}
func (s *Store) announcementAccess(token string) (string, string, error) {
	if token == "" {
		return "", "", nil
	}
	u, err := communitySession(s, s.DB, token)
	return u.Role, u.TeamID, err
}
func (s *Store) ListAnnouncements(token string, page int) ([]Announcement, error) {
	if err := communityPage(page); err != nil {
		return nil, err
	}
	role, team, err := s.announcementAccess(token)
	if err != nil {
		return nil, err
	}
	rows, err := s.DB.Query("SELECT "+announcementFields+" FROM announcements WHERE ?='admin' OR scope='public' OR (?='team' AND (scope='teams' OR (scope='team' AND team_id=?))) ORDER BY published DESC,id DESC LIMIT ? OFFSET ?", role, role, team, communityPageSize, page*communityPageSize)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	result := []Announcement{}
	for rows.Next() {
		a, err := announcementRow(rows)
		if err != nil {
			return nil, err
		}
		result = append(result, a)
	}
	return result, rows.Err()
}
func (s *Store) GetAnnouncement(token, aid string) (Announcement, error) {
	role, team, err := s.announcementAccess(token)
	if err != nil {
		return Announcement{}, err
	}
	a, err := announcementRow(s.DB.QueryRow("SELECT "+announcementFields+" FROM announcements WHERE id=? AND (?='admin' OR scope='public' OR (?='team' AND (scope='teams' OR (scope='team' AND team_id=?))))", aid, role, role, team))
	if errors.Is(err, sql.ErrNoRows) {
		err = problem(404, "公告不可访问")
	}
	return a, err
}
