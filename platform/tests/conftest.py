"""Resolve project-owned modules independently of pytest collection order."""

from pathlib import Path
import sys

PLATFORM = Path(__file__).resolve().parents[1]
for directory in (
    PLATFORM,
    PLATFORM / "scripts/science",
    PLATFORM.parents[1] / "benchmark-assets/adapters/science-probe",
):
    if str(directory) not in sys.path:
        sys.path.insert(0, str(directory))


def pytest_collection_modifyitems(items):
    """Retain, but explicitly skip, proofs for the deliberately removed release.

    These tests verify the retired role-copy runtime, not the active original
    Compose runtime. Do not silently regenerate its historical hash manifest.
    Current native Compose tests are never selected by this list.
    """
    import pytest

    original = (
        PLATFORM.parents[1]
        / "benchmark-assets/benchmark-releases/qiqc49-20261020-v1.json"
    )
    if original.exists():
        return
    retired = {
        "test_catalog_uses_runtime_scoring_without_cross_checkout_overlay",
        "test_candidate_copies_real_web_parser_helper_and_three_exact_templates",
        "test_main_pins_preserved_and_no_hidden_inputs_or_science_source",
        "test_scientific_role_uses_exact_frozen_hash_lock",
        "test_hamlearn_uses_its_own_minimal_image_contract",
        "test_independent_original_repo_hashes_when_checkout_available",
        "test_reject_main_lock_changing_upstream_pin",
        "test_install_prepares_interpreter_before_hash_install_and_keeps_clients_main_only",
        "test_only_cz_config_and_role_tests",
    }
    for item in items:
        if item.name.split("[", 1)[0] in retired or item.path.name in {
            "test_harbor_executor.py",
            "test_harbor_science_provider.py",
        }:
            item.add_marker(
                pytest.mark.skip(
                    reason="Retired qiqc49-20261020-v1 role-copy runtime was deliberately removed; native Compose acceptance is separate"
                )
            )
