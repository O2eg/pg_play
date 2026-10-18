"""Typed adapter to pg_maker's durable worker; no second build scheduler."""

from __future__ import annotations

import copy
import json
import re
from pathlib import Path
from typing import Any

from pg_play.contract import canonical_hash
from pg_play.runner import ComponentInvocation, ComponentRunner
from pg_play.state import exclusive_lock, read_state, write_json


class MakerError(RuntimeError):
    """A reviewed maker request cannot be executed."""


def checked_plan(plan: dict, expected: str) -> None:
    if plan.get("schema_version") != "pg_play/maker-plan-v1":
        raise MakerError("unsupported maker plan")
    actual = canonical_hash({k: v for k, v in plan.items() if k != "plan_hash"})
    if actual != expected or plan.get("plan_hash") != expected:
        raise MakerError("maker plan changed since review")


class MakerClient:
    def __init__(self, runner: ComponentRunner) -> None:
        self.runner = runner

    def call(self, arguments: tuple[str, ...], *, request_id: str = "pg-play-maker") -> dict:
        # Maker mutations always return after handing ownership to its worker. Never
        # attach pg_play's cancel_path: killing this CLI does not cancel that worker.
        response = self.runner.run(
            ComponentInvocation(
                "pg_maker",
                arguments,
                request_id,
                timeout_seconds=120,
            )
        )
        if response["status"] not in {"succeeded", "planned", "running"}:
            error = response.get("error") or {}
            detail = (
                ": " + json.dumps(error["details"], ensure_ascii=False)
                if error.get("details")
                else ""
            )
            raise MakerError(error.get("message", "pg_maker command failed") + detail)
        return response["result"]

    def validate(self, manifest: str | Path) -> dict:
        return self.call(("validate", str(Path(manifest).absolute())))

    def prepare(
        self,
        manifest: str | Path,
        output: str | Path,
        operations: str | Path,
        input_store: str | Path,
        operation_id: str,
        *,
        environments: str | Path | None = None,
        offline: bool = False,
        config_dir: str | None = None,
        features: list[str] | None = None,
        build_mode: str | None = None,
        with_doc: bool | None = None,
        doc_languages: list[str] | None = None,
    ) -> dict:
        command = ("env", "prepare") if environments is None else ("resolve",)
        args = (
            *command,
            str(Path(manifest).absolute()),
            "--out",
            str(Path(output).absolute()),
            "--operations",
            str(Path(operations).absolute()),
            "--input-store",
            str(Path(input_store).absolute()),
            "--operation-id",
            operation_id,
            "--detach",
        )
        if environments is not None:
            args += ("--environments", str(Path(environments).absolute()))
        if offline:
            args += ("--offline",)
        if config_dir is not None:
            args += ("--config-dir", str(Path(config_dir).absolute()))
        if build_mode is not None:
            args += ("--build-mode", build_mode)
        if with_doc is not None:
            args += ("--with-doc" if with_doc else "--without-doc",)
        for flag, values in (("--feature", features), ("--doc-language", doc_languages)):
            for value in values or []:
                args += (flag, value)
        result = self.call(args, request_id=f"pg-play-{command[0]}-{operation_id}")
        return self._reference(result, operations)

    def plan(
        self,
        lock: str | Path,
        *,
        input_store: str | Path | None = None,
        goal: str = "package",
        run_db_tests: bool = False,
    ) -> dict:
        lock_path = Path(lock).absolute()
        args = (
            "plan",
            str(lock_path),
            "--goal",
            goal,
            "--run-db-tests" if run_db_tests else "--skip-db-tests",
        )
        if input_store is not None:
            args += ("--input-store", str(Path(input_store).absolute()))
        build_plan = self.call(args)
        document = read_state(lock_path)
        # Match maker's durable-request convention: wall-clock replanning must
        # not change the reviewed identity of otherwise identical inputs.
        if "created_at" in document:
            build_plan["created_at"] = document["created_at"]
        result = {
            "schema_version": "pg_play/maker-plan-v1",
            "lock": document,
            "build_plan": build_plan,
            "input_store": str(Path(input_store).absolute())
            if input_store
            else document.get("transport", {}).get("input_store"),
            "goal": goal,
            "run_db_tests": run_db_tests,
        }
        result["plan_hash"] = canonical_hash(result)
        return result

    def start(
        self,
        plan: dict,
        plan_hash: str,
        operations: str | Path,
        operation_id: str,
        *,
        cache_store: str | Path | None = None,
    ) -> dict:
        checked_plan(plan, plan_hash)
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,79}", operation_id):
            raise MakerError("invalid maker operation_id")
        root = Path(operations).absolute()
        frozen = root / ".pg-play-plans" / operation_id
        frozen.mkdir(parents=True, exist_ok=True)
        with exclusive_lock(frozen / "plan.lock"):
            existing = read_state(frozen / "reviewed.json")
            if existing.get("state") != "not_found" and existing != plan:
                raise MakerError("operation_id already has a different reviewed plan")
            write_json(frozen / "reviewed.json", plan)
            lock = copy.deepcopy(plan["lock"])
            lock.setdefault("transport", {})["input_store"] = plan["input_store"]
            write_json(frozen / "lock.json", lock)
            build_plan = copy.deepcopy(plan["build_plan"])
            build_plan.setdefault("transport", {})["lock"] = str(frozen / "lock.json")
            write_json(frozen / "plan.json", build_plan)
        args = (
            "build",
            str(frozen / "lock.json"),
            "--plan",
            str(frozen / "plan.json"),
            "--plan-hash",
            build_plan["plan_hash"],
            "--goal",
            plan["goal"],
            "--operations",
            str(root),
            "--operation-id",
            operation_id,
            "--detach",
            "--run-db-tests" if plan["run_db_tests"] else "--skip-db-tests",
        )
        if plan["input_store"]:
            args += ("--input-store", plan["input_store"])
        if cache_store is not None:
            args += ("--cache-store", str(Path(cache_store).absolute()))
        result = self.call(args, request_id=f"pg-play-build-{operation_id}")
        return {
            **self._reference(result, root),
            "reviewed_plan_hash": plan_hash,
            "build_plan_hash": build_plan["plan_hash"],
        }

    def status(self, directory: str | Path) -> dict:
        return self.call(("status", str(Path(directory).absolute())))

    def events(self, directory: str | Path, after_sequence: int = 0, limit: int = 100) -> dict:
        return self.call(
            (
                "events",
                str(Path(directory).absolute()),
                "--after-sequence",
                str(after_sequence),
                "--limit",
                str(limit),
            )
        )

    def cancel(self, directory: str | Path) -> dict:
        return self.call(("cancel", str(Path(directory).absolute())))

    def resume(
        self, directory: str | Path, *, plan_hash: str | None = None, input_hash: str | None = None
    ) -> dict:
        args = ("resume", str(Path(directory).absolute()), "--detach")
        if plan_hash is not None:
            args += ("--plan-hash", plan_hash)
        if input_hash is not None:
            args += ("--input-hash", input_hash)
        return self.call(args)

    def artifacts(self, directory: str | Path) -> dict:
        return self.call(("artifacts", str(Path(directory).absolute())))

    def export(self, directory: str | Path, output: str | Path) -> dict:
        return self.call(
            ("export", str(Path(directory).absolute()), "--out", str(Path(output).absolute()))
        )

    @staticmethod
    def _reference(state: dict[str, Any], operations: str | Path) -> dict:
        return {
            **state,
            "operation_directory": str(Path(operations).absolute() / state["operation_id"]),
        }
