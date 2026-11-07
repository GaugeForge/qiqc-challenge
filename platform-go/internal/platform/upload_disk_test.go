package platform

import (
	"errors"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"testing"
)

func TestUploadDiskAdmissionRejectsBeforeBodyFilesystemAndQuota(t *testing.T) {
	for _, test := range []struct {
		name      string
		available uint64
		probeErr  error
	}{
		{"below-reserve", DefaultUploadReserveBytes - 1, nil},
		{"cannot-fit-incoming", DefaultUploadReserveBytes + MaxZip - 1, nil},
		{"measurement-error", 0, errors.New("statfs unavailable")},
	} {
		t.Run(test.name, func(t *testing.T) {
			s := testStore(t)
			token := readyUser(t, s, "disk@example.test", "team")
			withScanner(s)
			s.DiskProbe = func(string) (DiskSpace, error) { return DiskSpace{AvailableBytes: test.available}, test.probeErr }
			h := http.NewServeMux()
			s.RegisterBusiness(h, http.NotFoundHandler())
			r := httptest.NewRequest("POST", "/api/versions", nil)
			body := &unreadBody{}
			r.Body = body
			r.Header.Set("X-QIQC-Request", "1")
			r.AddCookie(&http.Cookie{Name: "qiqc_session", Value: token})
			w := httptest.NewRecorder()
			h.ServeHTTP(w, r)
			if w.Code != 503 || body.read {
				t.Fatalf("disk admission read body or accepted: status=%d", w.Code)
			}
			if _, err := s.Upload(token, []byte("no direct bypass")); status(err) != 503 {
				t.Fatal(err)
			}
			for _, name := range []string{"quarantine", "uploads"} {
				if _, err := os.Stat(filepath.Join(s.Root, name)); !os.IsNotExist(err) {
					t.Fatal("created upload directory on failed admission")
				}
			}
			for _, table := range []string{"versions", "ledger"} {
				var n int
				s.DB.QueryRow("SELECT COUNT(*) FROM " + table).Scan(&n)
				if n != 0 {
					t.Fatalf("unexpected %s writes", table)
				}
			}
		})
	}
}

func TestUploadDiskTrustedConfigCapacityAndRecovery(t *testing.T) {
	t.Setenv("QIQC_UPLOAD_RESERVE_BYTES", "536870912")
	s := testStore(t)
	token := readyUser(t, s, "recovered-disk@example.test", "team")
	withScanner(s)
	if s.UploadReserveBytes != 512*1024*1024 {
		t.Fatal("trusted threshold ignored")
	}
	available := uint64(0)
	s.DiskProbe = func(string) (DiskSpace, error) {
		return DiskSpace{AvailableBytes: available, TotalBytes: 2 * 1024 * 1024 * 1024}, nil
	}
	if _, err := s.Upload(token, []byte("uncharged")); status(err) != 503 {
		t.Fatal(err)
	}
	available = s.UploadReserveBytes + MaxZip
	capacity, err := s.UploadDiskCapacity()
	if err != nil || !capacity.Admitting || capacity.ReserveBytes != s.UploadReserveBytes {
		t.Fatal(capacity, err)
	}
	if _, err = s.Upload(token, []byte("admitted after recovery")); err != nil {
		t.Fatal(err)
	}
}

func TestUploadDiskInvalidTrustedConfigFailsStartup(t *testing.T) {
	for _, value := range []string{"0", "-1", "oops", "18446744073709551616"} {
		t.Setenv("QIQC_UPLOAD_RESERVE_BYTES", value)
		if s, err := Open(filepath.Join(t.TempDir(), "platform.db")); err == nil {
			s.Close()
			t.Fatal("invalid threshold accepted")
		}
	}
}
