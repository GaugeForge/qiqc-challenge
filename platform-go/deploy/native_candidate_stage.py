#!/usr/bin/env python3
"""Install/start only isolated local native candidate services; no cloud job.

Separate users, group, DB, scanner socket and ports. Never stops or changes any
qiqc-stage service. Explicit invocation; passwords stay in a root-only file.
"""

from __future__ import annotations

import argparse
import grp
import hashlib
import json
import os
import pwd
import secrets
import shutil
import sqlite3
import stat
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
DEST = Path("/opt/qiqc-native-candidate")
PROJECT = DEST / "project"
DATA = DEST / "data"
CONF = DEST / "config"
GROUP = "qiqc-native-data"
SCIENCE = ROOT / "competition-platform/platform/scripts/science"
sys.path.insert(0, str(SCIENCE))
from native_candidate import (commands, load_candidate, model_id, upstream_ip_family,
                              agent_budget_seconds, candidate_id_for_audit, IDENTITY_SCHEMA)


def command(argv, **kwargs):
    return subprocess.run(argv, check=True, **kwargs)


def private_copy(source, target, user):
    shutil.copyfile(source, target)
    os.chown(target, pwd.getpwnam(user).pw_uid, 0)
    target.chmod(0o600)


def source_audit_denied_paths(root, current):
    """Every retained audit contains private task metadata, not just the active one."""
    root, current = Path(root), Path(current)
    if current.parent != root:
        raise ValueError("Source audit must be inside candidate root")
    paths = set(root.glob("source-audit*.json")) | {current}
    return " ".join("-" + str(path) for path in sorted(paths))


def source_transition(args):
    """Validate both identities before modifying active files or services."""
    previous = None
    candidate_path = DEST / "candidate.json"
    if candidate_path.exists():
        previous, _ = load_candidate(candidate_path, verify_controller=False)
    audit_path = getattr(args, "source_audit", None)
    if audit_path is None:
        audit_path = Path(previous["source_audit"]) if previous else ROOT / "benchmark-assets/benchmark-releases/restored-source-audit-20261020.json"
    audit_path = Path(audit_path)
    if not audit_path.is_absolute() or audit_path.is_symlink() or not audit_path.is_file():
        raise SystemExit("Source audit must be an absolute regular file")
    raw = audit_path.read_bytes()
    audit = json.loads(raw)
    digest = hashlib.sha256(raw).hexdigest()
    unchanged = previous is not None and digest == previous["source_audit_sha256"]
    scheme = previous.get("candidate_identity_schema") if unchanged else IDENTITY_SCHEMA
    candidate_id = candidate_id_for_audit(audit, digest, scheme)
    changed = previous is not None and candidate_id != previous["candidate_id"]
    if changed and not getattr(args, "replace_drained_candidate_roster", False):
        raise SystemExit("Candidate task selection change requires explicit --replace-drained-candidate-roster")
    if changed and audit["source_files_sha256"] != previous["source_files_sha256"]:
        raise SystemExit("Roster migration requires identical source files; deploy changed source separately")
    if (DEST / "active-release.json").exists():
        raise SystemExit("Frozen active runtime cannot be refreshed in place; prepare a new candidate")
    from verify_restored_benchmark import verify
    source = DEST / "source"
    verify(source if source.exists() else ROOT / "benchmark-assets/qiqcbench-restored", audit)
    catalog_path = getattr(args, "science_task_set", None)
    if changed and not catalog_path:
        raise SystemExit("Candidate roster migration requires matching scientific task set")
    if catalog_path:
        catalog = json.loads(Path(catalog_path).read_bytes())
        if (catalog["release_id"] != candidate_id or
                [r["task_id"] for r in catalog["tasks"]] != sorted(audit["competition_task_ids"])):
            raise SystemExit("Scientific task set must match selected source audit and candidate identity")
        if catalog.get('schema') == 'qiqc-scientific-task-set/v2':
            from task_catalog import upgrade_catalog
            native_source = source if source.exists() else ROOT / "benchmark-assets/qiqcbench-restored"
            if upgrade_catalog(catalog, native_source) != catalog:
                raise SystemExit("Scientific native limits must match the original task TOML")
    if previous and previous["template_id"] != args.template_id and not (
        getattr(args, "replace_template_before_first_run", False) or
        getattr(args, "replace_drained_candidate_template", False)
    ):
        raise SystemExit("Template replacement requires explicit option")
    return {"previous": previous, "raw_audit": raw, "audit": audit,
            "audit_sha256": digest, "candidate_id": candidate_id,
            "identity_schema": scheme, "changed": changed}


