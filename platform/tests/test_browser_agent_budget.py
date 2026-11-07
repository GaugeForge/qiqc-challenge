"""Browser acceptance uses the explicit candidate budget, never a CLI guess."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from cloud_browser_check import candidate_agent_budget


@pytest.mark.parametrize('candidate,expected', [(None, 300), ({}, 300),
    ({'agent_budget_seconds': 180}, 180), ({'agent_budget_seconds': 300}, 300)])
def test_candidate_budget_is_exact_and_defaults_to_original(candidate, expected):
    assert candidate_agent_budget(candidate) == expected


@pytest.mark.parametrize('value', [None, True, False, 180.0, '180', 0, -1, 179, 181, 299, 301, 3600])
def test_browser_rejects_arbitrary_or_ambiguous_budget(value):
    with pytest.raises(ValueError, match='^invalid_candidate_agent_budget$'):
        candidate_agent_budget({'agent_budget_seconds': value})
