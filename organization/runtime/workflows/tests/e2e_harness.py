#!/usr/bin/env python3
"""Offline E2E harness for deterministic orchestrator workflow tests."""

from __future__ import annotations

import importlib.util
import inspect
import json
import shutil
import sys
import time
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[4]
SCRIPT_DIR = REPO_ROOT / "organization" / "runtime" / "workflows" / "scripts"


class HarnessFeatureUnavailable(RuntimeError):
    """Raised when a later-phase orchestrator module is not present."""


class HarnessAssertion(AssertionError):
    """Assertion that carries the full backing response for debugging."""

    def __init__(self, label: str, response: Any):
        self.label = label
        self.response = response
        super().__init__(f"{label}: {json.dumps(response, ensure_ascii=False, sort_keys=True)}")


def _load_module(path: Path, name: str, *, public_name: str | None = None) -> Any:
    if not path.exists():
        raise HarnessFeatureUnavailable(f"module missing: {path}")
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise HarnessFeatureUnavailable(f"cannot load module: {path}")
    module = importlib.util.module_from_spec(spec)
    if public_name:
        sys.modules[public_name] = module
    spec.loader.exec_module(module)
    return module


def _load_frontdoor() -> Any:
    _load_module(SCRIPT_DIR / "safe_paths.py", "saihai_harness_safe_paths", public_name="safe_paths")
    _load_module(SCRIPT_DIR / "run_store.py", "saihai_harness_run_store", public_name="run_store")
    if (SCRIPT_DIR / "run_lock.py").exists():
        _load_module(SCRIPT_DIR / "run_lock.py", "saihai_harness_run_lock", public_name="run_lock")
    if (SCRIPT_DIR / "run_lifecycle.py").exists():
        _load_module(SCRIPT_DIR / "run_lifecycle.py", "saihai_harness_run_lifecycle", public_name="run_lifecycle")
    _load_module(SCRIPT_DIR / "workflow_selector.py", "saihai_harness_workflow_selector", public_name="workflow_selector")
    return _load_module(SCRIPT_DIR / "frontdoor_orchestrator.py", "saihai_harness_frontdoor")


def _load_optional(name: str) -> Any | None:
    path = SCRIPT_DIR / f"{name}.py"
    if not path.exists():
        return None
    return _load_module(path, f"saihai_harness_{name}", public_name=name)


def _call_supported(func: Any, kwargs: dict[str, Any]) -> Any:
    signature = inspect.signature(func)
    if any(param.kind == inspect.Parameter.VAR_KEYWORD for param in signature.parameters.values()):
        return func(**kwargs)
    supported = {key: value for key, value in kwargs.items() if key in signature.parameters}
    return func(**supported)


def _assert(condition: bool, label: str, response: Any) -> None:
    if not condition:
        raise HarnessAssertion(label, response)


