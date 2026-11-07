"""Pure transcript checks; does not start or import any CLI runtime."""

import importlib.util
from pathlib import Path

import pytest

path = Path(__file__).resolve().parents[2] / 'adapters/terminal.py'
spec = importlib.util.spec_from_file_location('qiqc_template_terminal', path)
terminal = importlib.util.module_from_spec(spec)
spec.loader.exec_module(terminal)


@pytest.mark.parametrize('kind,rows', [
    ('codex', [{'type': 'turn.completed'}]),
    ('deepseek-harness', [{'type': 'final', 'text': 'ready'}]),
    ('pi-agent', [{'type': 'agent_settled', 'aborted': False}]),
])
def test_successful_native_terminal(kind, rows):
    assert terminal.successful_terminal(kind, rows)
    assert not terminal.successful_terminal(kind, [])


@pytest.mark.parametrize('kind,rows', [
    ('codex', [{'type': 'turn.completed'}, {'type': 'turn.failed'}]),
    ('deepseek-harness', [{'type': 'final', 'text': 'ready'}, {'type': 'error'}]),
    ('pi-agent', [{'type': 'agent_settled'}, {'message': {'stopReason': 'error'}}]),
    ('pi-agent', [{'type': 'agent_settled'}, {'messages': [{'stopReason': 'aborted'}]}]),
    ('pi-agent', [{'type': 'agent_settled', 'aborted': True}]),
])
def test_exit_zero_cannot_hide_reported_model_error(kind, rows):
    assert not terminal.successful_terminal(kind, rows)
