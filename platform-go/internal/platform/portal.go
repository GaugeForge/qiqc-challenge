package platform

import "os"

// CompetitionPortal separates the public competition entrance from disposable
// test installations. It does not certify scoring policy or scientific budgets.
func CompetitionPortal() bool { return os.Getenv("QIQC_COMPETITION_PORTAL") == "1" }
