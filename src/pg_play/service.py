"""Deterministic orchestration core shared by CLI, MCP, and tests."""

from __future__ import annotations

import hashlib
import os
import re
import secrets
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode

import yaml
from docker.utils import parse_bytes
from pg_diag.configuration_facts import CONFIGURATION_ITEM_IDS
from pg_diag.errors import ValidationError as PgDiagValidationError
from pg_stand.config import MANAGED_POSTGRES_PARAMETERS, StandConfig, load_config
from pg_stand.credentials import credential_paths
from pg_stand.database_credentials import read_database_credentials
from pg_stand.runtime_common import postgres_data_paths

from pg_play.configuration_review import (
    ConfigurationReviewError,
    build_configurator_inputs,
    compare_configuration,
    normalize_review_target,
    validate_configuration_candidate,
    write_comparison_artifacts,
)
from pg_play.configuration_review import (
    plan_configuration_review as build_configuration_review_plan,
)
from pg_play.contract import canonical_hash, validate_capabilities
from pg_play.live_diagnostics import LiveDiagnosticsError, LiveDiagnosticsManager
from pg_play.live_diagnostics import plan_live_diagnostics as build_live_diagnostics_plan
from pg_play.manifest import BenchmarkSpec, ExperimentManifest, load_manifest
from pg_play.report import compare_benchmark_summaries, compare_reports, inspect_report
from pg_play.runner import (
    ComponentCancelledError,
    ComponentInvocation,
    ComponentRunner,
    process_start_ticks,
    recorded_process_is_alive,
    terminate_recorded_process,
)
from pg_play.state import (
    RESUMABLE_STATES,
    RUN_STATE_SCHEMA_VERSION,
    TERMINAL_STATES,
    append_event,
    exclusive_lock,
    read_events,
    read_state,
    utc_now,
    write_json,
    write_state,
    write_text,
)

_RUN_ID_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,63}$")
_ACTIVE_STATES = {"queued", "running", "cancelling"}
_RESUMABLE_STEP_POLICIES = {
    "stand": "reconcile",
    "benchmark": "idempotent_database_reset",
    "prepare-db": "idempotent_database_prepare",
    "install-workload": "idempotent_profile_install",
    "start-workload": "desired_state",
    "diagnostics": "read_only",
    "stop-workload": "desired_state",
}
_REQUIRED_STEP_ARTIFACTS = {"benchmark", "diagnostics"}
_REQUIRED_COMPONENT_COMMANDS = {
    "pg_configurator": {"capabilities", "generate", "validate-input"},
    "pg_stand": {"apply", "capabilities", "down", "plan", "status", "up", "validate"},
    "pg_workload": {
        "capabilities",
        "install",
        "plan",
        "prepare-db",
        "start",
        "stop",
        "validate",
    },
    "pg_diag": {
        "capabilities",
        "configuration-facts",
        "explain-plan",
        "one-shot",
        "snapshots",
        "summarize",
        "validate",
        "validate-artifact",
    },
    "pg_perf_bench": {
        "benchmark",
        "capabilities",
        "join",
        "join-tasks",
        "plan",
        "profiles",
        "summarize",
        "validate",
        "validate-artifact",
    },
}


class OrchestrationError(RuntimeError):
    """An experiment cannot safely advance to the requested state."""


@dataclass(frozen=True)
class _ExecutionContext:
    run_id: str
    run_directory: Path
    state_path: Path
    events_path: Path
    cancel_path: Path
    active_process_path: Path


