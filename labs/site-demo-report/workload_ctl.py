"""Start or stop the demo workload manually with exactly the arguments pg_play uses.

Usage: workload_ctl.py start|stop [--params ...] [--root ...] [--tag ...]
"""

from __future__ import annotations

import argparse
import subprocess

from lab_common import Lab, add_common_arguments


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["start", "stop"])
    add_common_arguments(parser)
    args = parser.parse_args()
    lab = Lab.from_args(args)
    lab.write_manifest()
    context = lab.pg_play_context()
    extra = (
        (*lab.profile_arguments(context), "--enable-selected", "--run-immediately")
        if args.action == "start"
        else ()
    )
    command, env = lab.workload_command(context, args.action, *extra)
    raise SystemExit(subprocess.run(command, env=env, check=False).returncode)


if __name__ == "__main__":
    main()
