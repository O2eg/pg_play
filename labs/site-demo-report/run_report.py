"""Reproduce the public pg_diag sample report end to end (about 12 minutes).

Steps: read-only preflight of the stand → write the pg_play manifest and plan → raise the
stand's run-time limits, ensure the demo grant, reset pg_stat_statements → warm-up workload →
staged activity (idle transaction, slow CREATE INDEX / COPY / VACUUM, lock chain, real incidents)
→ pg_play snapshots run → restore the stand settings → anonymize, validate, render, audit →
run-summary.json with the path of the public HTML.

Usage: run_report.py [--tag TAG] [--duration 300] [--interval 30] [--warmup 180] [--force]
                     [--dry-run] [--skip-preflight] [--skip-finalize] [--no-staged-activity]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import yaml
from finalize import finalize
from lab_common import LAB_DIR, Lab, add_common_arguments, say, tool

RUN_SETTINGS = ("statement_timeout", "temp_file_limit")
TERMINAL_STATES = {"succeeded", "failed", "partial", "cancelled", "interrupted", "blocked"}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    add_common_arguments(parser)
    parser.add_argument(
        "--duration",
        type=int,
        help="snapshot window seconds (params: diagnostics.duration_seconds)",
    )
    parser.add_argument(
        "--interval",
        type=int,
        help="snapshot interval seconds (params: diagnostics.interval_seconds)",
    )
    parser.add_argument(
        "--warmup",
        type=int,
        help="warm-up seconds before the window (params: diagnostics.warmup_seconds)",
    )
    parser.add_argument(
        "--force", action="store_true", help="archive an existing run of this tag as <tag>-attemptN"
    )
    parser.add_argument("--dry-run", action="store_true", help="preflight, manifest and plan only")
    parser.add_argument("--skip-preflight", action="store_true")
    parser.add_argument("--skip-finalize", action="store_true")
    parser.add_argument(
        "--no-staged-activity",
        action="store_true",
        help="plain workload, no incidents or maintenance",
    )
    return parser.parse_args()


def apply_overrides(lab: Lab, args: argparse.Namespace) -> None:
    diagnostics = lab.params["diagnostics"]
    if args.duration is not None:
        diagnostics["duration_seconds"] = args.duration
    if args.interval is not None:
        diagnostics["interval_seconds"] = args.interval
    if args.warmup is not None:
        diagnostics["warmup_seconds"] = args.warmup


# ------------------------------------------------------------------ preflight
def docker_running(container: str) -> bool:
    completed = subprocess.run(
        ["docker", "inspect", "-f", "{{.State.Running}}", container],
        capture_output=True,
        text=True,
        check=False,
    )
    return completed.stdout.strip() == "true"


def workload_running(lab: Lab) -> bool | None:
    completed = subprocess.run(
        [
            tool("pg-workload"),
            "status",
            "--root",
            str(lab.root / lab.params["workload"]["project"]),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        return None
    return bool(yaml.safe_load(completed.stdout)["scheduler"]["running"])


async def preflight(lab: Lab) -> dict[str, Any]:
    """Read-only checks; raises SystemExit with every failed check listed."""
    report: dict[str, Any] = {}
    failures: list[str] = []
    if not docker_running(lab.container):
        failures.append(
            f"container {lab.container} is not running "
            f"(pg-stand up in {lab.root / lab.stand['project']})"
        )
    running = workload_running(lab)
    report["workload_scheduler_running"] = running
    if running:
        failures.append("pg_workload scheduler is running: stop it first (workload_ctl.py stop)")
    report["free_gib"] = round(lab.free_gib(), 1)
    if report["free_gib"] < float(lab.params["guards"]["min_free_gib"]):
        failures.append(f"only {report['free_gib']} GiB free under {lab.root}")
    for path in (
        lab.root / lab.stand["project"],
        lab.root / lab.params["workload"]["project"],
        lab.root / lab.stand["config"],
    ):
        if not path.exists():
            failures.append(f"missing {path}")
    try:
        connection = await lab.connect("postgres")
    except Exception as error:  # noqa: BLE001
        failures.append(f"cannot connect to 127.0.0.1:{lab.port} as postgres: {error}")
        raise SystemExit("preflight failed:\n- " + "\n- ".join(failures)) from error
    try:
        report["version"] = await connection.fetchval("select version()")
        report["in_recovery"] = await connection.fetchval("select pg_is_in_recovery()")
        report["replication"] = [
            dict(r)
            for r in await connection.fetch(
                "select application_name, state, sync_state from pg_stat_replication"
            )
        ]
        report["run_settings"] = {
            r["name"]: dict(r)
            for r in await connection.fetch(
                "select name, setting, unit, sourcefile from pg_settings where name = any($1)",
                list(RUN_SETTINGS),
            )
        }
    finally:
        await connection.close()
    workload = await lab.connect()
    try:
        schemas = {r["nspname"] for r in await workload.fetch("select nspname from pg_namespace")}
        missing = [p for p in lab.params["workload"]["profiles"] if p not in schemas]
        if missing:
            failures.append(f"profile schemas missing (run prepare_stand.py): {missing}")
        report["lab_roles"] = await workload.fetchval(
            "select count(*) from pg_roles where rolname like 'lab\\_%'"
        )
        if report["lab_roles"] < 12:
            failures.append(
                f"{report['lab_roles']} lab_* roles, expected 12 (setup/site-lab-users.sql)"
            )
        report["invalid_indexes"] = [
            r["index"]
            for r in await workload.fetch(
                "select indexrelid::regclass::text as index from pg_index where not indisvalid"
            )
        ]
        if not report["invalid_indexes"]:
            failures.append(
                "no invalid index on the stand "
                "(prepare_stand.py creates trace_probe.trace_churn_dup_demo)"
            )
        extensions = {
            r["extname"] for r in await workload.fetch("select extname from pg_extension")
        }
        for name in ("pg_stat_statements", "pg_wait_sampling", "pg_stat_kcache", "pg_buffercache"):
            if name not in extensions:
                failures.append(f"extension {name} is not installed in {lab.database}")
        report["database_size"] = await workload.fetchval(
            "select pg_size_pretty(pg_database_size(current_database()))"
        )
    finally:
        await workload.close()
    if report["in_recovery"]:
        failures.append("target is in recovery; the lab collects from the primary")
    say(
        f"preflight: {report['version'].split(' on ')[0]}, db {report['database_size']}, "
        f"{len(report['replication'])} replication sender(s), {report['lab_roles']} lab roles, "
        f"free {report['free_gib']} GiB"
    )
    if failures:
        raise SystemExit("preflight failed:\n- " + "\n- ".join(failures))
    return report


def archive_previous(lab: Lab) -> str | None:
    """Move experiments/<tag>, the stop file and evidence of an earlier run to <tag>-attemptN."""
    if not lab.run_dir.exists() and not lab.stop_file.exists():
        return None
    attempts = [
        int(m.group(1))
        for p in lab.run_dir.parent.glob(f"{lab.tag}-attempt*")
        if (m := re.fullmatch(rf"{re.escape(lab.tag)}-attempt(\d+)", p.name))
    ]
    label = f"{lab.tag}-attempt{max(attempts, default=0) + 1}"
    if lab.run_dir.exists():
        lab.run_dir.rename(lab.run_dir.parent / label)
    if lab.stop_file.exists():
        lab.stop_file.rename(lab.root / f"{label}.stop")
    evidence = lab.root / "evidence"
    if evidence.exists():
        for path in evidence.glob(f"{lab.tag}-*"):
            if re.match(rf"{re.escape(lab.tag)}-attempt\d+", path.name):
                continue
            path.rename(evidence / (label + path.name[len(lab.tag) :]))
    say(f"previous run archived as {label}")
    return label


# ------------------------------------------------------- stand run settings
def _quoted(setting: str, unit: str | None) -> str:
    return f"{setting}{unit or ''}"


async def stand_prepare(lab: Lab) -> dict[str, Any]:
    """Record and raise run-time limits, ensure demo grants, reset pg_stat_statements."""
    connection = await lab.connect("postgres")
    try:
        previous = {
            r["name"]: dict(r)
            for r in await connection.fetch(
                "select name, setting, unit, sourcefile from pg_settings where name = any($1)",
                list(RUN_SETTINGS),
            )
        }
        for name, value in lab.params["stand_run_settings"].items():
            await connection.execute(f"ALTER SYSTEM SET {name} = '{value}'")
        await connection.execute("SELECT pg_reload_conf()")
    finally:
        await connection.close()
    workload = await lab.connect()
    try:
        for grant in lab.params.get("demo_grants") or []:
            await workload.execute(grant)
        await workload.execute("SELECT pg_stat_statements_reset()")
    finally:
        await workload.close()
    lab.evidence("stand-settings.json").write_text(
        json.dumps(
            {"previous": previous, "applied": lab.params["stand_run_settings"]},
            indent=2,
            default=str,
        )
    )
    say(
        "stand prepared: "
        + ", ".join(f"{k}={v}" for k, v in lab.params["stand_run_settings"].items())
        + f"; {len(lab.params.get('demo_grants') or [])} demo grant(s); pg_stat_statements reset"
    )
    return previous


async def stand_restore(lab: Lab, previous: dict[str, Any]) -> None:
    """Put every run-time limit back where it came from: ALTER SYSTEM SET for values that lived in
    postgresql.auto.conf, ALTER SYSTEM RESET otherwise (the base pg_stand file wins again)."""
    connection = await lab.connect("postgres")
    try:
        for name, row in previous.items():
            if str(row.get("sourcefile") or "").endswith("postgresql.auto.conf"):
                await connection.execute(
                    f"ALTER SYSTEM SET {name} = '{_quoted(row['setting'], row['unit'])}'"
                )
            else:
                await connection.execute(f"ALTER SYSTEM RESET {name}")
        await connection.execute("SELECT pg_reload_conf()")
        await asyncio.sleep(1)
        now = {
            r["name"]: r["setting"]
            for r in await connection.fetch(
                "select name, setting from pg_settings where name = any($1)", list(previous)
            )
        }
    finally:
        await connection.close()
    drift = {
        k: (previous[k]["setting"], now.get(k))
        for k in previous
        if previous[k]["setting"] != now.get(k)
    }
    say("stand restored: " + json.dumps(now) + (f" (DRIFT {drift})" if drift else ""))


# -------------------------------------------------------------------- window
def stand_containers(lab: Lab) -> list[str]:
    prefix = lab.container.split("-primary-")[0]
    completed = subprocess.run(
        ["docker", "ps", "--format", "{{.Names}}"], capture_output=True, text=True, check=False
    )
    return sorted(name for name in completed.stdout.split() if name.startswith(prefix))


def run_window(
    lab: Lab, args: argparse.Namespace, context: dict[str, Any], plan: dict[str, Any]
) -> dict[str, Any]:
    diagnostics = lab.params["diagnostics"]
    guards = lab.params["guards"]
    duration = int(diagnostics["duration_seconds"])
    warmup = int(diagnostics["warmup_seconds"])
    pre = int(diagnostics["pre_start_seconds"])
    lead = int(diagnostics["ops_lead_seconds"])
    service = context["service"]
    containers = stand_containers(lab)
    if warmup > 0:
        command, env = lab.workload_command(
            context,
            "start",
            *lab.profile_arguments(context),
            "--enable-selected",
            "--run-immediately",
        )
        with lab.evidence("warmup-start.log").open("w") as log:
            code = subprocess.run(
                command, env=env, stdout=log, stderr=subprocess.STDOUT, check=False
            ).returncode
        say(f"warm-up workload start rc={code}")
        if code != 0:
            raise RuntimeError("warm-up start failed, see evidence/*-warmup-start.log")
        time.sleep(warmup)
        command, env = lab.workload_command(context, "stop")
        with lab.evidence("warmup-stop.log").open("w") as log:
            code = subprocess.run(
                command, env=env, stdout=log, stderr=subprocess.STDOUT, check=False
            ).returncode
        say(f"warm-up workload stop rc={code}")
        if code != 0:
            raise RuntimeError("warm-up stop failed")
        time.sleep(3)
    helpers: list[subprocess.Popen] = []
    common = ["--params", args.params, "--root", str(lab.root), "--tag", lab.tag]
    if not args.no_staged_activity:
        with lab.evidence("progress-process.log").open("w") as log:
            helpers.append(
                subprocess.Popen(
                    [
                        sys.executable,
                        str(LAB_DIR / "progress.py"),
                        *common,
                        "--idle-delay",
                        "0",
                        "--ops-delay",
                        str(pre - lead),
                        "--max-seconds",
                        str(duration + pre + 120),
                    ],
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
            )
        say(
            "progress helper started; idle transaction now, "
            f"maintenance and lock chain in {pre - lead} s, pg_play in {pre} s"
        )
        time.sleep(pre - 2)
        with lab.evidence("events-process.log").open("w") as log:
            helpers.append(
                subprocess.Popen(
                    [
                        sys.executable,
                        str(LAB_DIR / "events.py"),
                        *common,
                        "--seconds",
                        str(duration + int(lab.params["staged_activity"]["events_extra_seconds"])),
                    ],
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
            )
        time.sleep(2)
    state: dict[str, Any] = {}
    try:
        state = service.start_experiment(
            lab.manifest_path, plan_hash=plan["plan_hash"], run_id=lab.tag
        )
        say(json.dumps({"started": lab.tag, "state": state["state"]}))
        started = time.monotonic()
        cancelled = False
        while True:
            state = service.experiment_status(lab.manifest_path, lab.tag)
            step = next(
                (
                    s["name"]
                    for s in reversed(state.get("steps") or [])
                    if s.get("status") == "running"
                ),
                None,
            )
            snapshot: dict[str, Any] = {
                "elapsed": round(time.monotonic() - started, 1),
                "state": state["state"],
                "step": step,
                "free_gib": round(lab.free_gib(), 2),
                "loadavg": os.getloadavg(),
            }
            if containers:
                stats = subprocess.run(
                    ["docker", "stats", "--no-stream", "--format", "{{json .}}", *containers],
                    text=True,
                    capture_output=True,
                    check=False,
                )
                snapshot["containers"] = [
                    json.loads(line) for line in stats.stdout.splitlines() if line.startswith("{")
                ]
            with lab.evidence("resources.jsonl").open("a") as log:
                log.write(json.dumps(snapshot) + "\n")
            say(json.dumps({k: v for k, v in snapshot.items() if k != "containers"}))
            if state["state"] in TERMINAL_STATES:
                break
            over_time = time.monotonic() - started > duration + int(
                guards["max_runtime_extra_seconds"]
            )
            if not cancelled and (
                snapshot["free_gib"] < float(guards["min_free_gib"]) or over_time
            ):
                service.cancel_experiment(
                    lab.manifest_path,
                    run_id=lab.tag,
                    reason="Bounded experiment resource or runtime guard",
                )
                cancelled = True
                say("cancellation requested")
            time.sleep(10)
        lab.evidence("final-state.json").write_text(json.dumps(state, indent=2))
        say(f"final state: {state['state']}")
    finally:
        lab.stop_file.touch()
        for helper in helpers:
            try:
                helper.wait(timeout=int(guards["helper_grace_seconds"]))
            except subprocess.TimeoutExpired:
                helper.terminate()
                helper.wait(timeout=15)
            say(f"helper {helper.pid} exit {helper.returncode}")
    return state


# ------------------------------------------------------------------- summary
def summarize(lab: Lab, state: dict[str, Any], published: dict[str, Any] | None) -> dict[str, Any]:
    expected = lab.params["expected"]
    source = Path(published["json"]) if published else lab.run_dir / f"{lab.tag}.json"
    summary: dict[str, Any] = {"tag": lab.tag, "state": state.get("state"), "artifact": str(source)}
    if source.exists():
        artifact = json.loads(source.read_text())
        statuses: dict[str, int] = {}
        for item in artifact["items"].values():
            statuses[item.get("collection_status")] = (
                statuses.get(item.get("collection_status"), 0) + 1
            )
        runtime = artifact.get("runtime") or {}
        plans = (
            (artifact["items"].get("server_log.auto_explain_plans") or {}).get("result") or {}
        ).get("plan_count")
        coverage = (runtime.get("log_collection") or {}).get("coverage") or {}
        summary.update(
            {
                "items": len(artifact["items"]),
                "statuses": statuses,
                "snapshots": runtime.get("snapshot_count"),
                "interval_seconds": runtime.get("interval_seconds"),
                "plans": plans,
                "log_records": coverage.get("parsed_records"),
                "log_ranking_complete": coverage.get("ranking_complete"),
            }
        )
        checks = {
            "items": len(artifact["items"]) == expected["items"],
            "ok_min": statuses.get("ok", 0) >= expected["ok_min"],
            "error_max": statuses.get("error", 0) <= expected["error_max"],
            "snapshots": runtime.get("snapshot_count") == expected["snapshots"],
            "plans_min": (plans or 0) >= expected["plans_min"],
        }
        summary["checks"] = checks
        summary["all_checks_passed"] = all(checks.values())
    if published:
        summary["public_html"] = published["html"]
        summary["public_json"] = published["json"]
        summary["anonymization"] = published["changes"]
        summary["browser"] = published["browser"]
    lab.run_dir.mkdir(parents=True, exist_ok=True)
    (lab.run_dir / "run-summary.json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=False, default=str)
    )
    return summary


def main() -> None:
    args = parse_args()
    lab = Lab.from_args(args)
    apply_overrides(lab, args)
    say(
        f"lab root {lab.root}, tag {lab.tag}, "
        f"window {lab.params['diagnostics']['duration_seconds']} s / "
        f"{lab.params['diagnostics']['interval_seconds']} s"
    )
    if not args.dry_run and (lab.run_dir.exists() or lab.stop_file.exists()):
        if not args.force:
            raise SystemExit(
                f"{lab.run_dir} already exists; pick another --tag or pass --force to archive it"
            )
        archive_previous(lab)
    if not args.skip_preflight:
        asyncio.run(preflight(lab))
    manifest_path = lab.write_manifest()
    context = lab.pg_play_context()
    plan = context["service"].plan_experiment(manifest_path)
    lab.evidence("plan.json").write_text(json.dumps(plan, indent=2, default=str))
    say(f"manifest {manifest_path}; plan_hash {plan['plan_hash']}")
    if args.dry_run:
        print(manifest_path.read_text())
        print(
            json.dumps(
                {
                    k: v
                    for k, v in plan.items()
                    if k in ("plan_hash", "diagnostics", "stand", "workload")
                },
                indent=2,
                default=str,
            )[:3000]
        )
        return
    previous = asyncio.run(stand_prepare(lab))
    state: dict[str, Any] = {}
    try:
        state = run_window(lab, args, context, plan)
    finally:
        asyncio.run(stand_restore(lab, previous))
    if state.get("state") != "succeeded":
        summarize(lab, state, None)
        raise SystemExit(
            f"experiment ended in state {state.get('state')}; see {lab.run_dir}/worker.log"
        )
    published = None if args.skip_finalize else finalize(lab)
    summary = summarize(lab, state, published)
    say(
        "RUN COMPLETED "
        + json.dumps(
            {k: summary.get(k) for k in ("statuses", "snapshots", "plans", "all_checks_passed")}
        )
    )
    if published:
        print(published["html"])


if __name__ == "__main__":
    main()
