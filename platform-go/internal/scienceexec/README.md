# Scientific smoke executor

This Go package runs a fixed organizer-owned Python SDK bridge. It accepts only
`kind=infra`, `mode=scientific-smoke`, one diagnostic task, and the exact reviewed zip
SHA. Participant Python/shell is never imported, installed or run on the VPS.
The Python bridge invokes the custom Harbor trial on separate E2B roles.

The worker mints an expiring attempt-scoped model token. Bridge model calls use
only the loopback Go gateway; the bridge does not read Hy4 master credentials.
The normal worker renews the lease every ten seconds. Cancellation, stale lease
or shutdown signals the bridge, allowing remote E2B cleanup before exit. Resource
receipts under the private data directory `scientific/` record sandbox IDs and
created/destroyed states, without keys. On host loss those receipts support
positive reconciliation; an expired lease is not proof a sandbox stopped.

Complete natural Harbor rollout is spooled temporarily under
`cache/<run>/<attempt>/rollout.tar.gz` with atomic fsync `pending.json`. This is
bounded upload-recovery state, not a historical rollout store. Native Go's
conditional immutable ObjectStore uploads and verifies R2 before publishing
completion, then deletes the spool. R2 failure retains exact evidence and the
same attempt for `ResumeUploads`; it does not rerun science or debit again.

Required worker flags:

```
-science-project /root/QQ-Challenge
-science-python /opt/qiqc-staging/venv/bin/python
-model-gateway http://127.0.0.1:8789
```

`QIQC_R2_ENABLED=1` and the private `QIQC_R2_ENV` must be configured for the Go
worker/API. API `ScienceHashes` must use `benchmark-assets/adapters/science-probe/allowed-submissions.json`.
The interpreter must contain the reviewed E2B SDK; set `QIQC_SCIENCE_PYTHON`
when running acceptance tools if the deployment uses another trusted virtual
environment. The old Harbor CLI interpreter path is not a valid E2B runtime.
Run this diagnostic serially while images are bootstrapped. The Go worker keeps
platform ownership; Python is a trusted Harbor/E2B compatibility bridge.

The archive includes filtered `public-view.txt` and `public-view.json` plus the
natural Harbor trial tree. Runtime comes from Harbor's agent_execution interval.
Scientific smoke is explicitly nonranking and cannot be relabelled as 49-task
full evaluation. Tests distinguish fake storage outage unit tests from the real
`cmd/science-e2e-check` acceptance.

## Positive remote cleanup reconciliation

If the controller dies without proving remote cleanup, the task enters
`worker_lost:reconciliation_required` and keeps its slot. After the operator
inspects the private receipt, run the trusted `cmd/science-reconcile` command
with `-data`, `-run`, `-attempt`, `-reason`. It uses only recorded E2B IDs,
confirms deletion through provider API, fsyncs a checked receipt, then calls
`Store.ReconcileScientificResources`. The method rejects unchecked, non-destroyed,
wrong-run, wrong-attempt and stale receipts and writes an audit entry. This does
not automatically re-run science or refund quota; normal admin policy applies
after reconciliation. Provider auth/network failures never count as deletion.

Upload race guard: the bridge initially writes `staged.json`, which maintenance
ignores. Only after the controller exits and exactly three resources have
confirmed `destroyed` does Go atomically promote it to `pending.json`. A crash
between archive creation and remote cleanup retains staged evidence for trusted
resource reconciliation; after positive cleanup reconciliation it resumes upload,
not scientific execution. The maintenance-negative test covers this boundary.

## Public CZ relay and evidence limits

The current controller uses a fixed-target qsim file relay so the main sandbox
can disable all internet access, with an empty outbound allowlist. The qsim URL,
ingress capability and MCP session remain in trusted controller memory. Do not
reintroduce a qsim hostname allowlist: the recorded real security probe reached
a different controlled sandbox by using allowed TLS SNI with another HTTP Host.
Before release, use the updated security matrix and fresh CLI browser reports;
older successful CLI runs do not prove this transport revision.

Controller downloads from the untrusted sandbox are streamed with byte limits.
Natural Harbor evidence has a 16 MiB per-file limit and 64 MiB compressed directory
limit; archive validation separately bounds expanded data at 128 MiB. Files are
written atomically inside the private tmpfs trial directory. Oversized, nonregular
or symbolic-link input fails with a controlled error and cannot become a normal
scientific zero. These limits protect this public diagnostic and must be measured
and frozen separately before extending to the full scientific catalog.

## Private catalog acceptance

The administrator can bind an approved official-template version before any
batch is accepted, using `POST /api/admin/actions` with action
`scientific_target`, `target` (version ID), `task_index` (0 through 48),
`actor`, `reason`, and an idempotency key. The index refers to the sorted task
IDs of frozen release `qiqc49-20261020-v1`. No participant can select the target.
The binding is immutable; accepting a scientific Infra batch copies it into
`batch_scientific_targets` and the hashed batch policy. It reserves one ordinary
Infra debit and one run, with `public=0`, regardless of whether the chosen task
is otherwise public. No catalog task names or indices appear in team responses.

The bridge receives `science_task_index`, `science_release_id`, and
`agent_budget_seconds=300` from the fenced persisted target. An unbound version
continues to use the public CZ diagnostic. Normal catalog completion requires
zero agent exit; a nonzero exit is accepted only when the trusted controller
reports `budget_exhausted=true`, `agent_stop_reason=budget_exhausted`, and the
exact 300-second budget. Both paths still require a completed trusted verifier, binary
reward, infrastructure health, standard Harbor evidence, positive sandbox
cleanup, and immutable R2 publication. Budget exhaustion does not forgive
infrastructure errors.

The team sees a completed diagnostic score and a 300-second bounded-test label;
its trace route returns 404. Admin snapshots include the neutral target index.
The private full rollout remains available only through trusted R2 evidence
verification. These diagnostics do not enable formal 49-task competition mode.

For catalog acceptance only, a trusted verifier can classify a scientific
rejection as exit 1 plus reward 0 using `verifier_scientific_rejection=true`.
The controller must establish this from its private structured score report,
not contestant output. Exit 1 without that classification, reward 1 with the
classification, or any other verifier failure remains an infrastructure error.
Legacy public CZ does not gain this exception.
