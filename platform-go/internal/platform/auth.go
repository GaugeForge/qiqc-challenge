package platform

import (
	"crypto/rand"
	"crypto/subtle"
	"database/sql"
	"encoding/base64"
	"encoding/hex"
	"errors"
	"fmt"
	"golang.org/x/crypto/scrypt"
	"net/mail"
	"strings"
	"unicode/utf8"
)

type User struct {
	ID         string         `json:"id"`
	Email      string         `json:"email"`
	TeamID     string         `json:"team_id"`
	Team       string         `json:"team"`
	Role       string         `json:"role"`
	MustChange int            `json:"must_change"`
	Quotas     map[string]int `json:"quotas"`
}

func validatePassword(password string) error {
	if !utf8.ValidString(password) || utf8.RuneCountInString(password) < 12 || utf8.RuneCountInString(password) > 200 {
		return problem(400, "密码长度为 12–200 个字符")
	}
	return nil
}
func hashPassword(password string) (string, error) {
	if err := validatePassword(password); err != nil {
		return "", err
	}
	salt := make([]byte, 16)
	if _, err := rand.Read(salt); err != nil {
		return "", err
	}
	k, err := scrypt.Key([]byte(password), salt, 16384, 8, 1, 64)
	if err != nil {
		return "", err
	}
	return hex.EncodeToString(salt) + ":" + hex.EncodeToString(k), nil
}
func passwordMatches(password, stored string) bool {
	p := strings.Split(stored, ":")
	if len(p) != 2 {
		return false
	}
	salt, e := hex.DecodeString(p[0])
	if e != nil || len(salt) != 16 {
		return false
	}
	want, e := hex.DecodeString(p[1])
	if e != nil || len(want) != 64 {
		return false
	}
	got, e := scrypt.Key([]byte(password), salt, 16384, 8, 1, 64)
	return e == nil && subtle.ConstantTimeCompare(got, want) == 1
}
func quotas(q queryer, team string) (map[string]int, error) {
	result := map[string]int{"full": 1, "public": 3, "infra": 10}
	rows, err := q.Query("SELECT kind,SUM(delta) FROM ledger WHERE team_id=? GROUP BY kind", team)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	for rows.Next() {
		var k string
		var n int
		if err = rows.Scan(&k, &n); err != nil {
			return nil, err
		}
		result[k] += n
	}
	return result, rows.Err()
}
func readUser(q queryer, uid string) (User, error) {
	var u User
	err := q.QueryRow(`SELECT u.id,u.email,u.team_id,t.name,u.role,u.must_change FROM users u JOIN teams t ON t.id=u.team_id WHERE u.id=?`, uid).Scan(&u.ID, &u.Email, &u.TeamID, &u.Team, &u.Role, &u.MustChange)
	if err != nil {
		return u, rowError(err)
	}
	u.Quotas, err = quotas(q, u.TeamID)
	return u, err
}
func (s *Store) CreateAccount(email, password, team, role string) (User, error) {
	return s.createAccount(email, password, team, role, nil)
}
func (s *Store) createAccount(email, password, team, role string, authorize func(*sql.Tx, string, string) error) (User, error) {
	email = strings.ToLower(strings.TrimSpace(email))
	parsed, e := mail.ParseAddress(email)
	if e != nil || parsed.Address != email || len(email) > 200 || strings.TrimSpace(team) == "" || utf8.RuneCountInString(team) > 100 || (role != "team" && role != "admin") {
		return User{}, problem(400, "无效账号参数")
	}
	hash, err := hashPassword(password)
	if err != nil {
		return User{}, err
	}
	uid, tid := id(), id()
	err = s.write(func(tx *sql.Tx) error {
		var exists int
		if err := tx.QueryRow("SELECT COUNT(*) FROM users WHERE email=?", email).Scan(&exists); err != nil {
			return err
		}
		if exists > 0 {
			return problem(409, "账号已存在")
		}
		if _, err := tx.Exec("INSERT INTO teams(id,name) VALUES(?,?)", tid, team); err != nil {
			return err
		}
		_, err := tx.Exec("INSERT INTO users(id,email,password,team_id,role,must_change) VALUES(?,?,?,?,?,1)", uid, email, hash, tid, role)
		if err != nil {
			return err
		}
		if authorize != nil {
			return authorize(tx, uid, tid)
		}
		return nil
	})
	if err != nil {
		return User{}, err
	}
	return readUser(s.DB, uid)
}
func (s *Store) Login(email, password, ip string) (string, User, error) {
	if len(email) > 200 || utf8.RuneCountInString(password) > 200 {
		return "", User{}, problem(400, "无效登录参数")
	}
	email = strings.ToLower(email)
	identity := ip + ":" + email
	// Bound memory used by scrypt, even across many simultaneous login requests.
	s.authSlots <- struct{}{}
	defer func() { <-s.authSlots }()
	var uid, stored string
	err := s.DB.QueryRow("SELECT id,password FROM users WHERE email=?", email).Scan(&uid, &stored)
	if err != nil && !errors.Is(err, sql.ErrNoRows) {
		return "", User{}, err
	}
	valid := false
	if uid != "" {
		valid = passwordMatches(password, stored)
	} else {
		// Equal-cost work avoids a cheap nonexistent-account path.
		scrypt.Key([]byte(password), make([]byte, 16), 16384, 8, 1, 64)
	}
	raw := make([]byte, 32)
	if _, err = rand.Read(raw); err != nil {
		return "", User{}, err
	}
	token := base64.RawURLEncoding.EncodeToString(raw)
	var denied error
	err = s.write(func(tx *sql.Tx) error {
		now := s.now()
		if _, err := tx.Exec("DELETE FROM login_attempts WHERE created<?", now-900); err != nil {
			return err
		}
		for _, limit := range []struct {
			key string
			n   int
		}{{identity, 6}, {"ip:" + ip, 60}, {"account:" + email, 20}} {
			var n int
			if err := tx.QueryRow("SELECT COUNT(*) FROM login_attempts WHERE identity=?", limit.key).Scan(&n); err != nil {
				return err
			}
			if n >= limit.n {
				denied = problem(429, "尝试次数过多，请 15 分钟后重试")
				return nil
			}
		}
		if valid {
			var current string
			if err := tx.QueryRow("SELECT password FROM users WHERE id=?", uid).Scan(&current); err != nil {
				return err
			}
			valid = current == stored
		}
		if !valid {
			for _, key := range []string{identity, "ip:" + ip, "account:" + email} {
				if _, err := tx.Exec("INSERT INTO login_attempts(identity,created) VALUES(?,?)", key, now); err != nil {
					return err
				}
			}
			denied = problem(401, "邮箱或密码不正确")
			return nil
		}
		if _, err := tx.Exec("INSERT INTO sessions(hash,user_id,expires,ip) VALUES(?,?,?,?)", tokenHash(token), uid, now+43200, ip); err != nil {
			return err
		}
		_, err := tx.Exec("DELETE FROM login_attempts WHERE identity=?", identity)
		return err
	})
	if err != nil {
		return "", User{}, err
	}
	if denied != nil {
		return "", User{}, denied
	}
	u, err := readUser(s.DB, uid)
	return token, u, err
}
func (s *Store) session(q queryer, token string) (User, error) {
	if len(token) == 0 || len(token) > 256 {
		return User{}, problem(401, "请先登录")
	}
	var uid string
	if err := q.QueryRow("SELECT user_id FROM sessions WHERE hash=? AND expires>?", tokenHash(token), s.now()).Scan(&uid); err != nil {
		return User{}, rowError(err)
	}
	return readUser(q, uid)
}
func (s *Store) Session(token string) (User, error) { return s.session(s.DB, token) }
func (s *Store) Logout(token string) error {
	_, err := s.DB.Exec("DELETE FROM sessions WHERE hash=?", tokenHash(token))
	return err
}
func (s *Store) ChangePassword(token, password string) error {
	u, err := s.Session(token)
	if err != nil {
		return err
	}
	s.authSlots <- struct{}{}
	defer func() { <-s.authSlots }()
	if err := validatePassword(password); err != nil {
		return err
	}
	var previous string
	if err := s.DB.QueryRow("SELECT password FROM users WHERE id=?", u.ID).Scan(&previous); err != nil {
		return err
	}
	// Re-entering the emailed password must not clear the first-change gate.
	// Do expensive password work outside the write transaction, then fence the
	// snapshot below so a concurrent reset cannot be silently overwritten.
	if passwordMatches(password, previous) {
		return problem(400, "新密码不能与当前密码相同")
	}
	hash, err := hashPassword(password)
	if err != nil {
		return err
	}
	return s.write(func(tx *sql.Tx) error {
		u, err := s.session(tx, token)
		if err != nil {
			return err
		}
		var current string
		if err := tx.QueryRow("SELECT password FROM users WHERE id=?", u.ID).Scan(&current); err != nil {
			return err
		}
		if current != previous {
			return problem(409, "密码状态已变化，请重新登录后重试")
		}
		if _, err = tx.Exec("UPDATE users SET password=?,must_change=0 WHERE id=?", hash, u.ID); err != nil {
			return err
		}
		_, err = tx.Exec("DELETE FROM sessions WHERE user_id=? AND hash!=?", u.ID, tokenHash(token))
		return err
	})
}
func (s *Store) ResetPassword(token, target, password, actor, reason, ip string) error {
	u, err := s.Session(token)
	if err != nil {
		return err
	}
	if u.Role != "admin" || u.MustChange != 0 {
		return problem(403, "仅完成改密的管理员可访问")
	}
	actor = strings.TrimSpace(actor)
	reason = strings.TrimSpace(reason)
	if actor == "" || len(actor) > 240 || utf8.RuneCountInString(reason) < 3 || utf8.RuneCountInString(reason) > 1000 {
		return problem(400, "请输入操作者和操作理由")
	}
	s.authSlots <- struct{}{}
	defer func() { <-s.authSlots }()
	hash, err := hashPassword(password)
	if err != nil {
		return err
	}
	return s.write(func(tx *sql.Tx) error {
		u, err := s.session(tx, token)
		if err != nil {
			return err
		}
		if u.Role != "admin" || u.MustChange != 0 {
			return problem(403, "仅完成改密的管理员可访问")
		}
		targetUser, err := readUser(tx, target)
		if err != nil {
			return problem(404, "账号不存在")
		}
		// Team passwords set by staff are final: teams sign in directly and are
		// not forced through the first-login change. Staff accounts still are.
		mustChange := 1
		if targetUser.Role == "team" {
			mustChange = 0
		}
		after := fmt.Sprintf(`{"sessions":"revoked","must_change":%d}`, mustChange)
		if err = s.audit(tx, u, token, ip, actor, "password_reset", target, reason, `{"sessions":"active_or_expired"}`, after); err != nil {
			return err
		}
		if _, err = tx.Exec("UPDATE users SET password=?,must_change=? WHERE id=?", hash, mustChange, targetUser.ID); err != nil {
			return err
		}
		if _, err = tx.Exec("DELETE FROM sessions WHERE user_id=?", target); err != nil {
			return err
		}
		_, err = tx.Exec("INSERT INTO outbox(id,event,team_id,payload,created) VALUES(?,'password_reset',?,'{\"delivery\":\"manual_demo\",\"contains_password\":false}',?)", id(), targetUser.TeamID, s.now())
		return err
	})
}
