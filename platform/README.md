# QIQC shared interface, scientific controllers and acceptance

Current HTTP business services are in [platform-go](../platform-go/README.md). The public endpoint is [the VPS website](https://173.249.36.41/); the independent bilingual staff interface is [/admin](https://173.249.36.41/admin). Old Python HTTP services and SSH tunnels are not current entrances.

- `web/`: participant UI, independent `admin.html`, staff workflows and English/Chinese translation. Values, raw evidence and user text are never translated.
- `qiqc/`: fixed organizer-controlled archive/science utilities, including compatibility fixtures. Uploaded code is never executed by a VPS helper.
- `scripts/`: repeatable isolated acceptance, scientific diagnostics, browser and fault drivers.
- `tests/`: unit/contract tests for retained trusted components.
- `pytest.ini`: keeps pytest focused on `tests/` and excludes dated `test-results/` evidence copies.
- `test-results/`: dated evidence and screenshots; not a disposable cache.
- `.venv/`: locked local tool environment; not the public web root.

Only reviewed public `web/` assets are served by Go. Private configuration and data live outside these source paths. Current rules, tests and runbooks start at [documentation navigation](../../docs/README.md).

The current scientific contract is a role-aware native catalog: 39 binary
tasks for the live leaderboard, 7 racing tasks reserved for the final top-five
Elo pass, 3 infrastructure tests, and a 5-task public subset inside the binary
set. Historical roleless catalogs remain readable for old evidence.

From this directory, `.venv/bin/python -m pytest -q` uses
`pytest.ini`, so dated evidence copies under `test-results/` are not collected
as duplicate modules. Browser driver tests skip when the Playwright executable
is not installed; run them after installing Chromium.

The previous Python prototype README is preserved in [historical development](../../docs/archive/early-development/PYTHON-PLATFORM-README.md). Use its commands only when explicitly reproducing that historical environment.