class PgPlayService:
    def __init__(self, runner: ComponentRunner | None = None) -> None:
        self.runner = runner or ComponentRunner()
        self._execution_context: _ExecutionContext | None = None

    def component_capabilities(self, component: str | None = None) -> dict[str, Any]:
        components = (
            [component]
            if component is not None
            else [
                "pg_configurator",
                "pg_stand",
                "pg_workload",
                "pg_diag",
                "pg_perf_bench",
            ]
        )
        unknown = sorted(set(components).difference(_REQUIRED_COMPONENT_COMMANDS))
        if unknown:
            raise OrchestrationError("unknown component(s): " + ", ".join(unknown))
        result = {}
        for name in components:
            envelope = self._invoke(
                name,
                ("--component-capabilities",),
                request_id=f"capabilities-{name}",
                timeout_seconds=30,
            )
            result[name] = validate_capabilities(
                envelope["result"],
                expected_component=name,
                required_commands=_REQUIRED_COMPONENT_COMMANDS[name],
            )
        return result

    @staticmethod
    def plan_live_diagnostics(
        target: dict[str, Any],
        intent: str = "performance",
        duration_seconds: float = 60,
        interval_seconds: float = 5,
    ) -> dict[str, Any]:
        return build_live_diagnostics_plan(
            target,
            intent,
            duration_seconds,
            interval_seconds,
        )

    def start_live_diagnostics(
        self,
        plan: dict[str, Any],
        plan_hash: str,
        output_directory: str | Path,
        capture_id: str,
    ) -> dict[str, Any]:
        try:
            return LiveDiagnosticsManager(self.runner).start(
                plan,
                plan_hash,
                output_directory,
                capture_id,
            )
        except LiveDiagnosticsError as exc:
            raise OrchestrationError(str(exc)) from exc

    def live_diagnostics_status(self, capture_directory: str | Path) -> dict[str, Any]:
        try:
            return LiveDiagnosticsManager(self.runner).status(capture_directory)
        except LiveDiagnosticsError as exc:
            raise OrchestrationError(str(exc)) from exc

    def live_diagnostics_events(
        self,
        capture_directory: str | Path,
        *,
        after_sequence: int = 0,
        limit: int = 1000,
    ) -> dict[str, Any]:
        try:
            return LiveDiagnosticsManager(self.runner).events(
                capture_directory,
                after_sequence=after_sequence,
                limit=limit,
            )
        except (LiveDiagnosticsError, ValueError) as exc:
            raise OrchestrationError(str(exc)) from exc

    def cancel_live_diagnostics(
        self,
        capture_directory: str | Path,
        *,
        reason: str | None = None,
    ) -> dict[str, Any]:
        try:
            return LiveDiagnosticsManager(self.runner).cancel(
                capture_directory,
                reason=reason,
            )
        except LiveDiagnosticsError as exc:
            raise OrchestrationError(str(exc)) from exc

    @staticmethod
    def plan_configuration_review(
        target: dict[str, Any],
        tuning_inputs: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return build_configuration_review_plan(target, tuning_inputs)

    def collect_configuration_facts(
        self,
        target: dict[str, Any],
        output_directory: str | Path,
        review_id: str,
    ) -> dict[str, Any]:
        if not _RUN_ID_RE.fullmatch(review_id):
            raise OrchestrationError("review_id contains unsupported characters")
        try:
            normalized, missing, errors = normalize_review_target(target)
        except (ConfigurationReviewError, PgDiagValidationError) as exc:
            raise OrchestrationError(str(exc)) from exc
        if missing or errors:
            details = [*(f"missing {field}" for field in missing), *errors]
            raise OrchestrationError("invalid review target: " + "; ".join(details))

        directory = Path(output_directory).expanduser().resolve()
        directory.mkdir(parents=True, exist_ok=True)
        report_path = directory / f"{review_id}-diagnostic.json"
        facts_path = directory / f"{review_id}-configuration-facts.json"
        database = normalized["database"]
        ssh = normalized["ssh"]
        arguments = [
            "one-shot",
            "--host",
            database["host"],
            "--port",
            str(database["port"]),
            "--database",
            database["database"],
            "--user",
            database["user"],
            "--collection-mode",
            "remote",
            "--ssh-host",
            ssh["host"],
            "--ssh-port",
            str(ssh["port"]),
            "--ssh-user",
            ssh["user"],
            "--ssh-key",
            ssh["key_path"],
            "--ssh-known-hosts",
            ssh["known_hosts_path"],
            "--item-id=[" + ",".join(CONFIGURATION_ITEM_IDS) + "]",
            "--out",
            str(directory),
            "--json-out",
            str(report_path),
            "--output-format=json",
        ]
        if database.get("passfile"):
            arguments.extend(("--passfile", database["passfile"]))
        if ssh.get("connect_timeout") is not None:
            arguments.extend(("--ssh-connect-timeout", str(ssh["connect_timeout"])))
        if ssh.get("key_passphrase_env"):
            arguments.extend(("--ssh-key-passphrase-env", ssh["key_passphrase_env"]))
        environment = None
        if ssh.get("key_passphrase_env"):
            environment_name = ssh["key_passphrase_env"]
            environment = {environment_name: os.environ[environment_name]}

        collection = self._require(
            self._invoke(
                "pg_diag",
                tuple(arguments),
                request_id=f"configuration-review-{review_id}-collect",
                environment=environment,
                timeout_seconds=120,
                cancellable=False,
            ),
            {"succeeded", "partial"},
        )
        facts_envelope = self._require(
            self._invoke(
                "pg_diag",
                ("configuration-facts", str(report_path), "--out", str(facts_path)),
                request_id=f"configuration-review-{review_id}-facts",
                timeout_seconds=30,
                cancellable=False,
            ),
            {"succeeded"},
        )
        return {
            "schema_version": "pg_play/configuration-facts-collection-v1",
            "review_id": review_id,
            "status": collection["status"],
            "report_path": str(report_path),
            "facts_path": str(facts_path),
            "facts": facts_envelope["result"],
            "artifacts": [
                *(collection.get("artifacts") or []),
                *(facts_envelope.get("artifacts") or []),
            ],
        }

    def generate_configuration_candidate(
        self,
        facts_path: str | Path,
        tuning_inputs: dict[str, Any],
        output_directory: str | Path,
        review_id: str,
    ) -> dict[str, Any]:
        if not _RUN_ID_RE.fullmatch(review_id):
            raise OrchestrationError("review_id contains unsupported characters")
        try:
            inputs, context = build_configurator_inputs(facts_path, tuning_inputs)
        except (ConfigurationReviewError, PgDiagValidationError) as exc:
            raise OrchestrationError(str(exc)) from exc
        envelope = self._require(
            self._invoke(
                "pg_configurator",
                ("--input-json=-", "--output-format=json"),
                request_id=f"configuration-review-{review_id}-candidate",
                input_document={"schema_version": "pg_configurator/input-v1", "inputs": inputs},
                timeout_seconds=30,
                cancellable=False,
            ),
            {"succeeded"},
        )
        try:
            candidate = validate_configuration_candidate(envelope["result"]["artifact"])
        except ConfigurationReviewError as exc:
            raise OrchestrationError(str(exc)) from exc
        directory = Path(output_directory).expanduser().resolve()
        candidate_path = directory / f"{review_id}-configuration-candidate.json"
        write_json(candidate_path, candidate)
        return {
            "schema_version": "pg_play/configuration-candidate-result-v1",
            "review_id": review_id,
            "facts_path": str(Path(facts_path).expanduser().resolve()),
            "candidate_path": str(candidate_path),
            "candidate_hash": candidate["artifact_hash"],
            "inputs": inputs,
            "derived_inputs": context["derived_inputs"],
            "resource_overrides": context["resource_overrides"],
            "warnings": envelope.get("warnings") or [],
        }

    @staticmethod
    def compare_configuration_candidate(
        facts_path: str | Path,
        candidate_path: str | Path,
        output_directory: str | Path,
        review_id: str,
    ) -> dict[str, Any]:
        try:
            comparison = compare_configuration(facts_path, candidate_path)
            json_path, markdown_path = write_comparison_artifacts(
                comparison, output_directory, review_id
            )
        except (ConfigurationReviewError, PgDiagValidationError) as exc:
            raise OrchestrationError(str(exc)) from exc
        return {
            "review_id": review_id,
            "comparison": comparison,
            "outputs": [str(json_path), str(markdown_path)],
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
        diagnostic_validation = self._require(
            self._invoke(
                "pg_diag",
                ("validate",),
                request_id=f"{manifest.experiment_id}-validate-diag-content",
            ),
            {"succeeded"},
        )
        diagnostic_plan = self._require(
            self._invoke(
                "pg_diag",
                (
                    "explain-plan",
                    "--pg-version",
                    str(base_config.postgres.version * 10_000),
                    "--run-mode",
                    manifest.diagnostics.mode,
                    "--collection-mode",
                    manifest.diagnostics.collection_mode,
                ),
                request_id=f"{manifest.experiment_id}-validate-diag-plan",
            ),
            {"succeeded"},
        )
        benchmark_validation = self._require(
            self._invoke(
                "pg_perf_bench",
                ("validate",),
                request_id=f"{manifest.experiment_id}-validate-benchmark-content",
            ),
            {"succeeded"},
        )
        benchmark_plan = None
        if manifest.phases.benchmark:
            assert manifest.benchmark is not None
            benchmark_plan = self._require(
                self._invoke(
                    "pg_perf_bench",
                    (
                        "plan",
                        *self._benchmark_args(
                            manifest.benchmark,
                            base_config,
                            manifest.artifact_root / ".benchmark-validation",
                        ),
                    ),
                    request_id=f"{manifest.experiment_id}-validate-benchmark-plan",
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
                "pg_diag": {
                    "content": diagnostic_validation,
                    "plan": diagnostic_plan,
                },
                "pg_perf_bench": {
                    "content": benchmark_validation,
                    "plan": benchmark_plan,
                },
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
                    *(("--recreate",) if manifest.phases.recreate_workload_database else ()),
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
                    *(
                        (
                            "--job-interval-seconds",
                            str(manifest.workload.job_interval_seconds),
                        )
                        if manifest.workload.job_interval_seconds is not None
                        else ()
                    ),
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
        benchmark_validation = self._require(
            self._invoke(
                "pg_perf_bench",
                ("validate",),
                request_id=f"{manifest.experiment_id}-plan-benchmark-content",
            ),
            {"succeeded"},
        )
        benchmark_plan = None
        if manifest.phases.benchmark:
            assert manifest.benchmark is not None
            benchmark_plan = self._require(
                self._invoke(
                    "pg_perf_bench",
                    (
                        "plan",
                        *self._benchmark_args(
                            manifest.benchmark,
                            resolved_config,
                            manifest.artifact_root / ".benchmark-plan",
                        ),
                    ),
                    request_id=f"{manifest.experiment_id}-plan-benchmark",
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
                *benchmark_validation.get("warnings", []),
                *(benchmark_plan.get("warnings", []) if benchmark_plan else []),
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
                "pg_perf_bench": benchmark_validation["component_version"],
            },
            "configuration": {
                "artifact_hash": config_artifact["artifact_hash"],
                "postgresql_version": resolved_config.postgres.version,
                "parameter_count": len(candidate_parameters),
                "stand_parameter_count": len(parameters),
                "parameters": candidate_parameters,
                "stand_managed_parameters": stand_managed_parameters,
            },
            "phases": {
                "benchmark": manifest.phases.benchmark,
                "workload_diagnostics": manifest.phases.workload_diagnostics,
                "recreate_workload_database": manifest.phases.recreate_workload_database,
            },
            "stand": stand_envelope["result"],
            "workload": {
                "prepare_db": self._compact_workload_plan(prepare_plan["result"]),
                "install": self._compact_workload_plan(install_plan["result"]),
                "scheduler": self._compact_workload_plan(scheduler_plan["result"]),
            },
            "diagnostics": diagnostic_plan["result"],
            "benchmark": benchmark_plan["result"] if benchmark_plan is not None else None,
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
        """Run synchronously for CLI compatibility using the durable state engine."""
        manifest, plan, state = self._create_run(
            manifest_path,
            plan_hash=plan_hash,
            run_id=run_id,
            allow_existing_success=True,
        )
        if state["state"] == "succeeded":
            return state
        return self._execute_experiment(manifest, plan, state, resume=False)

    def start_experiment(
        self,
        manifest_path: str | Path,
        *,
        plan_hash: str,
        run_id: str,
    ) -> dict[str, Any]:
        """Create a durable run and return immediately after starting its worker."""
        manifest, _plan, state = self._create_run(
            manifest_path,
            plan_hash=plan_hash,
            run_id=run_id,
            allow_existing_success=False,
        )
        context = self._run_context(manifest, run_id)
        with exclusive_lock(context.run_directory / "control.lock"):
            state = read_state(context.state_path)
            if state.get("state") != "queued" or context.cancel_path.exists():
                raise OrchestrationError(
                    f"run_id {run_id} changed state before its worker could start"
                )
            return self._spawn_worker(manifest, state, resume=False)

    def resume_experiment(
        self,
        manifest_path: str | Path,
        *,
        plan_hash: str,
        run_id: str,
    ) -> dict[str, Any]:
        """Resume only a verified failed, cancelled, or interrupted durable run."""
        self._validate_run_id(run_id)
        manifest = load_manifest(manifest_path)
        context = self._run_context(manifest, run_id)
        if not context.state_path.is_file():
            raise OrchestrationError(f"run_id {run_id} does not exist")
        with exclusive_lock(context.run_directory / "control.lock"):
            return self._resume_experiment_locked(
                manifest,
                context,
                plan_hash=plan_hash,
            )

    def _resume_experiment_locked(
        self,
        manifest: ExperimentManifest,
        context: _ExecutionContext,
        *,
        plan_hash: str,
    ) -> dict[str, Any]:
        run_id = context.run_id
        state = self._reconcile_worker(context)
        if state.get("state") not in RESUMABLE_STATES:
            raise OrchestrationError(
                f"run_id {run_id} is not resumable from state {state.get('state')!r}"
            )
        worker = state.get("worker")
        if isinstance(worker, dict) and recorded_process_is_alive(worker):
            raise OrchestrationError(
                f"run_id {run_id} still has a live worker finishing its current attempt"
            )
        if state.get("plan_hash") != plan_hash:
            raise OrchestrationError(
                f"resume plan hash mismatch: state has {state.get('plan_hash')}, "
                f"request supplied {plan_hash}"
            )
        if state.get("manifest_hash") != manifest.document_hash:
            raise OrchestrationError("experiment manifest changed since the run was created")
        plan = self._load_stored_plan(context, plan_hash)
        self._validate_core_artifacts(
            manifest,
            plan,
            state,
            expected_run_id=context.run_id,
        )
        self._validate_component_versions(plan)
        self._validate_resumable_steps(state, context.run_directory)
        if terminate_recorded_process(context.active_process_path):
            self._event(
                context,
                "orphan_component_terminated",
                state=state.get("state"),
            )
        self._archive_cancel_request(context, int(state.get("attempt", 1)))
        state["attempt"] = int(state.get("attempt", 1)) + 1
        state["state"] = "queued"
        state["worker"] = None
        state["error"] = None
        state["resumed_at"] = utc_now()
        write_state(context.state_path, state)
        self._event(
            context,
            "resume_requested",
            state="queued",
            data={"attempt": state["attempt"]},
        )
        return self._spawn_worker(manifest, state, resume=True)

    def experiment_events(
        self,
        manifest_path: str | Path,
        run_id: str,
        *,
        after_sequence: int = 0,
        limit: int = 1000,
    ) -> dict[str, Any]:
        self._validate_run_id(run_id)
        manifest = load_manifest(manifest_path)
        context = self._run_context(manifest, run_id)
        if read_state(context.state_path).get("state") == "not_found":
            raise OrchestrationError(f"run_id {run_id} does not exist")
        return {
            "schema_version": "pg_play/run-events-v1",
            "experiment_id": manifest.experiment_id,
            "run_id": run_id,
            **read_events(
                context.events_path,
                after_sequence=after_sequence,
                limit=limit,
            ),
        }

    def cancel_experiment(
        self,
        manifest_path: str | Path,
        run_id: str,
        *,
        reason: str | None = None,
    ) -> dict[str, Any]:
        """Request cooperative cancellation without trusting an unowned PID."""
        self._validate_run_id(run_id)
        manifest = load_manifest(manifest_path)
        context = self._run_context(manifest, run_id)
        if not context.state_path.is_file():
            raise OrchestrationError(f"run_id {run_id} does not exist")
        with exclusive_lock(context.run_directory / "control.lock"):
            return self._cancel_experiment_locked(manifest, context, reason=reason)

    def _cancel_experiment_locked(
        self,
        manifest: ExperimentManifest,
        context: _ExecutionContext,
        *,
        reason: str | None,
    ) -> dict[str, Any]:
        run_id = context.run_id
        state = self._reconcile_worker(context)
        current = state.get("state")
        if current == "cancelled":
            return state
        if current in TERMINAL_STATES:
            raise OrchestrationError(f"run_id {run_id} is already terminal: {current}")
        clean_reason = (reason or "requested by operator").strip()
        if not clean_reason or len(clean_reason) > 500:
            raise OrchestrationError("cancellation reason must contain 1 to 500 characters")
        request = {
            "schema_version": "pg_play/cancel-request-v1",
            "run_id": run_id,
            "requested_at": utc_now(),
            "reason": clean_reason,
        }
        if not context.cancel_path.exists():
            write_json(context.cancel_path, request)
            self._event(
                context,
                "cancellation_requested",
                state="cancelling",
                data={"reason": clean_reason},
            )
        else:
            request = read_state(context.cancel_path)
        worker = state.get("worker")
        if not isinstance(worker, dict) or not recorded_process_is_alive(worker):
            terminate_recorded_process(context.active_process_path)
            try:
                self._stop_workload_after_lost_worker(manifest, state, context)
            except Exception as exc:
                self._fail_running_step(state, context, "failed", str(exc))
                state["state"] = "failed"
                state["cancellation"] = request
                state["error"] = {
                    "code": "cancellation_cleanup_failed",
                    "message": f"workload cleanup failed after worker loss: {exc}",
                }
                write_state(context.state_path, state)
                self._event(
                    context,
                    "cancellation_cleanup_failed",
                    state="failed",
                    data={"message": str(exc)},
                )
                return state
            state["state"] = "cancelled"
            state["cancellation"] = request
            state["error"] = {
                "code": "cancelled",
                "message": str(request.get("reason") or clean_reason),
            }
            write_state(context.state_path, state)
            self._event(context, "run_cancelled", state="cancelled")
            return state
        response = dict(state)
        response["effective_state"] = "cancelling"
        response["cancellation"] = request
        return response

    def _stop_workload_after_lost_worker(
        self,
        manifest: ExperimentManifest,
        state: dict[str, Any],
        context: _ExecutionContext,
    ) -> None:
        if not manifest.workload.stop_after_report:
            return
        start_status = self._latest_step_status(state, "start-workload")
        stop_status = self._latest_step_status(state, "stop-workload")
        if start_status not in {"running", "succeeded", "partial"} or stop_status == "succeeded":
            return
        previous_context = self._execution_context
        self._execution_context = context
        try:
            self._begin_step(state, context, "stop-workload", cancellable=False)
            stop_result = self._invoke(
                "pg_workload",
                ("stop", "--root", str(manifest.workload.project)),
                request_id=f"{manifest.experiment_id}-{context.run_id}-cancel-stop-workload",
                cancellable=False,
            )
            self._step(
                state,
                context.state_path,
                "stop-workload",
                stop_result,
                {"succeeded", "blocked"},
            )
        finally:
            self._execution_context = previous_context

    def _execute_experiment(
        self,
        manifest: ExperimentManifest,
        plan: dict[str, Any],
        state: dict[str, Any],
        *,
        resume: bool,
    ) -> dict[str, Any]:
        run_id = str(state["run_id"])
        context = self._run_context(manifest, run_id)
        run_directory = context.run_directory
        state_path = context.state_path
        self._execution_context = context
        state = read_state(state_path)
        state["state"] = "running"
        state["worker"] = {
            "pid": os.getpid(),
            "process_start_ticks": process_start_ticks(os.getpid()),
            "started_at": utc_now(),
            "mode": "background" if os.environ.get("PG_PLAY_WORKER") == "1" else "synchronous",
        }
        write_state(state_path, state)
        self._event(
            context,
            "run_resumed" if resume else "run_started",
            state="running",
            data={"attempt": state.get("attempt", 1)},
        )
        workload_stop_needed = self._latest_step_status(state, "start-workload") in {
            "running",
            "succeeded",
        }
        partial_result = any(
            self._latest_step_status(state, name) == "partial"
            for name in ("benchmark", "diagnostics")
        )
        try:
            candidate_parameters = plan["configuration"]["parameters"]
            parameters, _stand_managed = self._partition_parameters(candidate_parameters)
            config = load_config(
                manifest.stand_config,
                project_directory=manifest.stand_project,
                postgres_parameters=parameters,
            )
            expected_stand_hash = plan["stand"].get("desired_state_hash")
            if not isinstance(expected_stand_hash, str):
                raise OrchestrationError("stored experiment plan has no stand desired-state hash")
            if config.config_hash != expected_stand_hash:
                raise OrchestrationError(
                    "stand desired configuration changed since the experiment was planned"
                )
            connection = self._connection_descriptor(manifest, config)
            stand_completed = self._step_completed(state, "stand", run_directory)
            stand_plan = plan["stand"]
            if resume:
                stand_plan = self._require(
                    self._invoke(
                        "pg_stand",
                        (
                            "--config",
                            str(manifest.stand_config),
                            "--parameters-json=-",
                            "plan",
                        ),
                        request_id=f"{manifest.experiment_id}-{run_id}-resume-plan-stand",
                        input_document=parameters,
                        cwd=manifest.stand_project,
                    ),
                    {"succeeded", "blocked"},
                )["result"]
                if stand_plan.get("desired_state_hash") != expected_stand_hash:
                    raise OrchestrationError(
                        "stand desired configuration changed since the experiment was planned"
                    )
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
            stand_needs_reconcile = resume and required_action != "none"
            if not stand_completed or stand_needs_reconcile:
                self._begin_step(state, context, "stand")
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
            elif resume:
                self._event(context, "step_reused", state="running", step="stand")
            secret_context = self._credential_context(manifest, config, connection)
            if manifest.phases.benchmark:
                assert manifest.benchmark is not None
                if not self._step_completed(state, "benchmark", run_directory):
                    self._begin_step(state, context, "benchmark")
                    benchmark = self._invoke(
                        "pg_perf_bench",
                        (
                            *self._benchmark_args(
                                manifest.benchmark,
                                config,
                                run_directory / "benchmark",
                            ),
                            "--plan-hash",
                            plan["benchmark"]["plan_hash"],
                        ),
                        request_id=f"{manifest.experiment_id}-{run_id}-benchmark",
                        environment={
                            **self._connection_environment(config, None),
                            "PGPASSWORD": secret_context["admin_password"],
                        },
                        timeout_seconds=max(
                            900,
                            manifest.benchmark.command_timeout
                            * (len(manifest.benchmark.iterations) * 2 + 2),
                        ),
                    )
                    self._step(
                        state,
                        state_path,
                        "benchmark",
                        benchmark,
                        {"succeeded", "partial"},
                    )
                    state["artifacts"].extend(benchmark.get("artifacts") or [])
                    partial_result = partial_result or benchmark["status"] == "partial"
                    write_state(state_path, state)
                elif resume:
                    self._event(context, "step_reused", state="running", step="benchmark")
            if not manifest.phases.workload_diagnostics:
                self._raise_if_cancelled(context, "experiment completion")
                state["state"] = "partial" if partial_result else "succeeded"
                write_state(state_path, state)
                return state

            common_args = self._workload_common_args(manifest, connection)
            environment = self._connection_environment(config, secret_context["workload_password"])
            prepare_args = [
                "prepare-db",
                *common_args,
            ]
            if manifest.phases.recreate_workload_database:
                prepare_args.append("--recreate")
            prepare_args.extend(
                (
                    "--plan-hash",
                    plan["workload"]["prepare_db"]["plan_hash"],
                )
            )
            if not self._step_completed(state, "prepare-db", run_directory):
                self._begin_step(state, context, "prepare-db")
                self._step(
                    state,
                    state_path,
                    "prepare-db",
                    self._invoke(
                        "pg_workload",
                        tuple(prepare_args),
                        request_id=f"{manifest.experiment_id}-{run_id}-prepare-db",
                        environment=environment,
                        timeout_seconds=900,
                    ),
                    {"succeeded"},
                )
            elif resume:
                self._event(context, "step_reused", state="running", step="prepare-db")
            if manifest.workload.install and not self._step_completed(
                state, "install-workload", run_directory
            ):
                self._begin_step(state, context, "install-workload")
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
            elif manifest.workload.install and resume:
                self._event(context, "step_reused", state="running", step="install-workload")
            workload_stop_needed = True
            diagnostics_complete = self._step_completed(state, "diagnostics", run_directory)
            should_start_workload = not diagnostics_complete and (
                not self._step_completed(state, "start-workload", run_directory) or resume
            )
            if should_start_workload:
                self._begin_step(state, context, "start-workload")
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
                            *(
                                (
                                    "--job-interval-seconds",
                                    str(manifest.workload.job_interval_seconds),
                                )
                                if manifest.workload.job_interval_seconds is not None
                                else ()
                            ),
                            "--plan-hash",
                            plan["workload"]["scheduler"]["plan_hash"],
                        ),
                        request_id=f"{manifest.experiment_id}-{run_id}-start-workload",
                        environment=environment,
                    ),
                    {"running"},
                )
            elif resume:
                self._event(context, "step_reused", state="running", step="start-workload")
            if not diagnostics_complete:
                self._begin_step(state, context, "diagnostics")
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
                partial_result = partial_result or diagnostic["status"] == "partial"
            elif resume:
                self._event(context, "step_reused", state="running", step="diagnostics")
            self._raise_if_cancelled(context, "experiment completion")
            state["state"] = "partial" if partial_result else "succeeded"
            write_state(state_path, state)
            return state
        except ComponentCancelledError as exc:
            cancellation = (
                read_state(context.cancel_path)
                if context.cancel_path.exists()
                else {
                    "schema_version": "pg_play/cancel-request-v1",
                    "requested_at": utc_now(),
                    "reason": str(exc),
                }
            )
            self._fail_running_step(state, context, "cancelled", str(exc))
            state["state"] = "cancelled"
            state["cancellation"] = cancellation
            state["error"] = {"code": "cancelled", "message": str(exc)}
            write_state(state_path, state)
            self._event(context, "run_cancelled", state="cancelled")
            return state
        except Exception as exc:
            self._fail_running_step(state, context, "failed", str(exc))
            state["state"] = "failed"
            state["error"] = {"code": "orchestration_error", "message": str(exc)}
            write_state(state_path, state)
            self._event(
                context,
                "run_failed",
                state="failed",
                data={"message": str(exc)},
            )
            raise
        finally:
            if workload_stop_needed and manifest.workload.stop_after_report:
                try:
                    self._begin_step(state, context, "stop-workload", cancellable=False)
                    stop_result = self._invoke(
                        "pg_workload",
                        ("stop", "--root", str(manifest.workload.project)),
                        request_id=f"{manifest.experiment_id}-{run_id}-stop-workload",
                        cancellable=False,
                    )
                    self._step(
                        state,
                        state_path,
                        "stop-workload",
                        stop_result,
                        {"succeeded", "blocked"},
                    )
                    if (
                        stop_result["status"] not in {"succeeded", "blocked"}
                        and state["state"] == "succeeded"
                    ):
                        state["state"] = "partial"
                        state["error"] = {
                            "code": "workload_stop_failed",
                            "message": (
                                "diagnostics completed, but the workload could not be stopped"
                            ),
                        }
                except Exception as stop_error:
                    self._fail_running_step(state, context, "failed", str(stop_error))
                    if state["state"] == "succeeded":
                        state["state"] = "partial"
                        state["error"] = {
                            "code": "workload_stop_failed",
                            "message": (
                                "diagnostics completed, but the workload could not be stopped"
                            ),
                        }
            state["worker"] = None
            write_state(state_path, state)
            if state.get("state") in {"succeeded", "partial"}:
                self._event(context, "run_finished", state=state["state"])
            self._execution_context = None

    def experiment_status(self, manifest_path: str | Path, run_id: str) -> dict[str, Any]:
        self._validate_run_id(run_id)
        manifest = load_manifest(manifest_path)
        context = self._run_context(manifest, run_id)
        if not context.state_path.is_file():
            return read_state(context.state_path)
        with exclusive_lock(context.run_directory / "control.lock"):
            return self._reconcile_worker(context)

    @staticmethod
    def _validate_run_id(run_id: str) -> None:
        if not _RUN_ID_RE.fullmatch(run_id):
            raise OrchestrationError(f"run_id must match {_RUN_ID_RE.pattern}")

    @staticmethod
    def _run_context(manifest: ExperimentManifest, run_id: str) -> _ExecutionContext:
        run_directory = manifest.artifact_root / run_id
        return _ExecutionContext(
            run_id=run_id,
            run_directory=run_directory,
            state_path=run_directory / "state.json",
            events_path=run_directory / "events.jsonl",
            cancel_path=run_directory / "cancel.request.json",
            active_process_path=run_directory / "active-process.json",
        )

    @staticmethod
    def _event(
        context: _ExecutionContext,
        event_type: str,
        *,
        state: str | None = None,
        step: str | None = None,
        data: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return append_event(
            context.events_path,
            run_id=context.run_id,
            event_type=event_type,
            state=state,
            step=step,
            data=data,
        )

    def _create_run(
        self,
        manifest_path: str | Path,
        *,
        plan_hash: str,
        run_id: str,
        allow_existing_success: bool,
    ) -> tuple[ExperimentManifest, dict[str, Any], dict[str, Any]]:
        self._validate_run_id(run_id)
        manifest = load_manifest(manifest_path)
        context = self._run_context(manifest, run_id)
        existing = read_state(context.state_path)
        if (
            allow_existing_success
            and existing.get("state") == "succeeded"
            and existing.get("plan_hash") == plan_hash
        ):
            return manifest, self._load_stored_plan(context, plan_hash), existing
        if existing.get("state") != "not_found":
            hint = (
                "; use resume-experiment for a failed, cancelled, or interrupted run"
                if existing.get("state") in RESUMABLE_STATES
                else ""
            )
            raise OrchestrationError(
                f"run_id {run_id} already exists in state {existing.get('state')!r}{hint}"
            )
        plan = self.plan_experiment(manifest.source)
        if plan["plan_hash"] != plan_hash:
            raise OrchestrationError(
                f"stale experiment plan: expected {plan_hash}, current plan is {plan['plan_hash']}"
            )
        context.run_directory.mkdir(parents=True, exist_ok=False)
        created_at = utc_now()
        state: dict[str, Any] = {
            "schema_version": RUN_STATE_SCHEMA_VERSION,
            "experiment_id": manifest.experiment_id,
            "run_id": run_id,
            "plan_hash": plan_hash,
            "manifest_hash": manifest.document_hash,
            "state": "queued",
            "attempt": 1,
            "created_at": created_at,
            "updated_at": created_at,
            "worker": None,
            "steps": [],
            "artifacts": [],
            "cancellation": None,
            "error": None,
        }
        manifest_snapshot = context.run_directory / "experiment.yaml"
        plan_path = context.run_directory / "plan.json"
        candidate_path = context.run_directory / "postgresql-parameters.json"
        write_text(manifest_snapshot, manifest.source.read_text(encoding="utf-8"))
        write_json(plan_path, plan)
        candidate_parameters = plan["configuration"]["parameters"]
        write_json(candidate_path, candidate_parameters)
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
                {
                    "kind": "PostgreSQLParameters",
                    "path": str(candidate_path),
                    "hash": canonical_hash(candidate_parameters),
                },
                {
                    "kind": "ExperimentEvents",
                    "path": str(context.events_path),
                },
                {
                    "kind": "WorkerLog",
                    "path": str(context.run_directory / "worker.log"),
                },
            ]
        )
        write_state(context.state_path, state)
        self._event(
            context,
            "run_created",
            state="queued",
            data={"attempt": 1, "plan_hash": plan_hash},
        )
        return manifest, plan, state

    @staticmethod
    def _load_stored_plan(
        context: _ExecutionContext,
        expected_plan_hash: str,
    ) -> dict[str, Any]:
        path = context.run_directory / "plan.json"
        plan = read_state(path)
        if plan.get("state") == "not_found" or plan.get("plan_hash") != expected_plan_hash:
            raise OrchestrationError("stored experiment plan is missing or has the wrong hash")
        unhashed = dict(plan)
        unhashed.pop("plan_hash", None)
        if canonical_hash(unhashed) != expected_plan_hash:
            raise OrchestrationError("stored experiment plan content failed hash verification")
        return plan

    def _validate_core_artifacts(
        self,
        manifest: ExperimentManifest,
        plan: dict[str, Any],
        state: dict[str, Any],
        *,
        expected_run_id: str,
    ) -> None:
        if state.get("schema_version") != RUN_STATE_SCHEMA_VERSION:
            raise OrchestrationError("run state schema is not recoverable by this pg_play version")
        if state.get("run_id") != expected_run_id:
            raise OrchestrationError("durable run state has the wrong run_id")
        if state.get("experiment_id") != manifest.experiment_id:
            raise OrchestrationError("durable run state has the wrong experiment_id")
        if plan.get("experiment_id") != manifest.experiment_id:
            raise OrchestrationError("stored experiment plan has the wrong experiment_id")
        if plan.get("manifest_hash") != state.get("manifest_hash"):
            raise OrchestrationError("stored experiment plan has the wrong manifest hash")
        context = self._run_context(manifest, expected_run_id)
        manifest_path = context.run_directory / "experiment.yaml"
        parameters_path = context.run_directory / "postgresql-parameters.json"
        try:
            manifest_document = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError) as exc:
            raise OrchestrationError(
                f"stored experiment manifest cannot be verified: {exc}"
            ) from exc
        if canonical_hash(manifest_document) != state["manifest_hash"]:
            raise OrchestrationError("stored experiment manifest failed hash verification")
        if manifest.document_hash != state["manifest_hash"]:
            raise OrchestrationError("current experiment manifest differs from the stored manifest")
        if plan["configuration"]["parameters"] != read_state(parameters_path):
            raise OrchestrationError("stored PostgreSQL parameter artifact failed verification")
        parameters, _stand_managed = self._partition_parameters(plan["configuration"]["parameters"])
        current_config = load_config(
            manifest.stand_config,
            project_directory=manifest.stand_project,
            postgres_parameters=parameters,
        )
        if current_config.config_hash != plan["stand"].get("desired_state_hash"):
            raise OrchestrationError(
                "stand desired configuration changed since the experiment was planned"
            )

    def _validate_component_versions(self, plan: dict[str, Any]) -> None:
        for component, expected_version in plan["components"].items():
            envelope = self._require(
                self._invoke(
                    component,
                    ("--component-capabilities",),
                    request_id=f"resume-version-{component}",
                    timeout_seconds=30,
                ),
                {"succeeded"},
            )
            if envelope["component_version"] != expected_version:
                raise OrchestrationError(
                    f"component version changed for {component}: plan requires "
                    f"{expected_version}, installed version is {envelope['component_version']}"
                )

    def _validate_resumable_steps(
        self,
        state: dict[str, Any],
        run_directory: Path,
    ) -> None:
        changed = False
        for step in state.get("steps") or []:
            name = str(step.get("name") or "")
            expected_policy = _RESUMABLE_STEP_POLICIES.get(name)
            if expected_policy is None:
                raise OrchestrationError(
                    f"run contains a step with no safe resume policy: {name!r}"
                )
            if step.get("resume_policy") != expected_policy:
                raise OrchestrationError(f"run step {name!r} has an invalid safe resume policy")
            if step.get("status") in {"succeeded", "partial", "running"}:
                artifacts = step.get("artifacts") or []
                valid, problems = self._artifacts_valid(artifacts, run_directory)
                if name in _REQUIRED_STEP_ARTIFACTS and not artifacts:
                    valid = False
                    problems.append("completed report step recorded no artifacts")
                step["resume_validation"] = {
                    "checked_at": utc_now(),
                    "valid": valid,
                    "problems": problems,
                }
                changed = True
        if changed:
            context = _ExecutionContext(
                run_id=str(state["run_id"]),
                run_directory=run_directory,
                state_path=run_directory / "state.json",
                events_path=run_directory / "events.jsonl",
                cancel_path=run_directory / "cancel.request.json",
                active_process_path=run_directory / "active-process.json",
            )
            write_state(context.state_path, state)
            for step in state.get("steps") or []:
                validation = step.get("resume_validation") or {}
                if not validation.get("valid", True):
                    self._event(
                        context,
                        "step_artifacts_invalidated",
                        state=state.get("state"),
                        step=step.get("name"),
                        data={"problems": validation.get("problems") or []},
                    )

    @staticmethod
    def _artifacts_valid(
        artifacts: list[dict[str, Any]],
        run_directory: Path,
    ) -> tuple[bool, list[str]]:
        problems: list[str] = []
        for artifact in artifacts:
            raw_path = artifact.get("path")
            if not isinstance(raw_path, str) or not raw_path:
                problems.append("artifact has no path")
                continue
            path = Path(raw_path)
            if not path.is_absolute():
                path = run_directory / path
            if not path.exists():
                problems.append(f"missing artifact: {path}")
                continue
            expected_size = artifact.get("size")
            if (
                expected_size is not None
                and path.is_file()
                and path.stat().st_size != expected_size
            ):
                problems.append(f"artifact size mismatch: {path}")
            expected_hash = artifact.get("sha256") or artifact.get("hash")
            if (
                isinstance(expected_hash, str)
                and expected_hash.startswith("sha256:")
                and path.is_file()
            ):
                actual = "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
                if actual != expected_hash:
                    problems.append(f"artifact hash mismatch: {path}")
        return not problems, problems

    @staticmethod
    def _latest_step(state: dict[str, Any], name: str) -> dict[str, Any] | None:
        return next(
            (step for step in reversed(state.get("steps") or []) if step.get("name") == name),
            None,
        )

    @classmethod
    def _latest_step_status(cls, state: dict[str, Any], name: str) -> str | None:
        step = cls._latest_step(state, name)
        return str(step.get("status")) if step is not None else None

    def _step_completed(
        self,
        state: dict[str, Any],
        name: str,
        run_directory: Path,
    ) -> bool:
        step = self._latest_step(state, name)
        if step is None or step.get("status") not in {"succeeded", "partial"}:
            return False
        if name in _REQUIRED_STEP_ARTIFACTS and not step.get("artifacts"):
            return False
        validation = step.get("resume_validation")
        if isinstance(validation, dict) and validation.get("valid") is False:
            return False
        valid, _problems = self._artifacts_valid(step.get("artifacts") or [], run_directory)
        return valid

    def _begin_step(
        self,
        state: dict[str, Any],
        context: _ExecutionContext,
        name: str,
        *,
        cancellable: bool = True,
    ) -> None:
        policy = _RESUMABLE_STEP_POLICIES.get(name)
        if policy is None:
            raise OrchestrationError(f"step {name!r} has no safe resume policy")
        if cancellable and context.cancel_path.exists():
            raise ComponentCancelledError(f"cancelled before step {name}")
        state["steps"].append(
            {
                "name": name,
                "component": None,
                "command": None,
                "status": "running",
                "attempt": state.get("attempt", 1),
                "resume_policy": policy,
                "started_at": utc_now(),
                "finished_at": None,
                "artifacts": [],
                "warnings": [],
                "error": None,
            }
        )
        write_state(context.state_path, state)
        self._event(context, "step_started", state="running", step=name)

    @staticmethod
    def _raise_if_cancelled(context: _ExecutionContext, operation: str) -> None:
        if context.cancel_path.exists():
            raise ComponentCancelledError(f"cancelled before {operation}")

    def _fail_running_step(
        self,
        state: dict[str, Any],
        context: _ExecutionContext,
        status: str,
        message: str,
    ) -> None:
        step = next(
            (
                item
                for item in reversed(state.get("steps") or [])
                if item.get("status") == "running"
            ),
            None,
        )
        if step is None:
            return
        step["status"] = status
        step["finished_at"] = utc_now()
        step["error"] = {"code": status, "message": message}
        write_state(context.state_path, state)
        self._event(
            context,
            "step_cancelled" if status == "cancelled" else "step_failed",
            state=state.get("state"),
            step=step.get("name"),
            data={"message": message},
        )

    def _spawn_worker(
        self,
        manifest: ExperimentManifest,
        state: dict[str, Any],
        *,
        resume: bool,
    ) -> dict[str, Any]:
        context = self._run_context(manifest, str(state["run_id"]))
        gate = context.run_directory / "worker.starting"
        log_path = context.run_directory / "worker.log"
        write_text(gate, "wait\n")
        command = [
            sys.executable,
            "-m",
            "pg_play.worker",
            "--manifest",
            str(manifest.source),
            "--plan-hash",
            str(state["plan_hash"]),
            "--run-id",
            str(state["run_id"]),
            "--start-gate",
            str(gate),
        ]
        if resume:
            command.append("--resume")
        environment = os.environ.copy()
        environment["PG_PLAY_WORKER"] = "1"
        descriptor: int | None = None
        process: subprocess.Popen[bytes] | None = None
        try:
            descriptor = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            os.fchmod(descriptor, 0o600)
            worker_log = os.fdopen(descriptor, "ab")
            descriptor = None
            with worker_log:
                process = subprocess.Popen(
                    command,
                    stdin=subprocess.DEVNULL,
                    stdout=worker_log,
                    stderr=subprocess.STDOUT,
                    env=environment,
                    start_new_session=True,
                    close_fds=True,
                )
            state = read_state(context.state_path)
            state["state"] = "queued"
            state["worker"] = {
                "pid": process.pid,
                "process_start_ticks": process_start_ticks(process.pid),
                "started_at": utc_now(),
                "mode": "background",
            }
            write_state(context.state_path, state)
            self._event(
                context,
                "worker_started",
                state="queued",
                data={"pid": process.pid, "attempt": state.get("attempt", 1)},
            )
            return state
        except Exception as exc:
            if process is not None and process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
            state = read_state(context.state_path)
            state["state"] = "failed"
            state["worker"] = None
            state["error"] = {"code": "worker_start_failed", "message": str(exc)}
            write_state(context.state_path, state)
            self._event(
                context,
                "worker_start_failed",
                state="failed",
                data={"message": str(exc)},
            )
            raise OrchestrationError(f"cannot start experiment worker: {exc}") from exc
        finally:
            if descriptor is not None:
                os.close(descriptor)
            gate.unlink(missing_ok=True)

    def _reconcile_worker(self, context: _ExecutionContext) -> dict[str, Any]:
        state = read_state(context.state_path)
        if state.get("state") == "not_found" or state.get("state") not in _ACTIVE_STATES:
            return state
        worker = state.get("worker")
        if isinstance(worker, dict) and recorded_process_is_alive(worker):
            if context.cancel_path.exists():
                result = dict(state)
                result["effective_state"] = "cancelling"
                result["cancellation"] = read_state(context.cancel_path)
                return result
            return state
        state["state"] = "interrupted"
        state["worker"] = None
        state["error"] = {
            "code": "worker_lost",
            "message": "experiment worker is no longer running; verify and resume the run",
        }
        write_state(context.state_path, state)
        self._event(context, "worker_lost", state="interrupted")
        return state

    @staticmethod
    def _archive_cancel_request(context: _ExecutionContext, attempt: int) -> None:
        if not context.cancel_path.exists():
            return
        destination = context.run_directory / f"cancel.request.attempt-{attempt}.json"
        if destination.exists():
            raise OrchestrationError(f"archived cancellation request already exists: {destination}")
        context.cancel_path.replace(destination)

    def teardown_experiment(
        self,
        manifest_path: str | Path,
        *,
        clear_stand_data: bool = False,
    ) -> dict[str, Any]:
        """Stop owned workload processes and remove the manifest-owned stand."""
        manifest = load_manifest(manifest_path)
        workload = self._invoke(
            "pg_workload",
            ("stop", "--root", str(manifest.workload.project)),
            request_id=f"{manifest.experiment_id}-teardown-workload",
        )
        if workload["status"] not in {"succeeded", "blocked"}:
            self._require(workload, {"succeeded", "blocked"})
        arguments = ["--config", str(manifest.stand_config), "down"]
        if clear_stand_data:
            arguments.append("--clear-data")
        stand = self._require(
            self._invoke(
                "pg_stand",
                tuple(arguments),
                request_id=f"{manifest.experiment_id}-teardown-stand",
                cwd=manifest.stand_project,
                timeout_seconds=900,
            ),
            {"succeeded"},
        )
        return {
            "experiment_id": manifest.experiment_id,
            "clear_stand_data": clear_stand_data,
            "workload": workload,
            "stand": stand,
        }

    @staticmethod
    def inspect_report(path: str | Path) -> dict[str, Any]:
        return inspect_report(path)

    @staticmethod
    def compare_reports(baseline: str | Path, candidate: str | Path) -> dict[str, Any]:
        return compare_reports(baseline, candidate)

    def inspect_benchmark_report(self, path: str | Path) -> dict[str, Any]:
        report_path = Path(path).expanduser().resolve()
        envelope = self._require(
            self._invoke(
                "pg_perf_bench",
                ("summarize", str(report_path)),
                request_id=f"inspect-benchmark-{canonical_hash(str(report_path))[7:19]}",
            ),
            {"succeeded"},
        )
        return {
            "path": str(report_path),
            "summary": envelope["result"],
            "artifacts": envelope.get("artifacts") or [],
        }

    def benchmark_profiles(self) -> dict[str, Any]:
        """Return the installed pg_perf_bench maximum-TPS profile catalog."""
        envelope = self._require(
            self._invoke(
                "pg_perf_bench",
                ("profiles",),
                request_id="benchmark-profiles",
            ),
            {"succeeded"},
        )
        return envelope["result"]

    def benchmark_join_tasks(self) -> dict[str, Any]:
        """Return the installed pg_perf_bench JOIN scenario catalog."""
        envelope = self._require(
            self._invoke(
                "pg_perf_bench",
                ("join-tasks",),
                request_id="benchmark-join-tasks",
            ),
            {"succeeded"},
        )
        return envelope["result"]

    def compare_benchmark_reports(
        self,
        baseline: str | Path,
        candidate: str | Path,
    ) -> dict[str, Any]:
        baseline_result = self.inspect_benchmark_report(baseline)
        candidate_result = self.inspect_benchmark_report(candidate)
        return {
            "baseline": baseline_result,
            "candidate": candidate_result,
            **compare_benchmark_summaries(
                baseline_result["summary"],
                candidate_result["summary"],
            ),
        }

    def join_benchmark_reports(
        self,
        report_paths: list[str],
        join_task: str,
        output_directory: str,
        report_name: str,
    ) -> dict[str, Any]:
        """Join an exact, reviewed report set through pg_perf_bench."""
        paths = [Path(value).expanduser().resolve() for value in report_paths]
        if len(paths) < 2:
            raise OrchestrationError("at least two benchmark reports are required")
        if len(paths) != len(set(paths)):
            raise OrchestrationError("benchmark report paths must be unique")
        missing = [str(path) for path in paths if not path.is_file()]
        if missing:
            raise OrchestrationError("benchmark report does not exist: " + ", ".join(missing))
        if not join_task.strip():
            raise OrchestrationError("join_task must be non-empty")
        if not _RUN_ID_RE.fullmatch(report_name):
            raise OrchestrationError(f"report_name must match {_RUN_ID_RE.pattern}")
        output = Path(output_directory).expanduser().resolve()
        arguments = [
            "join",
            "--join-task",
            join_task,
            "--reference-report",
            str(paths[0]),
            "--report-name",
            report_name,
            "--out",
            str(output),
            "--log-dir",
            str(output / "log"),
        ]
        for path in paths:
            arguments.extend(("--report", str(path)))
        request_hash = canonical_hash(
            {
                "paths": [str(path) for path in paths],
                "join_task": join_task,
                "output": str(output),
                "report_name": report_name,
            }
        )[7:19]
        envelope = self._require(
            self._invoke(
                "pg_perf_bench",
                tuple(arguments),
                request_id=f"join-benchmark-{request_hash}",
            ),
            {"succeeded"},
        )
        return {
            "report_paths": [str(path) for path in paths],
            "join_task": join_task,
            "report_name": envelope["result"]["report_name"],
            "outputs": envelope["result"]["outputs"],
            "artifacts": envelope.get("artifacts") or [],
        }

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
        cancellable: bool = True,
    ) -> dict[str, Any]:
        context = self._execution_context if cancellable else None
        return self.runner.run(
            ComponentInvocation(
                component=component,
                arguments=arguments,
                request_id=request_id,
                cwd=cwd,
                input_document=input_document,
                environment=environment,
                timeout_seconds=timeout_seconds,
                cancel_path=context.cancel_path if context is not None else None,
                active_process_path=(context.active_process_path if context is not None else None),
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
        args = [
            "--root",
            str(manifest.workload.project),
            "--target=external",
            "--host",
            str(connection["host"]),
            "--port",
            str(connection["port"]),
            "--database",
            str(connection["database"]),
            "--user",
            str(connection["workload_user"]),
            "--admin-user",
            str(connection["admin_user"]),
            "--passfile",
            str(connection["passfile"]),
            "--pg-version",
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
        ]
        if manifest.workload.pgbench_duration_seconds is not None:
            args.extend(
                (
                    "--pgbench-duration",
                    str(manifest.workload.pgbench_duration_seconds),
                )
            )
        return tuple(args)

    @staticmethod
    def _benchmark_args(
        benchmark: BenchmarkSpec,
        config: StandConfig,
        output_directory: Path,
    ) -> tuple[str, ...]:
        host = (
            "127.0.0.1"
            if config.primary.bind_address in {"0.0.0.0", "::"}
            else config.primary.bind_address
        )
        pg_data_path, _bind_target = postgres_data_paths(config.postgres.version)
        axis_option = {
            "pgbench_clients": "--pgbench-clients",
            "pgbench_time": "--pgbench-time",
        }[benchmark.iteration_axis]
        args = [
            "benchmark",
            "--connection-type=docker",
            "--container-name",
            config.primary.container_name,
            "--allow-database-reset",
            "--host",
            host,
            "--port",
            str(config.primary.published_port),
            "--user",
            config.postgres.superuser,
            "--database",
            benchmark.database,
            "--pg-data-path",
            pg_data_path,
            "--pg-bin-path",
            f"/usr/lib/postgresql/{config.postgres.version}/bin",
            "--benchmark-type",
            benchmark.benchmark_type,
            axis_option,
            ",".join(str(value) for value in benchmark.iterations),
            "--command-timeout",
            str(benchmark.command_timeout),
            "--system-metrics-interval",
            str(benchmark.system_metrics_interval),
            "--report-name",
            benchmark.report_name,
            "--out",
            str(output_directory),
            "--log-dir",
            str(output_directory / "log"),
        ]
        if benchmark.pgbench_path is not None:
            args.extend(("--pgbench-path", benchmark.pgbench_path))
        if benchmark.psql_path is not None:
            args.extend(("--psql-path", benchmark.psql_path))
        if benchmark.system_metrics_duration is not None:
            args.extend(("--system-metrics-duration", str(benchmark.system_metrics_duration)))
        if benchmark.workload_path is not None:
            args.extend(("--workload-path", str(benchmark.workload_path)))
        if benchmark.workload_profile is not None:
            args.extend(("--workload-profile", benchmark.workload_profile))
        args.extend(("--workload-scale", str(benchmark.workload_scale)))
        if benchmark.workload_duration_seconds is not None:
            args.extend(
                (
                    "--workload-duration-seconds",
                    str(benchmark.workload_duration_seconds),
                )
            )
        if benchmark.init_command is not None:
            args.extend(("--init-command", benchmark.init_command))
        if benchmark.workload_command is not None:
            args.extend(("--workload-command", benchmark.workload_command))
        if benchmark.drop_os_caches:
            args.append("--drop-os-caches")
        if benchmark.collect_pg_logs:
            args.append("--collect-pg-logs")
        return tuple(args)

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
        return {
            "admin_password": stand_credentials.superuser_password,
            "workload_password": workload_password,
            "passfile": str(passfile),
        }

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
            str(run_directory / f"{manifest.diagnostics.report_name}.json"),
            "--html-out",
            str(run_directory / f"{manifest.diagnostics.report_name}.html"),
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
        local_status = "partial" if envelope["status"] == "partial" else "succeeded"
        running = next(
            (
                step
                for step in reversed(state.get("steps") or [])
                if step.get("name") == name and step.get("status") == "running"
            ),
            None,
        )
        record = self._step_record(name, envelope)
        record["status"] = local_status
        record["component_status"] = envelope["status"]
        record["finished_at"] = utc_now()
        if running is None:
            record["attempt"] = state.get("attempt", 1)
            record["resume_policy"] = _RESUMABLE_STEP_POLICIES[name]
            record["started_at"] = record["finished_at"]
            state["steps"].append(record)
        else:
            running.update(record)
        write_state(state_path, state)
        context = self._execution_context
        if context is not None:
            self._event(
                context,
                "step_completed",
                state=state.get("state"),
                step=name,
                data={"status": local_status, "component_status": envelope["status"]},
            )

    @staticmethod
    def _step_record(name: str, envelope: dict[str, Any]) -> dict[str, Any]:
        return {
            "name": name,
            "component": envelope["component"],
            "command": envelope["command"],
            "status": envelope["status"],
            "component_status": envelope["status"],
            "artifacts": envelope.get("artifacts") or [],
            "warnings": envelope.get("warnings") or [],
            "error": envelope.get("error"),
        }
