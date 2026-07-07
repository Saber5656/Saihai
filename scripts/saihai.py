#!/usr/bin/env python3
"""saihai - deterministic frontdoor/workflow command split."""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
FRONTDOOR_PATH = REPO_ROOT / "organization" / "runtime" / "workflows" / "scripts" / "frontdoor_orchestrator.py"

FRONTDOOR_COMMANDS = {"propose", "approve", "status"}
WORKFLOW_COMMANDS = {"create-run", "drain", "validate-report"}
assert not (FRONTDOOR_COMMANDS & WORKFLOW_COMMANDS)

PROPOSE_ALLOWED_STATUSES = {"proposed", "blocked", "waiting_human"}


def load_module(path: Path, name: str) -> Any:
    if str(path.parent) not in sys.path:
        sys.path.insert(0, str(path.parent))
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load module: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def frontdoor_module() -> Any:
    return load_module(FRONTDOOR_PATH, "saihai_frontdoor_orchestrator")


def print_json(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def print_blocked(reason: str) -> None:
    print_json({"schema_version": 1, "decision": "blocked", "reason": reason})


def principal_from_args(frontdoor: Any, args: argparse.Namespace) -> dict[str, str]:
    return frontdoor.principal_from_cli(
        args.principal_type,
        args.principal_id,
        args.authn_method,
    )


def state_root_from_args(frontdoor: Any, args: argparse.Namespace) -> Path:
    return Path(args.state_root or str(frontdoor.DEFAULT_STATE_ROOT)).expanduser()


def read_request_json(frontdoor: Any, raw: str) -> dict[str, Any]:
    payload = frontdoor.load_json_arg(raw)
    if not isinstance(payload, dict):
        raise frontdoor.FrontdoorError("frontdoor request json must be an object")
    return payload


def load_classification(frontdoor: Any, raw: Any) -> dict[str, Any] | None:
    if raw in (None, ""):
        return None
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str):
        raise frontdoor.FrontdoorError("classification must be an object, JSON string, or JSON file")
    classification = frontdoor.load_json_arg(raw)
    if not isinstance(classification, dict):
        raise frontdoor.FrontdoorError("classification must be an object")
    return classification


def request_value(
    request: dict[str, Any],
    args: argparse.Namespace,
    field: str,
    *,
    arg_name: str | None = None,
    default: Any = "",
) -> Any:
    value = getattr(args, arg_name or field, None)
    if value not in (None, "", []):
        return value
    return request.get(field, default)


def request_list(
    request: dict[str, Any],
    args: argparse.Namespace,
    field: str,
    *,
    arg_name: str,
) -> list[str]:
    value = getattr(args, arg_name, None)
    if value:
        return list(value)
    raw = request.get(field, [])
    if raw is None:
        return []
    if not isinstance(raw, list) or any(not isinstance(item, str) for item in raw):
        raise ValueError(f"{field} must be a list of strings")
    return list(raw)


def handle_frontdoor_propose(frontdoor: Any, args: argparse.Namespace) -> dict[str, Any]:
    request = read_request_json(frontdoor, args.request_json) if args.request_json else {}
    task_id = request_value(request, args, "task_id", arg_name="task_id")
    request_id = request_value(request, args, "request_id", arg_name="request_id")
    if not task_id or not request_id:
        raise frontdoor.FrontdoorError("task_id and request_id are required")

    classification_raw = request_value(request, args, "classification", arg_name="classification", default="")
    classification = load_classification(frontdoor, classification_raw)
    prompt = request_value(request, args, "prompt", arg_name="prompt", default=request.get("user_prompt", ""))
    payload = frontdoor.proposed_request(
        state_root=state_root_from_args(frontdoor, args),
        task_id=str(task_id),
        request_id=str(request_id),
        user_prompt=str(prompt),
        refs=request_list(request, args, "refs", arg_name="ref"),
        classification=classification,
        allowed_paths=request_list(request, args, "allowed_paths", arg_name="allowed_path"),
        expires_at=str(request_value(request, args, "expires_at", arg_name="expires_at", default="run_terminal")),
        frontdoor=str(request_value(request, args, "frontdoor", arg_name="frontdoor", default="codex")),
        chat_session_id=str(request_value(request, args, "chat_session_id", arg_name="chat_session_id", default="")),
    )
    status = str(
        payload.get("request_status")
        or (payload.get("activation") or {}).get("activation_status")
        or ""
    )
    if status not in PROPOSE_ALLOWED_STATUSES:
        print("frontdoor_propose_produced_approval", file=sys.stderr)
        raise SystemExit(3)
    if "approved_activation" in payload:
        print("frontdoor_propose_produced_approval", file=sys.stderr)
        raise SystemExit(3)
    return payload


def handle_frontdoor_approve(frontdoor: Any, args: argparse.Namespace) -> dict[str, Any]:
    return frontdoor.approve_request(
        state_root=state_root_from_args(frontdoor, args),
        request_id=args.request_id,
        human_action_id=args.nonce,
        principal=principal_from_args(frontdoor, args),
    )


