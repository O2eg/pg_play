"""Shared helpers for the site demo report lab (see README.md)."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import asyncpg
import yaml

LAB_DIR = Path(__file__).resolve().parent
# sys.executable inside a venv is <venv>/bin/python (a symlink: do not resolve it).
VENV_BIN = Path(sys.executable).parent
DEFAULT_PARAMS = LAB_DIR / "params.yaml"


def tool(name: str) -> str:
    """Console script of the pg_play venv (pg-workload, pg-diag), falling back to PATH."""
    candidate = VENV_BIN / name
    return str(candidate) if candidate.exists() else (shutil.which(name) or str(candidate))


def lab_path(value: str | Path) -> Path:
    """Absolute path for a parameter: relative values are anchored at the lab directory."""
    path = Path(value).expanduser()
    # abspath, not resolve(): a venv's bin/python is a symlink to the system interpreter.
    return Path(os.path.abspath(path if path.is_absolute() else LAB_DIR / path))


def say(message: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {message}", flush=True)


def load_params(path: str | Path | None = None) -> dict[str, Any]:
    with Path(path or DEFAULT_PARAMS).open() as handle:
        return yaml.safe_load(handle)


def add_common_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--params", default=str(DEFAULT_PARAMS), help="parameter file (params.yaml)"
    )
    parser.add_argument("--root", help="lab root; overrides params.root and $SITE_DEMO_ROOT")
    parser.add_argument("--tag", help="run id / report name; overrides params.tag")


@dataclass
class Lab:
    """Resolved parameters and paths of one lab run."""

    params: dict[str, Any]
    root: Path
    tag: str

    @classmethod
    def from_args(cls, args: argparse.Namespace) -> Lab:
        params = load_params(args.params)
        root = args.root or os.environ.get("SITE_DEMO_ROOT") or params["root"]
        return cls(params=params, root=lab_path(root), tag=args.tag or params["tag"])

    # ------------------------------------------------------------------ paths
    @property
    def manifest_path(self) -> Path:
        return self.root / f"experiment-{self.tag}.yaml"

    @property
    def run_dir(self) -> Path:
        return self.root / "experiments" / self.tag

    @property
    def stop_file(self) -> Path:
        return self.root / f"{self.tag}.stop"

    def evidence(self, name: str) -> Path:
        directory = self.root / "evidence"
        directory.mkdir(exist_ok=True)
        return directory / f"{self.tag}-{name}"

    @property
    def stand(self) -> dict[str, Any]:
        return self.params["stand"]

    @property
    def port(self) -> int:
        return int(self.stand["primary_port"])

    @property
    def container(self) -> str:
        return str(self.stand["primary_container"])

    @property
    def database(self) -> str:
        return str(self.params["workload"]["database"])

    def free_gib(self) -> float:
        return shutil.disk_usage(self.root).free / 1024**3

    # ------------------------------------------------------------ credentials
    def superuser_password(self) -> str:
        path = self.root / self.stand["project"] / ".pg_stand/credentials/database/passwords.json"
        return json.loads(path.read_text())["superuser"]["password"]

    def workload_password(self) -> str:
        return (self.root / "experiments/credentials/workload-password").read_text().strip()

    async def connect(self, database: str | None = None, **settings: str) -> asyncpg.Connection:
        return await asyncpg.connect(
            host="127.0.0.1",
            port=self.port,
            user="postgres",
            password=self.superuser_password(),
            database=database or self.database,
            timeout=10,
            command_timeout=900,
            server_settings=settings or None,
        )

    # --------------------------------------------------------------- manifest
    def manifest_document(self) -> dict[str, Any]:
        p = self.params
        diagnostics = p["diagnostics"]
        workload = p["workload"]
        return {
            "api_version": "pg_play/v1",
            "kind": "PostgreSQLExperiment",
            "metadata": {"id": "site-demo"},
            "spec": {
                "artifact_root": "./experiments",
                "stand": {
                    "config": "./" + self.stand["config"],
                    "project": "./" + self.stand["project"],
                },
                "configurator": {"inputs": dict(p["configurator_inputs"])},
                "workload": {
                    "project": "./" + workload["project"],
                    "profiles": list(workload["profiles"]),
                    "scale": workload["scale"],
                    "database": workload["database"],
                    "user": workload["user"],
                    "install": bool(workload["install"]),
                    "stop_after_report": True,
                    "resource_guard": dict(workload["resource_guard"]),
                },
                "diagnostics": {
                    "mode": diagnostics["mode"],
                    "collection_mode": diagnostics["collection_mode"],
                    "duration_seconds": diagnostics["duration_seconds"],
                    "interval_seconds": diagnostics["interval_seconds"],
                    "report_name": self.tag,
                    "log_depth_time_min": diagnostics["log_depth_time_min"],
                },
                "phases": {
                    "benchmark": False,
                    "workload_diagnostics": True,
                    "recreate_workload_database": False,
                },
            },
        }

    def write_manifest(self) -> Path:
        self.manifest_path.write_text(yaml.safe_dump(self.manifest_document(), sort_keys=False))
        return self.manifest_path

    # ---------------------------------------------------------------- pg_play
    def pg_play_context(self) -> dict[str, Any]:
        """Manifest, stand config and credentials exactly as pg_play resolves them."""
        from pg_stand.config import load_config

        from pg_play.manifest import load_manifest
        from pg_play.service import PgPlayService

        manifest = load_manifest(self.manifest_path)
        parameters = json.loads((self.root / self.stand["configurator_parameters"]).read_text())
        config = load_config(
            manifest.stand_config,
            project_directory=manifest.stand_project,
            postgres_parameters=parameters,
        )
        service = PgPlayService()
        descriptor = PgPlayService._connection_descriptor(manifest, config)
        secrets = service._credential_context(manifest, config, descriptor)
        return {
            "service": service,
            "manifest": manifest,
            "config": config,
            "descriptor": descriptor,
            "secrets": secrets,
        }

    def workload_command(
        self, context: dict[str, Any], action: str, *extra: str
    ) -> tuple[list[str], dict[str, str]]:
        """pg-workload invocation with pg_play's own arguments (stop accepts only --root)."""
        from pg_play.service import PgPlayService

        manifest, config = context["manifest"], context["config"]
        env = os.environ.copy()
        env.update(
            PgPlayService._connection_environment(config, context["secrets"]["workload_password"])
        )
        if action == "stop":
            common: tuple[str, ...] = ("--root", str(manifest.workload.project))
        else:
            common = PgPlayService._workload_common_args(manifest, context["descriptor"])
        return [tool("pg-workload"), action, *common, *extra], env

    def profile_arguments(self, context: dict[str, Any]) -> tuple[str, ...]:
        from pg_play.service import PgPlayService

        return PgPlayService._profile_args(context["manifest"])


def record_event(path: Path, started: float, event: str, **fields: Any) -> None:
    with path.open("a") as handle:
        row = {"elapsed": round(time.monotonic() - started, 2), "event": event, **fields}
        handle.write(json.dumps(row, default=str) + "\n")
