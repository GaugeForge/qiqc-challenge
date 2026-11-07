"""Trusted E2B-host exporter for explicitly public runs; never exports verifier files."""
import argparse
import json
import os
from pathlib import Path
import stat
from public_view import trajectory_view, public_view_json, MAX_SOURCE_BYTES


class SafeTrajectory:
    def __init__(self, path):
        self.path = Path(path)
    def stat(self):
        return self.path.lstat()
    def read_text(self, errors='replace'):
        for parent in self.path.parents:
            if parent.is_symlink(): raise ValueError('public_trajectory_symlink')
        fd = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, 'rb') as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_SOURCE_BYTES:
                raise ValueError('public_trajectory_invalid')
            data = stream.read(MAX_SOURCE_BYTES+1)
            if len(data) > MAX_SOURCE_BYTES: raise ValueError('public_trajectory_oversize')
        return data.decode('utf-8', errors=errors)


def export(trial, token_file):
    trial = Path(trial)
    receipt = json.loads((trial/'native-receipt.json').read_text())
    token = Path(token_file).read_text().strip()
    source = trial/'agent/native-trajectory.jsonl'
    view = {'mode':'scientific', 'standard_harbor_rollout':True,
            'reward':int(receipt['reward']), 'runtime_seconds':receipt['runtime_seconds'],
            'agent_stop_reason':receipt['agent_stop_reason'], 'agent_budget_seconds':receipt['agent_budget_seconds'],
            'trajectory_available':source.exists(), 'leaderboard_eligible':False,
            'summary':'Bounded real scientific execution. Only this public task Agent log is shown.'}
    if source.exists(): view.update(trajectory_view(SafeTrajectory(source), secrets=[token]))
    serialized = public_view_json(view)
    for name in ('public-view.txt','public-view.json'):
        fd = os.open(trial/name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd,'w') as stream: stream.write(serialized)


if __name__ == '__main__':
    p=argparse.ArgumentParser();p.add_argument('--trial',required=True);p.add_argument('--token-file',required=True)
    a=p.parse_args();export(a.trial,a.token_file)