class OrchestratorHarness:
    """Drives a full orchestrator flow against a throwaway state root."""

    def __init__(self, state_root: Path, repo_root: Path | None = None):
        self.state_root = Path(state_root)
        self.repo_root = Path(repo_root or REPO_ROOT)
        self.frontdoor = _load_frontdoor()
        self.optional_modules = {
            name: module
            for name in ("run_lifecycle", "provider_runner", "completion_gate", "task_state_bridge")
            if (module := _load_optional(name)) is not None
        }
        self.fixture_dir = self.repo_root / ".tmp" / f"e2e-harness-{int(time.time() * 1000)}"
        self.state_root.mkdir(parents=True, exist_ok=True)

    def __enter__(self) -> "OrchestratorHarness":
        return self

    def __exit__(self, _exc_type: Any, _exc: Any, _tb: Any) -> None:
        self.cleanup()

    def cleanup(self) -> None:
        if self.fixture_dir.exists():
            shutil.rmtree(self.fixture_dir)

    def make_ref(self, name: str = "ref.md", content: str = "fixture ref\n") -> str:
        path = self.fixture_dir / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return path.relative_to(self.repo_root).as_posix()

    def classification(self, **overrides: Any) -> dict[str, Any]:
        candidate = {
            "classification_version": "1",
            "classification_source": "deterministic_fixture",
            "classification_confidence": 1.0,
            "classification_evidence": ["e2e-harness-fixture"],
            "task_kind": "external_review",
            "permission_required": "readonly",
            "external_provider_required": True,
            "publication_required": False,
            "security_sensitive": False,
            "destructive_operation": False,
            "context_scope": "refs_only",
            "expected_artifacts": ["typed_report"],
        }
        candidate.update(overrides)
        return candidate

    def propose(
        self,
        task_id: str = "TSK-e2e",
        request_id: str = "req-e2e",
        *,
        prompt: str = "Run bounded offline external review.",
        refs: list[str] | None = None,
        classification: dict[str, Any] | None = None,
        allowed_paths: list[str] | None = None,
        expires_at: str = "run_terminal",
        frontdoor: str = "codex",
        chat_session_id: str = "harness",
    ) -> dict[str, Any]:
        response = self.frontdoor.proposed_request(
            state_root=self.state_root,
            task_id=task_id,
            request_id=request_id,
            user_prompt=prompt,
            refs=refs if refs is not None else [self.make_ref()],
            classification=classification if classification is not None else self.classification(),
            allowed_paths=allowed_paths or [],
            expires_at=expires_at,
            frontdoor=frontdoor,
            chat_session_id=chat_session_id,
        )
        _assert(response.get("decision") == "ok", "proposal failed", response)
        _assert(response.get("request_status") == "proposed", "proposal not proposed", response)
        return response

    def challenge(self, request_id: str) -> str:
        record = self.frontdoor.read_json(self.frontdoor.request_path(self.state_root, request_id))
        approval = record.get("approval") or {}
        challenge = approval.get("human_action_id")
        _assert(isinstance(challenge, str) and bool(challenge), "approval challenge missing", record)
        return challenge

    def approve(self, request_id: str, human_action_id: str | None = None) -> dict[str, Any]:
        challenge = human_action_id or self.challenge(request_id)
        try:
            response = self.frontdoor.approve_request(
                state_root=self.state_root,
                request_id=request_id,
                human_action_id=challenge,
            )
        except Exception as exc:  # frontdoor can raise module-local FrontdoorError.
            raise HarnessAssertion("approval blocked", {"decision": "blocked", "reason": str(exc)}) from exc
        _assert(response.get("decision") == "ok", "approval failed", response)
        _assert(response.get("request_status") == "approved", "request not approved", response)
        return response

    def create_run(self, request_id: str, run_id: str = "") -> dict[str, Any]:
        response = self.frontdoor.create_run(
            state_root=self.state_root,
            request_id=request_id,
            run_id=run_id,
            resume_policy="manual",
        )
        _assert(response.get("decision") == "ok", "run creation failed", response)
        return response

    def drain(self, run_id: str) -> dict[str, Any]:
        response = self.frontdoor.drain_run(state_root=self.state_root, run_id=run_id)
        _assert(response.get("decision") == "ok", "drain failed", response)
        return response

    def run_step(self, run_id: str, adapter: str = "fake_pass", **kwargs: Any) -> dict[str, Any]:
        module = self.optional_modules.get("provider_runner")
        if module is None:
            raise HarnessFeatureUnavailable("provider_runner.py is not available")
        func = getattr(module, "run_step", None) or getattr(module, "run_provider_step", None)
        if func is None:
            raise HarnessFeatureUnavailable("provider_runner has no run_step function")
        response = _call_supported(
            func,
            {
                "state_root": self.state_root,
                "repo_root": self.repo_root,
                "run_id": run_id,
                "adapter": adapter,
                "adapter_id": adapter,
                "live": False,
                **kwargs,
            },
        )
        _assert(isinstance(response, dict), "run_step returned non-object", response)
        _assert(response.get("decision") in {None, "ok"}, "run_step failed", response)
        return response

    def place_report(self, run_id: str, result: str = "pass", **overrides: Any) -> Path:
        prepared = self.frontdoor.prepare_claude_adapter(state_root=self.state_root, run_id=run_id)
        _assert(prepared.get("decision") == "ok", "adapter preparation failed", prepared)
        adapter_request = prepared["adapter_request"]
        evidence_path = Path(adapter_request["evidence_path"])
        transcript_path = Path(adapter_request["transcript_path"])
        report_path = Path(adapter_request["report_path"])
        evidence_path.parent.mkdir(parents=True, exist_ok=True)
        transcript_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.parent.mkdir(parents=True, exist_ok=True)
        evidence_path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "provider": "harness_fixture",
                    "run_id": run_id,
                    "outcome": result,
                    "live": False,
                },
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )
        transcript_path.write_text("harness fixture transcript\n", encoding="utf-8")
        report = {
            "report_version": "1",
            "report_id": f"report-{run_id}",
            "request_id": adapter_request["request_id"],
            "run_id": run_id,
            "workflow_id": adapter_request["workflow_id"],
            "step_id": adapter_request["step_id"],
            "result": result,
            "summary": "Offline harness fixture report.",
            "provider_evidence": {
                "provider": "harness_fixture",
                "intended_model": adapter_request["intended_model"],
                "effective_model": adapter_request["intended_model"],
                "request_id": adapter_request["request_id"],
                "provider_session_id": f"session-{run_id}",
                "transcript_path": str(transcript_path),
                "evidence_path": str(evidence_path),
            },
            "findings": [],
            "authority": {
                "canonical_result": "typed_report_file",
                "stdout_is_signal_only": True,
                "raw_transcript_shared": False,
            },
        }
        report.update(overrides)
        report_path.write_text(json.dumps(report, ensure_ascii=False) + "\n", encoding="utf-8")
        return report_path

    def validate_report(self, run_id: str) -> dict[str, Any]:
        response = self.frontdoor.validate_report(state_root=self.state_root, run_id=run_id)
        _assert(response.get("decision") == "ok", "report validation failed", response)
        return response

    def verify_completion(self, run_id: str) -> dict[str, Any]:
        module = self.optional_modules.get("completion_gate")
        if module is None:
            raise HarnessFeatureUnavailable("completion_gate.py is not available")
        func = getattr(module, "verify_completion", None)
        if func is None:
            raise HarnessFeatureUnavailable("completion_gate has no verify_completion function")
        response = _call_supported(func, {"state_root": self.state_root, "run_id": run_id})
        _assert(isinstance(response, dict), "verify_completion returned non-object", response)
        return response

    def run_record(self, run_id: str) -> dict[str, Any]:
        return self.frontdoor.run_store.load_run(self.state_root, run_id)

    def artifact_tree(self) -> list[str]:
        if not self.state_root.exists():
            return []
        paths = []
        for path in self.state_root.rglob("*"):
            if path.is_file() and ".tmp" not in path.name:
                paths.append(path.relative_to(self.state_root).as_posix())
        return sorted(paths)

    def happy_path(self, adapter: str = "fake_pass") -> dict[str, Any]:
        responses: dict[str, Any] = {}
        responses["propose"] = self.propose()
        responses["approve"] = self.approve("req-e2e")
        responses["create_run"] = self.create_run("req-e2e", "run-e2e")
        run_id = responses["create_run"]["workflow_run"]["run_id"]
        responses["drain"] = self.drain(run_id)
        try:
            responses["run_step"] = self.run_step(run_id, adapter=adapter)
        except HarnessFeatureUnavailable:
            responses["place_report"] = {"report_path": str(self.place_report(run_id))}
            responses["validate_report"] = self.validate_report(run_id)
        run = self.run_record(run_id)
        _assert(run.get("run_state") == "complete", "happy path did not complete", run)
        return {
            "run_id": run_id,
            "terminal": run.get("terminal") or {},
            "responses": responses,
        }
