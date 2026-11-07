"""Real source/archive checks; no benchmark execution or cloud calls."""
import json
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[3]
SCRIPTS = ROOT / "competition-platform/platform/scripts/science"
sys.path.insert(0, str(SCRIPTS))
from freeze_task_batches import freeze, select_batch, verify_release


@pytest.fixture(scope="module")
def frozen(tmp_path_factory):
    candidate = json.loads(Path("/opt/qiqc-native-candidate/candidate.json").read_text())
    args = (ROOT / "benchmark-assets/qiqcbench-restored", Path(candidate["source_audit"]),
            ROOT / "benchmark-assets/paper-task-roster.json", tmp_path_factory.mktemp("frozen-tasks"))
    path, report = freeze(*args)
    return path, report, args


def test_role_batches_preserve_source_and_select_admission_phase(frozen):
    path, report, _ = frozen
    assert report["verified"] and report["source_files"] == 3215
    assert report["runtime_activated"] is False
    index = json.loads((path / "release.json").read_text())
    for phase, kind, batch in (("practice", "infra", "infra-test-3"),
                               ("competition", "full", "formal-normal-39"),
                               ("competition", "elo", "formal-elo-7"),
                               ("competition", "public", "formal-public-5")):
        assert select_batch(index, phase, kind) == report["batches"][batch]["batch_id"]
    assert select_batch(index, "practice", "public") == report["batches"]["formal-public-5"]["batch_id"]
    for phase, kind in (("practice", "full"),
                        ("competition", "infra"), ("quiet", "infra"), ("closed", "full")):
        with pytest.raises(ValueError):
            select_batch(index, phase, kind)


def test_public_tasks_are_binary_subset_and_infra_is_disjoint(frozen):
    path, _, _ = frozen
    batches = {name: json.loads((path / name / "batch.json").read_text())
               for name in ("formal-normal-39", "formal-elo-7", "infra-test-3", "formal-public-5")}
    expected = [task for task in batches["formal-normal-39"]["tasks"] if task["public"]]
    assert len(expected) == 5
    assert batches["formal-public-5"]["tasks"] == expected
    assert {task["task_id"] for task in batches["infra-test-3"]["tasks"]}.isdisjoint(
        {task["task_id"] for task in expected})
    assert all(task["evaluation_role"] == "binary" for task in expected)
    assert all(task["evaluation_role"] == "infrastructure" for task in batches["infra-test-3"]["tasks"])
    assert all(task["evaluation_role"] == "racing" for task in batches["formal-elo-7"]["tasks"])
    for name in ("formal-normal-39", "formal-elo-7", "formal-public-5"):
        assert batches[name]["network_override"] is None
        assert batches[name]["agent_budget_policy"] == "native"
        for task in batches[name]["tasks"]:
            network = task["upstream_network"]["environment"]
            assert network.get("allow_internet") is True or network.get("network_mode") == "public"
    assert batches["infra-test-3"]["network_override"]["external_internet"] is False
    assert batches["infra-test-3"]["practice_agent_cap_seconds"] == 180
    assert batches["formal-public-5"]["competition_attempts_per_team"] == 3
    assert batches["formal-public-5"]["leaderboard_eligible"] is False


def test_freeze_is_reproducible_and_detects_mutated_archive(frozen, tmp_path):
    path, _, args = frozen
    second, _ = freeze(*args[:3], tmp_path / "second")
    assert second.name == path.name
    assert (second / "release.json").read_bytes() == (path / "release.json").read_bytes()
    archive = second / "infra-test-3/tasks.tar.gz"
    archive.chmod(0o600)
    archive.write_bytes(archive.read_bytes() + b"changed")
    with pytest.raises(ValueError, match="frozen file changed"):
        verify_release(second)


def test_invalid_public_roster_cannot_be_frozen(frozen, tmp_path):
    _, _, args = frozen
    roster = json.loads(args[2].read_text())
    next(task for task in roster["tasks"] if task["public"])["public"] = False
    bad = tmp_path / "bad-roster.json"
    bad.write_text(json.dumps(roster))
    with pytest.raises(ValueError, match="invalid public tasks"):
        freeze(args[0], args[1], bad, tmp_path / "rejected")
    assert not (tmp_path / "rejected").exists()
