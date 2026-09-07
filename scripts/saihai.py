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
sys.path.insert(0, str(REPO_ROOT))
from directory_paths import load_environment  # noqa: E402

ENV_DIAGNOSTICS = ({"status":"startup_diagnostic_deferred"} if sys.argv[1:2] == ["startup"]
                   else load_environment(checkout_root=REPO_ROOT, require_catalog=True))
FRONTDOOR_PATH = REPO_ROOT / "organization" / "runtime" / "workflows" / "scripts" / "frontdoor_orchestrator.py"

FRONTDOOR_COMMANDS = {"propose", "approve", "status"}
WORKFLOW_COMMANDS = {"create-run", "drain", "run-provider", "validate-report"}
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
    return frontdoor.trusted_state_root(args.state_root)


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
        provider_adapter_id=str(
            request_value(
                request,
                args,
                "provider_adapter_id",
                arg_name="provider_adapter_id",
                default="",
            )
        ),
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


def handle_workflow_run_provider(frontdoor: Any, args: argparse.Namespace) -> dict[str, Any]:
    return frontdoor.run_provider(
        state_root=state_root_from_args(frontdoor, args),
        run_id=args.run_id,
        adapter_id=args.adapter_id,
        timeout_seconds=args.timeout_seconds,
        fake_provider_mode=args.fake_provider_mode,
        live=args.live,
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
    propose.add_argument("--provider-adapter-id", default="")
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

    provider = workflow_sub.add_parser("run-provider", help="run a bounded headless provider adapter")
    provider.add_argument("--run-id", required=True)
    provider.add_argument("--adapter-id", default="claude_headless_p0")
    provider.add_argument("--timeout-seconds", type=int, default=1800)
    provider.add_argument("--live", action="store_true")
    provider.add_argument(
        "--fake-provider-mode",
        choices=["", "success", "findings", "blocked", "timeout", "nonzero", "malformed", "unavailable"],
        default="",
    )
    add_execution_principal(
        provider,
        principal_type="harness_runner",
        principal_id="local-harness",
    )
    provider.set_defaults(handler=handle_workflow_run_provider)

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


def handle_usage_prepare(frontdoor: Any, args: argparse.Namespace) -> dict[str, Any]:
    import request_intake
    import trusted_local_executor
    try:
        authority = trusted_local_executor.load_host_authorization(Path(args.authorization))
        request = read_request_json(frontdoor, args.request)
        ledger = read_request_json(frontdoor, args.requirement_ledger)
        state_root = Path(args.state_root)
        provider = request_intake.CodexIntakeProvider(authorization=authority, request=request, state_root=state_root)
        reference = request_intake.prepare(state_root=state_root, task_id=request['task_id'],
            request_id=request['request_id'], user_prompt=request['instruction'], ledger=ledger,
            provider=provider, intended_model=authority.model)
        artifact = request_intake.resolve(state_root, reference)
        questions = artifact['brief']['open_questions']
        return {'decision': 'waiting_human' if questions else 'ok', 'work_brief_ref': reference,
                'prepared_request': dict(request, work_brief_ref=reference),
                'host_binding': {'intake_digest': reference['digest'], 'authority_evidence_ref': authority.publication.authority_evidence_ref},
                'questions': questions, 'next_action': 'resolve_material_requirement' if questions else 'host_bind_existing_scope_and_run',
                'authority_created': False}
    except (request_intake.IntakeError, request_intake.scope.ScopeError, trusted_local_executor.TrustedLocalError) as exc:
        raise frontdoor.FrontdoorError(str(exc)) from exc


def handle_usage_reconcile_intake(frontdoor: Any, args: argparse.Namespace) -> dict[str, Any]:
    import request_intake
    import trusted_local_executor
    try:
        authority = trusted_local_executor.load_host_authorization(Path(args.authorization))
        request = read_request_json(frontdoor, args.request)
        return request_intake.reconcile_failed_attempt(state_root=Path(args.state_root), request=request,
            authorization=authority, invocation_id=args.invocation_id, source_digest=args.source_digest)
    except (request_intake.IntakeError, trusted_local_executor.TrustedLocalError) as exc:
        raise frontdoor.FrontdoorError(str(exc)) from exc


def handle_usage_run(frontdoor: Any, args: argparse.Namespace) -> dict[str, Any]:
    import trusted_local_executor
    try:
        authority = trusted_local_executor.load_host_authorization(Path(args.authorization))
    except trusted_local_executor.TrustedLocalError as exc:
        raise frontdoor.FrontdoorError(str(exc)) from exc
    request = read_request_json(frontdoor, args.request)
    return frontdoor.run_trusted_local(request=request, authorization=authority,
                                      state_root=Path(args.state_root))


def handle_usage_drive(frontdoor: Any, args: argparse.Namespace) -> dict[str, Any]:
    import trusted_local_executor
    try:
        authority = trusted_local_executor.load_host_authorization(Path(args.authorization))
    except trusted_local_executor.TrustedLocalError as exc:
        raise frontdoor.FrontdoorError(str(exc)) from exc
    request = read_request_json(frontdoor, args.request) if args.request else None
    plan = None
    if args.worker_recovery_plan:
        import run_store
        path = Path(args.worker_recovery_plan)
        worker = Path(authority.publication.worktree).resolve()
        if not path.is_absolute() or path.resolve() != path or worker == path or worker in path.parents:
            raise frontdoor.FrontdoorError('host_recovery_plan_path_invalid')
        plan = run_store.read_json(path)
    return frontdoor.drive_trusted_local(authorization=authority, state_root=Path(args.state_root),
        request=request, worker_recovery_plan=plan, max_iterations=args.max_iterations, duration_seconds=args.duration_seconds,
        poll_interval_seconds=args.poll_interval_seconds)


def handle_usage_advance(frontdoor: Any, args: argparse.Namespace) -> dict[str, Any]:
    import trusted_local_executor
    try:
        authority = trusted_local_executor.load_host_authorization(Path(args.authorization))
    except trusted_local_executor.TrustedLocalError as exc:
        raise frontdoor.FrontdoorError(str(exc)) from exc
    return frontdoor.advance_trusted_local(authorization=authority, state_root=Path(args.state_root))


def handle_usage_status(frontdoor: Any, args: argparse.Namespace) -> dict[str, Any]:
    import trusted_local_executor
    try:
        return trusted_local_executor.usage_status(args.execution_id, Path(args.state_root))
    except trusted_local_executor.TrustedLocalError as exc:
        raise frontdoor.FrontdoorError(str(exc)) from exc


def handle_usage_repair_validation(frontdoor: Any, args: argparse.Namespace) -> dict[str, Any]:
    import trusted_local_executor
    try:
        authority = trusted_local_executor.load_host_authorization(Path(args.authorization))
    except trusted_local_executor.TrustedLocalError as exc:
        raise frontdoor.FrontdoorError(str(exc)) from exc
    return frontdoor.repair_trusted_local_validation(authorization=authority, state_root=Path(args.state_root),
                                                   repair_instruction=args.repair_instruction)


def handle_output_status(frontdoor: Any, args: argparse.Namespace) -> dict[str, Any]:
    import output_monitor
    return output_monitor.status(frontdoor, state_root=state_root_from_args(frontdoor,args),
        principal=frontdoor.default_manual_principal(), stale_seconds=args.stale_seconds)


def handle_task_scaffold(frontdoor: Any, args: argparse.Namespace) -> dict[str, Any]:
    import vault_task_records
    try:
        brief = read_request_json(frontdoor, args.brief)
        return {'decision':'ok', 'task':vault_task_records.scaffold(vault_task_records.canonical_root(),
            args.task_id, project=args.project, brief=brief)}
    except vault_task_records.VaultTaskError as exc:
        raise frontdoor.FrontdoorError(exc.reason_class) from exc


def build_usage_parser(sub: Any) -> None:
    parser = sub.add_parser('usage', help='explicit trusted-local execution and host publication')
    commands = parser.add_subparsers(dest='command', required=True)
    prepare = commands.add_parser('prepare', help='classify and shape one bounded request under existing host authority')
    prepare.add_argument('--request', required=True)
    prepare.add_argument('--requirement-ledger', required=True)
    prepare.add_argument('--authorization', required=True)
    prepare.add_argument('--state-root', required=True)
    prepare.set_defaults(handler=handle_usage_prepare)
    reconcile = commands.add_parser('reconcile-intake', help='acknowledge one observed failed intake attempt without resetting its budget')
    reconcile.add_argument('--request', required=True)
    reconcile.add_argument('--invocation-id', required=True)
    reconcile.add_argument('--source-digest', default='', help='select an exact saved source when multiple revisions exist')
    reconcile.add_argument('--authorization', required=True)
    reconcile.add_argument('--state-root', required=True)
    reconcile.set_defaults(handler=handle_usage_reconcile_intake)
    run = commands.add_parser('run', help='run one authorized task and real validation')
    run.add_argument('--request', required=True)
    run.add_argument('--authorization', required=True, help='private host-owned authorization file')
    run.add_argument('--state-root', required=True)
    run.set_defaults(handler=handle_usage_run)
    drive = commands.add_parser('drive', help='run or resume a bounded authorized task through CI and completion')
    drive.add_argument('--authorization', required=True)
    drive.add_argument('--state-root', required=True)
    drive.add_argument('--request', default='', help='initial request; omit to resume the existing execution')
    drive.add_argument('--worker-recovery-plan', default='', help='private host plan for a finished failed worker child only')
    drive.add_argument('--max-iterations', type=int, default=32)
    drive.add_argument('--duration-seconds', type=float, default=300)
    drive.add_argument('--poll-interval-seconds', type=float, default=5)
    drive.set_defaults(handler=handle_usage_drive)
    advance = commands.add_parser('advance', help='advance host PR, CI, merge and integrated validation')
    advance.add_argument('--authorization', required=True)
    advance.add_argument('--state-root', required=True)
    advance.set_defaults(handler=handle_usage_advance)
    status = commands.add_parser('status', help='read saved trusted-local execution status')
    status.add_argument('--execution-id', required=True)
    status.add_argument('--state-root', required=True)
    status.set_defaults(handler=handle_usage_status)
    repair = commands.add_parser('repair-validation', help='repair a failed validation tree with a fresh execution')
    repair.add_argument('--authorization', required=True)
    repair.add_argument('--state-root', required=True)
    repair.add_argument('--repair-instruction', default='', help='bounded host guidance within original task scope')
    repair.set_defaults(handler=handle_usage_repair_validation)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Sahai deterministic frontdoor/workflow CLI",
    )
    sub = parser.add_subparsers(dest="group", required=True)
    sub.add_parser("startup", help="host startup/recovery diagnostics; use startup --help")
    build_frontdoor_parser(sub)
    build_workflow_parser(sub)
    build_usage_parser(sub)
    output = sub.add_parser('output', help='host acknowledgement monitoring')
    outputs = output.add_subparsers(dest='command', required=True)
    status = outputs.add_parser('status')
    status.add_argument('--state-root', default='')
    status.add_argument('--stale-seconds', type=int, default=900)
    status.set_defaults(handler=handle_output_status)
    task = sub.add_parser('task', help='canonical host task records')
    tasks = task.add_subparsers(dest='command', required=True)
    scaffold = tasks.add_parser('scaffold')
    scaffold.add_argument('--task-id', required=True)
    scaffold.add_argument('--project', required=True)
    scaffold.add_argument('--brief', required=True, help='host-approved typed objective/scope/acceptance JSON')
    scaffold.set_defaults(handler=handle_task_scaffold)
    return parser


def main(argv: list[str] | None = None) -> int:
    supplied = list(sys.argv[1:] if argv is None else argv)
    if supplied[:1] == ["startup"]:
        sys.path.insert(0, str(FRONTDOOR_PATH.parent))
        import startup_recovery
        return startup_recovery.cli(supplied[1:])
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        frontdoor = frontdoor_module()
    except (OSError, RuntimeError, ImportError) as exc:
        print_blocked(str(exc))
        return 2

    run_store_error = frontdoor.run_store.RunStoreError
    try:
        payload = args.handler(frontdoor, args)
    except frontdoor.FrontdoorError as exc:
        print_blocked(str(exc))
        return 2
    except (ValueError, KeyError, TypeError) as exc:
        print_blocked(str(exc))
        return 2
    except run_store_error as exc:
        payload = {
            "schema_version": 1,
            "decision": "blocked",
            "reason": exc.reason_class,
            "errors": exc.errors,
        }
        print_json(payload)
        return 2

    print_json(payload)
    if payload.get("decision") == "blocked" or payload.get("request_status") == "blocked":
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
