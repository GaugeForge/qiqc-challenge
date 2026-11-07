# M07 native Go model gateway

This package owns HTTP protocol and concurrency behavior. It does not load `.env`,
know database credentials, start a listener, or choose a competition model/price.
The trusted service wires policy and persistence through two callbacks:

```go
proxy, err := modelproxy.New(modelproxy.Config{
    BaseURL: configuredProviderURL,
    MasterKey: configuredProviderKey,
    Authorize: func(ctx context.Context, token, model string) (modelproxy.Grant, error) {
        // Check hash, expiry, run/attempt, running state, cancellation and model.
        // Return Grant{RunID, Attempt, Model, TokenHash}; never the master key.
        return grant, nil
    },
    RecordUsage: func(ctx context.Context, record modelproxy.UsageRecord) error {
        // Durably append provider attempt/status and nullable usage, bound to
        // record.Grant.RunID/Attempt/Model. Apply the frozen pricing version.
        return nil
    },
})
```

The callbacks must honor their contexts. Authorization is called before waiting,
after obtaining the global slot, and before each upstream retry. The grant must
remain identical. `RecordUsage` receives an independent two-second deadline so a
client disconnect does not erase an incurred charge or unknown-usage event.
Unknown token values are nil, not zero. `UsageRecord.Attempt` is the upstream
HTTP retry number; `UsageRecord.Grant.Attempt` is the scientific task attempt.
`HTTPError{Status: 401/403/429}` carries safe authorization status; arbitrary
callback error messages never enter public responses.

## Supported routes and defaults

- `POST /v1/chat/completions`, `POST /v1/responses`, `GET /healthz`.
- JSON responses and SSE; Chat cumulative usage and Responses completed/incomplete
  usage. Provider `response.incomplete` can represent output-limit completion,
  whereas broken transport or absent stream terminal is an infrastructure error.
- Two concurrent requests, three eligible HTTP attempts, 120-second total request
  budget including queue/retries, retry delays capped at four seconds.
- Four MiB request and SSE-event bounds, sixteen MiB nonstream response bound.
- Provider URL accepts a base or full Chat/Responses endpoint; endpoint switching
  strips the existing suffix. Userinfo, query and fragment are rejected.
- HTTPS required unless a trusted caller explicitly opts into local-test HTTP.
- Only local `function`/`custom` tool types allowed by default. Provider-hosted
  web/MCP/file-search tools require an explicit frozen capability policy.

These are bounded Demo implementation defaults, not a frozen supplier contract.
Set production values from the approved model policy. Server read/write/header
limits and protected listener binding remain the integrating service's job.
Callbacks must not perform unbounded blocking work or record prompts/keys.

## Boundaries

Only the trusted `MasterKey` becomes upstream Authorization. Incoming headers,
provider Set-Cookie, redirects, and raw error bodies are not forwarded. A supplied
HTTP client's redirects are disabled and its cookie jar is not reused. The
built-in transport ignores environment proxy settings; a custom transport must
be trusted. HTTP-200 JSON error payloads are also redacted.

Cross-request `previous_response_id`, conversation references and `item_reference`
are rejected; Responses storage is disabled. These protections do not themselves
prove sandbox egress, hidden-truth isolation, provider retention, or safety of all
possible multimodal/provider features. They need the separate scientific and
model-policy gates.

A cancelled, expired, or queued-invalid run cannot initiate a new provider call.
Client cancellation closes the provider response and releases the slot. Provider
HTTP 429/502/503/504 retries are bounded, reauthorized, and recorded individually.
Ambiguous network errors are not automatically retried. Retry-After integers and
HTTP dates are bounded by the request deadline and configured maximum.

Streaming events are validated and bounded before forwarding. Usage is durably
recorded before a successful terminal event. Ledger failure suppresses that
terminal and emits a generic SSE error; nonstream ledger failure returns 503.
An interrupted stream is not silently labelled successful and is never retried
from its beginning after emitting content. Usage not reported by the provider
stays unknown and requires reconciliation.

## Acceptance evidence

`platform/test-results/go-modelproxy-red-20261020.txt` records the initial missing
implementation failure. `go-modelproxy-20261020.jsonl` records strict fake-provider
contracts with the race detector, including endpoint switching, both protocols,
stream usage, missing usage, request/response/event bounds, authorization, pending
revocation, waiting/cancellation deadlines, bounded 429, no redirect leakage,
provider error redaction, unknown usage on cancellation, slot release, and ledger
failure. These tests call only local httptest servers and never Hy4 or live tokens.

Run from `platform-go`:

```bash
go test -race ./internal/modelproxy -count=1 -json
```

Formal supplier prices, account limits, model snapshot, live stream compatibility,
pricing policy, and service supervision still need their own evidence. The root
controller integrates database authorization and usage callbacks separately.
