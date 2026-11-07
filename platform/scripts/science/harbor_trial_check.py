#!/usr/bin/env python3
"""Real Harbor Trial lifecycle on E2B, followed by private R2 archive/readback."""

import json
import sys

from e2b_runtime import ROOT


def main():
    # Reuse the tested credential/real-R2 callbacks from the acceptance runner,
    # replacing only its executor with the genuine Harbor lifecycle.
    import argparse

    from harbor_executor import run_harbor_scientific_probe

    sys.path.insert(0, str(ROOT / "benchmark-assets/adapters/science-probe"))
    import run_executor

    run_executor.run_scientific_probe = run_harbor_scientific_probe
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--adapter", choices=["codex", "pi-agent", "deepseek-harness"], default="codex"
    )
    args = parser.parse_args()
    run_executor.run(args.adapter)
    result = json.loads(
        (
            ROOT
            / "benchmark-assets/adapters/science-probe"
            / ("executor-result-" + args.adapter + ".json")
        ).read_text()
    )
    (
        ROOT
        / "competition-platform/platform/test-results"
        / ("science-harbor-" + args.adapter + ".json")
    ).write_text(json.dumps(result, indent=2) + "\n")


if __name__ == "__main__":
    main()
