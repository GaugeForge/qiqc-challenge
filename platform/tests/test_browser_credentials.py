import json
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
from browser_credentials import isolate, record_initial_admin_change


def test_concurrent_attempts_keep_distinct_team_credentials_and_seed(tmp_path):
    seed = tmp_path / "seed.json"
    original = {
        "admin": {"email": "staff@example.test", "password": "fixture"},
        "teams": [{"email": "old@example.test"}],
    }
    seed.write_text(json.dumps(original))
    seed.chmod(0o600)

    def attempt(index):
        path = isolate(seed, tmp_path / "private")
        value = json.loads(path.read_text())
        assert "teams" not in value
        value["teams"] = [
            {"email": f"team{index}@example.test", "password": "fixture-only"}
        ]
        path.write_text(json.dumps(value))
        return path

    with ThreadPoolExecutor(max_workers=4) as pool:
        paths = list(pool.map(attempt, range(4)))
    assert len(set(paths)) == 4 and all(p.stat().st_mode & 0o077 == 0 for p in paths)
    assert {json.loads(p.read_text())["teams"][0]["email"] for p in paths} == {
        f"team{i}@example.test" for i in range(4)
    }
    assert json.loads(seed.read_text()) == original
    record_initial_admin_change(
        seed, {"email": "staff@example.test", "password": "changed-fixture"}
    )
    assert json.loads(seed.read_text())["teams"] == original["teams"]


def test_public_or_symlinked_seed_is_rejected(tmp_path):
    seed = tmp_path / "seed.json"
    seed.write_text("{}")
    seed.chmod(0o644)
    with pytest.raises(ValueError):
        isolate(seed, tmp_path / "private")
    seed.chmod(0o600)
    link = tmp_path / "link"
    link.symlink_to(seed)
    with pytest.raises(ValueError):
        isolate(link, tmp_path / "private")
