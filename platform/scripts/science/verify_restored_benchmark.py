#!/usr/bin/env python3
"""Verify an unmodified, independent upstream checkout; never run science code.

The audit manifest lives outside the checkout. It records source equivalence,
not acceptance of built images, scientific execution or a competition release.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import subprocess
from pathlib import Path


def git(root, *args):
    env = dict(os.environ, GIT_OPTIONAL_LOCKS='0')
    return subprocess.check_output(['git', '-C', str(root), *args], env=env)


def inventory(root):
    root = root.resolve()
    files = {}
    for entry in git(root, 'ls-files', '--stage', '-z').split(b'\0'):
        if not entry:
            continue
        metadata, raw_name = entry.split(b'\t', 1)
        mode, blob, stage = metadata.decode().split()
        name = raw_name.decode()
        if stage != '0' or mode not in {'100644', '100755', '120000'}:
            raise ValueError('Unsupported source entry')
        path = root / name
        if path.is_symlink():
            if mode != '120000' or not path.resolve(strict=True).is_relative_to(root):
                raise ValueError('Source link escapes checkout')
            data = os.readlink(path).encode()
        else:
            info = path.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise ValueError('Source is not an independent regular file')
            actual_mode = '100755' if info.st_mode & 0o111 else '100644'
            if mode != actual_mode:
                raise ValueError('Source executable mode mismatch')
            data = path.read_bytes()
        if hashlib.sha1(b'blob ' + str(len(data)).encode() + b'\0' + data).hexdigest() != blob:
            raise ValueError('Worktree content differs from Git source')
        files[name] = {'git_mode': mode, 'git_blob': blob, 'bytes': len(data),
                       'sha256': hashlib.sha256(data).hexdigest()}
    actual = set()
    for directory, dirs, names in os.walk(root, followlinks=False):
        dirs[:] = [d for d in dirs if not (Path(directory) == root and d == '.git')]
        for name in list(dirs):
            path = Path(directory) / name
            if path.is_symlink():
                actual.add(path.relative_to(root).as_posix())
                dirs.remove(name)
        for name in names:
            actual.add((Path(directory) / name).relative_to(root).as_posix())
    if actual != files.keys():
        raise ValueError('Checkout has missing or extra worktree files')
    if git(root, 'status', '--porcelain').strip():
        raise ValueError('Checkout is not clean')
    return files


def canonical_digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def verify(snapshot, manifest):
    if git(snapshot, 'remote').strip():
        raise ValueError('Restored checkout must have no remote')
    if (snapshot / '.git/objects/info/alternates').exists():
        raise ValueError('Restored checkout depends on another object store')
    git(snapshot, 'fsck', '--full', '--no-reflogs')
    if git(snapshot, 'rev-parse', 'HEAD').decode().strip() != manifest['source_commit']:
        raise ValueError('Source commit mismatch')
    if git(snapshot, 'rev-parse', 'HEAD^{tree}').decode().strip() != manifest['source_tree']:
        raise ValueError('Source tree mismatch')
    files = inventory(snapshot)
    if files != manifest['files'] or canonical_digest(files) != manifest['source_files_sha256']:
        raise ValueError('Source audit mismatch')
    task_ids = manifest['competition_task_ids']
    if len(task_ids) != 49 or len(set(task_ids)) != 49:
        raise ValueError('Competition selection must contain 49 distinct tasks')
    for task in task_ids:
        for suffix in ('instruction.md', 'task.toml', 'environment/docker-compose.yaml',
                       'environment/Dockerfile', 'tests/test.sh', 'tests/Dockerfile'):
            if f'harbor_tasks/{task}/{suffix}' not in files:
                raise ValueError('Selected task source incomplete')
        if f'configs/tasks/{task}.yaml' not in files:
            raise ValueError('Selected task configuration missing')
    return {'source_copy_verified': True, 'files_verified': len(files),
            'regular_files': sum(v['git_mode'] != '120000' for v in files.values()),
            'internal_symlinks': sum(v['git_mode'] == '120000' for v in files.values()),
            'competition_task_count': len(task_ids),
            'upstream_task_count': len(list((snapshot / 'harbor_tasks').glob('*/task.toml'))),
            'source_commit': manifest['source_commit'], 'source_tree': manifest['source_tree'],
            'source_files_sha256': manifest['source_files_sha256'],
            'original_checkout_required': False, 'remote_configured': False,
            'scientific_execution_accepted': False, 'competition_release_frozen': False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--snapshot', type=Path, required=True)
    parser.add_argument('--manifest', type=Path, required=True)
    parser.add_argument('--compare-source', type=Path)
    parser.add_argument('--selection-from-release', type=Path)
    parser.add_argument('--report', type=Path)
    args = parser.parse_args()
    if args.compare_source:
        if args.manifest.exists() or args.selection_from_release is None:
            parser.error('new manifest and existing 49-task selection required')
        source_files = inventory(args.compare_source)
        if inventory(args.snapshot) != source_files:
            raise ValueError('Restored tree differs from original source')
        release = json.loads(args.selection_from_release.read_text())
        manifest = {'schema': 'qiqc-upstream-restoration-audit/v1',
                    'status': 'source-verified-runtime-acceptance-pending-not-frozen',
                    'source_commit': git(args.compare_source, 'rev-parse', 'HEAD').decode().strip(),
                    'source_tree': git(args.compare_source, 'rev-parse', 'HEAD^{tree}').decode().strip(),
                    'competition_task_ids': sorted(release['tasks']),
                    'source_files_sha256': canonical_digest(source_files), 'files': source_files}
        result = verify(args.snapshot, manifest)
        args.manifest.parent.mkdir(parents=True, exist_ok=True)
        with args.manifest.open('x') as stream:
            json.dump(manifest, stream, indent=2, sort_keys=True)
            stream.write('\n')
    else:
        result = verify(args.snapshot, json.loads(args.manifest.read_text()))
    result['audit_manifest_sha256'] = hashlib.sha256(args.manifest.read_bytes()).hexdigest()
    if args.report:
        with args.report.open('x') as stream:
            json.dump(result, stream, indent=2)
            stream.write('\n')
    print(json.dumps(result))


if __name__ == '__main__':
    main()
