"""Typed, versioned adapter for pg-logical-repl; SQL state belongs to the component."""

from __future__ import annotations

import hashlib
import math
from pathlib import Path

from pg_play.contract import canonical_hash, validate_capabilities
from pg_play.runner import ComponentInvocation, ComponentRunner
from pg_play.state import read_state

PLAN_SCHEMA = "pg_play/logical-plan-v1"
OPERATIONS = {
    "prepare",
    "start",
    "pause",
    "resume",
    "resolve",
    "resync-table",
    "abort",
    "cleanup",
    "cutover-finalize",
    "cutover-commit",
    "ddl-exec",
    "ddl-queue-skip",
}
INSPECTIONS = {"validate", "inspect-config", "check", "status", "events", "verify", "report"}
OPTIONS = {
    "pause": {"reason", "max_duration", "wal_rate"},
    "resume": {"reason"},
    "resolve": {"reason", "action", "lsn"},
    "resync-table": {"reason", "table"},
    "abort": {"reason", "source_unavailable"},
    "cleanup": {"reason"},
    "cutover-commit": {"reason", "source_unavailable"},
    "cutover-finalize": {"write_fence"},
    "ddl-exec": {"sql_file"},
    "ddl-queue-skip": {"job_id", "evidence"},
}


class LogicalError(RuntimeError):
    """A logical replication operation cannot safely continue."""


def file_reference(path: str | Path) -> dict:
    source = Path(path).expanduser().resolve(strict=True)
    if not source.is_file():
        raise LogicalError("Input must be a regular file")
    return {"path": str(source), "sha256": hashlib.sha256(source.read_bytes()).hexdigest()}


def check_references(references: list[dict]) -> None:
    for reference in references:
        if file_reference(reference["path"]) != reference:
            raise LogicalError("Reviewed input changed: " + reference["path"])


def bounded(value, name, minimum=1, maximum=86400):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise LogicalError(name + " must be a number")
    if not math.isfinite(value) or not minimum <= value <= maximum:
        raise LogicalError(f"{name} must be between {minimum} and {maximum}")
    return value


def checked_plan(plan: dict, expected_hash: str | None = None, *, inputs=True) -> dict:
    if not isinstance(plan, dict) or plan.get("schema_version") != PLAN_SCHEMA:
        raise LogicalError("Unsupported logical plan schema")
    body = dict(plan)
    actual = body.pop("plan_hash", None)
    if canonical_hash(body) != actual or (expected_hash is not None and expected_hash != actual):
        raise LogicalError("Reviewed plan hash does not match")
    if plan.get("operation") not in OPERATIONS | {"controller", "experiment"}:
        raise LogicalError("Unsupported logical operation")
    if inputs:
        check_references(plan["inputs"])
    return plan


def command(operation: str, native: dict, options: dict) -> tuple[str, ...]:
    if operation.startswith("cutover-"):
        args = ["cutover", operation.removeprefix("cutover-")]
    elif operation == "ddl-exec":
        args = ["ddl", "exec", "--sql-file", options["sql_file"]]
    elif operation == "ddl-queue-skip":
        args = ["ddl", "queue", "skip", str(options["job_id"]), "--evidence", options["evidence"]]
    else:
        args = [operation]
    args += ["--plan", native["path"], "--plan-hash", native["plan_hash"]]
    if operation == "cutover-finalize":
        args += ["--write-fence", options["write_fence"]]
    return tuple(args)


