package platform

import (
	"net/http"
	"net/http/httptest"
	"testing"
)

type unreadBody struct{ read bool }

func (b *unreadBody) Read([]byte) (int, error) {
	b.read = true
	panic("saturated request body must not be read")
}
func (b *unreadBody) Close() error { return nil }
func TestUploadCapacityRejectsBeforeReadingZipOrDebitingQuota(t *testing.T) {
	s := testStore(t)
	token := readyUser(t, s, "captain@example.test", "team")
	for i := 0; i < cap(s.uploadSlots); i++ {
		s.uploadSlots <- struct{}{}
	}
	h := http.NewServeMux()
	s.RegisterBusiness(h, http.NotFoundHandler())
	r := httptest.NewRequest("POST", "/api/versions", nil)
	body := &unreadBody{}
	r.Body = body
	r.Header.Set("X-QIQC-Request", "1")
	r.AddCookie(&http.Cookie{Name: "qiqc_session", Value: token})
	w := httptest.NewRecorder()
	h.ServeHTTP(w, r)
	if w.Code != 429 || body.read {
		t.Fatal("unbounded upload admission", w.Code)
	}
	var ledger int
	s.DB.QueryRow("SELECT COUNT(*) FROM ledger").Scan(&ledger)
	if ledger != 0 {
		t.Fatal("busy upload charged quota")
	}
}
