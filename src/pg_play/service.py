"""Deterministic orchestration core shared by CLI, MCP, and tests."""

from __future__ import annotations

import os
import re
import secrets
import subprocess
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode

from docker.utils import parse_bytes
from pg_stand.config import MANAGED_POSTGRES_PARAMETERS, StandConfig, load_config
from pg_stand.credentials import credential_paths
from pg_stand.database_credentials import read_database_credentials

from pg_play.contract import canonical_hash
from pg_play.manifest import ExperimentManifest, load_manifest
from pg_play.report import compare_reports, inspect_report
from pg_play.runner import ComponentInvocation, ComponentRunner
from pg_play.state import read_state, write_json, write_state, write_text

_RUN_ID_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,63}$")


class OrchestrationError(RuntimeError):
    """An experiment cannot safely advance to the requested state."""


class PgPlayService:
    def __init__(self, runner: ComponentRunner | None = None) -> None:
        self.runner = runner or ComponentRunner()

    def component_capabilities(self, component: str | None = None) -> dict[str, Any]:
        components = (
            [component]
            if component is not None
            else ["pg_configurator", "pg_stand", "pg_workload", "pg_diag"]
        )
        arguments = {
            "pg_configurator": ("--capabilities",),
            "pg_stand": ("capabilities",),
            "pg_workload": ("--component-capabilities",),
            "pg_diag": ("--component-capabilities",),
        }
        unknown = sorted(set(components).difference(arguments))
        if unknown:
            raise OrchestrationError("unknown component(s): " + ", ".join(unknown))
        return {
            name: self._invoke(
                name,
                arguments[name],
                request_id=f"capabilities-{name}",
                timeout_seconds=30,
            )["result"]
            for name in components
        }

    def validate_experiment(self, manifest_path: str | Path) -> dict[str, Any]:
        manifest = load_manifest(manifest_path)
        base_config = load_config(
            manifest.stand_config,
            project_directory=manifest.stand_project,
        )
        self._validate_supported_stand(base_config)
        inputs = self._configurator_inputs(manifest, base_config)
        config_result = self._require(
            self._invoke(
                "pg_configurator",
                ("--input-json=-", "--validate-input"),
                request_id=f"{manifest.experiment_id}-validate-configurator",
                input_document={
                    "schema_version": "pg_configurator/input-v1",
                    "inputs": inputs,
                },
            ),
            {"succeeded"},
        )
        self._validate_resource_alignment(
            config_result["result"]["normalized_inputs"],
            base_config,
        )
        stand_result = self._require(
            self._invoke(
                "pg_stand",
                ("--config", str(manifest.stand_config), "validate"),
                request_id=f"{manifest.experiment_id}-validate-stand",
                cwd=manifest.stand_project,
            ),
            {"succeeded"},
        )
        workload_result = self._require(
            self._invoke(
                "pg_workload",
                (
                    "validate",
                    "--root",
                    str(manifest.workload.project),
                    *self._profile_args(manifest),
                ),
                request_id=f"{manifest.experiment_id}-validate-workload",
            ),
            {"succeeded"},
        )
        return {
            "schema_version": "pg_play/validation-v1",
            "valid": True,
            "experiment_id": manifest.experiment_id,
            "manifest_hash": manifest.document_hash,
            "components": {
                "pg_configurator": config_result,
                "pg_stand": stand_result,
                "pg_workload": workload_result,
            },
        }

    def plan_experiment(self, manifest_path: str | Path) -> dict[str, Any]:
        manifest = load_manifest(manifest_path)
        base_config = load_config(
            manifest.stand_config,
            project_directory=manifest.stand_project,
        )
        self._validate_supported_stand(base_config)
        inputs = self._configurator_inputs(manifest, base_config)
        config_envelope = self._require(
            self._invoke(
                "pg_configurator",
                ("--input-json=-", "--output-format=json"),
                request_id=f"{manifest.experiment_id}-plan-configurator",
                input_document={
                    "schema_version": "pg_configurator/input-v1",
                    "inputs": inputs,
                },
            ),
            {"succeeded"},
        )
        config_artifact = config_envelope["result"]["artifact"]
        self._validate_resource_alignment(config_artifact["inputs"], base_config)
        candidate_parameters = self._semantic_postgresql_parameters(
            config_artifact["postgresql_conf"]
        )
        parameters, stand_managed_parameters = self._partition_parameters(candidate_parameters)
        resolved_config = load_config(
            manifest.stand_config,
            project_directory=manifest.stand_project,
            postgres_parameters=parameters,
        )
        stand_envelope = self._invoke(
            "pg_stand",
            (
                "--config",
                str(manifest.stand_config),
                "--parameters-json=-",
                "plan",
            ),
            request_id=f"{manifest.experiment_id}-plan-stand",
            input_document=parameters,
            cwd=manifest.stand_project,
        )
        if stand_envelope["status"] not in {"succeeded", "blocked"}:
            self._require(stand_envelope, {"succeeded", "blocked"})
        connection = self._connection_descriptor(manifest, resolved_config)
        prepare_plan = self._require(
            self._invoke(
                "pg_workload",
                (
                    "plan",
                    *self._workload_common_args(manifest, connection),
                    "--operation=prepare-db",
                ),
                request_id=f"{manifest.experiment_id}-plan-workload-prepare-db",
            ),
            {"planned"},
        )
        install_plan = self._require(
            self._invoke(
                "pg_workload",
                (
                    "plan",
                    *self._workload_common_args(manifest, connection),
                    "--operation=install",
                    *self._profile_args(manifest),
                ),
                request_id=f"{manifest.experiment_id}-plan-workload-install",
            ),
            {"planned"},
        )
        scheduler_plan = self._require(
            self._invoke(
                "pg_workload",
                (
                    "plan",
                    *self._workload_common_args(manifest, connection),
                    "--operation=scheduler",
                    *self._profile_args(manifest),
                    "--enable-selected",
                    "--run-immediately",
                ),
                request_id=f"{manifest.experiment_id}-plan-workload-scheduler",
            ),
            {"planned"},
        )
        diagnostic_plan = self._require(
            self._invoke(
                "pg_diag",
                (
                    "explain-plan",
                    "--pg-version",
                    str(resolved_config.postgres.version * 10_000),
                    "--run-mode",
                    manifest.diagnostics.mode,
                    "--collection-mode",
                    manifest.diagnostics.collection_mode,
                ),
                request_id=f"{manifest.experiment_id}-plan-diag",
            ),
            {"succeeded"},
        )
        workload_versions = {
            envelope["component_version"]
            for envelope in (prepare_plan, install_plan, scheduler_plan)
        }
        if len(workload_versions) != 1:
            raise OrchestrationError("pg_workload version changed while building the plan")
        warnings = sorted(
            {
                *config_envelope.get("warnings", []),
                *stand_envelope.get("warnings", []),
                *prepare_plan.get("warnings", []),
                *install_plan.get("warnings", []),
                *scheduler_plan.get("warnings", []),
                *diagnostic_plan.get("warnings", []),
            }
        )
        if stand_managed_parameters:
            warnings.append(
                "pg_stand retains ownership of topology, TLS, fixed logging, and preload "
                "parameters listed in configuration.stand_managed_parameters"
            )
        stable_plan = {
            "schema_version": "pg_play/plan-v1",
            "experiment_id": manifest.experiment_id,
            "manifest_hash": manifest.document_hash,
            "components": {
                "pg_configurator": config_envelope["component_version"],
                "pg_stand": stand_envelope["component_version"],
                "pg_workload": workload_versions.pop(),
                "pg_diag": diagnostic_plan["component_version"],
            },
            "configuration": {
                "artifact_hash": config_artifact["artifact_hash"],
                "postgresql_version": resolved_config.postgres.version,
                "parameter_count": len(candidate_parameters),
                "stand_parameter_count": len(parameters),
                "parameters": candidate_parameters,
                "stand_managed_parameters": stand_managed_parameters,
            },
            "stand": stand_envelope["result"],
            "workload": {
                "prepare_db": self._compact_workload_plan(prepare_plan["result"]),
                "install": self._compact_workload_plan(install_plan["result"]),
                "scheduler": self._compact_workload_plan(scheduler_plan["result"]),
            },
            "diagnostics": diagnostic_plan["result"],
            "warnings": warnings,
        }
        stable_plan["plan_hash"] = canonical_hash(stable_plan)
        return stable_plan

    def run_experiment(
        self,
        manifest_path: str | Path,
        *,
        plan_hash: str,
        run_id: str,
    ) -> dict[str, Any]:
        if not _RUN_ID_RE.fullmatch(run_id):
            raise OrchestrationError(f"run_id must match {_RUN_ID_RE.pattern}")
        manifest = load_manifest(manifest_path)
        plan = self.plan_experiment(manifest_path)
        if plan["plan_hash"] != plan_hash:
            raise OrchestrationError(
                f"stale experiment plan: expected {plan_hash}, current plan is {plan['plan_hash']}"
            )
        run_directory = manifest.artifact_root / run_id
        state_path = run_directory / "state.json"
        existing = read_state(state_path)
        if existing.get("state") == "succeeded" and existing.get("plan_hash") == plan_hash:
            return existing
        if existing.get("state") != "not_found":
            raise OrchestrationError(
                f"run_id {run_id} already has immutable state {existing.get('state')!r}"
            )
        run_directory.mkdir(parents=True, exist_ok=True)
        state: dict[str, Any] = {
            "schema_version": "pg_play/run-state-v1",
            "experiment_id": manifest.experiment_id,
            "run_id": run_id,
            "plan_hash": plan_hash,
            "manifest_hash": manifest.document_hash,
            "state": "running",
            "steps": [],
            "artifacts": [],
            "error": None,
        }
        write_state(state_path, state)
        manifest_snapshot = run_directory / "experiment.yaml"
        write_text(
            manifest_snapshot,
            manifest.source.read_text(encoding="utf-8"),
        )
        plan_path = run_directory / "plan.json"
        write_json(plan_path, plan)
        state["artifacts"].extend(
            [
                {
                    "kind": "ExperimentManifest",
                    "path": str(manifest_snapshot),
                    "hash": manifest.document_hash,
                },
                {
                    "kind": "ExperimentPlan",
                    "path": str(plan_path),
                    "hash": plan_hash,
                },
            ]
        )
        write_state(state_path, state)
        candidate_parameters = plan["configuration"]["parameters"]
        parameters, _stand_managed = self._partition_parameters(candidate_parameters)
        config = load_config(
            manifest.stand_config,
            project_directory=manifest.stand_project,
            postgres_parameters=parameters,
        )
        connection = self._connection_descriptor(manifest, config)
        candidate_path = run_directory / "postgresql-parameters.json"
        write_json(candidate_path, candidate_parameters)
        state["artifacts"].append(
            {
                "kind": "PostgreSQLParameters",
                "path": str(candidate_path),
                "hash": canonical_hash(candidate_parameters),
            }
        )
        write_state(state_path, state)
        workload_stop_needed = False
        try:
            stand_plan = plan["stand"]
            required_action = stand_plan["required_action"]
            if required_action == "blocked":
                raise OrchestrationError(str(stand_plan.get("reason", "stand plan is blocked")))
            if required_action == "up":
                stand_args = ("up",)
            elif required_action == "none":
                stand_args = ("status",)
            elif required_action in {"reload", "restart"}:
                stand_args = (
                    "apply",
                    f"--{required_action}",
                    "--plan-hash",
                    stand_plan["plan_hash"],
                )
            else:
                raise OrchestrationError(f"unsupported stand action: {required_action}")
            self._step(
                state,
                state_path,
                "stand",
                self._invoke(
                    "pg_stand",
                    (
                        "--config",
                        str(manifest.stand_config),
                        "--parameters-json=-",
                        *stand_args,
                    ),
                    request_id=f"{manifest.experiment_id}-{run_id}-stand",
                    input_document=parameters,
                    cwd=manifest.stand_project,
                    timeout_seconds=900,
                ),
                {"succeeded"},
            )
            secret_context = self._credential_context(manifest, config, connection)
            common_args = self._workload_common_args(manifest, connection)
            environment = self._connection_environment(config, secret_context["workload_password"])
            self._step(
                state,
                state_path,
                "prepare-db",
                self._invoke(
                    "pg_workload",
                    (
                        "prepare-db",
                        *common_args,
                        "--plan-hash",
                        plan["workload"]["prepare_db"]["plan_hash"],
                    ),
                    request_id=f"{manifest.experiment_id}-{run_id}-prepare-db",
                    environment=environment,
                    timeout_seconds=900,
                ),
                {"succeeded"},
            )
            if manifest.workload.install:
                self._step(
                    state,
                    state_path,
                    "install-workload",
                    self._invoke(
                        "pg_workload",
                        (
                            "install",
                            *common_args,
                            *self._profile_args(manifest),
                            "--plan-hash",
                            plan["workload"]["install"]["plan_hash"],
                        ),
                        request_id=f"{manifest.experiment_id}-{run_id}-install",
                        environment=environment,
                        timeout_seconds=3600,
                    ),
                    {"succeeded"},
                )
            workload_stop_needed = True
            self._step(
                state,
                state_path,
                "start-workload",
                self._invoke(
                    "pg_workload",
                    (
                        "start",
                        *common_args,
                        *self._profile_args(manifest),
                        "--enable-selected",
                        "--run-immediately",
                        "--plan-hash",
                        plan["workload"]["scheduler"]["plan_hash"],
                    ),
                    request_id=f"{manifest.experiment_id}-{run_id}-start-workload",
                    environment=environment,
                ),
                {"running"},
            )
            diagnostic = self._invoke(
                "pg_diag",
                self._diagnostic_args(manifest, config, connection, run_directory),
                request_id=f"{manifest.experiment_id}-{run_id}-diag",
                environment=self._connection_environment(config, None),
                timeout_seconds=max(900, manifest.diagnostics.duration_seconds + 300),
            )
            self._step(
                state,
                state_path,
                "diagnostics",
                diagnostic,
                {"succeeded", "partial"},
            )
            state["artifacts"].extend(diagnostic.get("artifacts") or [])
            state["state"] = "partial" if diagnostic["status"] == "partial" else "succeeded"
            write_state(state_path, state)
            return state
        except Exception as exc:
            state["state"] = "failed"
            state["error"] = {"code": "orchestration_error", "message": str(exc)}
            write_state(state_path, state)
            raise
        finally:
            if workload_stop_needed and manifest.workload.stop_after_report:
                try:
                    stop_result = self._invoke(
                        "pg_workload",
                        ("stop", "--root", str(manifest.workload.project)),
                        request_id=f"{manifest.experiment_id}-{run_id}-stop-workload",
                    )
                    state["steps"].append(self._step_record("stop-workload", stop_result))
                    if stop_result["status"] != "succeeded" and state["state"] == "succeeded":
                        state["state"] = "partial"
                        state["error"] = {
                            "code": "workload_stop_failed",
                            "message": (
                                "diagnostics completed, but the workload could not be stopped"
                            ),
                        }
                except Exception as stop_error:
                    state["steps"].append(
                        {
                            "name": "stop-workload",
                            "component": "pg_workload",
                            "command": "stop",
                            "status": "failed",
                            "artifacts": [],
                            "warnings": [],
                            "error": {
                                "code": "workload_stop_failed",
                                "message": str(stop_error),
                            },
                        }
                    )
                    if state["state"] == "succeeded":
                        state["state"] = "partial"
                        state["error"] = {
                            "code": "workload_stop_failed",
                            "message": (
                                "diagnostics completed, but the workload could not be stopped"
                            ),
                        }
                write_state(state_path, state)

    def experiment_status(self, manifest_path: str | Path, run_id: str) -> dict[str, Any]:
        manifest = load_manifest(manifest_path)
        return read_state(manifest.artifact_root / run_id / "state.json")

    @staticmethod
    def inspect_report(path: str | Path) -> dict[str, Any]:
        return inspect_report(path)

    @staticmethod
    def compare_reports(baseline: str | Path, candidate: str | Path) -> dict[str, Any]:
        return compare_reports(baseline, candidate)

    def _invoke(
        self,
        component: str,
        arguments: tuple[str, ...],
        *,
        request_id: str,
        input_document: dict[str, Any] | None = None,
        environment: dict[str, str] | None = None,
        cwd: Path | None = None,
        timeout_seconds: float = 600,
    ) -> dict[str, Any]:
        return self.runner.run(
            ComponentInvocation(
                component=component,
                arguments=arguments,
                request_id=request_id,
                cwd=cwd,
                input_document=input_document,
                environment=environment,
                timeout_seconds=timeout_seconds,
            )
        )

    @staticmethod
    def _require(envelope: dict[str, Any], statuses: set[str]) -> dict[str, Any]:
        if envelope["status"] not in statuses:
            error = envelope.get("error") or {}
            raise OrchestrationError(
                f"{envelope['component']} {envelope['command']} returned {envelope['status']}: "
                f"{error.get('message', 'no error detail')}"
            )
        return envelope

    @staticmethod
    def _configurator_inputs(
        manifest: ExperimentManifest,
        base_config: StandConfig,
    ) -> dict[str, Any]:
        inputs = dict(manifest.configurator_inputs)
        inputs.setdefault("db_cpu", base_config.primary.cpu_limit)
        inputs.setdefault("db_ram", base_config.primary.memory_limit)
        requested_version = str(inputs.setdefault("pg_version", base_config.postgres.version))
        if requested_version != str(base_config.postgres.version):
            raise OrchestrationError(
                "configurator pg_version must match spec.stand.config PostgreSQL version"
            )
        expected_replication_mode = {
            "single": "none",
            "primary_standby": "physical",
            "primary_standby_logical": "logical",
        }[base_config.topology.mode]
        requested_replication_mode = str(
            inputs.setdefault("replication_mode", expected_replication_mode)
        )
        if requested_replication_mode != expected_replication_mode:
            raise OrchestrationError(
                "configurator replication_mode must match the pg_stand topology "
                f"({expected_replication_mode})"
            )
        if base_config.has_logical_replica:
            inputs.setdefault("logical_subscription_count", 1)
        return inputs

    @staticmethod
    def _validate_resource_alignment(
        normalized_inputs: dict[str, Any],
        config: StandConfig,
    ) -> None:
        configured_cpu = float(config.primary.cpu_limit)
        requested_cpu = float(normalized_inputs["cpu_cores"])
        if requested_cpu != configured_cpu:
            raise OrchestrationError(
                "configurator db_cpu must equal the pg_stand primary cpu_limit "
                f"({configured_cpu:g})"
            )
        configured_ram = int(parse_bytes(config.primary.memory_limit))
        requested_ram = int(normalized_inputs["ram_bytes"])
        if requested_ram != configured_ram:
            raise OrchestrationError(
                "configurator db_ram must equal the pg_stand primary memory_limit "
                f"({config.primary.memory_limit})"
            )

    @staticmethod
    def _validate_supported_stand(config: StandConfig) -> None:
        if config.postgres.tls.enabled:
            raise OrchestrationError(
                "pg_play/v1 does not yet orchestrate pg_stand TLS credentials for the "
                "dedicated workload role; use a non-TLS stand until workload client "
                "certificates are part of the component contract"
            )

    @staticmethod
    def _profile_args(manifest: ExperimentManifest) -> tuple[str, ...]:
        return tuple(
            argument
            for profile in manifest.workload.profiles
            for argument in ("--profile", profile)
        )

    @staticmethod
    def _connection_descriptor(
        manifest: ExperimentManifest,
        config: StandConfig,
    ) -> dict[str, Any]:
        passfile = manifest.artifact_root / "credentials" / "pgpass"
        return {
            "host": config.primary.bind_address,
            "port": config.primary.published_port,
            "database": manifest.workload.database,
            "workload_user": manifest.workload.user,
            "admin_user": config.postgres.superuser,
            "pg_major": config.postgres.version,
            "passfile": passfile,
        }

    @staticmethod
    def _workload_common_args(
        manifest: ExperimentManifest,
        connection: dict[str, Any],
    ) -> tuple[str, ...]:
        return (
            "--root",
            str(manifest.workload.project),
            "--target=external",
            "--host",
            str(connection["host"]),
            "--port",
            str(connection["port"]),
            "--database",
            str(connection["database"]),
            "--workload-user",
            str(connection["workload_user"]),
            "--admin-user",
            str(connection["admin_user"]),
            "--passfile",
            str(connection["passfile"]),
            "--pg-major",
            str(connection["pg_major"]),
            "--scale",
            str(manifest.workload.scale),
            "--resource-disk-max-used-pct",
            str(manifest.workload.resource_guard.disk_max_used_pct),
            "--resource-mem-min-available-pct",
            str(manifest.workload.resource_guard.mem_min_available_pct),
            "--resource-mem-min-available-mb",
            str(manifest.workload.resource_guard.mem_min_available_mb),
            "--resource-cpu-max-pct",
            str(manifest.workload.resource_guard.cpu_max_pct),
            "--resource-cpu-window-seconds",
            str(manifest.workload.resource_guard.cpu_window_seconds),
            "--resource-check-interval",
            str(manifest.workload.resource_guard.check_interval),
        )

    @staticmethod
    def _pgpass_escape(value: str) -> str:
        return value.replace("\\", "\\\\").replace(":", "\\:")

    @staticmethod
    def _partition_parameters(
        candidate_parameters: dict[str, Any],
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        stand_parameters = {
            name: value
            for name, value in candidate_parameters.items()
            if name not in MANAGED_POSTGRES_PARAMETERS
        }
        managed_parameters = {
            name: value
            for name, value in candidate_parameters.items()
            if name in MANAGED_POSTGRES_PARAMETERS
        }
        return stand_parameters, managed_parameters

    @staticmethod
    def _semantic_postgresql_parameters(parameters: dict[str, Any]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for name, value in parameters.items():
            if (
                isinstance(value, str)
                and len(value) >= 2
                and value.startswith("'")
                and value.endswith("'")
            ):
                result[name] = value[1:-1].replace("''", "'")
            else:
                result[name] = value
        return result

    @staticmethod
    def _compact_workload_plan(plan: dict[str, Any]) -> dict[str, Any]:
        profiles = [
            {
                "name": profile.get("name"),
                "profile_hash": profile.get("profile_hash"),
                "selected_jobs": profile.get("selected_jobs"),
            }
            for profile in plan.get("profiles", [])
        ]
        result = {
            key: plan.get(key)
            for key in (
                "schema_version",
                "operation",
                "plan_hash",
                "root",
                "target",
                "postgresql",
                "database_changes",
                "execution",
            )
        }
        result["profiles"] = profiles
        if "scheduler" in plan:
            result["scheduler"] = plan["scheduler"]
        return result

    def _credential_context(
        self,
        manifest: ExperimentManifest,
        config: StandConfig,
        connection: dict[str, Any],
    ) -> dict[str, str]:
        stand_paths = credential_paths(config.project_directory)
        stand_credentials = read_database_credentials(
            stand_paths.database,
            expected_superuser=config.postgres.superuser,
        )
        credential_root = manifest.artifact_root / "credentials"
        credential_root.mkdir(parents=True, exist_ok=True)
        os.chmod(credential_root, 0o700)
        workload_secret_path = credential_root / "workload-password"
        if workload_secret_path.exists():
            if workload_secret_path.is_symlink() or not workload_secret_path.is_file():
                raise OrchestrationError("workload credential path is not a regular file")
            if workload_secret_path.stat().st_mode & 0o077:
                raise OrchestrationError("workload credential file permissions must be 0600")
            workload_password = workload_secret_path.read_text(encoding="utf-8").strip()
            if not workload_password:
                raise OrchestrationError("workload credential file is empty")
        else:
            workload_password = secrets.token_urlsafe(32)
            write_text(workload_secret_path, workload_password + "\n")
        passfile = Path(connection["passfile"])
        endpoints = {
            (str(connection["host"]), int(connection["port"])),
            ("127.0.0.1", 5432),
        }
        lines = []
        for host, port in sorted(endpoints):
            for user, password in (
                (config.postgres.superuser, stand_credentials.superuser_password),
                (manifest.workload.user, workload_password),
            ):
                values = [host, str(port), "*", user, password]
                lines.append(":".join(self._pgpass_escape(value) for value in values))
        write_text(passfile, "\n".join(lines) + "\n")
        return {"workload_password": workload_password, "passfile": str(passfile)}

    @staticmethod
    def _connection_environment(
        config: StandConfig,
        workload_password: str | None,
    ) -> dict[str, str]:
        environment = {}
        if workload_password is not None:
            environment["WORKLOAD_PASSWORD"] = workload_password
        if config.postgres.tls.enabled:
            paths = credential_paths(config.project_directory).tls
            environment.update(
                {
                    "PGSSLMODE": "verify-full",
                    "PGSSLROOTCERT": str(paths / "ca.crt"),
                    "PGSSLCERT": str(paths / "postgres.crt"),
                    "PGSSLKEY": str(paths / "postgres.key"),
                }
            )
        return environment

    @staticmethod
    def _diagnostic_args(
        manifest: ExperimentManifest,
        config: StandConfig,
        connection: dict[str, Any],
        run_directory: Path,
    ) -> tuple[str, ...]:
        args = [
            manifest.diagnostics.mode,
            "--host",
            str(connection["host"]),
            "--port",
            str(connection["port"]),
            "--database",
            str(connection["database"]),
            "--user",
            str(connection["admin_user"]),
            "--passfile",
            str(connection["passfile"]),
            "--collection-mode",
            manifest.diagnostics.collection_mode,
            "--out",
            str(run_directory),
            "--json-out",
            str(run_directory / "report.json"),
            "--html-out",
            str(run_directory / "report.html"),
        ]
        if manifest.diagnostics.mode == "snapshots":
            args.extend(
                [
                    "--duration-seconds",
                    str(manifest.diagnostics.duration_seconds),
                    "--interval-seconds",
                    str(manifest.diagnostics.interval_seconds),
                ]
            )
        if manifest.diagnostics.collection_mode == "remote":
            paths = credential_paths(config.project_directory)
            known_hosts = run_directory / "ssh-known-hosts"
            PgPlayService._capture_ssh_host_key(
                config.primary.bind_address,
                config.primary.ssh_published_port,
                known_hosts,
            )
            args.extend(
                [
                    "--host",
                    "127.0.0.1",
                    "--port",
                    "5432",
                    "--ssh-host",
                    config.primary.bind_address,
                    "--ssh-port",
                    str(config.primary.ssh_published_port),
                    "--ssh-user",
                    "root",
                    "--ssh-key",
                    str(paths.ssh_private_key),
                    "--ssh-known-hosts",
                    str(known_hosts),
                ]
            )
        if config.postgres.tls.enabled:
            paths = credential_paths(config.project_directory).tls
            query = urlencode(
                {
                    "sslmode": "verify-full",
                    "sslrootcert": str(paths / "ca.crt"),
                    "sslcert": str(paths / "postgres.crt"),
                    "sslkey": str(paths / "postgres.key"),
                    "passfile": str(connection["passfile"]),
                }
            )
            host = (
                "127.0.0.1"
                if manifest.diagnostics.collection_mode == "remote"
                else connection["host"]
            )
            port = 5432 if manifest.diagnostics.collection_mode == "remote" else connection["port"]
            dsn = (
                f"postgresql://{quote(str(connection['admin_user']), safe='')}@"
                f"{host}:{port}/{quote(str(connection['database']), safe='')}?{query}"
            )
            filtered = []
            skip = 0
            for argument in args:
                if skip:
                    skip -= 1
                    continue
                if argument in {"--host", "--port", "--database", "--user", "--passfile"}:
                    skip = 1
                    continue
                filtered.append(argument)
            args = [filtered[0], "--dsn", dsn, *filtered[1:]]
        return tuple(args)

    @staticmethod
    def _capture_ssh_host_key(host: str, port: int, destination: Path) -> None:
        try:
            completed = subprocess.run(
                ["ssh-keyscan", "-T", "5", "-p", str(port), host],
                check=False,
                capture_output=True,
                text=True,
                timeout=10,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
            raise OrchestrationError(f"cannot capture pg_stand SSH host key: {exc}") from exc
        keys = [line for line in completed.stdout.splitlines() if line and not line.startswith("#")]
        if completed.returncode != 0 or not keys:
            detail = completed.stderr.strip() or "no host key returned"
            raise OrchestrationError(f"cannot capture pg_stand SSH host key: {detail}")
        write_text(destination, "\n".join(keys) + "\n")

    def _step(
        self,
        state: dict[str, Any],
        state_path: Path,
        name: str,
        envelope: dict[str, Any],
        statuses: set[str],
    ) -> None:
        self._require(envelope, statuses)
        state["steps"].append(self._step_record(name, envelope))
        write_state(state_path, state)

    @staticmethod
    def _step_record(name: str, envelope: dict[str, Any]) -> dict[str, Any]:
        return {
            "name": name,
            "component": envelope["component"],
            "command": envelope["command"],
            "status": envelope["status"],
            "artifacts": envelope.get("artifacts") or [],
            "warnings": envelope.get("warnings") or [],
            "error": envelope.get("error"),
        }
