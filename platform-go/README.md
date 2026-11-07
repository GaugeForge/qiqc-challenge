# QIQC native Go platform

This directory is the active Go module. The standalone Go API serves the existing homepage and competition console; a separate Go worker owns the persistent task queue, leases and result publication. The Go model gateway validates run tokens and forwards Chat Completions and Responses. Fixed organizer scanner/science controllers remain in Python. Submitted Harness code is executed only in the configured E2B scientific path, never by the upload scanner or a VPS subprocess.

The historical Go proxy prototype is archived at `../../archive/early-go-proxy/`. Build the `go.mod` beside this README.

## Bootstrap a clean local development environment

The commands below assume the workspace is `/root/QQ-Challenge`, with the website repository at `competition-platform/`, the reviewed task tooling at `Quantum-Harbor/`, and organizer release archives under `benchmark-assets/adapters/`. Adjust those paths together on another machine. No `.env` or cloud credential is required for the isolated fixture acceptance.

Install these host prerequisites before building:

| Prerequisite | Why it is needed |
|---|---|
| Go 1.22 or newer | The module declares Go 1.22; the current host acceptance used Go 1.22.2. |
| C compiler and libc development headers, with CGO enabled | `github.com/mattn/go-sqlite3` uses CGO; a Go-only installation is insufficient. On Debian/Ubuntu the compiler toolchain is provided by `build-essential`. |
| Python 3.12 with `venv` and `pip` | The platform test dependency lock was resolved for Python 3.12. Python is also used by the fixed ZIP parser and test drivers. |
| Node.js 20 or newer | Builds the existing website using its repository build script. |
| Docker engine and permission to run the fixed local parser image | Required by the isolated upload integration. Deployed API identities use the restricted helper socket instead of Docker access. |
| Playwright Chromium and its Linux libraries | Required for browser acceptance, in addition to the Python `playwright` package. |

Confirm the selected executables with `go version`, `go env CGO_ENABLED`, `cc --version`, `python3.12 --version`, `node --version` and `docker version`. Do not print complete environment variables or credential files.

From the website repository:

```sh
cd /root/QQ-Challenge/competition-platform
python3.12 -m venv platform/.venv
platform/.venv/bin/python -m pip install -r platform/requirements.lock
platform/.venv/bin/python -m playwright install --with-deps chromium
node build.mjs
cd platform-go
go mod download
CGO_ENABLED=1 go build ./...
```

`platform/requirements.lock` pins versions but currently does **not** contain package hashes. Install it with `-r`; adding `--require-hashes` to this file will fail. `uv pip sync --python platform/.venv/bin/python platform/requirements.lock` is an alternative when `uv` is already installed. Neither command constitutes hash-verified dependency provenance. The Playwright `--with-deps` step may require host package-install privileges.

The scanner image must come from reviewed organizer sources, not an unrelated registry image with the same tag. If an accepted local `qiqcbench-agent:latest` already exists, retain it and record its image ID. On a new host, the repository-provided build is:

```sh
cd /root/QQ-Challenge/Quantum-Harbor
docker build -t qiqcbench-agent:latest docker/agent/
docker image inspect --format '{{.Id}}' qiqcbench-agent:latest
```

This build installs organizer dependencies and may access package registries. Its transitive dependencies are not a complete reproducible image lock; a new build can produce a different digest and needs its own acceptance. The running scanner resolves the image to an immutable local `sha256:` ID and uses `--pull=never`, no network, a read-only filesystem, a non-root container UID, dropped capabilities and resource limits. It reads the ZIP as data and does not install or execute its contents.

The current three scientific starter ZIPs are built deterministically by `adapters/build_templates.py` from the reviewed runtime, terminal and qsim helpers. Each has exactly `harness/`, `adapter/`, and `harness.json` at its root; `adapters/manifest.json` records its SHA. The separate `native-runtime-bundles.json` pins offline CLI dependencies prepared inside E2B. Historical `benchmark-assets/adapters/*-runtime.zip` archives belong to older releases and must not overwrite the current templates. `adapters/reference.zip` is only a local fixture and is not scientific acceptance evidence.

## Reproducible local acceptance

From `/root/QQ-Challenge/competition-platform`:

```sh
platform/.venv/bin/python platform/scripts/isolated_e2e.py \
  --pressure-teams 8 \
  --output platform/test-results/isolated-e2e-latest.json
cd platform-go
go test ./...
go test -race ./...
go vet ./...
```