def handle_frontdoor_status(frontdoor: Any, args: argparse.Namespace) -> dict[str, Any]:
    record = frontdoor.read_json(frontdoor.request_path(state_root_from_args(frontdoor, args), args.request_id))
    return {
        "schema_version": 1,
        "decision": "ok",
        "request_status": record.get("status"),
        "request_id": record.get("request_id"),
        "task_id": record.get("task_id"),
        "request": record,
    }


def handle_workflow_create_run(frontdoor: Any, args: argparse.Namespace) -> dict[str, Any]:
    return frontdoor.create_run(
        state_root=state_root_from_args(frontdoor, args),
        request_id=args.request_id,
        run_id=args.run_id,
        resume_policy=args.resume_policy,
        principal=principal_from_args(frontdoor, args),
    )


def handle_workflow_drain(frontdoor: Any, args: argparse.Namespace) -> dict[str, Any]:
    return frontdoor.drain_run(
        state_root=state_root_from_args(frontdoor, args),
        run_id=args.run_id,
        principal=principal_from_args(frontdoor, args),
    )


def handle_workflow_validate_report(frontdoor: Any, args: argparse.Namespace) -> dict[str, Any]:
    return frontdoor.validate_report(
        state_root=state_root_from_args(frontdoor, args),
        run_id=args.run_id,
        report_path_arg=args.report_path,
        principal=principal_from_args(frontdoor, args),
    )


def add_state_root(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--state-root", default="", help="orchestrator state root")


def add_execution_principal(
    parser: argparse.ArgumentParser,
    *,
    principal_type: str = "manual_operator",
    principal_id: str = "manual-cli",
    authn_method: str = "local_cli",
) -> None:
    parser.add_argument("--principal-type", default=principal_type)
    parser.add_argument("--principal-id", default=principal_id)
    parser.add_argument("--authn-method", default=authn_method)


def build_frontdoor_parser(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> argparse.ArgumentParser:
    parser = sub.add_parser("frontdoor", help="frontdoor proposal and approval commands")
    add_state_root(parser)
    frontdoor_sub = parser.add_subparsers(dest="frontdoor_command", required=True)

    propose = frontdoor_sub.add_parser("propose", help="propose an activation artifact")
    propose.add_argument("request_json", nargs="?", help="frontdoor request JSON object or file")
    propose.add_argument("--task-id", default="")
    propose.add_argument("--request-id", default="")
    propose.add_argument("--prompt", default="")
    propose.add_argument("--classification", default="")
    propose.add_argument("--ref", action="append", default=[])
    propose.add_argument("--allowed-path", action="append", default=[])
    propose.add_argument("--expires-at", default="")
    propose.add_argument("--frontdoor", choices=["codex", "claude", "manual"], default="")
    propose.add_argument("--chat-session-id", default="")
    propose.set_defaults(handler=handle_frontdoor_propose)

    approve = frontdoor_sub.add_parser("approve", help="approve a proposed activation artifact")
    approve.add_argument("--request-id", required=True)
    approve.add_argument("--nonce", required=True)
    add_execution_principal(
        approve,
        principal_type="human_operator",
        principal_id="human-ui",
        authn_method="local_ui",
    )
    approve.set_defaults(handler=handle_frontdoor_approve)

    status = frontdoor_sub.add_parser("status", help="read stored request state")
    status.add_argument("--request-id", required=True)
    status.set_defaults(handler=handle_frontdoor_status)
    return parser


def build_workflow_parser(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> argparse.ArgumentParser:
    parser = sub.add_parser("workflow", help="workflow run commands")
    add_state_root(parser)
    workflow_sub = parser.add_subparsers(dest="workflow_command", required=True)

    create = workflow_sub.add_parser("create-run", help="create a workflow run from an accepted request")
    create.add_argument("--request-id", required=True)
    create.add_argument("--run-id", default="")
    create.add_argument("--resume-policy", choices=["manual", "daemon_future"], default="manual")
    add_execution_principal(create)
    create.set_defaults(handler=handle_workflow_create_run)

    drain = workflow_sub.add_parser("drain", help="drain a created run into a work order")
    drain.add_argument("--run-id", required=True)
    add_execution_principal(drain)
    drain.set_defaults(handler=handle_workflow_drain)

    report = workflow_sub.add_parser("validate-report", help="validate a typed workflow report")
    report.add_argument("--run-id", required=True)
    report.add_argument("--report-path", default="")
    add_execution_principal(
        report,
        principal_type="harness_runner",
        principal_id="local-harness",
    )
    report.set_defaults(handler=handle_workflow_validate_report)
    return parser


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Saihai deterministic frontdoor/workflow CLI",
    )
    sub = parser.add_subparsers(dest="group", required=True)
    build_frontdoor_parser(sub)
    build_workflow_parser(sub)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        frontdoor = frontdoor_module()
    except (OSError, RuntimeError, ImportError) as exc:
        print_blocked(str(exc))
        return 2

    try:
        payload = args.handler(frontdoor, args)
    except frontdoor.FrontdoorError as exc:
        print_blocked(str(exc))
        return 2
    except (ValueError, KeyError, TypeError) as exc:
        print_blocked(str(exc))
        return 2

    print_json(payload)
    if payload.get("decision") == "blocked" or payload.get("request_status") == "blocked":
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
