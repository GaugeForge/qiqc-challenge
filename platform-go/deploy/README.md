# Current VPS deployment

The active installation is `/opt/qiqc-native-candidate`, reached through **https://173.249.36.41/**. The basename is an internal historical path; it does not imply that an old staging service is the active portal. Do not move a live database or credential directory to change a label.

| Component | systemd unit | Responsibility |
|---|---|---|
| HTTPS | `nginx.service` | TLS and reverse proxy to loopback Go API |
| API and static pages | `qiqc-native-api.service` | Go business endpoints, participant site and independent staff console |
| Worker pool (infra/public) | `qiqc-native-worker-key2.service` | E2B scheduling with `E2B_API_KEY_2`, capped at 20 concurrent tasks |
| Worker pool (full/ELO) | `qiqc-native-worker-key1.service` | E2B scheduling with `E2B_API_KEY`, capped at 100 concurrent tasks |
| Scanner | `qiqc-native-scanner.service` | Fixed isolated upload parser |
| Review | `qiqc-native-review.service` | Automated review tied to immutable uploads |
| Model gateway | `qiqc-native-gateway.service` | Scoped model tokens, protocol forwarding and usage |
| Health / backup | `qiqc-ops-health.timer`, `qiqc-ops-backup.timer` | Periodic monitoring and consistent local snapshots |

The source `operations/competition-portal.conf` installs as `/etc/systemd/system/qiqc-native-api.service.d/competition-portal.conf`. It enables `QIQC_COMPETITION_PORTAL=1`: reject new simulation requests and exclude synthetic rankings/baseline values from publication. It does not grant time-limited science results official ranking eligibility.

An interface/API update must pass isolated tests, take the existing backup, and check active batches before replacing the binary and public assets. Never synchronize the workspace root into a public directory. Frontend files come only from built `dist/` and `platform/web/`. Restart only the affected service; no whole-host reboot is needed. Keep a temporary previous binary for rollback until HTTPS and role-boundary checks pass; preserve database history on rollback.

[Persistent deployment](../../../docs/operations/PERSISTENT-DEPLOYMENT-20261021.md), [health and backup](../../../docs/operations/PERSISTENT-HEALTH-BACKUP-20261021.md), [entry and account guide](../../../docs/WEB-ENTRANCES-AND-ACCESS.md), [current acceptance](../../../docs/testing/reports/PLATFORM-ALIGNMENT-20261021.md).

`stage.py`, `cloud_stage.py`, and their old staging units are retained for historical recovery and tests, not normal upgrades of the active native installation. The full prior procedure is [archived](../../../docs/archive/early-development/LEGACY-STAGING-README.md). Do not invoke an old installer to update the current portal.
