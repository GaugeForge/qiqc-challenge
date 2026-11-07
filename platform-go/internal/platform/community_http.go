package platform

import (
	"encoding/json"
	"io"
	"net/http"
	"strconv"
	"strings"
)

func communityBody(w http.ResponseWriter, r *http.Request, target any) error {
	r.Body = http.MaxBytesReader(w, r.Body, 65536)
	decoder := json.NewDecoder(r.Body)
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(target); err != nil {
		return problem(400, "请求格式无效或含未知字段")
	}
	if err := decoder.Decode(new(any)); err != io.EOF {
		return problem(400, "请求格式无效")
	}
	return nil
}
func communityRequestPage(r *http.Request) (int, error) {
	raw := r.URL.Query().Get("page")
	if raw == "" {
		return 0, nil
	}
	page, err := strconv.Atoi(raw)
	if err != nil {
		return 0, problem(400, "无效分页参数")
	}
	return page, communityPage(page)
}

// Participant support tickets were retired. Keep the historical staff-only
// records readable for internal reconciliation, but never expose the ticket
// routes to a team account; participants are directed to email the organizers.
func (s *Store) staffSupport(token string) error {
	u, err := s.Session(token)
	if err != nil {
		return err
	}
	if u.Role != "admin" {
		return problem(404, "工单不可访问")
	}
	return nil
}

func (s *Store) RegisterCommunity(mux *http.ServeMux) {
	mux.HandleFunc("/api/announcements", endpointHandler("GET", func(w http.ResponseWriter, r *http.Request) error {
		page, err := communityRequestPage(r)
		if err != nil {
			return err
		}
		items, err := s.ListAnnouncements(cookieToken(r), page)
		if err != nil {
			return err
		}
		respond(w, items)
		return nil
	}))
	mux.HandleFunc("/api/announcements/", endpointHandler("GET", func(w http.ResponseWriter, r *http.Request) error {
		item, err := s.GetAnnouncement(cookieToken(r), strings.TrimPrefix(r.URL.Path, "/api/announcements/"))
		if err != nil {
			return err
		}
		respond(w, item)
		return nil
	}))
	mux.HandleFunc("/api/admin/announcements", endpointHandler("POST", func(w http.ResponseWriter, r *http.Request) error {
		var input *AnnouncementInput
		if err := communityBody(w, r, &input); err != nil {
			return err
		}
		if input == nil {
			return problem(400, "请求格式无效")
		}
		item, err := s.PublishAnnouncement(cookieToken(r), *input, clientIP(r))
		if err != nil {
			return err
		}
		respond(w, item)
		return nil
	}))
	mux.HandleFunc("/api/support", func(w http.ResponseWriter, r *http.Request) {
		if r.Method == "POST" {
			endpointHandler("POST", func(w http.ResponseWriter, r *http.Request) error {
				if err := s.staffSupport(cookieToken(r)); err != nil {
					return err
				}
				var input *SupportInput
				if err := communityBody(w, r, &input); err != nil {
					return err
				}
				if input == nil {
					return problem(400, "请求格式无效")
				}
				item, err := s.CreateSupportTicket(cookieToken(r), *input, clientIP(r))
				if err != nil {
					return err
				}
				respond(w, item)
				return nil
			})(w, r)
		} else {
			endpointHandler("GET", func(w http.ResponseWriter, r *http.Request) error {
				if err := s.staffSupport(cookieToken(r)); err != nil {
					return err
				}
				page, err := communityRequestPage(r)
				if err != nil {
					return err
				}
				items, err := s.ListSupportTickets(cookieToken(r), page)
				if err != nil {
					return err
				}
				respond(w, items)
				return nil
			})(w, r)
		}
	})
	mux.HandleFunc("/api/support/", func(w http.ResponseWriter, r *http.Request) {
		parts := strings.Split(strings.TrimPrefix(r.URL.Path, "/api/support/"), "/")
		if len(parts) == 1 {
			endpointHandler("GET", func(w http.ResponseWriter, r *http.Request) error {
				if err := s.staffSupport(cookieToken(r)); err != nil {
					return err
				}
				item, err := s.GetSupportTicket(cookieToken(r), parts[0])
				if err != nil {
					return err
				}
				respond(w, item)
				return nil
			})(w, r)
			return
		}
		if len(parts) == 2 && parts[1] == "replies" {
			endpointHandler("POST", func(w http.ResponseWriter, r *http.Request) error {
				if err := s.staffSupport(cookieToken(r)); err != nil {
					return err
				}
				var input *SupportReply
				if err := communityBody(w, r, &input); err != nil {
					return err
				}
				if input == nil {
					return problem(400, "请求格式无效")
				}
				item, err := s.ReplySupportTicket(cookieToken(r), parts[0], *input, clientIP(r))
				if err != nil {
					return err
				}
				respond(w, item)
				return nil
			})(w, r)
			return
		}
		endpointHandler("GET", func(http.ResponseWriter, *http.Request) error { return problem(404, "工单不可访问") })(w, r)
	})
}
