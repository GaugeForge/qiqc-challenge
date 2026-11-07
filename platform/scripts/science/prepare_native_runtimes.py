#!/usr/bin/env python3
"""Build pinned DSH/Pi bundles inside E2B containers before any task session.

Does not alter original task images. A new E2B snapshot retains the bundles;
the digest manifest is the only local build artifact. No provider credentials
or private task mounts enter the dependency containers.
"""

import argparse
import hashlib
import json
import shlex
import time
from pathlib import Path

from e2b import Sandbox
from e2b_runtime import ROOT, load_key
from native_runtime_bundles import PACKAGES, SCHEMA

NODE_SHA = "f52ec50e959d72d5c680d9731420b2661cd2a8070e94c7369b6ddfcd8b7278be"
NODE_URL = "https://nodejs.org/dist/v24.9.0/node-v24.9.0-linux-x64.tar.xz"

INSTALL = r'''set -eu
mkdir -p /build/node
curl --fail --silent --show-error --location --max-time 120 NODE_URL -o /build/node.tar.xz
echo 'NODE_SHA  /build/node.tar.xz' | sha256sum -c - >/dev/null
tar --no-same-owner -xJf /build/node.tar.xz --strip-components=1 -C /build/node
rm /build/node.tar.xz
export PATH=/build/node/bin:$PATH
export npm_config_cache=/tmp/npm-cache npm_config_update_notifier=false
test "$(node --version)" = v24.9.0
npm ci --prefix /build --ignore-scripts --legacy-peer-deps --no-audit --no-fund
test "$(/build/node_modules/.bin/CLI --version | tail -n 1)" = VERSION
tar -czf /build/bundle.tar.gz -C /build node node_modules
'''.replace("NODE_URL", NODE_URL).replace("NODE_SHA", NODE_SHA)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--template", required=True)
    p.add_argument("--report", type=Path, required=True)
    p.add_argument("--manifest", type=Path, required=True)
    args = p.parse_args()
    if args.report.exists() or args.manifest.exists():
        p.error("Use new report and manifest paths")
    args.report.parent.mkdir(parents=True, exist_ok=True)
    report = {"schema": "qiqc-native-runtime-build/v1", "passed": False,
              "source_template": args.template, "started_at": time.time(),
              "science_executed": False, "packages": {}}
    key = load_key()
    sandbox = None

    def save():
        args.report.write_text(json.dumps(report, indent=2, default=str) + "\n")

    def run(command, timeout=60):
        result = sandbox.commands.run(command, user="root", timeout=timeout)
        if result.exit_code:
            raise RuntimeError("runtime_preparation_command_failed")
        return result.stdout.strip()

    try:
        sandbox = Sandbox.create(template=args.template, api_key=key, timeout=900,
                                 network={"allow_public_traffic": False},
                                 metadata={"purpose": "qiqc-pinned-runtime-build"})
        report["sandbox_id"] = sandbox.sandbox_id
        save()
        run("docker info >/dev/null && mkdir -p /opt/qiqc-agent-runtimes /tmp/qiqc-runtime-build")
        base = run("docker image inspect qiqcbench-agent:latest --format '{{.Id}}'")
        report["original_agent_image"] = base
        for kind, (package, version) in PACKAGES.items():
            label = "deepseek" if kind == "deepseek-harness" else "pi"
            cli = "dsh" if label == "deepseek" else "pi"
            lock = ROOT / "benchmark-assets/adapters/science-probe/locks" / (label + "-package-lock.json")
            raw = lock.read_bytes()
            data = json.loads(raw)
            if data["packages"]["node_modules/" + package]["version"] != version:
                raise ValueError("native_runtime_lock_version_mismatch")
            directory = "/tmp/qiqc-runtime-build/" + kind
            run("mkdir -p " + directory)
            sandbox.files.write(directory + "/package-lock.json", raw, user="root")
            sandbox.files.write(directory + "/package.json", json.dumps({
                "name": "qiqc-pinned-runtime", "version": "1.0.0",
                "dependencies": data["packages"][""]["dependencies"]}), user="root")
            sandbox.files.write(directory + "/install.sh",
                                INSTALL.replace("CLI", cli).replace("VERSION", version), user="root")
            # The only bind contains package manifests, never source/truth or keys.
            run("docker run --rm --name qiqc-build-" + kind +
                " --cap-drop ALL --security-opt no-new-privileges --memory 2g --cpus 2" +
                " --mount type=bind,src=" + directory + ",dst=/build " +
                shlex.quote(base) + " sh /build/install.sh >" + directory + "/install.log 2>&1", 420)
            target = "/opt/qiqc-agent-runtimes/" + kind + ".tar.gz"
            run("mv " + directory + "/bundle.tar.gz " + target)
            sha = run("sha256sum " + target).split()[0]
            size = int(run("stat -c %s " + target))
            report["packages"][kind] = {
                "package": package, "version": version, "node_version": "v24.9.0",
                "lock_sha256": hashlib.sha256(raw).hexdigest(), "sha256": sha, "bytes": size,
            }
            save()
            print(json.dumps({"runtime": kind, "prepared": True}), flush=True)
        if run("docker image inspect qiqcbench-agent:latest --format '{{.Id}}'") != base:
            raise ValueError("native_original_image_changed")
        run("rm -rf /tmp/qiqc-runtime-build && sync")
        snapshot = sandbox.create_snapshot(name="qiqc-cli-runtimes-20261021", mode="filesystem")
        report["snapshot"] = vars(snapshot)
        report["passed"] = True
        args.manifest.parent.mkdir(parents=True, exist_ok=True)
        args.manifest.write_text(json.dumps({"schema": SCHEMA, "packages": report["packages"]}, indent=2) + "\n")
    except Exception as error:
        report["error_class"] = type(error).__name__
        # Dependency logs may be helpful, but never send arbitrary output to users.
        if sandbox:
            try:
                for kind in PACKAGES:
                    log = sandbox.files.read("/tmp/qiqc-runtime-build/" + kind + "/install.log", user="root")
                    (args.report.parent / (args.report.stem + "-" + kind + ".log")).write_text(log.replace(key, "[REDACTED]"))
            except Exception:
                pass
    finally:
        if sandbox:
            try:
                sandbox.kill()
                report["sandbox_destroyed"] = True
            except Exception as error:
                report["cleanup_error_class"] = type(error).__name__
                report["passed"] = False
        report["finished_at"] = time.time()
        save()
    print(json.dumps({"passed": report["passed"], "report": str(args.report)}))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
