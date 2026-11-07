package platform

import (
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"
	"time"
)

func TestAnnouncementsRequireAdminAndKeepVersionScopeAndPolicy(t *testing.T) {
	s := testStore(t)
	s.Now = func() time.Time { return time.Date(2026, 11, 8, 0, 0, 0, 0, time.UTC) }
	admin := readyUser(t, s, "notice-admin@example.test", "admin")
	a := readyUser(t, s, "notice-a@example.test", "team")
	b := readyUser(t, s, "notice-b@example.test", "team")
	ua, _ := s.Session(a)
	draft := AnnouncementInput{Version: "notice-v1", Title: "比赛开始", Body: "比赛已开始，请查看已冻结的规则。", Scope: "public", PRDVersion: CurrentRuleVersion, Actor: "operator", Reason: "发布经过复核的开始公告", Idem: "publish-1"}
	if _, err := s.PublishAnnouncement(a, draft, "local"); status(err) != 403 {
		t.Fatal("team published notice", err)
	}
	bad := draft
	bad.PRDVersion = "PRD 0.1"
	if _, err := s.PublishAnnouncement(admin, bad, "local"); status(err) != 409 {
		t.Fatal("stale policy accepted", err)
	}
	bad = draft
	bad.Reason = ""
	if _, err := s.PublishAnnouncement(admin, bad, "local"); status(err) != 400 {
		t.Fatal("missing audit reason accepted")
	}
	one, err := s.PublishAnnouncement(admin, draft, "local")
	if err != nil {
		t.Fatal(err)
	}
	if one.PublishedAtUTC8 != "2026-11-08T08:00:00+08:00" || one.TimeZone != "Asia/Singapore" {
		t.Fatal("wrong publication zone", one)
	}
	repeated, err := s.PublishAnnouncement(admin, draft, "local")
	if err != nil || repeated.ID != one.ID {
		t.Fatal("duplicate announcement", err)
	}
	bad = draft
	bad.Body = "changed using existing key"
	if _, err = s.PublishAnnouncement(admin, bad, "local"); status(err) != 409 {
		t.Fatal("idempotency collision accepted")
	}
	bad = draft
	bad.Idem = "publish-new-key"
	if _, err = s.PublishAnnouncement(admin, bad, "local"); status(err) != 409 {
		t.Fatal("frozen version overwritten")
	}
	draft.Version = "notice-team"
	draft.Idem = "private-1"
	draft.Scope = "team"
	draft.TeamID = ua.TeamID
	draft.Body = "Only this team may see the private notice."
	private, err := s.PublishAnnouncement(admin, draft, "local")
	if err != nil {
		t.Fatal(err)
	}
	for _, tc := range []struct {
		token string
		want  int
	}{{"", 1}, {a, 2}, {b, 1}, {admin, 2}} {
		got, err := s.ListAnnouncements(tc.token, 0)
		if err != nil || len(got) != tc.want {
			t.Fatal("wrong announcement visibility", len(got), err)
		}
	}
	if _, err = s.GetAnnouncement(b, private.ID); status(err) != 404 {
		t.Fatal("private notice ID leaked")
	}
	var n int
	s.DB.QueryRow("SELECT COUNT(*) FROM audit WHERE action='announcement_publish'").Scan(&n)
	if n != 2 {
		t.Fatal("duplicate or missing publication audit")
	}
}

