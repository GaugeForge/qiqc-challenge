import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / 'competition-platform/platform/scripts/science'))
spec = importlib.util.spec_from_file_location('catalog_contract', ROOT / 'benchmark-assets/adapters/science-probe/catalog_contract.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def test_legacy_public_smoke_unchanged():
    assert module.execution_options({}, ROOT) == {}


@pytest.mark.parametrize('index', [-1, 49, True, 0.0, '0'])
def test_rejects_ambiguous_or_outside_index(index):
    with pytest.raises(ValueError, match='invalid_catalog_index'):
        module.execution_options({'science_task_index': index, 'agent_budget_seconds': 300}, ROOT)


@pytest.mark.parametrize('budget', [None, 299, 301, True, '300'])
def test_budget_is_fixed_not_contestant_controlled(budget):
    with pytest.raises(ValueError, match='invalid_catalog_budget'):
        module.execution_options({'science_task_index': 0, 'agent_budget_seconds': budget}, ROOT)


def test_each_index_resolves_exact_frozen_task(monkeypatch):
    tasks = {f'task_{n:02d}': {} for n in reversed(range(49))}
    monkeypatch.setattr(module, 'verify_release', lambda root: {'tasks': tasks, 'release_id': 'frozen-v1'})
    for index in range(49):
        assert module.execution_options({'science_task_index': index, 'agent_budget_seconds': 300, 'science_release_id': 'frozen-v1'}, ROOT) == {
            'task_id': f'task_{index:02d}', 'agent_budget_seconds': 300, 'generic_task': True,
        }


def test_tampered_release_does_not_resolve(monkeypatch):
    def invalid(root):
        raise ValueError('release-pin-mismatch')
    monkeypatch.setattr(module, 'verify_release', invalid)
    with pytest.raises(ValueError, match='release-pin-mismatch'):
        module.execution_options({'science_task_index': 0, 'agent_budget_seconds': 300}, ROOT)


def test_accepted_release_cannot_silently_change(monkeypatch):
    monkeypatch.setattr(module, 'verify_release', lambda root: {'release_id': 'different'})
    with pytest.raises(ValueError, match='catalog_release_mismatch'):
        module.execution_options({'science_task_index': 0, 'agent_budget_seconds': 300, 'science_release_id': 'frozen-v1'}, ROOT)