The isolated driver builds the API and worker, creates a disposable private database, issues temporary credentials, runs browser checks, uploads ZIPs through the real isolated scanner, reviews submissions, runs simulated batches, tests duplicate confirmations, kills a worker and exercises lease recovery, closes admission, and restarts the API. It stops its children and removes temporary private data. It does not send email, call cloud providers, restart systemd, or mutate existing platform accounts. The command above selects eight synthetic pressure teams; the default is 32, with 100 and 200 also available. These are local fixture workloads, not evidence of equivalent E2B or model capacity.

A focused real Docker scanner regression is explicitly enabled with:

```sh
QIQC_REAL_SCANNER_TEST=1 go test -race ./internal/platform \
  -run 'Test(DockerScanner|SocketScanner|Submission|ScannerFailure|UploadDisk)'
```

Inspect the report status and source identity instead of treating the existence of a JSON file as a pass. Provider-backed science, R2 and AI review have separate deliberate acceptance commands and consume their configured resources.

## Start a separate standalone fixture environment

Use a new private directory rather than the live staging data directory. From `platform-go`:

```sh
go build -o /tmp/qiqc-api .
go build -o /tmp/qiqc-worker ./cmd/qiqc-worker
go build -o /tmp/qiqc-admin ./cmd/qiqc-admin
/tmp/qiqc-api -listen 127.0.0.1:8790 -standalone \
  -data /absolute/private/demo-data \
  -site /root/QQ-Challenge/competition-platform/dist \
  -web /root/QQ-Challenge/competition-platform/platform/web \
  -adapters /root/QQ-Challenge/competition-platform/adapters \
  -reference /root/QQ-Challenge/competition-platform/adapters/reference.zip \
  -scanner /root/QQ-Challenge/competition-platform/platform/qiqc/archive.py
```

In another terminal, using the same private directory:

```sh
QIQC_R2_ENABLED=0 /tmp/qiqc-worker -data /absolute/private/demo-data -slots 4
```

The fixture worker uses a local object-store substitute. Local object files are a test dependency, not a change to the production rule that complete task rollouts belong in private R2. The standalone processes stop with their terminals; use the reviewed local service deployment when persistent operation is required. Do not run two worker implementations against the same live queue.

Bootstrap an administrator through private stdin to `qiqc-admin -data /absolute/private/demo-data`. Its input is a JSON object containing `email`, `password` and `name`; prepare that input in a private `0600` file or equivalent secret input facility. Do not put a real password in a shell command, repository file, report or this README. The first login requires changing the initial password. Afterward `/admin` can issue team accounts and record the corresponding local-only notification intention. There is no self-service password recovery or automatic password email.

The local HTTP example does not enable Secure cookies. A verified HTTPS deployment must explicitly use `-secure-cookies`; enabling it on plain HTTP prevents the browser from returning the cookie. Production domain, TLS, origin and administrator identity checks remain deployment gates.

## Current module usage: M00–M05 and M11

| Module | Entry point and current behavior |
|---|---|
| M00 environment and reproducibility | The bootstrap and isolated runner above build from this module and retain source identity in acceptance evidence. Native Go business routes do not require a Python HTTP server. |
| M01 accounts and permissions | Private administrator bootstrap, `/admin` team creation, mandatory initial password change, persisted login throttling and session revocation. Password hashes remain compatible with the migration database. |
| M02 submission storage | `/submit` accepts bounded ZIPs; the fixed scanner freezes bytes and records source SHA, parser SHA, ruleset and image ID. Each team retains at most 25 versions; repeated bytes reuse a version. Disk admission fails closed before reading an HTTP upload body or creating ZIP files. |
| M03 review | Static evidence and live-provider advice are bound to immutable ZIP bytes. Trusted platform policy automatically approves clean valid evidence or rejects risk/invalid evidence; the complete report is staff-only and downloadable. Provider unavailability cannot approve a package. Normal entrants do not need a human decision. |
| M04 admission and quotas | Upload includes evaluation intent. Approval triggers atomic admission, quota debit and queue creation without a second confirmation. Persistent intent/idempotency and phase checks survive restart and retries. Rejected requests do not consume quota. |
| M05 queue and recovery | The Go worker claims individual task runs, limits global task slots, persists leases and fences stale attempts. Lost real cloud execution retains an attention state until reconciliation confirms its resources; a process restart does not imply permission to rerun unknown cloud work. |
| M11 notifications | `cmd/qiqc-notifications` is separate and not installed as a service. Its default `local_only` mode records intentions without sending; explicit fake mode reports `fake_delivered`. No SMTP/network mail transport is shipped. |

