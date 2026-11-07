# Offline task input manifest acceptance

The catalog audit creates a separate candidate **data-input** manifest for `main`, `qsim` and `verifier` for each of 49 task identities. It reads only identity headers, compose and task.toml metadata. Hidden material bodies are not read or hashed by this catalog step. It records path, role and size; public paths follow the task author's explicit public mount declarations and are not a semantic safety judgement.

The audit does not deploy source compose files. All 49 source qsim definitions currently mount the whole config catalog; 47 task definitions declare internet access. Event providers must replace those settings with the scoped manifest and network policy. A successful static audit does not approve either source default.

Boundaries:

- `main`: only current instruction and current task's explicitly mounted read-only public materials. It cannot receive device truth, tests, another task, or whole config directories.
- `qsim`: only current task declaration, its bound device public/private configuration and current public materials. No verifier tests or private scorer material is implicitly copied. Three reviewed current-task private device inputs are assigned only when the exact task/device binding and literal Compose declaration match the reviewed allowlist; no complete hidden-material directory is authorized.
- `verifier`: current task/device materials and current tests. No other task's data is permitted.
- Named shared volumes: only the read-only poll-gated public job results and explicitly declared submission mounts are accepted for main. Full qsim logs, Docker socket and arbitrary mounts fail the audit.

These are not runnable bundles for all tasks. Shared Python dependencies, generated instances, provider replay fixtures, role images, exact private runtime dependencies and actual E2B filesystem/network attacks remain separate acceptance work. Public-classification mistakes inside author-provided files cannot be detected by a metadata-only check.

The existing public CZ bundle is additionally hardened: every required file must exist, each directory component is opened relative to a trusted directory descriptor with `O_NOFOLLOW`, hardlinks and nonregular inputs are rejected, and packaging reads from that opened descriptor. A change detected during read fails packaging. Required truth/test files can no longer silently disappear from the archive. This does not alter scoring logic or execute any task code on the controller.

Reproduce locally from the project root:

```sh
uv pip install --python competition-platform/platform/.venv/bin/python PyYAML==6.0.3
competition-platform/platform/.venv/bin/python \
  competition-platform/platform/scripts/science/manifest_acceptance.py
```

The existing platform development environment supplies pytest and E2B SDK imports for the CZ packaging regression. Importing the SDK does not create a sandbox. The command runs the catalog metadata audit, then actual pytest attack fixtures, and writes `platform/test-results/science-49-input-manifests.json`. Temporary JUnit output is removed. `offline_acceptance_passed` requires all 49 manifests and all attack tests to pass; `real_e2b_final_isolation_passed` remains false.

Attack fixtures cover traversal, absolute/Windows paths, NULs, duplicate input entries, omitted required files, parent and leaf symlinks, hardlink aliasing, FIFO inputs without blocking, modification during read, task/device rebinding, cross-task/config/truth/test insertion, broad/writeable public mounts, raw qsim log mounts, Docker socket mounts, and duplicate compose policy keys. They use synthetic files and never execute fixture code. The separate existing CZ archive regressions read only that public task's current role inputs; they do not read every hidden task payload.

## Correction and narrow input assignment — 2026-10-20

The previous count of **31** meant “tasks with files in their hidden-material directory.” It was not a missing-file check or proof that qsim needed every such file. The old checker attached the same pending label whenever that directory was nonempty; interpreting it as 31 missing private dependencies was incorrect.

Read-only review of upstream commit `97be24cda7f54766ebd8ec6c0d00392319f5e252` established the source behavior: the original qsim Dockerfile copies configs, Compose mounts configs read-only, the entrypoint resolves the explicit `QIQCBENCH_HIDDEN_PATH` before its development-example fallback, and the server passes that selected input to the typed hidden-device loader. Our event candidate manifests previously omitted three explicitly selected private device inputs. Those source files exist and are now included in the candidate qsim manifests:

| Task | Bound device | Exact current-task hidden file |
| --- | --- | --- |
| `distributed_nv_weighted_field_metrology` | `nv_network_8_v2` | `nv_network_8_v2.hidden.generated.yaml` |
| `nonmarkovian_ghz_frequency_metrology` | `trapped_ion_16q_v0` | `trapped_ion_16q_v0.hidden.generated.yaml` |
| `structured_control_margin` | `spin_chain_control_v0` | `hidden_device.json` |

Each file is under `configs/task_materials/<task>/hidden/`. `task_manifest.py` binds the exact task, device and path. `manifest_catalog_check.py` additionally requires the matching literal Compose path before adding it. A changed declaration pointing at `scorer.example.yaml`, another task, another device, an interpolated path or an unsafe mount does not acquire this permission. Main's inputs do not change. Verifier-only scorer files remain excluded from qsim.

The regenerated v2 report distinguishes **source existence**, **candidate assignment**, and **runtime validation**: 31 tasks have private materials; 3 reviewed private device inputs are assigned; 0 explicit source inputs are missing; 0 resolved explicit inputs remain unassigned. One selected hidden path is runtime-generated rather than a source file: `neutral_atom_loss_repair_control` uses `/qsim_logs/private/runtime_hidden.yaml`. The original entrypoint already calls `runtime_construction`, whose reviewed mapping declares this task. This checker preserves that type and requires verification of the existing generation contract; it does not label the implementation missing or substitute a static example.

This is a candidate data-manifest correction. It does not extend the production scientific executor beyond its current supported public task, execute benchmark code on the VPS, deploy all 49 role bundles, prove dynamic dependency closure, or prove final E2B isolation. Any separate live catalog acceptance must be cited as separate evidence. The static report deliberately retains `runtime_dependency_closure_verified=false` and `real_e2b_final_isolation_passed=false`.

Evidence:

- `platform/test-results/science-49-input-manifests-v2-20261020.json`: regenerated candidate manifests, 49/49 static path checks.
- `platform/test-results/science-49-original-private-dependencies-20261020.json`: original source path/config review and source provenance; records the initial omission diagnosis before the assignment fix.
- `platform/test-results/m06-private-assignment-tests-20261020.txt`: focused private-input and existing path-attack regression results. The tests confirm all three exact additions, scorer exclusion, main exclusion, wrong task/device rejection, and no permission expansion after a changed Compose declaration.

Focused reproduction (no cloud requests):

```sh
competition-platform/platform/.venv/bin/python -m pytest \
  competition-platform/platform/tests/test_manifest_private_dependencies.py \
  competition-platform/platform/tests/test_manifest_attacks.py -q
competition-platform/platform/.venv/bin/python \
  competition-platform/platform/scripts/science/manifest_catalog_check.py \
  --runtime benchmark-assets/qiqcbench-runtime \
  --out competition-platform/platform/test-results/science-49-input-manifests-v2-20261020.json
```
