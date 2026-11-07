# Isolated operations acceptance tools

These tools do not restart services, read credential files, call cloud services,
send mail, or change production settings. They support M09/M12/M16 acceptance.

## Run the synthetic drill

From the workspace root:

```bash
PYTHONPATH=competition-platform/platform \
  competition-platform/platform/.venv/bin/pytest -q \
  competition-platform/platform/ops/test_drills.py \
  --junitxml=competition-platform/platform/test-results/ops-drills-20261020.xml
```

Every test uses temporary directories, synthetic accounts/tasks, an explicitly
disabled R2 client, and a missing credential-file path. It exercises the real
Python Engine's persistence and upload recovery, not the shared Demo database.
The failure injection uses `OSError` labels; it does not reproduce HTTP/S3 SDK
behavior or actual account throttling. Test outputs do not contain credentials.

## Snapshot bundle

`python -m ops.snapshot_bundle create SOURCE DESTINATION --writes-quiesced`
uses SQLite's backup API, inventories every copied file, verifies full SHA-256,
sets private permissions, and publishes a verified local bundle. The operator
must actually pause application writes before acknowledging this flag: no lock
or maintenance API is invoked by the tool. Database and uploads are included;
rollout objects are excluded by default. Tests alone opt into synthetic objects.

`python -m ops.snapshot_bundle verify BUNDLE` checks the file set, paths, file
types, digests, SQLite integrity and foreign keys. `restore BUNDLE DESTINATION`
requires a new destination, verifies before and after copying, and never restores
into the live source. Capture the returned manifest digest outside the bundle
for trusted custody. The internal manifest provides integrity detection, not
cryptographic authenticity against someone who can rewrite both data and
manifest. Review that external digest before restoring a real backup.

The tool does not encrypt, upload, manage retention, back up real rollout data,
or reconcile live E2B leases. It requires a trusted operator-owned source and
quiesced writes. Same-host staging plus rename is not off-site disaster recovery
and does not claim power-loss durability of all directory metadata. Formal
backup requires encryption, independent storage/credentials, externally trusted
manifest custody, retention, and application-level restore sign-off.

## Lifecycle preview

`ops.lifecycle_preview.preview` produces metadata-only decisions and performs no
delete. It excludes active references/holds, unverified sources or copies,
unfrozen retention, and unexpired objects. Timezones and unique run/attempts are
required. Before any real deletion, recheck references and holds under a trusted
transaction/lock and use a separately authorized identity. A successful preview
does not prove R2 lifecycle configuration or delete permission separation.

## Current evidence and limits

`test-results/ops-drills-20261020.{txt,xml}` records 22 cases:

- Real Engine restoration preserves quota, score, trace, immutable request
  identity, and cross-team denial.
- DB, ZIP, or synthetic rollout corruption prevents publication of a restore.
- Missing/extra files, links, and manifest traversal are rejected.
- Existing destinations and non-quiesced creation are rejected.
- Default bundles exclude rollout history.
- Simulated 429, 503, permission, and network faults preserve the same upload
  bytes through Engine restart without another Agent attempt or quota charge.
- Retention candidate preview protects holds/references, unverified backups,
  unfrozen policy, and future expiry; ambiguous timezones/duplicates fail.

Real R2 lifecycle timing, actual provider 429 behavior, off-site recovery,
encryption custody, production identities, and mail/HTTPS readiness remain
separate formal gates. See `docs/handbooks/operations-manual.*.md`.
