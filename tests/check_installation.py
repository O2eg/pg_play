"""Check installed component versions/contracts, including standalone environments."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from importlib.metadata import PackageNotFoundError, distribution, version
from pathlib import Path

COMPONENTS = (
    "pg-configurator",
    "pg-converter",
    "pg-diag",
    "pg-logical-repl",
    "pg-maker",
    "pg-perf-bench",
    "pg-stand",
    "pg-workload",
)


def check_component(name: str) -> None:
    installed = version(name)
    executable = str(Path(sys.executable).with_name(name))
    output = subprocess.check_output([executable, "--version"], text=True, timeout=30)
    if installed not in output:
        raise RuntimeError(f"{name}: CLI and distribution versions differ: {output.strip()}")
    output = subprocess.check_output(
        [executable, "--machine", "--component-capabilities"], text=True, timeout=30
    )
    envelope = json.loads(output)
    capabilities = envelope["result"]
    if capabilities["component"] != name.replace("-", "_"):
        raise RuntimeError(f"{name}: unexpected component identity")
    if capabilities["component_version"] != installed:
        raise RuntimeError(f"{name}: capability and distribution versions differ")
    print(f"{name} {installed}: CLI and capabilities verified", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--standalone", choices=COMPONENTS)
    args = parser.parse_args()
    if args.standalone:
        try:
            distribution("pg-play")
        except PackageNotFoundError:
            pass
        else:
            raise RuntimeError("Standalone installation unexpectedly pulled in pg-play")
        check_component(args.standalone)
        return

    from pg_play.service import PgPlayService

    capabilities = PgPlayService().component_capabilities()
    if set(capabilities) != {name.replace("-", "_") for name in COMPONENTS}:
        raise RuntimeError("pg-play must discover all eight components")
    for name in COMPONENTS:
        check_component(name)
    print(f"pg-play {version('pg-play')}: all eight components verified")


if __name__ == "__main__":
    main()