class LogicalClient:
    def __init__(self, runner: ComponentRunner | None = None):
        self.runner = runner or ComponentRunner()

    def call(self, config: str | None, args: tuple, *, request_id="logical", **kwargs):
        arguments = (("--config", str(config)) if config else ()) + tuple(map(str, args))
        return self.runner.run(
            ComponentInvocation(
                component="pg_logical_repl",
                arguments=arguments,
                request_id=request_id,
                **kwargs,
            )
        )

    @staticmethod
    def require(envelope):
        if envelope["status"] not in {"succeeded", "planned"}:
            error = envelope.get("error") or {}
            raise LogicalError(
                error.get("code", envelope["status"])
                + ": "
                + error.get("message", "Component did not complete the operation")
            )
        return envelope["result"]

    def capabilities(self):
        result = self.require(self.call(None, ("--component-capabilities",)))
        return validate_capabilities(
            result,
            expected_component="pg_logical_repl",
            required_commands={"inspect-config", "plan", "run", "status"},
        )

    def inspect(self, config: str, operation="status", *, after=0, limit=100, level="checksums"):
        if operation not in INSPECTIONS:
            raise LogicalError("Unsupported inspection")
        args = [operation]
        if operation == "events":
            if (
                type(after) is not int
                or after < 0
                or type(limit) is not int
                or not 1 <= limit <= 10000
            ):
                raise LogicalError("Invalid event cursor or limit")
            args += ["--after", after, "--limit", limit]
        if operation == "verify":
            if level not in {"counts", "checksums", "full"}:
                raise LogicalError("Invalid verification level")
            args += ["--level", level]
        return self.call(str(Path(config).expanduser().resolve()), tuple(args))

    def native_plan(self, config, operation, options, **kwargs):
        args = ["plan", operation]
        for name, value in options.items():
            if name == "write_fence":
                continue
            if name == "source_unavailable":
                if value:
                    args.append("--source-unavailable")
            else:
                args += ["--" + name.replace("_", "-"), str(value)]
        result = self.require(self.call(config, tuple(args), **kwargs))
        if not isinstance(result, dict) or not {"plan", "path", "plan_hash"} <= result.keys():
            raise LogicalError("Component returned an invalid native plan")
        return result

    def plan(
        self,
        config: str,
        operation: str,
        *,
        options: dict | None = None,
        duration_seconds=60,
        poll_interval_seconds=5,
        scenario: str | None = None,
        timeout_seconds=600,
    ) -> dict:
        if operation not in OPERATIONS | {"controller", "experiment"}:
            raise LogicalError("Unsupported logical operation")
        options = dict(options or {})
        if scenario is not None and operation != "experiment":
            raise LogicalError("scenario is only supported for experiment")
        if set(options) - OPTIONS.get(operation, set()):
            raise LogicalError("Unsupported operation options")
        for name, value in options.items():
            if name == "source_unavailable":
                if type(value) is not bool:
                    raise LogicalError("source_unavailable must be boolean")
            elif name in {"max_duration", "wal_rate", "job_id"}:
                if type(value) is not int or value <= 0:
                    raise LogicalError(name + " must be a positive integer")
            elif not isinstance(value, str) or not value or "\x00" in value:
                raise LogicalError("Invalid operation option: " + name)
        if operation == "cutover-finalize" and not options.get("write_fence"):
            raise LogicalError("cutover-finalize requires write_fence")
        inputs = [file_reference(config)]
        config = inputs[0]["path"]
        for name in ("sql_file", "evidence", "write_fence"):
            if options.get(name):
                reference = file_reference(options[name])
                inputs.append(reference)
                options[name] = reference["path"]
        caps = self.capabilities()
        description = self.require(self.call(config, ("inspect-config",)))
        plan = dict(
            schema_version=PLAN_SCHEMA,
            operation=operation,
            config=config,
            component_version=caps["component_version"],
            configuration=description,
            options=options,
            inputs=inputs,
            duration_seconds=bounded(duration_seconds, "duration_seconds"),
            poll_interval_seconds=bounded(poll_interval_seconds, "poll_interval_seconds", 0.1, 60),
            timeout_seconds=bounded(timeout_seconds, "timeout_seconds"),
            safety=dict(
                stop_controller_preserves_replication=True,
                automatic_recovery=False,
                native_plans_required=True,
                credentials="references_only",
            ),
        )
        if operation in OPERATIONS:
            native = self.native_plan(config, operation, options)
            inputs.append(file_reference(native["path"]))
            plan["native_plan"] = {k: native[k] for k in ("path", "plan_hash")}
            plan["intent"] = {
                k: native["plan"].get(k)
                for k in ("source", "target", "state", "actions", "global_roles_to_create")
            }
        elif operation == "experiment":
            if not scenario:
                raise LogicalError("experiment requires a scenario file")
            from pg_play.logical_experiment import plan_scenario

            plan["scenario"] = plan_scenario(self, config, description, scenario, inputs)
        elif scenario:
            raise LogicalError("scenario is only supported for experiment")
        plan["plan_hash"] = canonical_hash(plan)
        return plan

    def validate(self, plan):
        checked_plan(plan)
        if self.capabilities()["component_version"] != plan["component_version"]:
            raise LogicalError("Component version changed; review a new plan")
        description = self.require(self.call(plan["config"], ("inspect-config",)))
        if description != plan["configuration"]:
            raise LogicalError("Migration configuration changed")

    def operate(self, plan, **kwargs):
        checked_plan(plan)
        native = plan["native_plan"]
        # The component validates its own RFC 8785 hash, catalog identities and
        # retry receipts. pg_play's wrapper hash is never substituted for it.
        return self.call(
            plan["config"], command(plan["operation"], native, plan["options"]), **kwargs
        )


def load_plan(path):
    return checked_plan(read_state(Path(path)), inputs=False)