func TestSupportOwnershipReferencesRepliesAndImmutableAudit(t *testing.T) {
	s := testStore(t)
	withScanner(s)
	admin := readyUser(t, s, "help-admin@example.test", "admin")
	a := readyUser(t, s, "help-a@example.test", "team")
	b := readyUser(t, s, "help-b@example.test", "team")
	ua, _ := s.Session(a)
	ub, _ := s.Session(b)
	v, err := s.Upload(a, []byte("team-a version"))
	if err != nil {
		t.Fatal(err)
	}
	if err = s.Review(admin, v.ID, "approve", "operator", "reviewed package", "local"); err != nil {
		t.Fatal(err)
	}
	batch, err := s.Confirm(a, v.ID, "infra", "simulated", "support-batch")
	if err != nil {
		t.Fatal(err)
	}
	_, err = s.DB.Exec("INSERT INTO outbox(id,event,team_id,payload,created) VALUES('team-a-event','test',?,'{}',1),('team-b-event','test',?,'{}',1)", ua.TeamID, ub.TeamID)
	if err != nil {
		t.Fatal(err)
	}
	input := SupportInput{Subject: "上传结果需要帮助", Body: "我在查看本次评测时遇到了问题，请协助核对。", BatchID: batch["id"].(string), SubmissionSHA: v.SHA256, EventID: "team-a-event", Idem: "ticket-1"}
	if _, err = s.CreateSupportTicket(b, input, "local"); status(err) != 404 {
		t.Fatal("cross-team batch accepted", err)
	}
	bad := input
	bad.BatchID = ""
	if _, err = s.CreateSupportTicket(b, bad, "local"); status(err) != 404 {
		t.Fatal("cross-team version accepted", err)
	}
	bad = input
	bad.EventID = "team-b-event"
	if _, err = s.CreateSupportTicket(a, bad, "local"); status(err) != 404 {
		t.Fatal("cross-team event accepted")
	}
	ticket, err := s.CreateSupportTicket(a, input, "local")
	if err != nil {
		t.Fatal(err)
	}
	if ticket.TeamID != ua.TeamID || ticket.SubmissionSHA != v.SHA256 || len(ticket.Messages) != 1 {
		t.Fatal(ticket)
	}
	if _, err = s.GetSupportTicket(b, ticket.ID); status(err) != 404 {
		t.Fatal("cross-team read")
	}
	if _, err = s.ReplySupportTicket(b, ticket.ID, SupportReply{Body: "cross-team reply", Idem: "bad"}, "local"); status(err) != 404 {
		t.Fatal("cross-team reply")
	}
	if _, err = s.ReplySupportTicket(a, ticket.ID, SupportReply{Body: "please close this", Status: "closed", Idem: "bad-state"}, "local"); status(err) != 403 {
		t.Fatal("team changed admin status")
	}
	reply := SupportReply{Body: "已核对本次提交，请重新查看结果。", Status: "resolved", Actor: "operator", Reason: "核对工单与所属评测完成", Idem: "reply-1"}
	ticket, err = s.ReplySupportTicket(admin, ticket.ID, reply, "local")
	if err != nil || ticket.Status != "resolved" || len(ticket.Messages) != 2 {
		t.Fatal(ticket, err)
	}
	repeated, err := s.ReplySupportTicket(admin, ticket.ID, reply, "local")
	if err != nil || len(repeated.Messages) != 2 {
		t.Fatal("duplicate reply", err)
	}
	ticket, err = s.ReplySupportTicket(a, ticket.ID, SupportReply{Body: "还有一个问题需要继续确认。", Idem: "followup-1"}, "local")
	if err != nil || ticket.Status != "open" || len(ticket.Messages) != 3 {
		t.Fatal(ticket, err)
	}
	reply.Idem = "close-1"
	reply.Status = "closed"
	ticket, err = s.ReplySupportTicket(admin, ticket.ID, reply, "local")
	if err != nil {
		t.Fatal(err)
	}
	if _, err = s.ReplySupportTicket(a, ticket.ID, SupportReply{Body: "closed ticket reply", Idem: "after-close"}, "local"); status(err) != 409 {
		t.Fatal("closed ticket reopened")
	}
	mine, err := s.ListSupportTickets(b, 0)
	if err != nil || len(mine) != 0 {
		t.Fatal("other team's ticket list leaked")
	}
	var n int
	s.DB.QueryRow("SELECT COUNT(*) FROM audit WHERE action='support_admin_reply'").Scan(&n)
	if n != 2 {
		t.Fatal("wrong admin audit count", n)
	}
	s.DB.QueryRow("SELECT COUNT(*) FROM ledger").Scan(&n)
	if n != 1 {
		t.Fatal("support changed quota", n)
	}
}

func TestCommunityConcurrentIntentAndRestart(t *testing.T) {
	s := testStore(t)
	a := readyUser(t, s, "persist-help@example.test", "team")
	admin := readyUser(t, s, "persist-admin@example.test", "admin")
	input := SupportInput{Subject: "需要协助确认", Body: "这是用于验证重复点击只产生一个工单的请求。", Idem: "repeat"}
	ids := make(chan string, 8)
	var wg sync.WaitGroup
	for i := 0; i < 8; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			v, err := s.CreateSupportTicket(a, input, "local")
			if err != nil {
				t.Error(err)
				return
			}
			ids <- v.ID
		}()
	}
	wg.Wait()
	close(ids)
	seen := map[string]bool{}
	for id := range ids {
		seen[id] = true
	}
	if len(seen) != 1 {
		t.Fatal("duplicate support intent", seen)
	}
	notice, err := s.PublishAnnouncement(admin, AnnouncementInput{Version: "restart-v1", Title: "恢复状态", Body: "平台恢复说明，历史公告保持可读取。", Scope: "teams", PRDVersion: CurrentRuleVersion, Actor: "operator", Reason: "发布恢复状态说明", Idem: "restart-notice"}, "local")
	if err != nil {
		t.Fatal(err)
	}
	path := s.Path
	s.Close()
	again, err := Open(path)
	if err != nil {
		t.Fatal(err)
	}
	defer again.Close()
	for ticketID := range seen {
		ticket, err := again.GetSupportTicket(a, ticketID)
		if err != nil || len(ticket.Messages) != 1 {
			t.Fatal("lost support history", err)
		}
	}
	restored, err := again.GetAnnouncement(a, notice.ID)
	if err != nil || restored.Version != "restart-v1" {
		t.Fatal("lost announcement", err)
	}
}