def preflight(args):
    """Read-only validation before user/group, service or filesystem mutation.

    A profile can be valid for another adapter while unsupported by this
    installer, which always installs a Responses Codex and Chat reviewer.
    """
    for name, default in (('functional_slots', 1), ('model_max_concurrent', 49)):
        value = getattr(args, name, default)
        if type(value) is not int or not 1 <= value <= 10000:
            raise SystemExit('Explicit concurrency must be 1 through 10000; this is not a capacity certification')
    try:
        upstream_ip_family({"upstream_ip_family": getattr(args, "upstream_ip_family", "auto")})
    except ValueError:
        raise SystemExit("Invalid upstream IP family") from None
    if getattr(args, "codex_reasoning_effort", None) not in (None, "none"):
        raise SystemExit("Unsupported Codex diagnostic reasoning effort")
    agent_budget_seconds({"agent_budget_seconds": getattr(args, "agent_budget_seconds", 300)})
    for name in ("build_plan_sha256", "build_receipt_sha256"):
        supplied = getattr(args, name, None)
        if supplied and (len(supplied) != 64 or any(c not in "0123456789abcdef" for c in supplied)):
            raise SystemExit("Invalid build input digest")
    catalog_path = getattr(args, "science_task_set", None)
    if catalog_path:
        try:
            path = Path(catalog_path)
            info = path.lstat()
            if (not path.is_absolute() or not stat.S_ISREG(info.st_mode)
                    or info.st_size > 65536 or any(p.is_symlink() for p in path.parents)):
                raise ValueError("invalid catalog file")
            catalog = json.loads(path.read_bytes())
            tasks = catalog['tasks']
            if (catalog['schema'] not in ('qiqc-scientific-task-set/v1', 'qiqc-scientific-task-set/v2')
                    or catalog['agent_budget_seconds'] != getattr(args, 'agent_budget_seconds', 300)
                    or len(tasks) != 49 or [t['task_index'] for t in tasks] != list(range(49))
                    or any(type(t['public']) is not bool for t in tasks)
                    or not 1 <= sum(t['public'] for t in tasks) <= 5
                    or type(catalog['infra_task_index']) is not int
                    or not 0 <= catalog['infra_task_index'] < 49
                    or not tasks[catalog['infra_task_index']]['public']):
                raise ValueError("invalid catalog contract")
            if catalog['schema'] == 'qiqc-scientific-task-set/v2':
                if sum(t['public'] for t in tasks) != 5:
                    raise ValueError("native catalog requires five public tasks")
                roles = [t.get('evaluation_role', '') for t in tasks]
                # A role-bearing catalog is the current active contract.
                # Empty roles are retained only for historical v2
                # acceptance records and follow the legacy public subset
                # route; partial role metadata is never guessed.
                if any(roles):
                    if any(role not in ('binary', 'racing', 'infrastructure') for role in roles):
                        raise ValueError("native catalog requires an evaluation role on every task")
                    if {role: roles.count(role) for role in ('binary', 'racing', 'infrastructure')} != {
                        'binary': 39, 'racing': 7, 'infrastructure': 3
                    }:
                        raise ValueError("native catalog requires the 39/7/3 task partition")
                for task in tasks:
                    limits = task['limits']
                    if (any(type(limits[k]) is not int or not 0 < limits[k] <= 604800
                            for k in ('agent_seconds', 'setup_seconds', 'build_seconds', 'verifier_seconds'))
                            or any(type(limits[k]) is not int or limits[k] <= 0 for k in ('cpus', 'memory_mb'))
                            or not isinstance(limits['source_sha256'], str)
                            or len(limits['source_sha256']) != 64
                            or any(c not in '0123456789abcdef' for c in limits['source_sha256'])):
                        raise ValueError("invalid native limits")
        except (OSError, ValueError, KeyError, TypeError):
            raise SystemExit("Scientific task set must be a bounded regular catalog with 49 tasks and an explicit public infra task") from None
    for name in ('event_calendar', 'formal_execution'):
        value = getattr(args, name, None)
        if value is not None:
            path = Path(value)
            if (not path.is_absolute() or path.is_symlink() or not path.is_file()
                    or path.stat().st_size > 65536 or any(p.is_symlink() for p in path.parents)):
                raise SystemExit("Event configuration must be a bounded regular file")
            try:
                if not isinstance(json.loads(path.read_bytes()), dict):
                    raise ValueError()
            except (ValueError, OSError):
                raise SystemExit("Event configuration must be a JSON object") from None
    diagnostic = getattr(args, "diagnostic_model", "")
    if diagnostic not in ("", "minimax/minimax-m3.1-flash:free"):
        raise SystemExit("Unsupported diagnostic alias; select an organizer provider profile")
    for name, maximum in (("diagnostic_rpm", 20), ("diagnostic_daily_cap", 1000)):
        value = getattr(args, name, 10 if name == "diagnostic_rpm" else 50)
        if type(value) is not int or not 1 <= value <= maximum:
            raise SystemExit("Invalid diagnostic admission limit")
    if not getattr(args, "provider_profile", None):
        return None
    from native_provider_profile import load_profile

    profile, _ = load_profile(args.provider_profile)
    if diagnostic and (profile["model"] != diagnostic or profile["billing"] != "free"):
        raise SystemExit("Diagnostic alias does not match approved profile")
    required = {"responses", "chat/completions"}
    missing = required - set(profile["endpoints"])
    if missing:
        raise SystemExit(
            "Profile is valid, but this stage combines Codex Responses and a Chat reviewer; "
            "the current combination requires both responses and chat/completions"
        )
    # Validate the reference without copying, printing, retaining or using its key.
    key_path = Path(profile["key_file"])
    try:
        if (not key_path.is_absolute() or os.path.normpath(str(key_path)) != str(key_path)
                or any(part.is_symlink() for part in (key_path, *key_path.parents))):
            raise ValueError("invalid key path")
        before = key_path.stat()
        if not stat.S_ISREG(before.st_mode) or before.st_mode & 0o037 or before.st_size > 1024:
            raise ValueError("invalid key permissions or size")
        fd = os.open(key_path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, "rb") as source:
            current = os.fstat(source.fileno())
            if (before.st_dev, before.st_ino) != (current.st_dev, current.st_ino):
                raise ValueError("key reference changed")
            raw = source.read(1025)
        key = raw.decode("utf-8").strip()
        if len(raw) > 1024 or not 12 <= len(key) <= 512 or "\r" in key or "\n" in key:
            raise ValueError("invalid bearer key format")
    except (OSError, ValueError, UnicodeError):
        raise SystemExit("Provider key reference must be an existing private regular file containing one valid bearer key") from None
    return profile


