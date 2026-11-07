package providerprofile

import (
	"context"
	"errors"
	"fmt"
	"net"
	"net/netip"
	"strings"
	"time"
)

func publicIP(ip netip.Addr) bool {
	ip = ip.Unmap()
	if !ip.IsValid() || !ip.IsGlobalUnicast() || ip.IsPrivate() || ip.IsLoopback() || ip.IsLinkLocalUnicast() {
		return false
	}
	for _, cidr := range []string{"0.0.0.0/8", "100.64.0.0/10", "192.0.0.0/24", "192.0.2.0/24", "198.18.0.0/15", "198.51.100.0/24", "203.0.113.0/24", "240.0.0.0/4", "2001:db8::/32"} {
		if netip.MustParsePrefix(cidr).Contains(ip) {
			return false
		}
	}
	return true
}
func publicHost(host string) bool {
	host = strings.ToLower(strings.TrimRight(host, "."))
	if ip, e := netip.ParseAddr(host); e == nil {
		return publicIP(ip)
	}
	if host == "localhost" || strings.HasSuffix(host, ".localhost") || !strings.Contains(host, ".") {
		return false
	}
	if ip, e := netip.ParseAddr(host); e == nil {
		return publicIP(ip)
	}
	return true
}

// DNS is resolved and checked at each connection, then the selected IP is dialed
// directly; TLS still validates the original URL hostname through net/http.
func DialPublic(ctx context.Context, network, address string) (net.Conn, error) {
	lookupNetwork := "ip"
	switch network {
	case "tcp":
	case "tcp4":
		lookupNetwork = "ip4"
	case "tcp6":
		lookupNetwork = "ip6"
	default:
		return nil, errors.New("provider network refused")
	}
	host, port, e := net.SplitHostPort(address)
	if e != nil || port != "443" || !publicHost(host) {
		return nil, errors.New("provider address refused")
	}
	ips, e := net.DefaultResolver.LookupNetIP(ctx, lookupNetwork, host)
	if e != nil || len(ips) == 0 {
		return nil, errors.New("provider DNS unavailable")
	}
	for _, ip := range ips {
		if !publicIP(ip) {
			return nil, errors.New("provider DNS address refused")
		}
	}
	d := net.Dialer{Timeout: 30 * time.Second}
	var last error
	for _, ip := range ips {
		c, e := d.DialContext(ctx, network, net.JoinHostPort(ip.String(), port))
		if e == nil {
			return c, nil
		}
		last = e
		if ctx.Err() != nil {
			break
		}
	}
	return nil, fmt.Errorf("provider connection unavailable: %w", last)
}
