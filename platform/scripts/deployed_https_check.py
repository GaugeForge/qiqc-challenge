"""Check an existing HTTPS release without changing accounts, calendar or task runs.

An existing private staff credential seed is used for one login/logout session.
No credential values or private API response bodies are written to the report.
"""
from pathlib import Path
from urllib.parse import urlsplit
import argparse
import hashlib
import json
import urllib.request

from playwright.sync_api import sync_playwright, expect

ROOT = Path(__file__).resolve().parents[3]
STAFF_TABS = (
    "overview", "reviews", "evaluations", "teams", "support",
    "announcements", "operations", "audit",
)


def fetch(url):
    with urllib.request.urlopen(url, timeout=20) as response:
        assert response.status == 200
        return response.read()


def check_release(args, report):
    base = args.url.rstrip("/")
    for path in ("/", "/submit", "/leaderboard", "/admin", "/documents.html", "/healthz"):
        fetch(base + path)
    report["checks"].append("HTTPS certificate and all public entry routes")
    downloads = [
        (f"/adapters/{name}/harness.zip", ROOT / f"competition-platform/adapters/{name}/harness.zip")
        for name in ("codex", "deepseek", "pi")
    ] + [
        ("/downloads/" + name, ROOT / "docs/handbooks/pdf" / name)
        for name in ("participant-manual.en.pdf", "participant-manual.zh-CN.pdf")
    ]
    for remote, local in downloads:
        assert hashlib.sha256(fetch(base + remote)).digest() == hashlib.sha256(local.read_bytes()).digest()
    report["checks"].append("Three adapter ZIPs and both participant PDFs match exact SHA256")
    config = json.loads(fetch(base + "/api/config"))
    assert config["phase"]["name"] == args.expected_phase
    if args.expected_phase in ("preparation", "quiet", "closed"):
        assert config["phase"]["upload_allowed"] is False
    assert config["scientific_task_set"]["public_task_count"] == 5
    assert config["global_task_slots"] == args.expected_slots
    report["phase"] = config["phase"]
    report["checks"].append("Scheduled phase, admission protection, public-five catalog and shared slots")
    candidate = json.loads(args.candidate.read_text())
    credentials = json.loads(Path(candidate["credentials"]).read_text())["admin"]
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True, args=["--no-sandbox"])
        context = browser.new_context(viewport={"width": 1440, "height": 1000})
        page = context.new_page()
        page.on("pageerror", lambda error: report["page_errors"].append(type(error).__name__))
        page.goto(base + "/admin")
        page.locator("#email").fill(credentials["email"])
        page.locator("#password").fill(credentials["password"])
        page.locator("#login-form button[type=submit]").click()
        expect(page.locator("#admin-stats")).to_be_visible(timeout=20000)
        headers = {"X-QIQC-Workspace": "staff"}
        response = context.request.get(base + "/api/admin", headers=headers)
        assert response.status == 200
        snapshot = response.json()
        assert "execution_policy" in snapshot and "pagination" in snapshot
        cookies = [c for c in context.cookies() if c["name"] == "qiqc_staff_session"]
        assert cookies and cookies[0]["secure"] and cookies[0]["httpOnly"]
        for locale in ("zh", "en"):
            page.locator(f'[data-locale="{locale}"]').click()
            for width in (1440, 390):
                page.set_viewport_size({"width": width, "height": 1000})
                for tab in STAFF_TABS:
                    page.locator(f'[data-staff-tab="{tab}"]').click()
                    expect(page.locator(f"#staff-section-{tab}")).to_be_visible()
                    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
        page.set_viewport_size({"width": 1440, "height": 1000})
        for section, tab in (("versions", "reviews"), ("batches", "evaluations"), ("usage", "operations"), ("audit", "audit")):
            page.locator(f'[data-staff-tab="{tab}"]').click()
            pager = page.locator(f'[data-history-page="{section}"]')
            expect(pager).to_be_visible()
            if snapshot["pagination"][section]["pages"] > 1:
                pager.locator('[data-page-next]').click()
                expect(pager).to_contain_text("Page 2", timeout=20000)
                pager.locator('[data-page-prev]').click()
                expect(pager).to_contain_text("Page 1", timeout=20000)
        report["checks"].append("Live staff history pagination, execution policy and 32 bilingual responsive section checks")
        page.locator('[data-staff-tab="overview"]').click()
        page.screenshot(path=str(args.output / "staff-desktop.png"), full_page=True)
        page.goto(base + "/submit")
        expect(page.locator("#login-form")).to_be_visible()
        assert not page.locator('a[href="/admin"]').count()
        page.screenshot(path=str(args.output / "submit-desktop.png"), full_page=True)
        report["checks"].append("Existing staff account, secure session and independent entrant sign-in")
        for width in (390, 1440):
            public = browser.new_context(viewport={"width": width, "height": 844})
            view = public.new_page()
            view.on("pageerror", lambda error: report["page_errors"].append(type(error).__name__))
            for route in ("/submit", "/leaderboard", "/admin"):
                view.goto(base + route)
                expect(view.locator("h1")).to_be_visible()
                assert view.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
            if width == 390:
                view.screenshot(path=str(args.output / "admin-mobile.png"), full_page=True)
            assert public.request.get(base + "/api/admin").status == 401
            public.close()
        assert context.request.post(base + "/api/logout", headers={**headers, "X-QIQC-Request": "1"}).status == 200
        browser.close()
    report["checks"].append("Public desktop/mobile layout and unauthenticated admin denial")
    assert not report["page_errors"]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--url", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--expected-phase", default="preparation")
    parser.add_argument("--expected-slots", type=int, default=5)
    args = parser.parse_args()
    origin = urlsplit(args.url)
    if origin.scheme != "https" or origin.username or origin.password or origin.path not in ("", "/") or origin.query or origin.fragment:
        parser.error("An HTTPS origin without credentials or path is required")
    args.output = args.output.resolve()
    args.output.mkdir(mode=0o700, parents=True, exist_ok=True)
    report = {"scope": "active HTTPS deployment; no calendar changes and no cloud jobs", "passed": False, "checks": [], "page_errors": []}
    try:
        check_release(args, report)
        report["passed"] = True
    except Exception as error:
        report["error_class"] = type(error).__name__
    finally:
        (args.output / "https-browser-check.json").write_text(json.dumps(report, indent=2) + "\n")
        print(json.dumps({key: report.get(key) for key in ("passed", "checks", "error_class")}))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
