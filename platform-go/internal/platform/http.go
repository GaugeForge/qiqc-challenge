package platform

import (
	"encoding/json"
	"errors"
	"io"
	"net"
	"net/http"
	"net/url"
	"os"
	"strings"
)

type endpoint func(http.ResponseWriter, *http.Request) error

func respond(w http.ResponseWriter, v any) {
	w.Header().Set("Content-Type", "application/json")
	json.NewEncoder(w).Encode(v)
}
func body(w http.ResponseWriter, r *http.Request, v any) error {
	r.Body = http.MaxBytesReader(w, r.Body, 65536)
	d := json.NewDecoder(r.Body)
	if err := d.Decode(v); err != nil {
		var limit *http.MaxBytesError
		if errors.As(err, &limit) {
			return problem(413, "请求过大")
		}
		return problem(400, "请求格式无效")
	}
	if err := d.Decode(new(any)); err != io.EOF {
		return problem(400, "请求格式无效")
	}
	return nil
}
func clientIP(r *http.Request) string {
	host, _, err := net.SplitHostPort(r.RemoteAddr)
	if err != nil {
		return r.RemoteAddr
	}
	// Trust only the explicitly configured local reverse proxy. Nginx overwrites
	// this header. Direct external callers cannot spoof audit/rate-limit identity.
	if os.Getenv("QIQC_TRUST_LOOPBACK_PROXY") == "1" && net.ParseIP(host) != nil && net.ParseIP(host).IsLoopback() {
		if forwarded := net.ParseIP(r.Header.Get("X-Real-IP")); forwarded != nil {
			return forwarded.String()
		}
	}
	return host
}

