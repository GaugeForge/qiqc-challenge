package modelproxy

import "errors"

// UpstreamNetwork validates trusted operator configuration without reflecting
// untrusted values in errors. Empty retains Go's ordinary dual-stack dialing.
func UpstreamNetwork(family string) (string, error) {
	switch family {
	case "", "auto":
		return "tcp", nil
	case "ipv4":
		return "tcp4", nil
	case "ipv6":
		return "tcp6", nil
	default:
		return "", errors.New("invalid upstream IP family")
	}
}
