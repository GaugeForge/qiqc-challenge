package main

import (
	"flag"
	"os"
	"os/exec"
	"strings"
	"testing"
)

func TestIPFamilyCLIEnvironmentValidation(t *testing.T) {
	if os.Getenv("QIQC_TEST_GATEWAY_FAMILY_HELPER") == "1" {
		flag.CommandLine = flag.NewFlagSet("fixture", flag.ExitOnError)
		os.Args = []string{"fixture"}
		if value := os.Getenv("QIQC_TEST_GATEWAY_FAMILY_FLAG"); value != "" {
			os.Args = append(os.Args, "-upstream-ip-family", value)
		}
		main()
		return
	}
	for _, test := range []struct{ env, flag, want string }{
		{"", "", "data and provider config required"},
		{"ipv4", "", "data and provider config required"},
		{"ipv6", "", "data and provider config required"},
		{"private-secret", "", "invalid upstream IP family"},
		{"auto", "private-secret", "invalid upstream IP family"},
		{"private-secret", "auto", "data and provider config required"},
	} {
		cmd := exec.Command(os.Args[0], "-test.run=^TestIPFamilyCLIEnvironmentValidation$")
		for _, value := range os.Environ() {
			if !strings.HasPrefix(value, "QIQC_") {
				cmd.Env = append(cmd.Env, value)
			}
		}
		cmd.Env = append(cmd.Env, "QIQC_TEST_GATEWAY_FAMILY_HELPER=1", "QIQC_MODEL_UPSTREAM_IP_FAMILY="+test.env,
			"QIQC_TEST_GATEWAY_FAMILY_FLAG="+test.flag)
		raw, err := cmd.CombinedOutput()
		if err == nil || !strings.Contains(string(raw), test.want) || strings.Contains(string(raw), "private-secret") {
			t.Fatal("CLI/env validation or safe error contract failed")
		}
	}
}
