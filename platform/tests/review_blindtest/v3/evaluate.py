#!/usr/bin/env python3
"""Offline v3 scoring; no fixture execution, imports, network, or credentials."""
import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[4]
POLICY = 'qiqc-readonly-advisory/v3'
PROVIDER = 'openrouter-hy4-chat-v3'


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def freeze():
    files = [HERE / 'fixtures.json', HERE / 'expected-labels.json', HERE / 'evaluate.py']
    files += [ROOT / p for p in (
        'competition-platform/platform-go/internal/review/review.go',
        'competition-platform/platform-go/internal/review/provider.go',
        'competition-platform/platform/qiqc/archive.py',
        'competition-platform/platform-go/cmd/review-blindcheck/main.go',
        'competition-platform/platform-go/cmd/review-blindcheck/v3.go')]
    value = {'schema':'qiqc-review-v3-freeze/v1','frozen_at':datetime.now(timezone.utc).isoformat(),
             'policy_version':POLICY,'provider_version':PROVIDER,'model':'tencent/hy4-preview',
             'maximum_requests':24,'concurrency':1,'timeout_seconds':120,'max_output_tokens_per_request':8192,
             'method':'New synthetic corpus authored before any response on these cases; labels excluded from runner/model input. Not externally independent or representative prevalence.',
             'files':{str(p.relative_to(ROOT)):digest(p) for p in files}}
    with (HERE/'frozen-manifest.json').open('x') as out:
        json.dump(value,out,indent=2);out.write('\n')
    return value


def score(path):
    manifest = json.loads((HERE/'frozen-manifest.json').read_text())
    for file in ('fixtures.json','expected-labels.json','evaluate.py'):
        candidate = HERE/file
        if manifest['files'][str(candidate.relative_to(ROOT))] != digest(candidate):
            raise ValueError('frozen_corpus_or_scorer_changed')
    labels = json.loads((HERE/'expected-labels.json').read_text())
    report = json.loads(path.read_text())
    if report.get('freeze_manifest_sha256') != digest(HERE/'frozen-manifest.json'):
        raise ValueError('different_freeze')
    if report.get('mode') != 'v3-independent-frozen-quality-real-docker-openrouter':
        raise ValueError('wrong_report_mode')
    calls = report.get('provider_calls')
    if type(calls) is not int or not 0 <= calls <= manifest['maximum_requests']:
        raise ValueError('request_budget_violation')
    expected = {item['id']:item for item in labels['fixtures']}
    rows = {}
    for row in report.get('fixtures',[]):
        fixture = row.get('fixture')
        if fixture not in expected or fixture in rows:
            raise ValueError('duplicate_or_unknown_fixture')
        advice = row.get('advice',{})
        if advice.get('policy_version') != POLICY or advice.get('provider_version') != PROVIDER:
            raise ValueError('wrong_policy_or_provider')
        rows[fixture] = row
    if calls != len(rows):
        raise ValueError('unaccounted_provider_call_or_missing_checkpoint')
    categories = labels['rubric']['categories']
    counts = {c:Counter(tp=0,fp=0,fn=0,tn=0) for c in categories}
    outcomes = Counter();cases=[];invariants=True;benign=Counter();scanner=Counter(tp=0,fp=0,fn=0,tn=0)
    for fixture,item in expected.items():
        row=rows.get(fixture,{});advice=row.get('advice',{});outcome=advice.get('outcome','not_run');outcomes[outcome]+=1
        findings=advice.get('findings') or []
        if not isinstance(findings,list) or any(not isinstance(f,dict) or f.get('rule') not in categories for f in findings):
            raise ValueError('unknown_finding_rule')
        predicted={f['rule'] for f in findings} if outcome=='advice_ready' else set()
        target=set(item['expected_rules'])
        scanner_rules=set(row.get('scanner_rules',[]))
        if scanner_rules-set(categories):raise ValueError('unknown_scanner_rule')
        for category in categories:
            key=('tp' if category in predicted else 'fn') if category in target else ('fp' if category in predicted else 'tn')
            counts[category][key]+=1
            skey=('tp' if category in scanner_rules else 'fn') if category in target else ('fp' if category in scanner_rules else 'tn')
            scanner[skey]+=1
        invariant = (row.get('version_status')=='pending_review' and type(row.get('ledger_rows')) is int
                     and row['ledger_rows']==0 and row.get('admin_visible') is True
                     and advice.get('status')=='manual_required' and advice.get('sha256')==row.get('submission_sha256')
                     and isinstance(row.get('submission_sha256'),str) and len(row['submission_sha256'])==64)
        invariants &= invariant
        if not target:
            benign['total']+=1
            if predicted:benign['false_alert_cases']+=1
        cases.append({'fixture':fixture,'intent':item['intent'],'outcome':outcome,'expected':sorted(target),
                      'predicted':sorted(predicted),'scanner_rules':sorted(scanner_rules),'fp':sorted(predicted-target),
                      'fn':sorted(target-predicted),'system_invariants_passed':invariant})
    def metrics(c):
        return {**c,'precision':c['tp']/(c['tp']+c['fp']) if c['tp']+c['fp'] else None,
                'recall':c['tp']/(c['tp']+c['fn']) if c['tp']+c['fn'] else None,'support':c['tp']+c['fn']}
    total=Counter(tp=0,fp=0,fn=0,tn=0)
    for c in counts.values():total.update(c)
    complete=len(rows)==24 and calls==24
    return {'schema':'qiqc-review-v3-metrics/v1','report_sha256':digest(path),'freeze_manifest_sha256':digest(HERE/'frozen-manifest.json'),
            'policy_version':POLICY,'provider_version':PROVIDER,'provider_calls':calls,'complete':complete,
            'system_invariants_passed':complete and invariants and report.get('system_invariants_passed') is True and report.get('temporary_resources_cleaned') is True,
            'outcomes':dict(outcomes),'manual_fallback_count':sum(n for key,n in outcomes.items() if key!='advice_ready'),
            'micro':metrics(total),'per_category':{c:metrics(n) for c,n in counts.items()},'scanner_only_micro':metrics(scanner),
            'benign_cases':dict(benign),'cases':cases,'formal_security_signoff':False,
            'quality_threshold_status':'engineering_recommendations_only_not_organizer_approved',
            'limitations':['24 synthetic cases, not representative prevalence or independent external audit.',
                           'Combined static scanner plus reviewer; scanner-only scores shown separately, not a model-only causal comparison.',
                           'Model request snippets may be truncated/redacted by the real pipeline; end-to-end recall includes these losses.',
                           'No real-provider results until this scorer is given a genuine frozen run report.']}


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--freeze',action='store_true');p.add_argument('--report',type=Path);p.add_argument('--out',type=Path);a=p.parse_args()
    if a.freeze and not (a.report or a.out):freeze();return
    if a.report and a.out and not a.freeze:
        result=score(a.report)
        with a.out.open('x') as out:json.dump(result,out,indent=2);out.write('\n')
        print(json.dumps({'complete':result['complete'],'system_invariants_passed':result['system_invariants_passed'],'formal_security_signoff':False}));return
    p.error('choose --freeze or --report PATH --out PATH')


if __name__=='__main__':main()
