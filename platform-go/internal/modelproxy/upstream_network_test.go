package modelproxy

import (
	"crypto/ecdsa"
	"crypto/elliptic"
	"crypto/rand"
	"crypto/tls"
	"crypto/x509"
	"crypto/x509/pkix"
	"math/big"
	"net"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"
)

func TestUpstreamIPFamilyValidationDefaultAndCustomClient(t *testing.T) {
	cfg, _, _ := config(t, func(http.ResponseWriter, *http.Request) {})
	for _, family := range []string{"", "auto", "ipv4", "ipv6"} {
		cfg.UpstreamIPFamily = family
		p := build(t, cfg)
		want := family
		if want == "" {
			want = "auto"
		}
		if p.cfg.UpstreamIPFamily != want {
			t.Fatal("default family changed")
		}
		custom := &http.Transport{}
		cfg.Client = &http.Client{Transport: custom}
		if build(t, cfg).client.Transport != custom {
			t.Fatal("custom transport changed")
		}
		cfg.Client = nil
	}
	for _, family := range []string{"tcp4", "IPv4", " ipv6", "private-secret", "ipv4\nprivate-secret", "https://secret.invalid/key"} {
		cfg.UpstreamIPFamily = family
		if _, err := New(cfg); err == nil || err.Error() != "invalid upstream IP family" {
			t.Fatal("invalid family accepted or reflected in error")
		}
	}
}

func localHostnameCertificate(t *testing.T) (tls.Certificate, *x509.CertPool) {
	t.Helper()
	key, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	cert := &x509.Certificate{SerialNumber: big.NewInt(1), Subject: pkix.Name{CommonName: "localhost"},
		DNSNames: []string{"localhost"}, NotBefore: time.Now().Add(-time.Minute), NotAfter: time.Now().Add(time.Hour),
		KeyUsage: x509.KeyUsageDigitalSignature, ExtKeyUsage: []x509.ExtKeyUsage{x509.ExtKeyUsageServerAuth}}
	der, err := x509.CreateCertificate(rand.Reader, cert, cert, &key.PublicKey, key)
	if err != nil {
		t.Fatal(err)
	}
	parsed, err := x509.ParseCertificate(der)
	if err != nil {
		t.Fatal(err)
	}
	pool := x509.NewCertPool()
	pool.AddCert(parsed)
	return tls.Certificate{Certificate: [][]byte{der}, PrivateKey: key}, pool
}

func TestForcedIPFamilyRealTLSHostnameAndNoCrossFamilyFallback(t *testing.T) {
	certificate, roots := localHostnameCertificate(t)
	for _, test := range []struct{ family, network, host, opposite string }{
		{"ipv4", "tcp4", "127.0.0.1", "ipv6"},
		{"ipv6", "tcp6", "::1", "ipv4"},
	} {
		t.Run(test.family, func(t *testing.T) {
			listener, err := net.Listen(test.network, net.JoinHostPort(test.host, "0"))
			if err != nil {
				t.Fatalf("local family listener unavailable: %v", err)
			}
			observed := make(chan string, 4)
			server := httptest.NewUnstartedServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				observed <- r.TLS.ServerName
				w.Header().Set("Content-Type", "application/json")
				_, _ = w.Write([]byte(`{"status":"completed","output":[]}`))
			}))
			server.Listener.Close()
			server.Listener = listener
			server.TLS = &tls.Config{Certificates: []tls.Certificate{certificate}}
			server.StartTLS()
			defer server.Close()
			cfg, rows, _ := config(t, func(http.ResponseWriter, *http.Request) {})
			_, port, err := net.SplitHostPort(listener.Addr().String())
			if err != nil {
				t.Fatal(err)
			}
			cfg.BaseURL = "https://" + net.JoinHostPort("localhost", port)
			cfg.UpstreamIPFamily = test.family
			p := build(t, cfg)
			transport := p.client.Transport.(*http.Transport)
			transport.TLSClientConfig.RootCAs = roots
			defer transport.CloseIdleConnections()
			if out := perform(p, "/v1/responses", `{"model":"hy4-test"}`); out.Code != 200 || len(*rows) != 1 {
				t.Fatal("selected address family failed real TLS exchange")
			}
			if <-observed != "localhost" {
				t.Fatal("TLS hostname or SNI replaced by resolved IP")
			}
			cfg.UpstreamIPFamily = test.opposite
			other := build(t, cfg)
			other.client.Transport.(*http.Transport).TLSClientConfig.RootCAs = roots
			if out := perform(other, "/v1/responses", `{"model":"hy4-test"}`); out.Code == 200 {
				t.Fatal("opposite-family request silently fell back")
			}
			if len(observed) != 0 {
				t.Fatal("opposite-family request reached server")
			}
		})
	}
}
