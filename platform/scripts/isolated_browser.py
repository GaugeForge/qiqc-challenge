"""Compatibility entry for the repeatable automatic-flow browser suite.

The former manual-confirmation UI no longer exists. The current suite owns a
private Go fixture, so it neither mutates the caller's legacy simulation DB nor
uses the supplied administrator credentials. Scientific dependencies are fixtures.
"""

from pathlib import Path
import json
from final_browser_acceptance import run


def run_browser(base, admin_email, admin_password, output):
    destination = Path(output) / "automatic-browser"
    run(destination)
    report = json.loads((destination / "report.json").read_text())
    return {
        "scope": report["scope"],
        "checks": report["checks"],
        "report": str(destination / "report.json"),
    }