`QIQC_UPLOAD_RESERVE_BYTES` is a trusted startup setting for M02. It must be a positive integer byte count; the default reserve is 256 MiB. Admission requires that reserve **plus** 20 MiB for a maximum-size incoming package. Invalid configuration prevents startup, and failed disk measurement returns 503. No evidence is automatically deleted. This guard is emergency headroom, not a full capacity plan or protection against unrelated disk writers.

For M03, see [the review worker contract](internal/review/README.md). An explicit `--provider https` plus a dedicated private key file is required to contact the permitted HTTPS provider; leaving the worker disabled never performs a model call. Do not pass the full project `.env` as a key file. Provider failure and untrusted/prompt-injection text cannot approve or reject a submission.

For M11, see [the notification dispatcher contract](internal/notifications/README.md). A local-only cycle is:

```sh
go run ./cmd/qiqc-notifications \
  -database /absolute/private/demo-data/platform.db -once
```

`-mode fake` enables only a local simulation. The fake provider's idempotency memory is not persistent across new fake instances; the tests do not establish real exactly-once email delivery. Real sender identity, delivery, bounce handling, durable provider reconciliation and support ownership remain unaccepted external dependencies.

### Calendar and frozen policy

The production calendar is scheduled and persisted, using Asia/Singapore (UTC+8): quiet starts six hours before the November 8 08:00 opening, admission closes November 10 21:00, and appeals close November 11 21:00. The Infra opening hour still requires owner confirmation. The same website changes capabilities according to server time; the browser cannot open admission. Native task limits, phase identity and execution policy are fixed per accepted batch. A persisted phase watermark and trusted clock-health file prevent clock rollback or lost synchronization from reopening admission.

Use [the event configuration and release guide](deploy/event/README.md). The API must explicitly receive `-event-calendar`, the v2 task catalog, a matching controller, and the clock-health configuration. Formal admission also requires an explicit formal model/execution profile; a practice key never silently fills that role. Isolated acceptance uses its own database and temporary practice window, not a change to the live competition clock. A correct calendar does not certify scientific capacity or deployment readiness.

## Deployment and synchronized upgrades

Use `QIQC_SCANNER_SOCKET` in the service deployment so the API identity has no Docker socket access. `QIQC_SHARED_DATA=1` explicitly selects the trusted service-group storage mode; default storage is private to one OS user. Opening an existing shared setgid directory without the matching setting is rejected before its permissions are changed. The deployment scripts establish ownership and groups; do not work around the guard with recursive `chmod`.

**Upgrade the API, `scanner_helper.py` and `archive.py` together.** The API validates the scanner report's ruleset, parser digest and image identity. A new API with an old helper/parser fails closed with 503 instead of accepting incomplete evidence. Freeze acceptance and drain or reconcile active work before replacing files, retain a consistent backup and a complete previous release, then verify permissions and run the relevant acceptance checks. Do not restart or overwrite files used by an active cloud task.

The existing installer and service boundaries are documented in [the local deployment runbook](deploy/README.md). Installation is an explicit operator operation; running unit tests or this README's fixture acceptance does not install or restart services. Reviewer and notification workers remain opt-in and are not silently added by this module documentation.

## Other packages and real execution boundaries

| Package | Responsibility |
|---|---|
| `internal/platform` | Accounts, versions, review integration, calendar, quotas, queue, result publication, audit and private trace authorization |
| `internal/modelproxy` | Protocol validation, scoped authorization, atomic request reservations, bounded forwarding and usage reporting |
| `internal/rollout` | Private R2 conditional writes, verified reads and local fixture substitute |
| `internal/scienceexec` | Fixed organizer controller bridge, scoped run tokens, sandbox receipts and scientific completion |
| `internal/review` | Bounded, read-only risk advice; platform-owned automatic decision and audited exception handling |
| `internal/notifications` | Local-only intentions and simulated delivery state machine |
| `cmd/qiqc-model-gateway` | Keeps the real model credential outside participant environments |
| `cmd/rollout-check` | Explicit low-volume real R2 smoke check |
| `cmd/science-e2e-check` | Explicit real public-task scientific acceptance |

Without science configuration, the worker executes fixture work only. Normal automatic admission accepts valid reviewed contestant ZIPs, not only organizer-template hashes. It binds the uploaded bytes, selected evaluation kind, authoritative task catalog and execution profile. The worker needs the matching trusted science project, Python environment, offline dependencies and model gateway; historical diagnostic manifests do not grant arbitrary task selection. See [the scientific controller contract](internal/scienceexec/README.md) and [scientific topology evidence](../../docs/testing/reports/SCIENCE-TOPOLOGY-VALIDATION.md). Mock Astra, fixture leaderboard results and real single-task scientific diagnostics are distinct. Local regression, fake notifications and a scheduled calendar do not establish production readiness.