// Workspace selects a browser session, never an authorization role. Existing
// non-browser clients retain their legacy session cookie; browser workspaces
// never fall back to it or borrow the other workspace's identity.
func sessionCookieName(r *http.Request) string {
	switch r.Header.Get("X-QIQC-Workspace") {
	case "staff":
		return "qiqc_staff_session"
	case "team":
		return "qiqc_team_session"
	default:
		return "qiqc_session"
	}
}
func cookieToken(r *http.Request) string {
	c, err := r.Cookie(sessionCookieName(r))
	if err != nil {
		return ""
	}
	return c.Value
}
func endpointHandler(method string, fn endpoint) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Cache-Control", "no-store")
		w.Header().Set("Content-Type", "application/json")
		w.Header().Set("X-QIQC-Module", "go-native")
		var err error
		if workspace := r.Header.Get("X-QIQC-Workspace"); workspace != "" && workspace != "staff" && workspace != "team" {
			err = problem(400, "Invalid workspace.")
		} else if r.Method != method {
			w.Header().Set("Allow", method)
			err = problem(405, "不支持的请求方法")
		} else if method != "GET" {
			if origin := r.Header.Get("Origin"); origin != "" {
				u, e := url.Parse(origin)
				if e != nil || u.Host != r.Host || (u.Scheme != "http" && u.Scheme != "https") {
					err = problem(403, "不允许跨站修改请求")
				}
			}
			if err == nil && r.URL.Path != "/api/login" && r.Header.Get("X-QIQC-Request") != "1" {
				err = problem(403, "缺少请求保护标记")
			}
		}
		if err == nil {
			err = fn(w, r)
		}
		if err != nil {
			code := status(err)
			message := "The platform is temporarily unavailable. Please try again."
			var p *Problem
			if errors.As(err, &p) {
				message = englishMessage(p.Message)
			}
			w.WriteHeader(code)
			respond(w, map[string]string{"error": message})
		}
	}
}
func (s *Store) Register(mux *http.ServeMux) {
	s.registerAssistant(mux)
	mux.HandleFunc("/api/admin/accounts", endpointHandler("POST", func(w http.ResponseWriter, r *http.Request) error {
		var d *struct{ Email, Password, Team, Role, Actor, Reason string }
		if err := body(w, r, &d); err != nil {
			return err
		}
		if d == nil {
			return problem(400, "无效账号参数")
		}
		u, err := s.IssueAccountWithRole(cookieToken(r), d.Email, d.Password, d.Team, d.Role, d.Actor, d.Reason, clientIP(r))
		if err != nil {
			return err
		}
		respond(w, u)
		return nil
	}))
	mux.HandleFunc("/api/admin/accounts/bulk", endpointHandler("POST", func(w http.ResponseWriter, r *http.Request) error {
		var d *struct {
			Accounts []AccountInput `json:"accounts"`
			Actor    string         `json:"actor"`
			Reason   string         `json:"reason"`
		}
		if err := body(w, r, &d); err != nil {
			return err
		}
		if d == nil {
			return problem(400, "无效账号参数")
		}
		result, err := s.IssueTeamAccounts(cookieToken(r), d.Accounts, d.Actor, d.Reason, clientIP(r))
		if err != nil {
			return err
		}
		respond(w, result)
		return nil
	}))

	mux.HandleFunc("/api/login", endpointHandler("POST", func(w http.ResponseWriter, r *http.Request) error {
		var d *struct {
			Email    string `json:"email"`
			Password string `json:"password"`
		}
		if err := body(w, r, &d); err != nil {
			return err
		}
		if d == nil {
			return problem(400, "请求格式无效")
		}
		token, u, err := s.Login(d.Email, d.Password, clientIP(r))
		if err != nil {
			return err
		}
		workspace := r.Header.Get("X-QIQC-Workspace")
		if (workspace == "team" && u.Role != "team") || (workspace == "staff" && u.Role != "admin") {
			if err := s.Logout(token); err != nil {
				return err
			}
			return problem(403, "Use the account issued for this workspace.")
		}
		http.SetCookie(w, &http.Cookie{Name: sessionCookieName(r), Value: token, Path: "/", MaxAge: 43200, HttpOnly: true, Secure: s.SecureCookies, SameSite: http.SameSiteStrictMode})
		respond(w, u)
		return nil
	}))
	mux.HandleFunc("/api/me", endpointHandler("GET", func(w http.ResponseWriter, r *http.Request) error {
		u, err := s.Session(cookieToken(r))
		if err != nil {
			return err
		}
		respond(w, u)
		return nil
	}))
	mux.HandleFunc("/api/logout", endpointHandler("POST", func(w http.ResponseWriter, r *http.Request) error {
		if err := s.Logout(cookieToken(r)); err != nil {
			return err
		}
		http.SetCookie(w, &http.Cookie{Name: sessionCookieName(r), Path: "/", MaxAge: -1, HttpOnly: true, Secure: s.SecureCookies, SameSite: http.SameSiteStrictMode})
		respond(w, map[string]bool{"ok": true})
		return nil
	}))
	mux.HandleFunc("/api/password", endpointHandler("POST", func(w http.ResponseWriter, r *http.Request) error {
		var d *struct {
			Password string `json:"password"`
		}
		if err := body(w, r, &d); err != nil {
			return err
		}
		if d == nil {
			return problem(400, "请求格式无效")
		}
		if err := s.ChangePassword(cookieToken(r), d.Password); err != nil {
			return err
		}
		respond(w, map[string]bool{"ok": true})
		return nil
	}))
	mux.HandleFunc("/api/admin/accounts/reset", endpointHandler("POST", func(w http.ResponseWriter, r *http.Request) error {
		var d *struct{ Target, Password, Actor, Reason string }
		if err := body(w, r, &d); err != nil {
			return err
		}
		if d == nil {
			return problem(400, "请求格式无效")
		}
		if err := s.ResetPassword(cookieToken(r), d.Target, d.Password, strings.TrimSpace(d.Actor), strings.TrimSpace(d.Reason), clientIP(r)); err != nil {
			return err
		}
		respond(w, map[string]bool{"ok": true})
		return nil
	}))
}
