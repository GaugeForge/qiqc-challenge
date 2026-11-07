# QIQC submission starter packages

Upload one ZIP with exactly three root entries: `harness/`, `adapter/`, and
`harness.json`. The platform scans the ZIP in an isolated container, keeps the
original bytes, reviews them, and automatically queues the selected evaluation.
A successful upload is not yet an accepted evaluation; the status panel shows
review, admission, execution, and completion separately.

The three source packages invoke the actual native CLIs. There is no HTTP-client
substitute for DeepSeek Harness or Pi. `build_templates.py` builds deterministic
ZIPs from `runtime.py`; `manifest.json` records each package SHA-256, the
minimal manifest and the derived (normalized) manifest. These source
packages supersede the old `v1-demo` templates. Old submitted ZIPs and their hashes
remain unchanged in the database.

| Package | Supported runtime versions | Model interface |
|---|---|---|
| Codex | `@openai/codex` 0.166.0, 0.165.0, 0.164.0, 0.163.0, 0.162.1, 0.162.0, 0.144.4 | Responses |
| DeepSeek Harness | `@deepseek-ai/dsh` 0.2.2-rc.1, 0.2.1-rc.5, 0.2.1-rc.4, 0.2.1-rc.3, 0.2.1-rc.2 | Chat Completions / Responses |
| Pi Agent | `@earendil-works/pi-coding-agent` 1.5.0, 1.4.0, 1.3.0, 1.2.0, 1.1.0, 1.0.4 | Chat Completions / Responses |

DSH/Pi dependency bundles were built in isolated E2B containers with the existing
npm locks and Node 24.9.0 (official archive SHA-256 verified). They are retained
in an execution snapshot; native-runtime-bundles.json binds their exact bytes.
The original task images are unchanged. Setup verifies the bundle and extracts
it only inside main, with no network installation during the task. Runtime
preparation is not scientific acceptance: the new packages still require their
full model/qsim/verifier acceptance before publication. The VPS never installs
or executes uploaded code.

## harness.json

Participants write only what the platform cannot infer. The minimal file is:

```json
{"runtime": "codex"}
```

| Field | Required | Meaning |
|---|---|---|
| `runtime` | yes | `codex`, `deepseek` or `pi` (`deepseek-harness` / `pi-agent` are accepted aliases) |
| `entrypoint` | no | Shell script inside `adapter/`, ending in `.sh`; default `adapter/run.sh` |
| `name` | no | Free-text label, 1–80 characters, stored with the version (`harness_name` in the API); default derived from the runtime |
| `description` | no | Free text, at most 500 characters |

The trusted parser (`platform/qiqc/archive.py`, `normalize_manifest`) derives
everything else from the selected runtime family: `schema`
(`qiqc-harness/v1`), `adapter`, `runtime_kind` (`native_cli`),
`runtime_package`, `runtime_version`, `model_interfaces`, `trajectory`
(`native_jsonl`) and `task_transport` (`native-http/v1`). The normalized form
is what the scanner stores in the review report and what the science
controller (`native_compose_contract.submission_manifest`) uses. `runtime_version`
in that report identifies the immutable organizer execution snapshot; the
compatibility window above controls which participant declarations are legal.

Old full-form manifests (the previous starters) remain valid. Any of the
derived fields may still be present, but each must use an accepted value for
the selected runtime; an unsupported `runtime_version` or an incompatible
interface is rejected with an English error that names the field. `model_interfaces` may declare a non-empty subset of the runtime's
interfaces; the first one is used. A legacy `version` string is tolerated and
ignored. Model keys, provider overrides, task selection and resource limits do
not belong in this file.

Participants do not number versions. The platform labels each team's stored
uploads `v1`, `v2`, `v3`, ... in upload order (computed on read; stored rows
are not rewritten). Re-uploading identical bytes returns the existing version.

The runner supplies `QIQC_TASK_INPUT`, `QIQC_MODEL_URL`, `QIQC_MODEL_TOKEN`,
`QIQC_MODEL`, `QIQC_MODEL_PROTOCOL`, `QIQC_AGENT_TIMEOUT`, `QIQC_MCP_SERVERS`,
`QIQC_OUTPUT` and `QIQC_TRAJECTORY`. The model token is scoped to one run.
The Codex starter preserves the runner's original MCP configuration. The timeout
comes from the authenticated per-task policy; the starter has no independent
five-minute limit. Task-native output requirements still apply; a final chat
message does not replace an answer artifact required by the verifier.

A completed evaluation may have reward 0. A missing runtime, failed verifier,
missing archive or unresolved sandbox cleanup is an infrastructure failure and
must not be relabeled as a valid zero score.

DSH/Pi use the supplied harness/qsim.py to discover and call the original task
MCP service through their shell tool. It uses the original image's MCP library;
it does not replace qsim or provide private scoring data. Pi uses the required
`$QIQC_MODEL_TOKEN` environment reference in its provider configuration.
