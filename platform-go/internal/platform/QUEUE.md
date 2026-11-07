# M05 native Go queue acceptance

Implemented in `queue.go`, compatible with the existing database schema:

- `Claim(owner, slots)`: one immediate transaction checks pause and global task
  slots, selects the next batch fairly, creates one attempt, and persists a
  60-second lease. Contest closure does not stop already accepted work.
- `Heartbeat(Lease)`: extends a valid unexpired capability only. Owner, run,
  batch, attempt, attempt ID, team, version, and ZIP digest must agree.
- `ValidateLease(Lease)`: diagnostic validation. Result publication must call
  `fence` within its own write transaction; a separate earlier check is unsafe.
- `BeginUpload(Lease)`: preserves the slot and attempt while evidence uploads,
  revoking the run's model token. It does not itself upload or publish reward.
- `Recover()`: expired simulated work can requeue; a real lost sandbox becomes
  `attention` and retains its global slot. Real cancellation remains pending
  until sandbox termination is proven. Uploading attempts remain untouched for
  evidence retransmission, not another Agent execution.

Task selection uses persisted `last_dispatch`, acceptance time, and ordinal.
The dispatcher advances its ordering value even at a fixed clock, so equal
clock samples cannot starve a batch. All workers must use the same trusted slot
configuration; participants never choose the slot limit or worker identity.
Malformed pause settings fail closed. A finalizer must reject stale attempts.

## Evidence

Run from `platform-go`:

```bash
go test -race ./internal/platform -run TestQueue -count=1 -json
```

Saved output: `platform/test-results/go-queue-20261020.jsonl`.

The tests exercise 64 concurrent claims from two Store instances against four
slots; fixed-clock round robin; pause versus deadline closure; persistent
heartbeat; expired/wrong capability rejection; real-run quarantine and token
revocation; pending-upload preservation; simulated/real cancellation; and real
Python-to-Go-to-Python database interoperability on a synthetic task.

## Migration boundary

No second worker daemon is started by this module or its tests. The Python
worker can consume a Go-created lease in the compatibility test, but running
old and new schedulers together for real execution is not approved: the legacy
worker does not reserve a slot for an unresolved real sandbox the same way.
Stop legacy dispatch before a live controller cutover, reconcile all leases,
and test real sandbox termination and result publication separately.

Native Go evidence retransmission/finalization, real sandbox reconciliation,
controller wiring, and production supervision are distinct integration gates.
This module's passing unit/race/interoperability tests do not claim them done.
