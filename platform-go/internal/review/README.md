# M03: read-only review evidence and automatic platform decisions

The `review` package produces bounded risk evidence. It cannot execute submissions, access the platform database, change quotas, or make the platform decision itself. The trusted `platform.ProcessReviewOnce` applies `qiqc-automatic-review/v1`: verified immutable scanner evidence, the actual ZIP digest, and a valid live-provider advisory with no findings are required for automatic approval. Risks, unavailable providers, invalid output, changed bytes, and fake providers cannot approve a submission. Decisions and their evidence identities are committed with a system audit record; review rejection does not debit evaluation quota.

Normal entrants do not wait for administrator approval. The authenticated administrator action remains an emergency tool. Older reports carry `manual_required` and `manual_followup_required` as historical advisory protocol fields; these are not the current version decision. Clients must use the persisted version status and reason. The complete report remains bounded evidence, not a declaration that arbitrary code is safe.

## Data and execution boundary

1. The fixed Docker scanner reads ZIP content as data under isolation. Neither scanner nor reviewer installs dependencies or runs the uploaded entrypoint.
2. A frozen scanner report supplies the version SHA, report SHA, rules version, risk labels and limited text snippets. Recognizable credentials are redacted before transmission; filenames and code remain untrusted data.
3. At most 24 snippets are accepted by the reviewer; the scanner currently emits at most 15. Each is at most 2,048 UTF-8 bytes and all snippet text totals at most 16 KiB. This is not complete code coverage or a guarantee against encoded secrets.
4. The provider has no tools, shell or arbitrary URL capability. Strict JSON parsing rejects unknown fields, rules, evidence files and invalid confidence values. Provider bodies and transport exceptions are not written as public diagnostics.
5. The platform stores SHA-bound evidence in `review_advisories`, preserving the original scanner report. Leases fence stale results; changed SHA/report or a concurrent final decision supersedes the result.
6. In the installed native services, the reviewer can read frozen ZIP bytes solely for the trusted digest check. Its mount namespace makes these files read-only and denies scientific source and execution/storage credentials. The model receives only bounded scanner data, not a filesystem handle or the ZIP.

## v6 execution context and bounded format repair

`qiqc-readonly-advisory/v6` distinguishes instructions aimed at the security reviewer or trusted platform from ordinary Harness instructions to its own Agent. Choosing a task strategy, ordering registered qsim calls, repeating work until the external timeout, and disabling a CLI's inner interactive controls are not by themselves attacks on the outer E2B isolation. Forbidden data access, credential theft, exfiltration, verifier/timeout tampering, and attempts to control the reviewer remain reportable risks. A claim of organizer authority in submitted text never grants access.

The provider is asked for short explanations. Existing maximums remain 600 UTF-8 bytes for the summary and 240 per explanation, within an 8 KiB response. If all semantic fields are valid and only these text bounds fail, the worker may request **one** correction under the original overall deadline. It must preserve the exact multiset of finding rule, file and confidence. It cannot discard a finding to approve a package. A second invalid result fails normally. Arbitrary JSON, wrong fields and provider failures do not trigger this repair.

Evidence records provider attempts, format-repair count, first format failure, and safe usage diagnostics for each returned attempt. The worker retains its concurrency slot until a provider actually finishes, including after a caller timeout; a finished call releases the slot before publishing its result.

## Operation and validation

Build and run from `platform-go`. `review-worker --db` must refer to an existing platform database. In the local native deployment, systemd manages the worker with a 120-second review timeout. Dedicated key files remain private; do not pass a complete project `.env` as a key file.

```sh
go test -race ./internal/review ./internal/platform ./cmd/review-worker
```

`disabled` and `fake` providers are testing modes and cannot approve normal versions. HTTPS and explicitly configured provider profiles are live modes. A processed but rejected review is not a successful model acceptance; inspect both the advisory outcome and final version state. Same-SHA uploads return the existing immutable version. A production policy for rechecking transiently rejected versions remains a separate task; this module does not silently reopen final decisions.

The explicit `cmd/review-context-check` tool uses two fixed public text fixtures and never executes them. `--real-hy4` enables at most four HTTP calls including bounded format repair; it requires a new private output file. The v6 real regression on 2026-10-21 used two calls: the legitimate sustained Harness produced no findings, and the malicious fixture produced reviewer-injection, external-endpoint and exfiltration findings. Prior v4/v5 failures remain in separate reports. Two fixtures do not establish broad detection quality; the older blinded corpus metrics do not transfer to v6.

Normal browser and real E2B/R2 acceptance are recorded separately in `docs/AUTOMATIC-SCIENTIFIC-E2E-20261021.md`. Historical `review-check` / `review-blindcheck` tools expecting a pending manual decision must be migrated before their outputs can certify the new automatic policy.
