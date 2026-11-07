#!/usr/bin/env python3
"""Build a disposable E2B Docker host for original Benchmark image builds.

Only the trusted host tools are installed here. Scientific Dockerfiles and their
dependency pins are not rewritten. No model/R2/admin credentials enter E2B.
"""
import argparse
import json
import time
from pathlib import Path

from e2b import Template
from e2b_runtime import load_key

INSTALL = r'''set -eu
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y --no-install-recommends ca-certificates curl gnupg git python3 python3-venv unzip
install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/debian/gpg -o /etc/apt/keyrings/docker.asc
chmod a+r /etc/apt/keyrings/docker.asc
. /etc/os-release
test "$ID" = debian
printf 'deb [arch=amd64 signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/debian %s stable\n' "$VERSION_CODENAME" >/etc/apt/sources.list.d/docker.list
apt-get update -qq
apt-get install -y --no-install-recommends docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
mkdir -p /var/log/qiqc-native /opt/qiqc-native
dpkg-query -W docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin >/opt/qiqc-native/host-packages.txt
docker compose version >/opt/qiqc-native/compose-version.txt
apt-get clean
rm -rf /var/lib/apt/lists/*
'''


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--alias', required=True)
    p.add_argument('--report', type=Path, required=True)
    p.add_argument('--cpu', type=int, default=4)
    p.add_argument('--memory-mb', type=int, default=8192)
    p.add_argument('--base-template')
    p.add_argument('--free-disk-mb', type=int, default=10240)
    args = p.parse_args()
    if args.report.exists():
        p.error('report exists')
    key = load_key()
    report = {'scope': 'native-compose-build-host-candidate', 'alias': args.alias,
              'cpu_count': args.cpu, 'memory_mb': args.memory_mb,
              'started_at': time.time(), 'scientific_images_built': False,
              'competition_release_frozen': False,'base_template':args.base_template,
              'min_free_disk_mb':args.free_disk_mb}
    try:
        template = (Template().from_template(args.base_template) if args.base_template
                    else Template().from_base_image().run_cmd(INSTALL, user='root'))
        template = template.set_start_cmd(
            "sudo sh -c 'if docker info >/dev/null 2>&1; then exec sleep infinity; "
            "else rm -f /var/run/docker.pid; exec dockerd --host=unix:///var/run/docker.sock "
            ">/tmp/qiqc-dockerd.log 2>&1; fi'",
            'sudo docker info >/dev/null 2>&1')
        def progress(entry):
            message = str(getattr(entry, 'message', entry)).replace(key, '[REDACTED]')
            # Only trusted installation/build log, never science data or secrets.
            print(message[:500], flush=True)
        result = Template.build(template, alias=args.alias, cpu_count=args.cpu,
                                memory_mb=args.memory_mb, min_free_disk_mb=args.free_disk_mb,
                                on_build_logs=progress, api_key=key)
        report['passed'] = True
        report['build_info'] = vars(result)
    except Exception as exc:
        report['passed'] = False
        report['error_type'] = type(exc).__name__
        report['error'] = str(exc).replace(key, '[REDACTED]')[:2000]
    report['finished_at'] = time.time()
    args.report.parent.mkdir(parents=True, exist_ok=True)
    with args.report.open('x') as f:
        json.dump(report, f, indent=2, default=str)
        f.write('\n')
    print(json.dumps({'passed': report['passed'], 'report': str(args.report)}), flush=True)
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
