# QIQC Challenge platform

The English participant website and independent bilingual staff console share GaugeForge typography and green accents. Go services own accounts, uploads, review, quotas, scheduling, scoring and administration. Trusted Python components handle archive parsing and scientific orchestration; contestant code runs in E2B.

## Current service and requirements

The participant and staff portal is served by the same-site company route `/qiqc/submit/`; the browser uses the site API proxy and never needs a VPS origin. Staff use `/qiqc/submit/admin/`, with English/中文 switching and individual accounts. [All pages and downloads](../docs/WEB-ENTRANCES-AND-ACCESS.md), [PRD](../docs/PRD.md), [module status](../docs/NEXT-WORK-PLAN.md), [deployment](platform-go/deploy/README.md).

The public portal accepts scientific evaluation requests. Historical simulations remain test evidence and do not appear as official standings. Time-limited scientific batches are not official competition scores; final scoring qualification and the measured Astra baseline remain tracked work. This distinction is enforced server-side.

## Source layout

| Path | Responsibility |
|---|---|
| `generate.mjs`, `src/` | Homepage, documents, event facts, shared visual design and interaction |
| `platform/web/` | Participant workspace, leaderboard, independent staff shell and translation |
| `platform-go/` | Native Go API, worker, gateway, review, modules and tests |
| `platform/qiqc/` | Trusted scientific controllers/parser and historical fixture compatibility |
| `platform/scripts/`, `platform/tests/` | Isolated acceptance, browser, fault and capacity checks |
| `adapters/` | Codex, DeepSeek and Pi participant templates |
| `dist/` | Generated public files; never private configuration, database or staff handbooks |

## Build and test

Node.js 20+ is required for `node build.mjs`; no npm packages are needed. The build copies the preserved historical `Participant Guide` and `Partner Handbook` files from `src/downloads` unchanged. The public `documents.html` page is intentionally static so a build cannot replace those historical downloads with private handbooks. Edit `generate.mjs` and source assets, then rebuild; do not edit generated HTML directly.

Go build, dependencies and isolated acceptance commands are documented in [platform-go/README.md](platform-go/README.md). The bilingual portal check is `platform/.venv/bin/python platform/scripts/portal_acceptance.py --go-binary /path/to/built-api`. It uses disposable accounts and no cloud calls.

`npm start` is a static development preview only; real uploads and staff operations require the Go API. The installed site is served by systemd-managed Go and Nginx, not a developer shell. This workspace is maintained locally; Git publication is not part of these commands.

Historical prototypes and prior instructions are indexed under [docs/archive](../docs/archive/). They are not current deployment instructions. Attribution remains in `THIRD-PARTY-NOTICES.md` and `src/assets/`.

## Automatic submission release acceptance

The current implementation plan is `../docs/NEXT-WORK-PLAN.md`. Source, local
acceptance and active systemd deployment are tracked separately in
`../docs/testing/reports/FINAL-DEVELOPMENT-20261021.md`.

Run Go checks in `platform-go/` with `go test ./...`, `go vet ./...`, and
`go build ./...`. Python test dependencies are pinned in
`platform/requirements-test.lock`, including Harbor 0.23.0. Run pytest against
`platform/tests/`, `platform/ops/test_drills.py` and
`platform-go/deploy/operations/test_native_ops.py`.

For current browser acceptance, run:

```sh
platform/.venv/bin/python platform/scripts/final_browser_acceptance.py \
  --output /absolute/private/path/to/browser-evidence
```

This launches a disposable loopback Go test server, scans ZIPs with the real
isolated Docker scanner, and exercises automatic admission, results, rankings,
phase boundaries and staff reports with Chromium and WebKit. AI/science/storage
are explicit local fixtures. It never calls E2B or a model provider. The Docker
scanner image must already be installed. The older `isolated_browser` entry now
calls this suite rather than the removed two-click confirmation flow.

Historical role-copy runtime tests explicitly skip if their deliberately removed
release package is absent. Active original-Compose tests run separately; a skip
is never a passed scientific acceptance. Existing historical release/evidence
hashes are not regenerated to make tests green.