func TestCommunityHTTPPermissionStrictInputAndPlainText(t *testing.T) {
	s := testStore(t)
	admin := readyUser(t, s, "http-admin@example.test", "admin")
	a := readyUser(t, s, "http-team@example.test", "team")
	mux := http.NewServeMux()
	s.RegisterBusiness(mux, http.NotFoundHandler())
	payload := AnnouncementInput{Version: "html-v1", Title: "<img src=x onerror=alert(1)>", Body: "<script>document.body.dataset.attacked='yes'</script>", Scope: "public", PRDVersion: CurrentRuleVersion, Actor: "operator", Reason: "验证公告按纯文本呈现", Idem: "html-notice"}
	raw, _ := json.Marshal(payload)
	if got := call(t, mux, "POST", "/api/announcements", a, string(raw)); got.Code != 405 {
		t.Fatal("participant announcement write route", got.Code)
	}
	if got := call(t, mux, "POST", "/api/admin/announcements", a, string(raw)); got.Code != 403 {
		t.Fatal("team admin route", got.Code)
	}
	got := call(t, mux, "POST", "/api/admin/announcements", admin, string(raw))
	if got.Code != 200 {
		t.Fatal(got.Code, got.Body.String())
	}
	if got = call(t, mux, "GET", "/api/announcements", "", ""); got.Code != 200 || !strings.Contains(got.Body.String(), "html-v1") {
		t.Fatal("public notice unavailable")
	}
	unknown := `{"subject":"support","body":"this request has an unauthorized team field","idempotency_key":"forged","team_id":"other-team"}`
	if got = call(t, mux, "POST", "/api/support", a, unknown); got.Code != 404 {
		t.Fatal("unknown ownership override accepted", got.Code)
	}
	if got = call(t, mux, "GET", "/api/support?page=-1", a, ""); got.Code != 404 {
		t.Fatal("participant support route remained available", got.Code)
	}
	if got = call(t, mux, "GET", "/api/support", "", ""); got.Code != 401 {
		t.Fatal("anonymous support data")
	}
	if got = call(t, mux, "GET", "/api/support", a, ""); got.Code != 404 {
		t.Fatal("participant support route remained available", got.Code)
	}
}

func TestSupportRejectsSameTeamMixedReferencesAndReopenLimit(t *testing.T) {
	s := testStore(t)
	withScanner(s)
	admin := readyUser(t, s, "mix-admin@example.test", "admin")
	team := readyUser(t, s, "mix-team@example.test", "team")
	u, _ := s.Session(team)
	v, err := s.Upload(team, []byte("batch A archive"))
	if err != nil {
		t.Fatal(err)
	}
	if err = s.Review(admin, v.ID, "approve", "operator", "reviewed original package", "local"); err != nil {
		t.Fatal(err)
	}
	first, err := s.Confirm(team, v.ID, "infra", "simulated", "batch-a")
	if err != nil {
		t.Fatal(err)
	}
	if err = s.Cancel(team, first["id"].(string)); err != nil {
		t.Fatal(err)
	}
	second, err := s.Confirm(team, v.ID, "infra", "simulated", "batch-b")
	if err != nil {
		t.Fatal(err)
	}
	var eventID string
	if err = s.DB.QueryRow("SELECT id FROM outbox WHERE team_id=? AND event='evaluation_accepted' AND payload LIKE ?", u.TeamID, "%"+first["id"].(string)+"%").Scan(&eventID); err != nil {
		t.Fatal(err)
	}
	mixed := SupportInput{Subject: "混合关联测试", Body: "请协助检查同队不同评测混合的记录。", BatchID: second["id"].(string), SubmissionSHA: v.SHA256, EventID: eventID, Idem: "mixed"}
	if _, err = s.CreateSupportTicket(team, mixed, "local"); status(err) != 409 {
		t.Fatal("same-team mixed event accepted", err)
	}
	other, err := s.Upload(team, []byte("another frozen version"))
	if err != nil {
		t.Fatal(err)
	}
	mixed.EventID = ""
	mixed.SubmissionSHA = other.SHA256
	if _, err = s.CreateSupportTicket(team, mixed, "local"); status(err) != 409 {
		t.Fatal("same-team mixed version accepted", err)
	}
	ticket, err := s.CreateSupportTicket(team, SupportInput{Subject: "先答复的工单", Body: "此问题稍后可能需要追加信息。", Idem: "resolved-first"}, "local")
	if err != nil {
		t.Fatal(err)
	}
	reply := SupportReply{Body: "已经完成本次核对。", Status: "resolved", Actor: "operator", Reason: "核对后先标记已答复", Idem: "resolve-first"}
	if _, err = s.ReplySupportTicket(admin, ticket.ID, reply, "local"); err != nil {
		t.Fatal(err)
	}
	for i := 0; i < 20; i++ {
		if _, err = s.CreateSupportTicket(team, SupportInput{Subject: "仍需处理的问题", Body: "这是一个独立的支持请求，需要协助。", Idem: fmt.Sprint("open-", i)}, "local"); err != nil {
			t.Fatal(err)
		}
	}
	if _, err = s.ReplySupportTicket(team, ticket.ID, SupportReply{Body: "申请重新处理这个问题。", Idem: "cannot-reopen"}, "local"); status(err) != 429 {
		t.Fatal("team bypassed open limit", err)
	}
	reply.Status = "open"
	reply.Idem = "operator-reopen"
	reply.Reason = "人工确认需超限重新处理此问题"
	if _, err = s.ReplySupportTicket(admin, ticket.ID, reply, "local"); err != nil {
		t.Fatal("audited operator override rejected", err)
	}
}

