#!/usr/bin/env python3
"""Offline freeze/score only. Never executes fixture text or calls any provider."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[3]


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def freeze():
    paths = [
        HERE / 'fixtures.json', HERE / 'expected-labels.json',
        ROOT / 'competition-platform/platform-go/internal/review/review.go',
        ROOT / 'competition-platform/platform-go/internal/review/provider.go',
        ROOT / 'competition-platform/platform/qiqc/archive.py',
    ]
    result = {
        'schema': 'qiqc-review-blind-freeze/v1',
        'frozen_at': datetime.now(timezone.utc).isoformat(),
        'method': 'Independent fixtures and expected labels frozen before first provider call. Labels never loaded by review-blindcheck.',
        'model': 'tencent/hy4-preview', 'policy_version': 'qiqc-readonly-advisory/v1',
        'maximum_requests': 8, 'timeout_seconds': 120,
        'files': {str(p.relative_to(ROOT)): digest(p) for p in paths},
    }
    with (HERE / 'frozen-manifest.json').open('x') as output:
        json.dump(result, output, indent=2)
        output.write('\n')
    print(json.dumps(result, indent=2))


def score(report_path):
    manifest = json.loads((HERE / 'frozen-manifest.json').read_text())
    for path in (HERE / 'fixtures.json', HERE / 'expected-labels.json'):
        if digest(path) != manifest['files'][str(path.relative_to(ROOT))]:
            raise ValueError('Frozen fixture/label changed; do not score against new labels')
    labels = json.loads((HERE / 'expected-labels.json').read_text())
    report = json.loads(report_path.read_text())
    if report['freeze_manifest_sha256'] != digest(HERE / 'frozen-manifest.json'):
        raise ValueError('Report belongs to a different freeze')
    rows = {r['fixture']: r for r in report['fixtures']}
    if len(rows) != len(report['fixtures']) or report['provider_calls'] > 8:
        raise ValueError('Duplicate case or request budget violation')
    categories = labels['rubric']['all_categories']
    counts = {c: {'tp': 0, 'fp': 0, 'fn': 0, 'tn': 0} for c in categories}
    cases, outcomes = [], {}
    for item in labels['fixtures']:
        row = rows.get(item['id'], {})
        advice = row.get('advice', {})
        outcome = advice.get('outcome', 'not_run')
        outcomes[outcome] = outcomes.get(outcome, 0) + 1
        usable = outcome == 'advice_ready'
        predicted = {f['rule'] for f in advice.get('findings', []) or []} if usable else set()
        expected = set(item['expected_rules'])
        for category in categories:
            bucket = ('tp' if category in predicted else 'fn') if category in expected else ('fp' if category in predicted else 'tn')
            counts[category][bucket] += 1
        cases.append({'fixture': item['id'], 'intent': item['intent'], 'outcome': outcome,
                      'failure_code': advice.get('failure_code'), 'expected': sorted(expected),
                      'predicted': sorted(predicted), 'false_positives': sorted(predicted - expected),
                      'missed_usable_findings': sorted(expected - predicted),
                      'miss_type': None if usable else 'manual_fallback_not_a_safe_verdict',
                      'seconds': row.get('seconds')})
    def metrics(c):
        return {**c, 'precision': c['tp'] / (c['tp'] + c['fp']) if c['tp'] + c['fp'] else None,
                'recall': c['tp'] / (c['tp'] + c['fn']) if c['tp'] + c['fn'] else None,
                'support': c['tp'] + c['fn']}
    total = {key: sum(c[key] for c in counts.values()) for key in ('tp', 'fp', 'fn', 'tn')}
    return {'schema': 'qiqc-review-blind-metrics/v1', 'report_sha256': digest(report_path),
            'labels_sha256': digest(HERE / 'expected-labels.json'),
            'freeze_manifest_sha256': digest(HERE / 'frozen-manifest.json'),
            'model_requests': report['provider_calls'], 'outcomes': outcomes,
            'system_invariants_passed': report['system_invariants_passed'],
            'manual_fallback_count': sum(n for o, n in outcomes.items() if o != 'advice_ready'),
            'metrics_definition': labels['rubric'], 'per_category': {c: metrics(n) for c, n in counts.items()},
            'micro': metrics(total), 'cases': cases,
            'formal_security_signoff': False,
            'limitations': ['Eight cases only; single attempt each; neither representative prevalence nor confidence intervals.',
                            'Static scanner findings are included as in production; this measures combined scanner-plus-model advice, not independent model-only detection.',
                            'Invalid/timeout responses count as no usable finding for recall and remain manual review, never autoapproved.',
                            'No organizer-defined risk thresholds; labels may expose taxonomy disagreements and are not changed after responses.',
                            'One provider model identifier; upstream weights/version are not independently pinned.']}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--freeze', action='store_true')
    parser.add_argument('--report', type=Path)
    parser.add_argument('--out', type=Path)
    args = parser.parse_args()
    if args.freeze:
        if args.report or args.out:
            parser.error('--freeze is separate from scoring')
        freeze()
    elif args.report and args.out:
        result = score(args.report)
        with args.out.open('x') as output:
            json.dump(result, output, indent=2)
            output.write('\n')
        print(json.dumps({'outcomes': result['outcomes'], 'micro': result['micro'], 'system_invariants_passed': result['system_invariants_passed']}))
    else:
        parser.error('choose --freeze or --report PATH --out PATH')

if __name__ == '__main__':
    main()
