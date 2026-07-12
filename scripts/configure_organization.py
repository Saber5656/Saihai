#!/usr/bin/env python3
"""Read organization configuration and produce execution decisions."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
from saihai_env import load_environment  # noqa: E402

ENV_DIAGNOSTICS = load_environment(checkout_root=REPO_ROOT, require_vault=True)
from typing import Any

ORG_ROOT = REPO_ROOT / "organization"
SETTINGS_PATH = ORG_ROOT / "settings.json"
ITB_RUNTIME_ROOT = ORG_ROOT / "runtime" / "infra-team-bootstrap"
ITB_BUILDER = ITB_RUNTIME_ROOT / "scripts" / "itb_bootstrap_builder.py"
ITB_HOOKS = ITB_RUNTIME_ROOT / "hooks"
ITD_MONITOR = ORG_ROOT / "runtime" / "infra-task-dispatcher" / "scripts" / "itd_monitor.py"
WORKFLOW_SELECTOR = ORG_ROOT / "runtime" / "workflows" / "scripts" / "workflow_selector.py"
WORKFLOW_FRONTDOOR = ORG_ROOT / "runtime" / "workflows" / "scripts" / "frontdoor_orchestrator.py"
WORKFLOW_FRONTDOOR_SERVER = ORG_ROOT / "runtime" / "workflows" / "scripts" / "frontdoor_server.py"
SAHAI_CLI = REPO_ROOT / "scripts" / "saihai.py"
VALIDATE_ALL = REPO_ROOT / "scripts" / "validate_all.py"
ITB_FACADE_COMMANDS = {
    "agent-call",
    "agent-switch",
    "provider-failover",
    "agent-surfaces",
    "transport-status",
}

TRUTHY = {"1", "true", "yes", "on", "enabled"}
FALSY = {"0", "false", "no", "off", "disabled"}

MAINTENANCE_PATTERNS = [
    r"組織",
    r"organization",
    r"Sahai",
    r"Saihai",
    r"Agent-Teams-Viewer",
    r"Agent-Org-Viewer",
    r"configure-organization",
    r"COMMON-AGENTS",
    r"\bHook\b",
    r"\bITB\b",
    r"gate-prompt-formatter",
    r"infra-team-bootstrap",
]

STRICT_PATTERNS = [
    r"修正",
    r"実装",
    r"変更",
    r"追加",
    r"削除",
    r"commit",
    r"push",
    r"PR",
    r"レビュー",
    r"security",
    r"権限",
    r"policy",
    r"モデル",
    r"hook",
    r"本番",
    r"検証",
    r"テスト",
    r"複数",
    r"認証",
    r"設計",
    r"採用",
    r"選定",
    r"判断",
    r"worktree",
    r"Vault",
]

FAST_PATTERNS = [
    r"教えて",
    r"確認して",
    r"調べる",
    r"比較",
    r"どちら",
    r"人気",
    r"売上",
    r"シェア",
    r"どこ",
    r"何",
    r"短く",
    r"軽く",
    r"一言",
]

WEATHER_RE = re.compile(r"(天気|天気予報|weather|forecast)", re.I)
LOCATION_HINT_RE = re.compile(
    r"(東京|大阪|京都|名古屋|福岡|札幌|沖縄|横浜|神戸|Seattle|San Francisco|New York|Los Angeles|Tokyo|Osaka|市|区|県|府|都|州)",
    re.I,
)


def read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def bool_env(name: str) -> bool | None:
    raw = os.environ.get(name)
    if raw is None:
        return None
    value = raw.strip().lower()
    if value in TRUTHY:
        return True
    if value in FALSY:
        return False
    return None


def load_state() -> dict[str, Any]:
    settings = read_json(SETTINGS_PATH, {})
    role_index = read_json(ORG_ROOT / "role-index.json", {"roles": []})
    policy_index = read_json(ORG_ROOT / "policy-index.json", {"policies": []})
    return {
        "settings": settings,
        "role_count": len(role_index.get("roles") or []),
        "policy_count": len(policy_index.get("policies") or []),
        "repo_root": str(REPO_ROOT),
        "generated_at": time.time(),
    }


def runtime_paths() -> dict[str, Any]:
    paths = {
        "itb_runtime_root": ITB_RUNTIME_ROOT,
        "itb_builder": ITB_BUILDER,
        "itb_hooks": ITB_HOOKS,
        "itd_monitor": ITD_MONITOR,
        "workflow_selector": WORKFLOW_SELECTOR,
        "workflow_frontdoor": WORKFLOW_FRONTDOOR,
        "workflow_frontdoor_server": WORKFLOW_FRONTDOOR_SERVER,
        "sahai_cli": SAHAI_CLI,
        "saihai_cli": SAHAI_CLI,
        "validate_all": VALIDATE_ALL,
        "role_root": ORG_ROOT / "roles",
        "runtime_registry": ITB_RUNTIME_ROOT / "config" / "role-agent-registry.yaml",
        "runtime_registry_mirror": ORG_ROOT / "runtime" / "role-agent-registry.yaml",
    }
    return {
        "schema_version": 1,
        "decision": "ok",
        "sahai_root": str(REPO_ROOT),
        # Legacy compatibility keys for callers that have not renamed yet.
        "saihai_root": str(REPO_ROOT),
        "agent_teams_viewer_root": str(REPO_ROOT),
        "runtime_paths": {
            key: {"path": str(path), "exists": path.exists()}
            for key, path in paths.items()
        },
    }


def run_runtime_script(script: Path, args: list[str]) -> int:
    if not script.exists():
        print(
            json.dumps(
                {
                    "schema_version": 1,
                    "decision": "block",
                    "reason": f"runtime script missing: {script}",
                },
                ensure_ascii=False,
            )
        )
        return 2
    command = [sys.executable, str(script), *args]
    completed = subprocess.run(command, check=False)
    return completed.returncode


def has_any(patterns: list[str], text: str) -> bool:
    return any(re.search(pattern, text, flags=re.IGNORECASE) for pattern in patterns)


def classify(prompt: str, *, requested_mode: str = "", organization_state: str = "") -> dict[str, Any]:
    state = load_state()
    settings = state["settings"]
    control = settings.get("control") or {}
    maintenance = settings.get("maintenance") or {}
    hook_policy = settings.get("hook_policy") or {}
    provider_transport_policy = settings.get("provider_transport_policy") or {}
    modes = settings.get("modes") or {}

    env_enabled = bool_env("AGENT_ORG_ENABLED")
    env_maintenance = bool_env("AGENT_ORG_MAINTENANCE")
    configured_state = organization_state or os.environ.get("AGENT_ORG_STATE") or control.get("state") or "enabled"
    if env_enabled is False:
        configured_state = "disabled"
    if env_maintenance is True:
        configured_state = "maintenance"

    text = prompt.strip()
    prompt_is_maintenance = has_any(MAINTENANCE_PATTERNS, text)
    missing_information: list[str] = []
    clarification_required = False
    if WEATHER_RE.search(text) and not LOCATION_HINT_RE.search(text):
        missing_information.append("location")
        clarification_required = True

    if configured_state == "disabled":
        mode = "fast"
        reason = "organization disabled by configuration"
        flow_enabled = False
        main_agent_can_execute = True
    elif configured_state == "maintenance" or prompt_is_maintenance:
        mode = "fast"
        reason = "organization maintenance or organization-system prompt"
        flow_enabled = bool(maintenance.get("organization_flow_enabled", False))
        main_agent_can_execute = bool(maintenance.get("main_agent_can_execute", True))
        configured_state = "maintenance"
    else:
        if requested_mode in {"fast", "strict"}:
            mode = requested_mode
            reason = f"mode explicitly requested: {requested_mode}"
        elif len(text) <= 120 and has_any(FAST_PATTERNS, text) and not has_any(STRICT_PATTERNS, text):
            mode = "fast"
            reason = "small low-risk prompt matched fast heuristics"
        else:
            mode = "strict"
            reason = "default strict mode for non-trivial work"
        flow_enabled = True
        main_agent_can_execute = bool((modes.get(mode) or {}).get("main_agent_can_execute"))

    mode_config = modes.get(mode) or {}
    return {
        "schema_version": 1,
        "decision": "ok",
        "organization_state": configured_state,
        "organization_flow_enabled": flow_enabled,
        "mode": mode,
        "reason": reason,
        "task_required": True,
        "task_policy": control.get("task_policy", "all_work_must_have_task_record"),
        "main_agent_can_execute": main_agent_can_execute,
        "role_dispatch_required": bool(mode_config.get("role_dispatch_required", False)) and flow_enabled,
        "review_required": mode_config.get("review_required", "optional" if mode == "fast" else "required"),
        "vault_update_required": True,
        "missing_information": missing_information,
        "clarification_required": clarification_required,
        "hook_policy": {
            "mode": hook_policy.get("mode", "observer"),
            "hard_block": bool(hook_policy.get("hard_block", False)),
        },
        "provider_transport_policy": provider_transport_policy,
        "performance_target_seconds": mode_config.get("performance_target_seconds"),
        "sahai_root": str(REPO_ROOT),
        # Legacy compatibility keys for callers that have not renamed yet.
        "saihai_root": str(REPO_ROOT),
        "agent_teams_viewer_root": str(REPO_ROOT),
        "role_count": state["role_count"],
        "policy_count": state["policy_count"],
        "generated_at": state["generated_at"],
    }


def main() -> None:
    if len(sys.argv) > 1 and sys.argv[1] == "itb":
        raise SystemExit(run_runtime_script(ITB_BUILDER, sys.argv[2:]))
    if len(sys.argv) > 1 and sys.argv[1] == "itd-monitor":
        raise SystemExit(run_runtime_script(ITD_MONITOR, sys.argv[2:]))
    if len(sys.argv) > 1 and sys.argv[1] == "workflow-selector":
        raise SystemExit(run_runtime_script(WORKFLOW_SELECTOR, sys.argv[2:]))
    if len(sys.argv) > 1 and sys.argv[1] == "workflow-frontdoor":
        raise SystemExit(run_runtime_script(WORKFLOW_FRONTDOOR, sys.argv[2:]))
    if len(sys.argv) > 1 and sys.argv[1] == "workflow-frontdoor-server":
        raise SystemExit(run_runtime_script(WORKFLOW_FRONTDOOR_SERVER, sys.argv[2:]))
    if len(sys.argv) > 1 and sys.argv[1] == "validate-all":
        raise SystemExit(run_runtime_script(VALIDATE_ALL, sys.argv[2:]))
    if len(sys.argv) > 1 and sys.argv[1] in ITB_FACADE_COMMANDS:
        raise SystemExit(run_runtime_script(ITB_BUILDER, sys.argv[1:]))

    parser = argparse.ArgumentParser(description="Configure organization execution mode")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status")
    sub.add_parser("runtime-paths")
    classify_parser = sub.add_parser("classify")
    classify_parser.add_argument("--prompt", default="")
    classify_parser.add_argument("--mode", choices=["fast", "strict"], default="")
    classify_parser.add_argument("--organization-state", choices=["enabled", "disabled", "maintenance"], default="")
    itb_parser = sub.add_parser("itb", help="Run the Sahai ITB runtime builder")
    itb_parser.add_argument("runtime_args", nargs=argparse.REMAINDER)
    itd_parser = sub.add_parser("itd-monitor", help="Run the Sahai ITD monitor runtime")
    itd_parser.add_argument("runtime_args", nargs=argparse.REMAINDER)
    workflow_parser = sub.add_parser("workflow-selector", help="Run Orchestrator P0 workflow selector")
    workflow_parser.add_argument("runtime_args", nargs=argparse.REMAINDER)
    workflow_frontdoor_parser = sub.add_parser("workflow-frontdoor", help="Run Orchestrator P0 frontdoor harness")
    workflow_frontdoor_parser.add_argument("runtime_args", nargs=argparse.REMAINDER)
    workflow_frontdoor_server_parser = sub.add_parser("workflow-frontdoor-server", help="Run Orchestrator P0 frontdoor HTTP API")
    workflow_frontdoor_server_parser.add_argument("runtime_args", nargs=argparse.REMAINDER)
    validate_all_parser = sub.add_parser("validate-all", help="Run all offline validation suites")
    validate_all_parser.add_argument("runtime_args", nargs=argparse.REMAINDER)
    for command in sorted(ITB_FACADE_COMMANDS):
        facade_parser = sub.add_parser(command, help=f"Run ITB facade command: {command}")
        facade_parser.add_argument("runtime_args", nargs=argparse.REMAINDER)
    args = parser.parse_args()

    if args.command == "status":
        payload = load_state()
    elif args.command == "runtime-paths":
        payload = runtime_paths()
    elif args.command == "itb":
        raise SystemExit(run_runtime_script(ITB_BUILDER, args.runtime_args))
    elif args.command == "itd-monitor":
        raise SystemExit(run_runtime_script(ITD_MONITOR, args.runtime_args))
    elif args.command == "workflow-selector":
        raise SystemExit(run_runtime_script(WORKFLOW_SELECTOR, args.runtime_args))
    elif args.command == "workflow-frontdoor":
        raise SystemExit(run_runtime_script(WORKFLOW_FRONTDOOR, args.runtime_args))
    elif args.command == "workflow-frontdoor-server":
        raise SystemExit(run_runtime_script(WORKFLOW_FRONTDOOR_SERVER, args.runtime_args))
    elif args.command == "validate-all":
        raise SystemExit(run_runtime_script(VALIDATE_ALL, args.runtime_args))
    elif args.command in ITB_FACADE_COMMANDS:
        raise SystemExit(run_runtime_script(ITB_BUILDER, [args.command, *args.runtime_args]))
    else:
        payload = classify(
            args.prompt,
            requested_mode=args.mode,
            organization_state=args.organization_state,
        )
    print(json.dumps(payload, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