def prepare(args):
    profile_input = preflight(args)
    transition = source_transition(args)
    if os.geteuid() != 0:
        raise SystemExit("Local installer needs root")
    for group in (GROUP, "qiqc-native-science", "qiqc-native-model"):
        try:
            grp.getgrnam(group)
        except KeyError:
            command(["groupadd", "--system", group])
    for role in ("api", "worker", "gateway", "review"):
        user = "qiqc-native-" + role
        try:
            pwd.getpwnam(user)
        except KeyError:
            command(
                [
                    "useradd",
                    "--system",
                    "--no-create-home",
                    "--gid",
                    GROUP,
                    "--shell",
                    "/usr/sbin/nologin",
                    user,
                ]
            )
    command(["usermod", "-a", "-G", "qiqc-native-science", "qiqc-native-worker"])
    command(["usermod", "-a", "-G", "qiqc-native-science", "qiqc-native-api"])
    for role in ("gateway", "review"):
        command(["usermod", "-a", "-G", "qiqc-native-model", "qiqc-native-" + role])
    DEST.mkdir(exist_ok=True)
    DEST.chmod(0o755)
    if (DEST / "active-release.json").exists():
        raise SystemExit(
            "Frozen active runtime cannot be refreshed in place; prepare a new candidate"
        )
    CONF.mkdir(exist_ok=True)
    CONF.chmod(0o755)
    if (DATA / "platform.db").exists():
        with sqlite3.connect(
            (DATA / "platform.db").as_uri() + "?mode=ro", uri=True
        ) as db:
            if db.execute(
                "SELECT count(*) FROM runs WHERE state IN ('queued','running','uploading')"
            ).fetchone()[0]:
                raise SystemExit("Candidate has active work")
    if (DATA / "platform.db").exists():
        command(["systemctl", "stop", "qiqc-native-api.service"])
        with sqlite3.connect(
            (DATA / "platform.db").as_uri() + "?mode=ro", uri=True
        ) as db:
            active = db.execute(
                "SELECT count(*) FROM runs WHERE state IN ('queued','running','uploading') OR (state='attention' AND error='worker_lost:reconciliation_required')"
            ).fetchone()[0]
        if active:
            command(["systemctl", "start", "qiqc-native-api.service"])
            raise SystemExit(
                "Candidate work needs completion or reconciliation before controller refresh"
            )
        worker_units = [
            unit for unit in ("qiqc-native-worker-key2.service", "qiqc-native-worker-key1.service")
            if subprocess.run(["systemctl", "cat", unit], stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL, check=False).returncode == 0
        ]
        legacy = "qiqc-native-worker.service"
        if subprocess.run(["systemctl", "cat", legacy], stdout=subprocess.DEVNULL,
                          stderr=subprocess.DEVNULL, check=False).returncode == 0:
            worker_units.append(legacy)
        command(["systemctl", "stop", *worker_units, "qiqc-native-review.service", "qiqc-native-gateway.service"])
    # Deployment source copy is an active candidate input, not a historical backup.
    source = DEST / "source"
    if not source.exists():
        shutil.copytree(ROOT / "benchmark-assets/qiqcbench-restored", source, symlinks=True)
    os.chown(source, 0, grp.getgrnam("qiqc-native-science").gr_gid)
    source.chmod(0o750)
    for directory in (
        "competition-platform/platform/scripts/science",
        "benchmark-assets/adapters/science-probe",
        "competition-platform/platform/qiqc",
        "competition-platform/platform/web",
        "competition-platform/adapters",
        "competition-platform/dist",
    ):
        shutil.copytree(
            ROOT / directory,
            PROJECT / directory,
            dirs_exist_ok=True,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
        )
    # Readback binds imports to this deployed directory. Keep its ordinary
    # browser audit helper alongside the strict checker, never an old snapshot.
    scripts = Path("competition-platform/platform/scripts")
    for name in ("ordinary_account_browser_check.py", "ordinary_scientific_readback.py",
                 "ordinary_result_browser_read.py", "browser_credentials.py",
                 "browser_candidate_runtime.py", "browser_upload.py", "cloud_browser_check.py"):
        target = PROJECT / scripts / name
        shutil.copyfile(ROOT / scripts / name, target)
        os.chown(target, 0, 0)
        target.chmod(0o644)
    (PROJECT / "benchmark-assets").mkdir(parents=True, exist_ok=True)
    shutil.copyfile(ROOT / "benchmark-assets/baseline-mock.json", PROJECT / "benchmark-assets/baseline-mock.json")
    # Retain an external audit file; source checkout itself remains untouched.
    audit = DEST / ("source-audit-" + transition["audit_sha256"] + ".json")
    if audit.exists() and audit.read_bytes() != transition["raw_audit"]:
        raise SystemExit("Immutable source audit changed")
    if not audit.exists():
        with audit.open("xb") as stream:
            stream.write(transition["raw_audit"])
    os.chown(audit, 0, grp.getgrnam("qiqc-native-science").gr_gid)
    audit.chmod(0o640)
    # Retained audit bytes remain evidence, but no live service needs to read
    # their private task metadata after the selection has been replaced.
    for retained in DEST.glob("source-audit*.json"):
        if retained != audit:
            if retained.is_symlink() or not retained.is_file():
                raise SystemExit("Retained source audit must be a regular file")
            os.chown(retained, 0, 0)
            retained.chmod(0o400)
    from native_compose_template import archive_bytes

    adapters = DEST / "adapters"
    (adapters / "codex").mkdir(parents=True, exist_ok=True)
    data = archive_bytes(sustained=args.sustained_codex,
                         reasoning_effort=getattr(args, "codex_reasoning_effort", None),
                         agent_budget_seconds=getattr(args, "agent_budget_seconds", 300))
    (adapters / "codex/harness.zip").write_bytes(data)
    shutil.copyfile(
        ROOT / "competition-platform/adapters/reference.zip", adapters / "reference.zip"
    )
    manifest = DEST / "science-submissions.json"
    manifest.write_text(
        json.dumps({"codex": {"sha256": hashlib.sha256(data).hexdigest()}}) + "\n"
    )
    if getattr(args, "science_task_set", None) and json.loads(Path(args.science_task_set).read_bytes()).get('schema') == 'qiqc-scientific-task-set/v2':
        # Native v2 publishes the three actual starters, never the legacy
        # diagnostic sustained-Codex ZIP under a new label.
        starters = json.loads((ROOT / 'competition-platform/adapters/manifest.json').read_text())
        entries = {}
        for name in ('codex', 'deepseek', 'pi'):
            original = ROOT / 'competition-platform/adapters' / name / 'harness.zip'
            row = starters['packages'][name]
            if hashlib.sha256(original.read_bytes()).hexdigest() != row['sha256']:
                raise SystemExit('Starter archive changed since manifest generation')
            target = adapters / name / 'harness.zip'
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(original, target)
            entries[row.get('derived', row['manifest'])['adapter']] = {'sha256': row['sha256']}
        manifest.write_text(json.dumps(entries) + '\n')
    for file, user in [
        ("api-r2.env", "api"),
        ("worker-r2.env", "worker"),
        ("provider.env", "gateway"),
        ("review.key", "review"),
        ("worker-e2b.env", "worker"),
    ]:
        private_copy(
            Path("/etc/qiqc-staging") / file, CONF / file, "qiqc-native-" + user
        )
    DATA.mkdir(exist_ok=True)
    os.chown(DATA, pwd.getpwnam("qiqc-native-api").pw_uid, grp.getgrnam(GROUP).gr_gid)
    DATA.chmod(0o2770)
    for sub in ("uploads", "quarantine", "objects", "cache", "jobs"):
        p = DATA / sub
        p.mkdir(exist_ok=True)
        os.chown(p, pwd.getpwnam("qiqc-native-api").pw_uid, grp.getgrnam(GROUP).gr_gid)
        p.chmod(0o2770)
    candidate = DEST / "candidate.json"
    if not candidate.exists():
        command(
            [
                sys.executable,
                str(SCIENCE / "native_candidate.py"),
                "prepare",
                "--source-root",
                str(source),
                "--source-audit",
                str(audit),
                "--candidate-root",
                str(DEST),
                "--project-root",
                str(PROJECT),
                "--python",
                "/opt/qiqc-staging/venv/bin/python",
                "--adapter-manifest",
                str(manifest),
                "--adapters",
                str(adapters),
                "--provider-env",
                str(CONF / "provider.env"),
                "--r2-env",
                str(CONF / "api-r2.env"),
                "--review-key",
                str(CONF / "review.key"),
                "--scanner-socket",
                "/run/qiqc-native-scanner/scanner.sock",
                "--template-id",
                args.template_id,
                "--output",
                str(candidate),
            ]
        )
    # The old config is read before its inputs change; audit files are immutable.
    config = dict(transition["previous"]) if transition["previous"] else load_candidate(candidate, verify_controller=False)[0]
    config.update(source_audit=str(audit), source_audit_sha256=transition["audit_sha256"],
                  candidate_id=transition["candidate_id"],
                  source_files_sha256=transition["audit"]["source_files_sha256"],
                  source_commit=transition["audit"]["source_commit"])
    if transition["identity_schema"] is not None:
        config["candidate_identity_schema"] = transition["identity_schema"]
    config["python"] = "/opt/qiqc-staging/venv/bin/python"
    for name in ('event_calendar', 'formal_execution'):
        supplied = getattr(args, name, None)
        if supplied is not None:
            destination = CONF / (name.replace('_', '-') + '.json')
            destination.write_bytes(Path(supplied).read_bytes())
            os.chown(destination, 0, grp.getgrnam(GROUP).gr_gid)
            destination.chmod(0o640)
            config[name + '_path'] = str(destination)
    if getattr(args, "science_task_set", None):
        raw_catalog = Path(args.science_task_set).read_bytes()
        catalog = json.loads(raw_catalog)
        if (catalog.get("release_id") != config["candidate_id"]
            or catalog.get("agent_budget_seconds") != getattr(args, "agent_budget_seconds", 300)
            or [r.get("task_id") for r in catalog.get("tasks", [])] != sorted(json.loads(audit.read_text())["competition_task_ids"])):
            raise SystemExit("Scientific task set must match original candidate and budget")
        task_set = CONF / "science-task-set.json"
        task_set.write_bytes(raw_catalog)
        os.chown(task_set, 0, grp.getgrnam(GROUP).gr_gid)
        task_set.chmod(0o640)
        config["science_task_set_path"] = str(task_set)
    else:
        config.pop("science_task_set_path", None)
    config["functional_slots"] = args.functional_slots
    config['model_max_concurrent'] = getattr(args, 'model_max_concurrent', 49)
    config["agent_budget_seconds"] = getattr(args, "agent_budget_seconds", 300)
    config["upstream_ip_family"] = getattr(args, "upstream_ip_family", "auto")
    config["diagnostic_model"] = args.diagnostic_model
    config["diagnostic_rpm"] = args.diagnostic_rpm
    config["diagnostic_daily_cap"] = args.diagnostic_daily_cap
    if profile_input is not None:
        from native_provider_profile import canonical, profile_identity
        key_path = CONF / "profile.key"
        private_copy(Path(profile_input["key_file"]), key_path, "qiqc-native-gateway")
        # Gateway and reviewer share the same quota identity and read-only key.
        os.chown(key_path, 0, grp.getgrnam("qiqc-native-model").gr_gid)
        key_path.chmod(0o640)
        deployed_profile = {**profile_input, "key_file": str(key_path)}
        profile_path = CONF / "provider-profile.json"
        profile_path.write_bytes(canonical(deployed_profile)+b"\n")
        os.chown(profile_path, 0, grp.getgrnam(GROUP).gr_gid)
        profile_path.chmod(0o640)
        config["provider_profile_path"] = str(profile_path)
        config["provider_profile"] = profile_identity(deployed_profile)
        config["diagnostic_rpm"] = profile_input["requests_per_minute"]
        config["diagnostic_daily_cap"] = profile_input["requests_per_day"]
    else:
        config.pop("provider_profile_path", None)
        config.pop("provider_profile", None)
    config["account_continuous_limit_seconds"] = args.account_continuous_limit_seconds
    config["diagnostic_continuous_limit_seconds"] = 3600
    config["runtime_capacity"] = {
        "cpus": args.runtime_cpus,
        "memory_mb": args.runtime_memory_mb,
    }
    if config["template_id"] != args.template_id:
        if not (
            args.replace_template_before_first_run
            or args.replace_drained_candidate_template
        ):
            raise SystemExit(
                "Template replacement needs explicit before-first-run option"
            )
        with sqlite3.connect(
            (DATA / "platform.db").as_uri() + "?mode=ro", uri=True
        ) as db:
            if (
                db.execute("SELECT count(*) FROM runs").fetchone()[0]
                and not args.replace_drained_candidate_template
            ):
                raise SystemExit(
                    "Candidate has run evidence; create a new candidate deployment"
                )
        config["template_id"] = args.template_id
    os.chown(candidate, 0, grp.getgrnam("qiqc-native-science").gr_gid)
    candidate.chmod(0o640)
    bins = Path(config["binaries"])
    bins.mkdir(exist_ok=True)
    for name, package in [
        ("api", "."),
        ("worker", "./cmd/qiqc-worker"),
        ("gateway", "./cmd/qiqc-model-gateway"),
        ("admin", "./cmd/qiqc-admin"),
        ("native-release-activate", "./cmd/native-release-activate"),
        ("science-reconcile", "./cmd/science-reconcile"),
        ("review", "./cmd/review-worker"),
    ]:
        target = bins / (name + ".next")
        command(
            ["go", "build", "-trimpath", "-o", str(target), package],
            cwd=ROOT / "competition-platform/platform-go",
        )
        target.replace(bins / name)
    credentials = Path(config["credentials"])
    if not credentials.exists():
        admin = {
            "email": "admin@native-candidate.local",
            "password": secrets.token_urlsafe(28),
            "name": "Native candidate operator",
        }
        env = dict(os.environ, QIQC_SHARED_DATA="1")
        command(
            commands(config)["bootstrap"],
            input=json.dumps(admin).encode(),
            stdout=subprocess.DEVNULL,
            env=env,
        )
        credentials.write_text(
            json.dumps(
                {
                    "admin": {"email": admin["email"], "password": admin["password"]},
                    "teams": [],
                },
                indent=2,
            )
            + "\n"
        )
        credentials.chmod(0o600)
        for name in ("platform.db", "platform.db-wal", "platform.db-shm"):
            p = DATA / name
            if p.exists():
                os.chown(
                    p,
                    pwd.getpwnam("qiqc-native-api").pw_uid,
                    grp.getgrnam(GROUP).gr_gid,
                )
                p.chmod(0o660)
    scanner = DEST / "scanner_helper.py"
    shutil.copyfile(Path(__file__).with_name("scanner_helper.py"), scanner)
    from native_controller import digest, inventory

    controller = inventory(config["project_root"], config["binaries"],
                           upstream_ip_family=upstream_ip_family(config),
                           agent_budget_seconds=agent_budget_seconds(config),
                           model_max_concurrent=config['model_max_concurrent'])
    control_path = DEST / "controller-manifest.json"
    control_path.write_text(json.dumps(controller, indent=2) + "\n")
    os.chown(control_path, 0, grp.getgrnam("qiqc-native-science").gr_gid)
    control_path.chmod(0o640)
    config["controller_manifest"] = str(control_path)
    config["controller_sha256"] = digest(controller)
    for name in ("build_plan_sha256", "build_receipt_sha256"):
        supplied = getattr(args, name, None)
        if supplied:
            if len(supplied) != 64 or any(
                c not in "0123456789abcdef" for c in supplied
            ):
                raise SystemExit("Invalid build input digest")
            config[name] = supplied
    temporary = candidate.with_suffix(".next")
    temporary.write_text(json.dumps(config, indent=2) + "\n")
    os.chown(temporary, 0, grp.getgrnam("qiqc-native-science").gr_gid)
    temporary.chmod(0o640)
    load_candidate(temporary)
    if transition["changed"]:
        command([str(bins / "native-release-activate"), "-data", str(DATA),
                 "-migrate-candidate", "-candidate-id", config["candidate_id"],
                 "-previous-candidate-id", transition["previous"]["candidate_id"],
                 "-actor", "native_candidate_stage",
                 "-reason", "Explicit source-audit roster migration; historical targets retained"],
                env=dict(os.environ, QIQC_SHARED_DATA="1"))
    temporary.replace(candidate)
    load_candidate(candidate)
    # Preserve the venv entry path: resolving its symlink loses site-packages.
    command(
        [
            "runuser",
            "-u",
            "qiqc-native-worker",
            "--",
            "env",
            "PYTHONDONTWRITEBYTECODE=1",
            "QIQC_PROJECT_ROOT=" + str(PROJECT),
            "GIT_CONFIG_COUNT=1",
            "GIT_CONFIG_KEY_0=safe.directory",
            "GIT_CONFIG_VALUE_0=" + str(source),
            config["python"],
            "-c",
            "import sys;sys.path.insert(0,sys.argv[1]);import native_worker_bridge;native_worker_bridge.load_candidate(sys.argv[2])",
            str(PROJECT / "benchmark-assets/adapters/science-probe"),
            str(candidate),
        ]
    )
    parser = PROJECT / "competition-platform/platform/qiqc/archive.py"
    image = subprocess.check_output(
        ["docker", "image", "inspect", "python:3.13-slim", "--format", "{{.Id}}"],
        text=True,
    ).strip()
    hardening = """NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectControlGroups=true
RestrictSUIDSGID=true
RestrictRealtime=true
LockPersonality=true
RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6
CapabilityBoundingSet=
AmbientCapabilities=
UMask=0007
Restart=on-failure
RestartSec=2
TimeoutStopSec=480
"""
    units = {}
    worker_launch = commands(config)["worker"]
    worker_units = (("worker-key2", 20, "key2"), ("worker-key1", 100, "key1"))
    for role in ("api", "worker-key2", "worker-key1", "gateway", "review"):
        base_role = "worker" if role.startswith("worker-") else role
        launch = list(worker_launch if base_role == "worker" else commands(config)[base_role])
        if base_role == "worker":
            slots = dict((name, value) for name, value, _ in worker_units)[role]
            slot_index = launch.index("-slots") + 1
            launch[slot_index] = str(slots)
        environment = f"Environment=QIQC_SHARED_DATA=1\nEnvironment=QIQC_MODEL={model_id(config)}\nEnvironment=QIQC_BASELINE={PROJECT}/benchmark-assets/baseline-mock.json\nEnvironment=PYTHONDONTWRITEBYTECODE=1\n"
        denied = "-/run/docker.sock -/var/run/docker.sock -/var/lib/qiqc-staging/data -/etc/qiqc-staging -/opt/qiqc-staging/science"
        if base_role in ("api", "worker"):
            environment += (
                f"Environment=QIQC_NATIVE_RELEASE_POINTER={DEST}/active-release.json\n"
            )
            environment += f"Environment=QIQC_R2_ENABLED=1\nEnvironment=QIQC_R2_ENV={CONF}/{base_role}-r2.env\n"
        if base_role == "api":
            environment += "SupplementaryGroups=qiqc-native-science\nEnvironment=QIQC_SCANNER_SOCKET=/run/qiqc-native-scanner/scanner.sock\n"
        if base_role == "worker":
            pool = "key2" if role == "worker-key2" else "key1"
            environment += f"Environment=GIT_CONFIG_COUNT=1\nEnvironment=GIT_CONFIG_KEY_0=safe.directory\nEnvironment=GIT_CONFIG_VALUE_0={source}\nSupplementaryGroups=qiqc-native-science\nEnvironment=QIQC_NATIVE_CANDIDATE={candidate}\nEnvironment=QIQC_E2B_KEY_FILE={CONF}/worker-e2b.env\n"
            environment += f"Environment=QIQC_WORKER_POOL={pool}\n"
        else:
            denied += " -" + str(source) + " " + source_audit_denied_paths(DEST, audit) + " -" + str(candidate)
        if base_role in ("gateway", "review"):
            denied += " " + " ".join(
                "-" + str(DATA / p)
                for p in (
                    *(("uploads",) if role == "gateway" else ()),
                    "quarantine",
                    "cache",
                    "scientific",
                    "failure-evidence",
                    "objects",
                )
            )
        if base_role == "review":
            launch += ["-timeout", "120s"]
            environment += f"ReadOnlyPaths={DATA}/uploads\n"
        units["qiqc-native-" + role + ".service"] = f"""[Unit]
Description=QIQC independent native candidate {role}
After=network.target qiqc-native-scanner.service
[Service]
Type=simple
User={'qiqc-native-worker' if base_role == 'worker' else 'qiqc-native-' + base_role}
Group={GROUP}
WorkingDirectory={DEST}
{environment}ReadWritePaths={DATA}
InaccessiblePaths={denied}
ExecStart={" ".join(launch)}
{hardening}
"""
    units["qiqc-native-scanner.service"] = f"""[Unit]
Description=QIQC independent native candidate scanner
After=docker.service
Requires=docker.service
[Service]
Type=simple
User=root
Group={GROUP}
RuntimeDirectory=qiqc-native-scanner
RuntimeDirectoryMode=0750
StateDirectory=qiqc-native-scanner
StateDirectoryMode=0700
ExecStart=/usr/bin/python3 {scanner} --socket /run/qiqc-native-scanner/scanner.sock --quarantine {DATA}/quarantine --spool /var/lib/qiqc-native-scanner --parser {parser} --image {image} --api-user qiqc-native-api
ReadWritePaths=/run/qiqc-native-scanner /var/lib/qiqc-native-scanner
{hardening}
"""
    for name, body in units.items():
        body += "\n[Install]\nWantedBy=multi-user.target\n"
        Path("/etc/systemd/system", name).write_text(body)
    command(["systemctl", "daemon-reload"])
    command(["systemctl", "enable", *units])
    command(["systemctl", "restart", "qiqc-native-scanner.service"])
    command(
        [
            "systemctl",
            "restart",
            *[name for name in units if name != "qiqc-native-scanner.service"],
        ]
    )
    print(
        json.dumps(
            {
                "status": "candidate_local_services_started",
                "url": config["url"],
                "candidate_config": str(candidate),
                "frozen": False,
                "current_staging_changed": False,
                "cloud_task_started": False,
            }
        )
    )


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--template-id", required=True)
    p.add_argument("--provider-profile", type=Path, help="trusted organizer profile JSON; never a participant URL")
    p.add_argument("--upstream-ip-family", default="auto", help="auto, ipv4 or ipv6; no cross-family fallback")
    p.add_argument("--diagnostic-model", choices=("", "minimax/minimax-m3.1-flash:free"), default="")
    p.add_argument("--diagnostic-rpm", type=int, choices=range(1, 21), default=10)
    p.add_argument("--diagnostic-daily-cap", type=int, choices=range(1, 1001), default=50)
    p.add_argument("--science-task-set", type=Path, help="trusted automatic scientific catalog; never participant-supplied")
    p.add_argument("--event-calendar", type=Path, help="reviewed server calendar JSON; omitted retains existing calendar")
    p.add_argument("--formal-execution", type=Path, help="reviewed formal model configuration; no practice fallback")
    p.add_argument("--source-audit", type=Path, help="exact verified source and 49-task roster audit; omitted preserves current selection")
    p.add_argument("--replace-drained-candidate-roster", action="store_true",
                   help="explicit audited candidate identity migration; preserve all historical batches")
    p.add_argument("--agent-budget-seconds", type=int, choices=(180, 300), default=300)
    p.add_argument("--functional-slots", type=int, default=1,
                   help="configured task slots; not restricted to the number of benchmark tasks")
    p.add_argument('--model-max-concurrent', type=int, default=49,
                   help='independent model request cap; retained legacy test default, not formal capacity')
    p.add_argument("--codex-reasoning-effort", choices=("none",), default=None,
                   help="explicit organizer Harness CLI setting; omitted preserves original ZIP bytes")
    p.add_argument("--sustained-codex", action="store_true",
                   help="explicit five-minute test Harness: keep performing real task checks after successful Codex sessions")
    p.add_argument("--replace-template-before-first-run", action="store_true")
    p.add_argument(
        "--replace-drained-candidate-template",
        action="store_true",
        help="explicit new template/controller for unfrozen drained candidate; retain immutable prior evidence",
    )
    p.add_argument("--account-continuous-limit-seconds", type=int, default=86400)
    p.add_argument("--build-plan-sha256")
    p.add_argument("--build-receipt-sha256")
    p.add_argument(
        "--runtime-cpus", type=int, required=True, help="measured Docker host CPU count"
    )
    p.add_argument(
        "--runtime-memory-mb",
        type=int,
        required=True,
        help="measured Docker host MemTotal MiB",
    )
    args = p.parse_args()
    if args.runtime_cpus < 1 or args.runtime_memory_mb < 512:
        p.error("valid measured capacity required")
    prepare(args)