func TestCommunityBoundsPaginationAndChangedIntentConflict(t *testing.T) {
	s := testStore(t)
	team := readyUser(t, s, "bounds-team@example.test", "team")
	admin := readyUser(t, s, "bounds-admin@example.test", "admin")
	u, _ := s.Session(team)
	in := SupportInput{Subject: "测试重复请求", Body: "这个工单用于验证同键换内容被拒绝。", Idem: "bound-key"}
	ticket, err := s.CreateSupportTicket(team, in, "local")
	if err != nil {
		t.Fatal(err)
	}
	in.Body += "changed"
	if _, err = s.CreateSupportTicket(team, in, "local"); status(err) != 409 {
		t.Fatal("changed create intent accepted")
	}
	reply := SupportReply{Body: "已核对并保存处理结论。", Status: "resolved", Actor: "operator", Reason: "处理重复请求边界测试", Idem: "reply-bound"}
	if _, err = s.ReplySupportTicket(admin, ticket.ID, reply, "local"); err != nil {
		t.Fatal(err)
	}
	reply.Body += "changed"
	if _, err = s.ReplySupportTicket(admin, ticket.ID, reply, "local"); status(err) != 409 {
		t.Fatal("changed reply intent accepted")
	}
	in.Idem = "too-long"
	in.Body = strings.Repeat("量", 8001)
	if _, err = s.CreateSupportTicket(team, in, "local"); status(err) != 400 {
		t.Fatal("oversize support accepted")
	}
	notice := AnnouncementInput{Version: "invalid-public", Title: "公开范围检查", Body: "内容必须只含公开材料", Scope: "public", TeamID: u.TeamID, PRDVersion: CurrentRuleVersion, Actor: "operator", Reason: "检查公开关联保护", Idem: "bad-public"}
	if _, err = s.PublishAnnouncement(admin, notice, "local"); status(err) != 400 {
		t.Fatal("public notice linked internal team")
	}
	notice.TeamID = ""
	notice.Body = strings.Repeat("量", 8001)
	if _, err = s.PublishAnnouncement(admin, notice, "local"); status(err) != 400 {
		t.Fatal("oversize notice accepted")
	}
	for i := 0; i < 51; i++ {
		if _, err = s.DB.Exec("INSERT INTO announcements(id,version,title,body,scope,prd_version,published,created_by) VALUES(?,?,?,?,'public','PRD 1.2',?,?)", fmt.Sprint("notice-", i), fmt.Sprint("page-v", i), "分页标题", "分页正文内容", i, u.ID); err != nil {
			t.Fatal(err)
		}
	}
	first, err := s.ListAnnouncements("", 0)
	if err != nil || len(first) != 50 {
		t.Fatal("unbounded first page", len(first), err)
	}
	next, err := s.ListAnnouncements("", 1)
	if err != nil || len(next) != 1 {
		t.Fatal("missing next page", len(next), err)
	}
	seen := map[string]bool{}
	for _, n := range first {
		seen[n.ID] = true
	}
	if seen[next[0].ID] {
		t.Fatal("page overlap")
	}
	for i := 0; i < 51; i++ {
		if _, err = s.DB.Exec("INSERT INTO support_tickets(id,team_id,subject,status,created,updated) VALUES(?,?,?,'closed',?,?)", fmt.Sprint("paged-ticket-", i), u.TeamID, "Historical support", i, i); err != nil {
			t.Fatal(err)
		}
	}
	page0, err := s.ListSupportTickets(team, 0)
	if err != nil || len(page0) != 50 {
		t.Fatal("unbounded support page", len(page0), err)
	}
	page1, err := s.ListSupportTickets(team, 1)
	if err != nil || len(page1) != 2 {
		t.Fatal("missing support history", len(page1), err)
	}
	for sequence := 3; sequence <= 100; sequence++ {
		if _, err = s.DB.Exec("INSERT INTO support_messages(id,ticket_id,sequence,author_id,author_role,body,created) VALUES(?,?,?,?,'team','historical message',1)", id(), ticket.ID, sequence, u.ID); err != nil {
			t.Fatal(err)
		}
	}
	if _, err = s.ReplySupportTicket(team, ticket.ID, SupportReply{Body: "message beyond safe bound", Idem: "too-many-messages"}, "local"); status(err) != 429 {
		t.Fatal("message bound bypassed", err)
	}
}

