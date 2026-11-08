#!/usr/bin/env python3
"""Local, root-only operations. Never executes submissions or contacts a provider."""
import argparse
import datetime
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import socket
import sqlite3
import ssl
import stat
import subprocess
import tempfile
import time
import urllib.request

# The production worker is deliberately split by E2B credential pool.  Keep
# both pools in the health contract; the retired single-worker unit must not
# make an otherwise healthy deployment look degraded.
SERVICES = [
    "qiqc-native-api.service",
    "qiqc-native-worker-key2.service",
    "qiqc-native-worker-key1.service",
    "qiqc-native-gateway.service",
    "qiqc-native-review.service",
    "qiqc-native-scanner.service",
]


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(value, indent=2) + "\n")
    temp.chmod(0o600)
    os.replace(temp, path)


def digest(path):
    h = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def write_clock_health(path, sampled_at, synchronized):
    """Publish only nonsecret time health, readable by the restricted API user."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
    path.parent.chmod(0o755)
    write_json(path, {"sampled_at": sampled_at, "synchronized": synchronized})
    path.chmod(0o644)


def connect_readonly(path):
    return sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=15)


def update_alerts(state_dir, checks, now):
    """Emit only opening, changing severity, and recovery events; no secrets."""
    state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    previous_path = state_dir / "health.json"
    previous = json.loads(previous_path.read_text()) if previous_path.exists() else {}
    old = previous.get("alerts", {})
    alerts = {k: v for k, v in checks.items() if v != "ok"}
    events = []
    for code in sorted(old.keys() | alerts.keys()):
        if old.get(code) != alerts.get(code):
            events.append({"at": now, "code": code, "state": alerts.get(code, "recovered")})
    log = state_dir / "alerts.jsonl"
    if log.exists() and log.stat().st_size > 5 * 1024 * 1024:
        os.replace(log, state_dir / "alerts.previous.jsonl")
    with log.open("a") as output:
        for event in events:
            output.write(json.dumps(event) + "\n")
            print(json.dumps(event), flush=True)
    log.chmod(0o600)
    report = {"sampled_at": now, "status": "attention" if alerts else "ok", "alerts": alerts,
              "checks": checks, "delivery": "persistent_local_log_and_systemd_journal",
              "external_delivery": False}
    write_json(previous_path, report)
    return report


def health(root, state_dir, backup_root):
    now = time.time()
    checks = {}
    try:
        result = subprocess.run(["timedatectl", "show", "--property=NTPSynchronized", "--value"], capture_output=True, text=True, timeout=5, check=False)
        synced = result.returncode == 0 and result.stdout.strip() == "yes"
    except Exception:
        synced = False
    checks["host_clock_synchronized"] = "ok" if synced else "critical"
    write_clock_health(Path("/run/qiqc-status/clock.json"), now, synced)
    config_file = Path("/etc/qiqc-operations/monitor.json")
    config = json.loads(config_file.read_text()) if config_file.exists() else {}
    for service in SERVICES + config.get("services", []):
        result = subprocess.run(["systemctl", "is-active", "--quiet", service], timeout=10, check=False)
        checks["service:" + service] = "ok" if result.returncode == 0 else "critical"
    try:
        with urllib.request.urlopen("http://127.0.0.1:19890/healthz", timeout=10) as response:
            checks["http_api"] = "ok" if response.status == 200 else "critical"
    except Exception:
        checks["http_api"] = "critical"
    if config.get("https_host"):
        try:
            with socket.create_connection((config.get("https_connect_host", "127.0.0.1"), 443), timeout=10) as plain:
                with ssl.create_default_context().wrap_socket(plain, server_hostname=config["https_host"]) as remote:
                    cert = remote.getpeercert()
                    remaining = ssl.cert_time_to_seconds(cert["notAfter"]) - now
                    checks["https_certificate_expiry"] = "warning" if remaining <= config.get("certificate_warning_hours", 48) * 3600 else "ok"
                    remote.sendall(("GET /healthz HTTP/1.1\r\nHost: " + config["https_host"] + "\r\nConnection: close\r\n\r\n").encode())
                    response = remote.recv(4096).split(b"\r\n", 1)[0]
                    checks["https_entry"] = "ok" if b" 200 " in response else "critical"
        except Exception:
            checks["https_entry"] = "critical"
    disk = shutil.disk_usage(root)
    used = (disk.total - disk.free) / disk.total
    checks["disk_capacity"] = "critical" if used >= .90 or disk.free < 5 * 1024**3 else "warning" if used >= .85 else "ok"
    try:
        with connect_readonly(root / "data/platform.db") as db:
            checks["sqlite_integrity"] = "ok" if db.execute("PRAGMA quick_check").fetchone()[0] == "ok" else "critical"
            stale = db.execute("SELECT COUNT(*) FROM runs WHERE state='running' AND lease_until<?", (now - 120,)).fetchone()[0]
            old = db.execute("SELECT COUNT(*) FROM runs r JOIN batches b ON b.id=r.batch_id WHERE r.state='queued' AND b.accepted_at<?", (now - 1800,)).fetchone()[0]
            attention = db.execute("SELECT COUNT(*) FROM runs WHERE state='attention'").fetchone()[0]
            checks["expired_worker_lease"] = "critical" if stale else "ok"
            checks["queue_wait_over_30m"] = "warning" if old else "ok"
            checks["runs_need_attention"] = "warning" if attention else "ok"
    except Exception:
        checks["database_read"] = "critical"
    backup_times = []
    for path in backup_root.glob("snapshot-*/manifest.json"):
        try:
            item = json.loads(path.read_text())
            if item.get("verified") is True:
                backup_times.append(item["created_at"])
        except (OSError, ValueError, KeyError):
            continue
    checks["backup_under_8h"] = "ok" if backup_times and now - max(backup_times) < 8 * 3600 else "warning"
    failed_backup = subprocess.run(["systemctl", "is-failed", "--quiet", "qiqc-ops-backup.service"], timeout=10, check=False)
    checks["last_backup_service"] = "critical" if failed_backup.returncode == 0 else "ok"
    return update_alerts(state_dir, checks, now)


def verify_snapshot(snapshot):
    manifest = json.loads((snapshot / "manifest.json").read_text())
    for entry in manifest["files"]:
        path = snapshot / entry["path"]
        if "link" in entry:
            if not path.is_symlink() or os.readlink(path) != entry["link"]:
                raise ValueError("snapshot symlink mismatch")
        elif not path.is_file() or path.is_symlink() or digest(path) != entry["sha256"]:
            raise ValueError("snapshot checksum mismatch")
    # Restore into a fresh private directory; do not replace the live DB.
    with tempfile.TemporaryDirectory(prefix="qiqc-restore-") as folder:
        target = Path(folder) / "platform.db"
        shutil.copy2(snapshot / "data/platform.db", target)
        with connect_readonly(target) as db:
            if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise ValueError("restored database integrity failure")
            hashes = [r[0] for r in db.execute("SELECT DISTINCT sha256 FROM versions")]
        for sha in hashes:
            if not re.fullmatch("[a-f0-9]{64}", sha):
                raise ValueError("invalid submission hash")
            source = snapshot / "data/uploads" / (sha + ".zip")
            copy = Path(folder) / (sha + ".zip")
            shutil.copy2(source, copy)
            if digest(copy) != sha:
                raise ValueError("restored submission hash mismatch")
    return {"database_integrity": "ok", "referenced_unique_zips": len(hashes), "files_checked": len(manifest["files"]),
            "restore_scope": "fresh SQLite database and every referenced immutable ZIP; no live replacement"}


def backup(root, backup_root, retention=3, include_host=True):
    if retention < 2:
        raise ValueError("retain at least two known-good backups")
    backup_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    backup_root.chmod(0o700)
    # Reserve for a complete copy before retention removes any known-good snapshot.
    estimated = sum(path.lstat().st_size for name in ("bin", "config", "project", "source", "adapters", "data/uploads")
                    for path in (root / name).rglob("*") if path.is_file() and not path.is_symlink())
    if shutil.disk_usage(backup_root).free < estimated + 2 * 1024**3:
        raise ValueError("insufficient backup disk reserve")
    name = "snapshot-" + datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    stage = backup_root / (".pending-" + name)
    stage.mkdir(mode=0o700)
    try:
        (stage / "data/uploads").mkdir(parents=True, mode=0o700)
        # SQLite online backup provides a consistent snapshot while services run.
        with connect_readonly(root / "data/platform.db") as source, sqlite3.connect(stage / "data/platform.db") as dest:
            source.backup(dest, pages=256, sleep=.05)
        with connect_readonly(stage / "data/platform.db") as db:
            hashes = [r[0] for r in db.execute("SELECT DISTINCT sha256 FROM versions")]
        for sha in hashes:
            if not re.fullmatch("[a-f0-9]{64}", sha):
                raise ValueError("invalid submission hash")
            source = root / "data/uploads" / (sha + ".zip")
            if source.is_symlink() or digest(source) != sha:
                raise ValueError("immutable submission unavailable or changed")
            shutil.copy2(source, stage / "data/uploads" / source.name)
        # Explicit allowlist excludes rollout/cache/objects/failure evidence and browser credentials.
        for name in ("bin", "config", "project", "source", "adapters"):
            shutil.copytree(root / name, stage / name, symlinks=True)
        metadata = {"candidate.json", "controller-manifest.json", "scanner_helper.py", "science-submissions.json", "source-audit.json"}
        metadata.update(path.name for path in root.glob("source-audit-*.json"))
        for name in sorted(metadata):
            if (root / name).is_symlink():
                raise ValueError("deployment metadata must not be a symlink")
            if (root / name).is_file():
                shutil.copy2(root / name, stage / name)
        unit_dir = stage / "systemd"
        unit_dir.mkdir()
        for source in (Path("/etc/systemd/system").glob("qiqc-*") if include_host else []):
            if source.is_file() and not source.is_symlink():
                shutil.copy2(source, unit_dir / source.name)
            elif source.is_dir() and source.name.endswith(".d") and not source.is_symlink():
                shutil.copytree(source, unit_dir / source.name, symlinks=True)
        for path in (("/etc/nginx", "/etc/letsencrypt", "/etc/qiqc-operations", "/opt/qiqc-operations", "/usr/local/lib/qiqc-ops") if include_host else []):
            source = Path(path)
            if source.is_dir() and not source.is_symlink():
                shutil.copytree(source, stage / "host" / path.lstrip("/"), symlinks=True)
        files = []
        for path in sorted(stage.rglob("*")):
            info = path.lstat()
            if stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
                item = {"path": str(path.relative_to(stage)), "uid": info.st_uid, "gid": info.st_gid,
                        "mode": stat.S_IMODE(info.st_mode)}
                item.update({"link": os.readlink(path)} if path.is_symlink() else {"sha256": digest(path)})
                # Ownership is preserved separately: copied files are held root-private.
                original = root / path.relative_to(stage)
                relative = path.relative_to(stage)
                if relative.parts[0] == "host":
                    original = Path("/").joinpath(*relative.parts[1:])
                elif relative.parts[0] == "systemd":
                    original = Path("/etc/systemd/system").joinpath(*relative.parts[1:])
                if original.exists() or original.is_symlink():
                    original_info = original.lstat()
                    item.update(uid=original_info.st_uid, gid=original_info.st_gid)
                files.append(item)
        directories = []
        for path in sorted(stage.rglob("*")):
            if path.is_dir() and not path.is_symlink():
                info = path.stat()
                original = root / path.relative_to(stage)
                relative = path.relative_to(stage)
                if relative.parts[0] == "host":
                    original = Path("/").joinpath(*relative.parts[1:])
                elif relative.parts[0] == "systemd":
                    original = Path("/etc/systemd/system").joinpath(*relative.parts[1:])
                if original.is_dir():
                    info = original.stat()
                directories.append({"path": str(path.relative_to(stage)), "uid": info.st_uid, "gid": info.st_gid,
                                    "mode": stat.S_IMODE(info.st_mode)})
        manifest = {"schema": 1, "created_at": time.time(), "root": str(root), "files": files, "directories": directories,
                    "verified": False, "offsite": False, "encrypted": False,
                    "scope": "consistent database, referenced ZIPs, trusted deployment, private configuration and systemd units"}
        write_json(stage / "manifest.json", manifest)
        result = verify_snapshot(stage)
        manifest.update(verified=True, restore_test=result)
        write_json(stage / "manifest.json", manifest)
        final = backup_root / stage.name.removeprefix(".pending-")
        os.rename(stage, final)
        # Only remove owned, complete snapshots, after successfully verifying the new one.
        complete = []
        for path in backup_root.glob("snapshot-*"):
            try:
                if not path.is_symlink() and json.loads((path / "manifest.json").read_text()).get("verified") is True:
                    complete.append(path)
            except (OSError, ValueError):
                pass
        for path in sorted(complete)[:-retention]:
            shutil.rmtree(path)
        return {"snapshot": str(final), "verified": True, "restore_test": result, "offsite": False}
    except Exception:
        shutil.rmtree(stage)
        raise


def dashboard_projection(result, state_dir, reboot_path):
    """Only explicitly allowlisted health facts cross into the API-readable file."""
    checks = {k: v for k, v in result.get("checks", {}).items()
              if re.fullmatch(r"[a-zA-Z0-9_.:-]{1,100}", k) and v in ("ok", "warning", "critical", "unavailable")}
    output = {"sampled_at": result["sampled_at"], "status": result["status"],
              "checks": checks, "external_delivery": False, "backup": {}, "reboot": {}}
    try:
        backup = json.loads((state_dir / "last-backup.json").read_text())
        snapshot = Path(backup["snapshot"])
        manifest = json.loads((snapshot / "manifest.json").read_text())
        restored = backup.get("restore_test", {})
        output["backup"] = {"verified": backup.get("verified") is True,
          "created_at": manifest.get("created_at", 0), "offsite": False,
          "files_checked": restored.get("files_checked", 0),
          "zips_checked": restored.get("referenced_unique_zips", 0)}
    except (OSError, ValueError, KeyError, TypeError):
        pass
    try:
        reboot = json.loads(reboot_path.read_text())
        output["reboot"] = {"passed": reboot.get("passed") is True, "checked_at": reboot.get("checked_at", 0)}
    except (OSError, ValueError, TypeError):
        pass
    return output


def publish_dashboard(result, state_dir):
    directory = Path("/run/qiqc-status")
    directory.mkdir(exist_ok=True, mode=0o755)
    directory.chmod(0o755)
    path = directory / "health.json"
    write_json(path, dashboard_projection(result, state_dir, Path("/var/lib/qiqc-ops/reboot/after.json")))
    path.chmod(0o644)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("health", "backup", "verify"))
    parser.add_argument("--root", type=Path, default=Path("/opt/qiqc-native-candidate"))
    parser.add_argument("--state-dir", type=Path, default=Path("/var/lib/qiqc-operations"))
    parser.add_argument("--backup-root", type=Path, default=Path("/var/backups/qiqc-native"))
    parser.add_argument("--snapshot", type=Path)
    args = parser.parse_args()
    os.umask(0o077)
    args.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (args.state_dir / (args.action + ".lock")).open("w") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        if args.action == "health":
            result = health(args.root, args.state_dir, args.backup_root)
            publish_dashboard(result, args.state_dir)
        elif args.action == "backup":
            result = backup(args.root, args.backup_root)
            write_json(args.state_dir / "last-backup.json", result)
        else:
            if not args.snapshot:
                parser.error("verify requires --snapshot")
            result = verify_snapshot(args.snapshot)
        print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
