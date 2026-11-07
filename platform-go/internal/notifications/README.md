# Local notification dispatcher

This package is a local implementation of the M11 delivery state machine. It contains **no SMTP, email API or other network delivery implementation**, and no existing service automatically starts it.

The default mode is `local_only`. A bounded cycle records versioned intentions in `notification_deliveries` without contacting a transport. Existing `outbox.status` records remain `local_only`. The only enabled test mode is `fake`; successful simulated sends are explicitly recorded as `fake_delivered`.

## Data and failure contract

- The existing outbox is read only. The additive delivery table is created by `New`; `schema.sql` is not changed.
- `(event_id, template_version, mode)` uniquely identifies a delivery intention.
- Only fixed event templates are rendered. The dispatcher does not read `outbox.payload`, so passwords, hidden tasks, rollout content and arbitrary submitted text cannot enter a message through that field.
- Each event resolves exactly one team account and carries exactly one recipient. Missing, ambiguous or malformed recipients become permanent failures. There are no CC/BCC fields.
- Provider errors and panic text are never persisted. Stored categories are `temporary`, `permanent`, or `lease_expired`.
- Claims have a random owner, attempt number and expiry; stale completions cannot overwrite the current attempt.
- Default lease: 30 seconds; send context deadline: 10 seconds; maximum attempts: 4. Retry delays: 5, 10 and 20 seconds before the last attempt; configurable backoff caps at 5 minutes.
- A restart recovers expired leases. Retries reuse the same provider idempotency key derived from the event ID and template version.
- Delivery state changes occur outside business transactions and never mutate scores, batches or quota ledgers.
- The admin snapshot adds only aggregate mode/status counts and explicitly reports `real_mail_enabled=false`. It contains no recipient or provider error text.

## Exactly-once limitation

The fake transport deduplicates keys **only within one in-memory fake provider instance**. Tests simulate a dispatcher restart while that provider remains alive. A new fake provider instance loses that memory. This is not evidence of persistent provider idempotence, real delivery, bounce processing, or exactly-once behavior across a real provider outage.

A production transport must resolve the ambiguous interval between “provider accepted” and “database completion committed.” It needs a durable provider idempotency key or a delivery-status reconciliation contract before automated retries can claim not to resend an accepted notice.

## Local worker

From the `platform-go` directory, a single cycle against an isolated database copy is:

```sh
go run ./cmd/qiqc-notifications -database /path/to/isolated-copy/platform.db -once
```

Explicit local simulation adds `-mode fake`. The command rejects `smtp`, `production` and other modes. It never prints message content or recipients. Shared data directories still require the repository's existing `QIQC_SHARED_DATA=1` setting; do not invoke a local fixture command on a running competition database.

## Verification and remaining gates

`go test -race ./internal/notifications` covers local-only defaults, idempotent ingestion, recipient isolation, retry and dead letter behavior, simulated permanent rejection, stale-worker fencing, provider acceptance followed by a lost completion, and a 200-team / 400-notice / 8-dispatcher fake burst.

Formal M11 sign-off remains blocked on approved sending identity/domain, provider credentials and quota, actual sandbox inbox receipt, verified bounce events, durable duplicate suppression, Chinese/English rendering, operator-approved templates, support mailbox ownership and an explicit send authorization. Announcements and support-ticket workflow are separate remaining product capabilities; this dispatcher does not claim to implement them.