func TestCommunityHTTPConcurrentIdempotentAnnouncementTicketAndReply(t *testing.T) {
	s := testStore(t)
	admin := readyUser(t, s, "burst-admin@example.test", "admin")
	_ = readyUser(t, s, "burst-team@example.test", "team")
	mux := http.NewServeMux()
	s.RegisterBusiness(mux, http.NotFoundHandler())
	server := httptest.NewServer(mux)
	defer server.Close()
	client := server.Client()
	client.Timeout = 30 * time.Second
	burst := func(path, token string, payload any) string {
		t.Helper()
		raw, err := json.Marshal(payload)
		if err != nil {
			t.Fatal(err)
		}
		var wg sync.WaitGroup
		ids := make(chan string, 32)
		errs := make(chan error, 32)
		started := time.Now()
		for i := 0; i < 32; i++ {
			wg.Add(1)
			go func() {
				defer wg.Done()
				req, e := http.NewRequest("POST", server.URL+path, strings.NewReader(string(raw)))
				if e != nil {
					errs <- e
					return
				}
				req.Header.Set("Content-Type", "application/json")
				req.Header.Set("X-QIQC-Request", "1")
				req.AddCookie(&http.Cookie{Name: "qiqc_session", Value: token})
				res, e := client.Do(req)
				if e != nil {
					errs <- e
					return
				}
				defer res.Body.Close()
				if res.StatusCode != 200 {
					errs <- fmt.Errorf("status=%d", res.StatusCode)
					return
				}
				var result struct {
					ID string `json:"id"`
				}
				if e = json.NewDecoder(res.Body).Decode(&result); e != nil {
					errs <- e
					return
				}
				ids <- result.ID
			}()
		}
		wg.Wait()
		close(errs)
		for e := range errs {
			t.Error(e)
		}
		close(ids)
		seen := map[string]int{}
		for id := range ids {
			seen[id]++
		}
		if len(seen) != 1 {
			t.Fatalf("duplicate objects: %v", seen)
		}
		for id, n := range seen {
			if id == "" || n != 32 {
				t.Fatalf("lost requests: count=%d", n)
			}
			t.Logf("%s: 32 concurrent real HTTP requests -> 1 object, %s", path, time.Since(started))
			return id
		}
		t.Fatal("no response")
		return ""
	}
	burst("/api/admin/announcements", admin, AnnouncementInput{Version: "burst-v1", Title: "并发公告测试", Body: "此公告验证并发重复请求不会重复发布。", Scope: "public", PRDVersion: CurrentRuleVersion, Actor: "operator", Reason: "验证同键公告并发发布", Idem: "burst-notice"})
	// Participant support tickets are retired; organizers use email for issues.
	// Keep this HTTP burst focused on the remaining public announcement contract.
	for query, want := range map[string]int{"SELECT COUNT(*) FROM announcements": 1, "SELECT COUNT(*) FROM audit WHERE action='announcement_publish'": 1, "SELECT COUNT(*) FROM ledger": 0} {
		var n int
		if err := s.DB.QueryRow(query).Scan(&n); err != nil || n != want {
			t.Fatal(query, n, want, err)
		}
	}
}
