"""Offline scoring semantics; synthetic advice only, no cloud calls or fixture execution."""
import importlib.util
import json
from pathlib import Path

import pytest

BASE = Path(__file__).resolve().parent / 'review_blindtest'
SPEC = importlib.util.spec_from_file_location('review_blind_evaluate', BASE / 'evaluate.py')
evaluate = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(evaluate)


def synthetic_report(tmp_path):
    labels = json.loads((BASE / 'expected-labels.json').read_text())
    report = {'freeze_manifest_sha256': evaluate.digest(BASE / 'frozen-manifest.json'),
              'provider_calls': 8, 'system_invariants_passed': True,
              'fixtures': [{'fixture': item['id'], 'seconds': 0,
                            'advice': {'outcome': 'advice_ready',
                                       'findings': [{'rule': r} for r in item['expected_rules']]}}
                           for item in labels['fixtures']]}
    path = tmp_path / 'synthetic.json'
    path.write_text(json.dumps(report))
    return path, report


def test_exact_match_has_perfect_supported_metrics_and_no_signoff(tmp_path):
    path, _ = synthetic_report(tmp_path)
    result = evaluate.score(path)
    assert result['micro']['precision'] == 1
    assert result['micro']['recall'] == 1
    assert result['per_category']['embedded_secret']['recall'] is None
    assert result['manual_fallback_count'] == 0
    assert result['formal_security_signoff'] is False


def test_invalid_is_manual_fallback_and_unavailable_detection_not_safe(tmp_path):
    path, report = synthetic_report(tmp_path)
    row = report['fixtures'][2]
    expected_count = len(row['advice']['findings'])
    row['advice']['outcome'] = 'invalid_response'
    path.write_text(json.dumps(report))
    result = evaluate.score(path)
    assert result['manual_fallback_count'] == 1
    assert result['micro']['fn'] == expected_count
    assert result['cases'][2]['predicted'] == []
    assert result['cases'][2]['miss_type'] == 'manual_fallback_not_a_safe_verdict'


def test_benign_network_is_not_automatically_theft(tmp_path):
    path, report = synthetic_report(tmp_path)
    report['fixtures'][1]['advice']['findings'].append({'rule': 'data_exfiltration'})
    path.write_text(json.dumps(report))
    result = evaluate.score(path)
    assert result['cases'][1]['expected'] == ['external_endpoint']
    assert result['cases'][1]['false_positives'] == ['data_exfiltration']
    assert result['micro']['fp'] == 1


def test_duplicate_run_rejected(tmp_path):
    path, report = synthetic_report(tmp_path)
    report['fixtures'].append(report['fixtures'][0])
    path.write_text(json.dumps(report))
    with pytest.raises(ValueError, match='Duplicate'):
        evaluate.score(path)


def test_wrong_freeze_rejected(tmp_path):
    path, report = synthetic_report(tmp_path)
    report['freeze_manifest_sha256'] = '0' * 64
    path.write_text(json.dumps(report))
    with pytest.raises(ValueError, match='different freeze'):
        evaluate.score(path)
