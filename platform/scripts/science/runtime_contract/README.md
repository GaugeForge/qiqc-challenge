# E2B runtime dependency contract

Status: undeployed local candidate. The controlled Docker/Compose capability probe
passed on 2026-10-20; the preferred next implementation preserves the upstream
Compose/images. Do not activate this alternate venv route merely because its
offline contract tests pass.

The scientific task release is unchanged. `runtime_contract.py` reads and verifies
the retained upstream Dockerfile, qsim lockfile and public agent helper files from
that release. It never reads the private upstream checkout during production.

* Main: preserve the 13 direct Python pins in the original agent Dockerfile and
  copy its nine public client helpers to `/opt/qiqcbench-agent`. The original
  Dockerfile leaves transitive dependencies floating; `main-requirements.txt`
  additionally freezes the 200-package closure resolved on 2026-10-20.
* qsim and ordinary verifier: use original `docker/qsim/requirements.txt` bytes,
  including all hashes and environment markers.
* Hamlearn verifier: use its original independent `tests/requirements.txt`,
  without installing the unrelated qsim dependency environment first.
* Every role: prepare CPython 3.11.14 in E2B using the checksum-bound uv bootstrap,
  then an isolated `/opt/qiqc-python` virtual environment. Install only wheels
  with hash checks, check dependency consistency, and record package versions,
  source hashes and interpreter provenance in `/opt/runtime-environment.json`.

The 3.11.14 interpreter patch follows the exact Hamlearn source Docker image;
ordinary upstream images specify 3.11 rather than this exact patch. The Linux
distribution and system package versions still require a frozen E2B template
and resource verification. These scripts alone do not establish Docker image
equivalence.

The agent closure was generated without executing package build scripts:

```text
uv 0.12.24
uv pip compile <original-direct-pins.in> --python-version 3.11 \
  --python-platform x86_64-manylinux_2_28 --no-build --generate-hashes \
  -o main-requirements.txt
```

`original-direct-pins.in` contains the original Dockerfile's quoted `name==version`
requirements, one per line. Contract validation rejects changes to these direct
pins. A dependency update requires explicit review and a new closure; package
resolution must never silently select a replacement for an unavailable version.

Offline tests verify source hashes, package contracts, role restrictions and
provision commands. PyPI metadata and resolver checks establish availability and
resolution only. Actual installation, compiled imports, client protocol behavior,
resource capacity and scientific verifier execution must still pass in E2B.
