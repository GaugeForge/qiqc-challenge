# Event configuration

`calendar.example.json` records the confirmed formal boundaries. The API uses an explicit `infra_ends_at` when it is present. The current
participant handbook opens infrastructure testing on 6 November 2026 at 20:00
Asia/Singapore and ends it on 8 November 2026 at 00:00; the eight-hour quiet
period then ends when the competition opens.
This is a reviewable configuration file; the active service loads its private
copy through `-event-calendar`.

The paired `infra_starts_at` and `infra_ends_at` values must remain no later
than the quiet boundary. Use a new calendar version when content changes; the
same version cannot be rebound. Pass the private reviewed copy through
`-event-calendar` when releasing the API.

The production API also requires the host health timer's clock record at
`/run/qiqc-status/clock.json`. Only disposable acceptance processes should use
`-clock-health ''`. The record cannot be written by a participant HTTP request.

Formal model admission requires `-formal-execution` with these fields:

- `profile_path`: absolute private provider-profile file;
- `profile_sha256`: canonical identity SHA returned by provider-profile validation;
- `model`: exactly the profile model;
- `gateway_url`: the trusted loopback gateway serving that profile;
- `max_requests_per_task`: explicitly selected positive integer, not a hidden default.

No formal credentials or official limits are supplied in this repository.
The profile references a separate key file and uses the existing provider-profile
schema. Validate gateway behavior and worker/controller compatibility before
loading the configuration. The API does not treat a readable file as a successful
cloud integration check. Test and formal credentials may use separate loopback
gateway processes on the same VPS; contestants keep the same website and account.

Release API, worker, scanner, native controller manifest, task catalog and adapter
packages consistently. Do not point the live old controller at the new catalog
just because parsing succeeds. See the operations handbook, chapter 29.

Task slots and model-request concurrency are independent. The installer exposes
`--functional-slots` and `--model-max-concurrent`; the worker and API share the
persisted task limit. Both accept explicit positive configuration up to the
existing 10,000-value guard, which is input validation, not tested capacity.
The legacy model default remains 49 for compatibility; it is not a formal
competition setting. The controller inventory binds the selected model cap.

Use `--account-continuous-limit-seconds` only with provider evidence. The current
49 original configurations require at most 15,660 seconds including all stages.
A bounded provider check admitted that timeout and immediately destroyed its
sandbox; it does not prove sustained duration. The old 5,400-second candidate
setting cannot support every formal task and must not be copied into a formal
release unchanged. See `artifacts/final-development/provider-window-20261021.json`.


The complete local deployment was activated on 22 October 2026 and the
infrastructure opening was corrected to 28 October 12:00 on 27 October 2026.
See
[the release record](../../../../docs/operations/RELEASE-ACTIVATION-20261022.md)
for the original controller identity, preserved data and HTTPS checks. The
active deployment now uses this event configuration; before the opening time,
preparation admission stays closed while account and staff pages remain
available. Missing formal credentials are still an explicit admission gate.

When running the installer under systemd, supply the build account's `GOPATH`
and `GOCACHE` explicitly; transient units do not inherit an interactive login
shell's home environment. The installer builds with `-trimpath`, matching the
accepted binaries. Keep these nonsecret build paths separate from service secrets.
