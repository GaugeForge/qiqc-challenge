package platform

import (
	"net/http"
	"strconv"
	"strings"
)

// RegisterBusiness moves endpoints as whole contracts. Unmigrated administrator
// actions retain their explicit legacy marker rather than claiming Go ownership.
func (s *Store) RegisterBusiness(mux *http.ServeMux, fallback http.Handler) {
	s.RegisterCommunity(mux)
	s.registerAutomaticSubmissions(mux)
	s.registerReviewReports(mux)
	mux.HandleFunc("/api/config", endpointHandler("GET", func(w http.ResponseWriter, r *http.Request) error {
		value, err := s.Config()
		if err != nil {
			return err
		}
		respond(w, value)
		return nil
	}))
	mux.HandleFunc("/api/admin", endpointHandler("GET", func(w http.ResponseWriter, r *http.Request) error {
		options, err := parseSnapshotOptions(r.URL.Query())
		if err != nil {
			return err
		}
		value, err := s.adminSnapshot(cookieToken(r), options)
		if err != nil {
			return err
		}
		respond(w, value)
		return nil
	}))

	// Standings are visible to signed-in teams and staff only, never publicly.
	mux.HandleFunc("/api/leaderboard", endpointHandler("GET", func(w http.ResponseWriter, r *http.Request) error {
		u, err := s.Session(cookieToken(r))
		if err != nil {
			return err
		}
		if u.MustChange != 0 {
			return problem(403, "Change your temporary password before viewing the leaderboard.")
		}
		board, err := s.Leaderboard()
		if err != nil {
			return err
		}
		respond(w, board)
		return nil
	}))
	mux.HandleFunc("/api/batches", func(w http.ResponseWriter, r *http.Request) {
		if r.Method == "POST" {
			endpointHandler("POST", func(w http.ResponseWriter, r *http.Request) error {
				var d *struct {
					VersionID  string `json:"version_id"`
					Kind, Mode string
					Idem       string `json:"idempotency_key"`
				}
				if err := body(w, r, &d); err != nil {
					return err
				}
				if d == nil {
					return problem(400, "请求格式无效")
				}
				b, err := s.Confirm(cookieToken(r), d.VersionID, d.Kind, d.Mode, d.Idem)
				if err != nil {
					return err
				}
				respond(w, b)
				return nil
			})(w, r)
		} else {
			endpointHandler("GET", func(w http.ResponseWriter, r *http.Request) error {
				bs, err := s.Batches(cookieToken(r))
				if err != nil {
					return err
				}
				respond(w, bs)
				return nil
			})(w, r)
		}
	})
	mux.HandleFunc("/api/batches/", func(w http.ResponseWriter, r *http.Request) {
		parts := strings.Split(strings.TrimPrefix(r.URL.Path, "/api/batches/"), "/")
		if len(parts) == 1 {
			endpointHandler("GET", func(w http.ResponseWriter, r *http.Request) error {
				b, err := s.GetBatch(cookieToken(r), parts[0])
				if err != nil {
					return err
				}
				respond(w, b)
				return nil
			})(w, r)
			return
		}
		if len(parts) == 2 && parts[1] == "cancel" {
			endpointHandler("POST", func(w http.ResponseWriter, r *http.Request) error {
				if err := s.Cancel(cookieToken(r), parts[0]); err != nil {
					return err
				}
				respond(w, map[string]bool{"ok": true})
				return nil
			})(w, r)
			return
		}
		if len(parts) == 2 && parts[1] == "cancel-attention" {
			endpointHandler("POST", func(w http.ResponseWriter, r *http.Request) error {
				result, err := s.CancelAttentionTasks(cookieToken(r), parts[0], clientIP(r))
				if err != nil {
					return err
				}
				respond(w, result)
				return nil
			})(w, r)
			return
		}
		if len(parts) == 2 && parts[1] == "retry-attention" {
			endpointHandler("POST", func(w http.ResponseWriter, r *http.Request) error {
				result, err := s.RetryAttentionTasks(cookieToken(r), parts[0], clientIP(r))
				if err != nil {
					return err
				}
				respond(w, result)
				return nil
			})(w, r)
			return
		}
		if len(parts) == 4 && parts[1] == "tasks" && (parts[3] == "cancel" || parts[3] == "retry") {
			endpointHandler("POST", func(w http.ResponseWriter, r *http.Request) error {
				ordinal, err := strconv.Atoi(parts[2])
				if err != nil || ordinal < 0 {
					return problem(404, "题目不存在")
				}
				if parts[3] == "cancel" {
					err = s.CancelTask(cookieToken(r), parts[0], ordinal, clientIP(r))
				} else {
					err = s.RetryTask(cookieToken(r), parts[0], ordinal, clientIP(r))
				}
				if err != nil {
					return err
				}
				respond(w, map[string]bool{"ok": true})
				return nil
			})(w, r)
			return
		}

		if len(parts) == 3 && parts[1] == "traces" {
			endpointHandler("GET", func(w http.ResponseWriter, r *http.Request) error {
				ordinal, err := strconv.Atoi(parts[2])
				if err != nil {
					return problem(404, "轨迹不可访问")
				}
				v, err := s.Trace(r.Context(), cookieToken(r), parts[0], ordinal)
				if err != nil {
					return err
				}
				respond(w, v)
				return nil
			})(w, r)
			return
		}
		w.Header().Set("X-QIQC-Module", "python-legacy")
		fallback.ServeHTTP(w, r)
	})
	mux.HandleFunc("/api/versions", func(w http.ResponseWriter, r *http.Request) {
		if r.Method == http.MethodPost {
			endpointHandler("POST", func(w http.ResponseWriter, r *http.Request) error {
				if _, err := s.Session(cookieToken(r)); err != nil {
					return err
				}
				select {
				case s.uploadSlots <- struct{}{}:
					defer func() { <-s.uploadSlots }()
				default:
					return problem(429, "上传检查繁忙，请稍后重试；未扣次数")
				}
				if err := s.CheckUploadDisk(); err != nil {
					return err
				}
				data, err := ReadPackage(r.Body)
				if err != nil {
					return err
				}
				v, err := s.Upload(cookieToken(r), data)
				if err != nil {
					return err
				}
				respond(w, v)
				return nil
			})(w, r)
		} else {
			endpointHandler("GET", func(w http.ResponseWriter, r *http.Request) error {
				v, err := s.Versions(cookieToken(r))
				if err != nil {
					return err
				}
				respond(w, v)
				return nil
			})(w, r)
		}
	})
	mux.HandleFunc("/api/admin/actions", endpointHandler("POST", func(w http.ResponseWriter, r *http.Request) error {
		var a *Action
		if err := body(w, r, &a); err != nil {
			return err
		}
		if a == nil {
			return problem(400, "请求格式无效")
		}
		if err := s.AdminAction(cookieToken(r), *a, clientIP(r)); err != nil {
			return err
		}
		respond(w, map[string]bool{"ok": true})
		return nil
	}))
	mux.HandleFunc("/api/appeals", endpointHandler("POST", func(w http.ResponseWriter, r *http.Request) error {
		var d *struct{ Body string }
		if err := body(w, r, &d); err != nil {
			return err
		}
		if d == nil {
			return problem(400, "请求格式无效")
		}
		if err := s.Appeal(cookieToken(r), d.Body); err != nil {
			return err
		}
		respond(w, map[string]bool{"ok": true})
		return nil
	}))
	mux.HandleFunc("/api/my-appeal", endpointHandler("GET", func(w http.ResponseWriter, r *http.Request) error {
		a, err := s.MyAppeal(cookieToken(r))
		if err != nil {
			return err
		}
		respond(w, a)
		return nil
	}))
}
