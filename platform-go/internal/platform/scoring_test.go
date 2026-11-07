package platform

import "testing"

func TestDecimalRuntimeRoundingAndInvalidValues(t *testing.T) {
	for _, tc := range []struct {
		in   []string
		want string
	}{{[]string{"1.0000005"}, "1.000001"}, {[]string{"0.1", "0.2"}, "0.150000"}, {[]string{"1.0000004", "1.0000005"}, "1.000000"}} {
		got, err := MeanSeconds(tc.in)
		if err != nil || got != tc.want {
			t.Fatalf("%v -> %s %v", tc.in, got, err)
		}
	}
	for _, in := range [][]string{nil, {"-1"}, {"NaN"}, {"Infinity"}, {"1/2"}} {
		if _, err := MeanSeconds(in); err == nil {
			t.Fatalf("invalid runtime %v", in)
		}
	}
}
func TestWholeBatchStandingsAndTies(t *testing.T) {
	rows := []Standing{{ID: "a1", TeamID: "a", Passed: 39, Mean: "20.000000", AcceptedAt: 5}, {ID: "a2", TeamID: "a", Passed: 40, Mean: "21.000000", AcceptedAt: 6}, {ID: "b1", TeamID: "b", Passed: 40, Mean: "21.000000", AcceptedAt: 4}, {ID: "c1", TeamID: "c", Passed: 38, Mean: "29.000000", AcceptedAt: 2}}
	got, err := Rank(rows, 38, "30.000000")
	if err != nil {
		t.Fatal(err)
	}
	if len(got) != 3 || got[0].TeamID != "b" || got[1].ID != "a2" || got[0].Rank != 1 || got[1].Rank != 1 || got[2].Rank != 3 || !got[0].Tie || !got[2].Challenger {
		t.Fatalf("wrong standings %+v", got)
	}
}
