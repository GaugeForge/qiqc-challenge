#!/usr/bin/env python3
"""Read actual private failed archives; emit only safe bounded diagnostic fields."""
import argparse
import io
import json
import sqlite3
import tarfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from verify_cloud_relay_evidence import fetch_archive, read_r2_config


def inspect(row, credentials):
    index, run, state, key, sha = row
    base = {'task_index': index, 'run_id': run, 'database_state': state, 'archive_sha256': sha}
    if not key:
        return {**base, 'diagnostic_state': 'archive_not_available'}
    raw = fetch_archive(credentials, {'key': key, 'sha256': sha})
    selected = {}
    with tarfile.open(fileobj=io.BytesIO(raw), mode='r:gz') as source:
        for item in source:
            name = Path(item.name).name
            if name in ('native-receipt.json', 'model-failure-evidence.json', 'qsim-progress.json'):
                if name in selected or not item.isfile() or item.size > 8*1024*1024:
                    raise ValueError('invalid_diagnostic_member')
                selected[name] = json.load(source.extractfile(item))
    receipt = selected.get('native-receipt.json') or {}
    failure = selected.get('model-failure-evidence.json') or {}
    recovery = failure.get('recovery') or {}
    terminal = receipt.get('final_container_evidence') or {}
    return {**base, 'diagnostic_state': 'private_archive_sha_verified',
            'failure_code': failure.get('failure_code'),
            **{key: receipt.get(key) for key in ('infrastructure_ok','agent_stop_reason','runtime_seconds','agent_exit','reward','verifier_exit','execution_duration_class')},
            'terminal_verified': terminal.get('verified'), 'terminal_errors': terminal.get('errors'),
            'model_recovery': {key: recovery.get(key) for key in ('successful_responses','failed_responses','recovered_failed_responses','unrecovered_failed_responses','planned_cancelled_responses','all_failures_recovered')},
            'qsim_success_count': (selected.get('qsim-progress.json') or {}).get('success_count')}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cohort', type=Path, required=True)
    parser.add_argument('--candidate', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    config = json.loads(args.candidate.read_text())
    credentials = read_r2_config(Path(config['candidate_root'])/'config/worker-r2.env')
    batches = {row['batch_id']: row['task_index'] for row in json.loads((args.cohort/'barrier-release.json').read_text())['queued']}
    with sqlite3.connect(Path(config['data']).joinpath('platform.db').as_uri()+'?mode=ro', uri=True) as db:
        rows = [(batches[batch],run,state,key,sha) for run,batch,state,key,sha in db.execute("SELECT r.id,r.batch_id,r.state,a.object_key,a.object_sha FROM runs r LEFT JOIN run_artifacts a ON a.run_id=r.id AND a.attempt=r.attempt AND a.kind='failed-rollout' WHERE r.state='attention'") if batch in batches]
    with ThreadPoolExecutor(max_workers=4) as pool:
        values = list(pool.map(lambda row: inspect(row,credentials), rows))
    report = {'schema':'qiqc-native-private-cohort-diagnostics/v1','controller_sha256':config['controller_sha256'],'failed_or_incomplete_attempts':sorted(values,key=lambda row:row['task_index']),'cloud_execution_started':False,'raw_trajectory_persisted_on_vps':False}
    with args.output.open('x') as stream:
        json.dump(report,stream,indent=2);stream.write('\n')
    print(json.dumps({'archives_inspected':len(values),'output':str(args.output)}))


if __name__ == '__main__':
    main()
