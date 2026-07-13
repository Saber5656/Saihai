#!/usr/bin/env python3
"""Build and check ITB bootstrap state for Claude/Codex hook wrappers."""

from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import hashlib
import json
import os
import re
import select as _select
import shlex
import shutil
import signal
import subprocess
import sys
import time
import traceback
import uuid
from pathlib import Path
from typing import Any, Callable

SAIHAI_CHECKOUT_ROOT = Path(__file__).resolve().parents[4]
if str(SAIHAI_CHECKOUT_ROOT) not in sys.path:
    sys.path.insert(0, str(SAIHAI_CHECKOUT_ROOT))

from directory_paths import expand_path_aliases, load_environment, validate_vault  # noqa: E402

ENV_DIAGNOSTICS = load_environment(checkout_root=SAIHAI_CHECKOUT_ROOT, require_catalog=True)

try:
    import yaml as _pyyaml
except ModuleNotFoundError:  # pragma: no cover - exercised when PyYAML is absent.
    _pyyaml = None


ITB_ROOT = Path(__file__).resolve().parents[1]
SAIHAI_ROOT = Path(
    os.environ.get("SAIHAI_ROOT")
    or str(ITB_ROOT.parents[2])
).expanduser()
SAIHAI_ROLE_ROOT = SAIHAI_ROOT / "organization" / "roles"
SKILLS_ROOT = Path(
    os.environ.get("SKILLS_ROOT")
    or str(SAIHAI_ROOT / "organization" / "roles")
).expanduser()
SAIHAI_MIGRATED_ROLE_IDS = frozenset(
    {
        "business-director",
        "business-information-strategy",
        "business-legal-reviewer",
        "business-marketing-director",
        "business-partnership-manager",
        "business-strategy",
        "contents-director",
        "contents-formatter",
        "contents-quality-manager",
        "contents-researcher",
        "gate-prompt-formatter",
        "gate-response-humanizer",
        "gate-task-assessor",
        "gate-task-creator",
        "gate-task-evaluator",
        "gate-task-guardian",
        "git-publisher",
        "infra-director",
        "infra-local-qa",
        "infra-task-dispatcher",
        "infra-team-bootstrap",
        "teams-developer",
        "teams-project-manager",
        "tech-architect",
        "tech-backend",
        "tech-data-structure",
        "tech-debugger",
        "tech-designer",
        "tech-devopssec",
        "tech-director",
        "tech-docs",
        "tech-frontend",
        "tech-infrastructure",
        "tech-lead",
        "tech-mobile",
        "tech-performance",
        "tech-qa",
        "tech-reviewer",
        "tech-security",
        "tech-tester",
    }
)
HOOK_BUNDLE_DIR = ITB_ROOT / "hooks"
TEAM_CONFIG = ITB_ROOT / "references" / "team-config.md"
MODEL_REGISTRY = ITB_ROOT / "references" / "model-registry.md"
ROLE_AGENT_REGISTRY = ITB_ROOT / "config" / "role-agent-registry.yaml"
COMPLETION_CHAIN_CONFIG = ITB_ROOT / "config" / "completion-chain.yaml"
GATE_OUTPUT_SCHEMAS_CONFIG = ITB_ROOT / "config" / "gate-output-schemas.yaml"
CHILD_AGENT_ENV = "ITB_AGENT_CHILD"
AGENTS_VAULT_ROOT = Path(os.environ.get("AGENTS_VAULT_ROOT") or ".").expanduser()
POLICY_ROOT = AGENTS_VAULT_ROOT / "03-Contexts/Policies"
POLICY_DIGEST_SOURCES = {
    "AI-Organization": POLICY_ROOT / "AI-Organization.md",
    "Gate-IO-Contract": POLICY_ROOT / "Gate-IO-Contract.md",
    "Dispatcher-IO-Contract": POLICY_ROOT / "Dispatcher-IO-Contract.md",
    "Task-File-Conventions": POLICY_ROOT / "Task-File-Conventions.md",
}
POLICY_DIGEST_SKILL_BLOCK_BEGIN = "<!-- ITB_POLICY_DIGEST_SNAPSHOT_START -->"
POLICY_DIGEST_SKILL_BLOCK_END = "<!-- ITB_POLICY_DIGEST_SNAPSHOT_END -->"
USER_VAULT_ROOT = Path(
    os.environ.get("USER_VAULT_ROOT")
    or str(AGENTS_VAULT_ROOT)
).expanduser()
YASU_VAULT_ROOT = USER_VAULT_ROOT
DEFAULT_PROVIDER_PERMISSION_MODE = "auto"
DEFAULT_CODEX_APPROVAL_POLICY = "never"
DEFAULT_CLAUDE_HAIKU_SONNET_EFFORT = "medium"
DEFAULT_CLAUDE_OPUS_EFFORT = "max"
DEFAULT_CODEX_MODEL = "gpt-5.5"
DEFAULT_CODEX_REASONING_EFFORT = "xhigh"
DEFAULT_CODEX_SERVICE_TIER = "fast"
ALLOWED_QUEUE_FINALIZERS = {"role-report"}
ALLOWED_REPORT_WRITE_MODES = {"builder_atomic"}
STATIC_ROLE_LAYERS = frozenset({"gate", "tpm", "director", "worker"})
ASSIGNMENT_ROLE_VALUES = frozenset({"none", "implementer", "reviewer", "qa", "approver", "observer"})
AGENT_CALL_MANIFEST_VERSION = "1"
AGENT_SWITCH_MANIFEST_VERSION = "1"
ROLE_LAYER_CONTEXT_PRESET_REFS: dict[str, list[dict[str, str]]] = {
    "gate": [
        {"type": "policy", "path": "organization/policies/AI-Organization.md"},
        {"type": "contract", "path": "organization/policies/Gate-IO-Contract.md"},
    ],
    "tpm": [
        {"type": "contract", "path": "organization/runtime/infra-team-bootstrap/config/completion-chain.yaml"},
        {"type": "catalog", "path": "organization/runtime/infra-team-bootstrap/config/role-agent-registry.yaml"},
        {"type": "catalog", "path": "organization/runtime/infra-team-bootstrap/references/team-config.md"},
    ],
    "director": [
        {"type": "catalog", "path": "organization/runtime/infra-team-bootstrap/config/role-agent-registry.yaml"},
        {"type": "catalog", "path": "organization/runtime/infra-team-bootstrap/references/team-config.md"},
        {"type": "contract", "path": "organization/runtime/agent-call-contract.md"},
    ],
    "worker": [
        {"type": "contract", "path": "organization/runtime/agent-call-contract.md"},
        {"type": "contract", "path": "organization/runtime/infra-team-bootstrap/SKILL.md"},
    ],
}
ASSIGNMENT_ROLE_CONTEXT_PRESET_REFS: dict[str, list[dict[str, str]]] = {
    "none": [],
    "implementer": [
        {"type": "checklist", "path": "organization/runtime/agent-call-contract.md#assignment-overlays"}
    ],
    "reviewer": [
        {"type": "checklist", "path": "organization/runtime/agent-call-contract.md#assignment-overlays"}
    ],
    "qa": [
        {"type": "checklist", "path": "organization/runtime/agent-call-contract.md#assignment-overlays"}
    ],
    "approver": [
        {"type": "checklist", "path": "organization/runtime/agent-call-contract.md#assignment-overlays"}
    ],
    "observer": [
        {"type": "checklist", "path": "organization/runtime/agent-call-contract.md#assignment-overlays"}
    ],
}
PROVIDER_SWITCH_DEFAULT_EXECUTION_MODES = {"anthropic": "agent", "openai": "codex"}
QUEUE_FINALIZER_TRANSPORT_TOOLS = {"Bash"}
AUTO_HANDOFF_CONTEXT_KEYS = (
    "vault_root",
    "vaultRoot",
    "auto_chain_dry_run",
    "autoChainDryRun",
    "prompt_submit_chain_id",
    "promptSubmitChainId",
    "task_detail_path",
    "taskDetailPath",
    "task_path",
    "taskPath",
    "context_ref",
    "contextRef",
)
CONTEXT_SURFACE_SKIP_DIR_NAMES = {
    ".git",
    ".hg",
    ".svn",
    ".obsidian",
    ".mypy_cache",
    ".pytest_cache",
    "__pycache__",
    "node_modules",
    ".venv",
    "venv",
}
CLAUDE_ACTIVATION_BUDGET_DEFAULTS_USD = {
    "haiku": "2.00",
    "sonnet": "8.00",
    "opus": "30.00",
    "default": "5.00",
}
DEFAULT_PROVIDER_ADD_DIR_CANDIDATES = (
    AGENTS_VAULT_ROOT,
)
SHARED_FILE_ROOTS = (
    AGENTS_VAULT_ROOT,
    YASU_VAULT_ROOT,
)
MODEL_REGISTRY_REQUIRED_COLUMNS = {
    "agent_id",
    "team",
    "status",
    "always_active",
    "provider",
    "primary_model",
    "execution_mode",
}
HOOK_WRAPPER_INSTALL_FILES = (
    "itb-hook-common.sh",
    "itb-session-start.sh",
    "itb-final-response-guard.sh",
)
SESSION_START_COMPACT_SOURCE_MARKERS = {"resume", "clear", "compact"}
EXECUTION_CONTEXT_TYPES = {"none", "intake", "execution"}
FINAL_GATE_REASON_CODES = {"no_active_context", "complete", "incomplete", "required_approval", "blocked"}
FINAL_GATE_NEXT_ACTIONS = {"plan", "fix", "ask_human", "mark_blocked"}
FINAL_GATE_DEFAULT_RECOVERY_CYCLE_BUDGET = 5
FINAL_GATE_RECOVERY_CYCLE_TUNING_RANGE = [5, 8]
FINAL_GATE_SAME_BLOCKER_CONSECUTIVE_CAP = 2
FINAL_GATE_BUDGET_UNIT = "gate_block_recovery_cycle"


def load_hook_input() -> dict[str, Any]:
    if sys.stdin.isatty():
        return {}
    try:
        ready, _, _ = _select.select([sys.stdin], [], [], 0.05)
    except (OSError, ValueError):
        ready = [sys.stdin]
    if not ready:
        return {}
    fd = sys.stdin.fileno()
    old_flags = fcntl.fcntl(fd, fcntl.F_GETFL)
    chunks: list[bytes] = []
    try:
        fcntl.fcntl(fd, fcntl.F_SETFL, old_flags | os.O_NONBLOCK)
        while True:
            try:
                chunk = os.read(fd, 65536)
            except BlockingIOError:
                break
            if not chunk:
                break
            chunks.append(chunk)
    finally:
        fcntl.fcntl(fd, fcntl.F_SETFL, old_flags)
    raw = b"".join(chunks).decode("utf-8", errors="replace")
    if not raw.strip():
        return {}
    return json.loads(raw)


def load_json_file_input(path_value: str) -> dict[str, Any]:
    path = Path(path_value).expanduser()
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("--input-json-file must contain a JSON object")
    return data


def safe_id(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", value or "unknown-session")


def bool_cell(value: str) -> bool:
    return value.strip().lower() in {"true", "yes", "1"}


def normalize_string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    return [str(value).strip()] if str(value).strip() else []


def expand_config_path_value(value: Any) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    raw = expand_path_aliases(raw)
    raw = raw.replace("${HOME}", str(Path.home())).replace("$HOME", str(Path.home()))
    return str(Path(raw).expanduser())


def strip_yaml_comment(line: str) -> str:
    in_single = False
    in_double = False
    escaped = False
    for index, char in enumerate(line):
        if in_double and escaped:
            escaped = False
            continue
        if in_double and char == "\\":
            escaped = True
            continue
        if char == "'" and not in_double:
            in_single = not in_single
            continue
        if char == '"' and not in_single:
            in_double = not in_double
            continue
        if char == "#" and not in_single and not in_double:
            if index == 0 or line[index - 1].isspace():
                return line[:index].rstrip()
    return line.rstrip()


def yaml_entries(raw: str, path: Path) -> list[tuple[int, int, str]]:
    entries: list[tuple[int, int, str]] = []
    for lineno, line in enumerate(raw.splitlines(), start=1):
        if "\t" in line[: len(line) - len(line.lstrip())]:
            raise ValueError(f"YAML config must use spaces for indentation: {path}:{lineno}")
        stripped = line.strip()
        if not stripped or stripped in {"---", "..."} or stripped.startswith("#"):
            continue
        without_comment = strip_yaml_comment(line)
        if not without_comment.strip():
            continue
        indent = len(without_comment) - len(without_comment.lstrip(" "))
        entries.append((lineno, indent, without_comment.strip()))
    return entries


def split_yaml_top_level(value: str, delimiter: str) -> list[str]:
    parts: list[str] = []
    start = 0
    depth = 0
    in_single = False
    in_double = False
    escaped = False
    for index, char in enumerate(value):
        if in_double and escaped:
            escaped = False
            continue
        if in_double and char == "\\":
            escaped = True
            continue
        if char == "'" and not in_double:
            in_single = not in_single
            continue
        if char == '"' and not in_single:
            in_double = not in_double
            continue
        if in_single or in_double:
            continue
        if char in "[{(":
            depth += 1
        elif char in "]})":
            depth -= 1
        elif char == delimiter and depth == 0:
            parts.append(value[start:index].strip())
            start = index + 1
    parts.append(value[start:].strip())
    return parts


def split_yaml_key_value(content: str, path: Path, lineno: int) -> tuple[str, str]:
    depth = 0
    in_single = False
    in_double = False
    escaped = False
    for index, char in enumerate(content):
        if in_double and escaped:
            escaped = False
            continue
        if in_double and char == "\\":
            escaped = True
            continue
        if char == "'" and not in_double:
            in_single = not in_single
            continue
        if char == '"' and not in_single:
            in_double = not in_double
            continue
        if in_single or in_double:
            continue
        if char in "[{(":
            depth += 1
        elif char in "]})":
            depth -= 1
        elif char == ":" and depth == 0:
            key = content[:index].strip()
            value = content[index + 1 :].strip()
            if not key:
                raise ValueError(f"YAML config key must not be empty: {path}:{lineno}")
            return yaml_key_scalar(key), value
    raise ValueError(f"YAML config mapping entry must contain ':': {path}:{lineno}")


def yaml_key_scalar(value: str) -> str:
    parsed = parse_yaml_scalar(value)
    return str(parsed)


def parse_yaml_inline_sequence(value: str) -> list[Any]:
    body = value[1:-1].strip()
    if not body:
        return []
    return [parse_yaml_scalar(part) for part in split_yaml_top_level(body, ",")]


def parse_yaml_inline_mapping(value: str) -> dict[str, Any]:
    body = value[1:-1].strip()
    if not body:
        return {}
    result: dict[str, Any] = {}
    dummy_path = Path("<inline-yaml>")
    for item in split_yaml_top_level(body, ","):
        key, raw_value = split_yaml_key_value(item, dummy_path, 1)
        if key in result:
            raise ValueError(f"YAML config duplicate key in inline mapping: {key}")
        result[key] = parse_yaml_scalar(raw_value)
    return result


def parse_yaml_scalar(value: str) -> Any:
    value = value.strip()
    if value == "":
        return ""
    if value[0] in {'"', "[", "{"}:
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            pass
    if value.startswith("'") and value.endswith("'") and len(value) >= 2:
        return value[1:-1].replace("''", "'")
    if value.startswith("[") and value.endswith("]"):
        return parse_yaml_inline_sequence(value)
    if value.startswith("{") and value.endswith("}"):
        return parse_yaml_inline_mapping(value)
    lowered = value.lower()
    if lowered in {"true", "yes", "on"}:
        return True
    if lowered in {"false", "no", "off"}:
        return False
    if lowered in {"null", "none", "~"}:
        return None
    if re.fullmatch(r"[-+]?\d+", value):
        try:
            return int(value)
        except ValueError:
            pass
    if re.fullmatch(r"[-+]?(?:\d+\.\d*|\d*\.\d+)(?:[eE][-+]?\d+)?", value):
        try:
            return float(value)
        except ValueError:
            pass
    return value


def parse_yaml_block(
    entries: list[tuple[int, int, str]], index: int, indent: int, path: Path
) -> tuple[Any, int]:
    if index >= len(entries):
        return {}, index
    lineno, actual_indent, content = entries[index]
    if actual_indent < indent:
        return {}, index
    if actual_indent > indent:
        raise ValueError(f"YAML config unexpected indentation: {path}:{lineno}")
    if content == "-" or content.startswith("- "):
        items: list[Any] = []
        while index < len(entries):
            lineno, item_indent, item_content = entries[index]
            if item_indent < indent:
                break
            if item_indent > indent:
                raise ValueError(f"YAML config unexpected sequence indentation: {path}:{lineno}")
            if item_content != "-" and not item_content.startswith("- "):
                break
            remainder = "" if item_content == "-" else item_content[2:].strip()
            index += 1
            if remainder:
                items.append(parse_yaml_scalar(remainder))
            elif index < len(entries) and entries[index][1] > indent:
                nested, index = parse_yaml_block(entries, index, entries[index][1], path)
                items.append(nested)
            else:
                items.append(None)
        return items, index

    result: dict[str, Any] = {}
    while index < len(entries):
        lineno, entry_indent, entry_content = entries[index]
        if entry_indent < indent:
            break
        if entry_indent > indent:
            raise ValueError(f"YAML config unexpected mapping indentation: {path}:{lineno}")
        if entry_content == "-" or entry_content.startswith("- "):
            break
        key, raw_value = split_yaml_key_value(entry_content, path, lineno)
        if key in result:
            raise ValueError(f"YAML config duplicate key {key!r}: {path}:{lineno}")
        index += 1
        if raw_value:
            result[key] = parse_yaml_scalar(raw_value)
        elif index < len(entries) and entries[index][1] > indent:
            result[key], index = parse_yaml_block(entries, index, entries[index][1], path)
        else:
            result[key] = None
    return result, index


def parse_basic_yaml_config(raw: str, path: Path) -> Any:
    entries = yaml_entries(raw, path)
    if not entries:
        return None
    parsed, index = parse_yaml_block(entries, 0, entries[0][1], path)
    if index != len(entries):
        lineno = entries[index][0]
        raise ValueError(f"YAML config parse stopped before end of file: {path}:{lineno}")
    return parsed


def load_yaml_config(path: Path) -> Any:
    raw = path.read_text(encoding="utf-8")
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass
    if _pyyaml is not None:
        try:
            return _pyyaml.safe_load(raw)
        except Exception as exc:  # pragma: no cover - depends on optional PyYAML.
            raise ValueError(f"YAML config parse failed: {path}: {exc}") from exc
    try:
        return parse_basic_yaml_config(raw, path)
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError(f"YAML config parse failed: {path}: {exc}") from exc


def parse_model_registry_file(path: Path) -> list[dict[str, str]]:
    lines = path.read_text(encoding="utf-8").splitlines()
    headers: list[str] | None = None
    rows: list[dict[str, str]] = []
    seen_agent_ids: dict[str, int] = {}
    for lineno, line in enumerate(lines, start=1):
        if not line.startswith("|"):
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if len(cells) < 3:
            continue
        if cells[0] == "agent_id":
            headers = cells
            missing_columns = sorted(MODEL_REGISTRY_REQUIRED_COLUMNS - set(headers))
            if missing_columns:
                raise ValueError(
                    f"model registry missing required columns at {path}:{lineno}: "
                    + ", ".join(missing_columns)
                )
            continue
        if headers is None or set(cells[0]) <= {"-"}:
            continue
        if len(cells) != len(headers):
            row_id = cells[0] or "<empty>"
            raise ValueError(
                f"model registry row has {len(cells)} cells but expected {len(headers)} "
                f"at {path}:{lineno}: {row_id}"
            )
        row = dict(zip(headers, cells))
        agent_id = row.get("agent_id", "").strip()
        if not agent_id:
            raise ValueError(f"model registry agent_id must not be empty at {path}:{lineno}")
        if agent_id in seen_agent_ids:
            raise ValueError(
                f"model registry duplicate agent_id at {path}:{lineno}: "
                f"{agent_id} first seen at line {seen_agent_ids[agent_id]}"
            )
        seen_agent_ids[agent_id] = lineno
        rows.append(row)
    if headers is None:
        raise ValueError(f"model registry header row not found: {path}")
    return rows


def parse_registry() -> list[dict[str, str]]:
    return parse_model_registry_file(MODEL_REGISTRY)


def registry_row_for(agent_id: str) -> dict[str, str]:
    for row in parse_registry():
        if row.get("agent_id") == agent_id:
            return row
    return {}


def parse_skill_frontmatter(skill_path: Path) -> dict[str, str]:
    if not skill_path.exists():
        return {}
    lines = skill_path.read_text(encoding="utf-8").splitlines()
    if not lines or lines[0].strip() != "---":
        return {}
    frontmatter: dict[str, str] = {}
    for line in lines[1:]:
        if line.strip() == "---":
            break
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        frontmatter[normalized_key(key)] = normalize_cell(value)
    return frontmatter


def normalize_allowed_tools(value: Any) -> list[str]:
    if isinstance(value, list):
        raw_values = value
    else:
        raw_values = str(value or "").split(",")
    return [normalize_cell(item) for item in raw_values if normalize_cell(item)]


def allowed_tools_argument(value: Any) -> str:
    tools = normalize_allowed_tools(value)
    return ",".join(tools) if tools else "default"


def sahai_role_skill_path(role_id: str) -> Path:
    return SAIHAI_ROLE_ROOT / role_id / "skill.md"


def legacy_sahai_role_skill_path(role_id: str) -> Path:
    return SAIHAI_ROLE_ROOT / f"{role_id}.md"


def role_definition_path(role_id: str) -> Path:
    sahai_path = sahai_role_skill_path(role_id)
    if sahai_path.exists():
        return sahai_path
    legacy_sahai_path = legacy_sahai_role_skill_path(role_id)
    if legacy_sahai_path.exists():
        return legacy_sahai_path
    skill_path = SKILLS_ROOT / role_id / "SKILL.md"
    if skill_path.exists():
        return skill_path
    return skill_path


def role_skill_path(role_id: str) -> Path:
    return role_definition_path(role_id)


def role_is_migrated_to_sahai(role_id: str) -> bool:
    return role_id in SAIHAI_MIGRATED_ROLE_IDS and (
        sahai_role_skill_path(role_id).exists() or legacy_sahai_role_skill_path(role_id).exists()
    )


def same_filesystem_path(left: Path, right: Path) -> bool:
    try:
        return left.resolve() == right.resolve()
    except OSError:
        return left.expanduser().absolute() == right.expanduser().absolute()


def skill_allowed_tools(role_id: str) -> list[str]:
    if role_is_migrated_to_sahai(role_id):
        return []
    skill_path = role_skill_path(role_id)
    frontmatter = parse_skill_frontmatter(skill_path)
    return normalize_allowed_tools(frontmatter.get("allowed_tools", ""))


def tools_argument_for_role(row: dict[str, Any]) -> str:
    return allowed_tools_argument(row.get("allowed_tools", []))


def transport_tools_argument_for_role(row: dict[str, Any]) -> str:
    tools = normalize_allowed_tools(row.get("allowed_tools", []))
    if (
        truthy_input(row.get("queue_consumer"))
        and normalize_cell(row.get("queue_finalizer")) == "role-report"
    ):
        for tool in sorted(QUEUE_FINALIZER_TRANSPORT_TOOLS):
            if tool not in tools:
                tools.append(tool)
    return allowed_tools_argument(tools)


def role_report_writer_command(
    runtime: str,
    state_root: Path | str,
    session_id: str,
    *,
    role_id: str = "",
    message_id: str = "",
) -> str:
    args = [
        "python3",
        str(ITB_ROOT / "scripts" / "itb_bootstrap_builder.py"),
        "role-report",
        "--runtime",
        runtime,
        "--state-root",
        str(state_root),
        "--session-id",
        session_id,
    ]
    if role_id:
        args.extend(["--role-id", role_id])
    if message_id:
        args.extend(["--message-id", message_id])
    return shlex.join(args)


def claude_transport_allowed_tools_argument_for_role(row: dict[str, Any]) -> str:
    patterns: list[str] = []
    runtime = normalize_cell(row.get("runtime")) or "codex"
    state_root = normalize_cell(row.get("state_root"))
    session_id = normalize_cell(row.get("parent_session_id"))
    role_id = normalize_cell(row.get("agent_id") or row.get("role_id"))
    if truthy_input(row.get("queue_consumer")) and normalize_cell(row.get("queue_finalizer")) == "role-report":
        if state_root and session_id:
            writer_command = role_report_writer_command(runtime, state_root, session_id, role_id=role_id)
            patterns.append(f"Bash({writer_command} --message-id * --report-json *)")
        elif role_id:
            patterns.append(
                f"Bash(python3 {ITB_ROOT / 'scripts' / 'itb_bootstrap_builder.py'} role-report * --role-id {role_id} --message-id * --report-json *)"
            )
    return ",".join(dict.fromkeys(patterns))


def claude_tools_argument_for_role(row: dict[str, Any]) -> str:
    tools = normalize_allowed_tools(row.get("allowed_tools", []))
    agent_id = normalize_cell(row.get("agent_id") or row.get("role_id"))
    if not tools and agent_id:
        registry_row = role_agent_row_for(agent_id)
        tools = normalize_allowed_tools(registry_row.get("allowed_tools", [])) if registry_row else []
    if not tools and agent_id:
        tools = skill_allowed_tools(agent_id)
    for tool in normalize_allowed_tools(claude_transport_allowed_tools_argument_for_role(row)):
        if tool not in tools:
            tools.append(tool)
    return ",".join(dict.fromkeys(tools))


def default_tools_argument_for_agent(agent_id: str, *, session_dir: Path | None = None) -> str:
    if session_dir is not None:
        roster_path = session_dir / "roster.json"
        try:
            roster = read_json(roster_path) if roster_path.exists() else []
        except (OSError, json.JSONDecodeError):
            roster = []
        if isinstance(roster, list):
            for row in roster:
                if isinstance(row, dict) and row.get("agent_id") == agent_id:
                    registry_row = role_agent_row_for(agent_id)
                    effective_row = dict(registry_row)
                    effective_row.update(row)
                    tools = transport_tools_argument_for_role(effective_row)
                    if tools != "default":
                        return tools
                    skill_tools = allowed_tools_argument(skill_allowed_tools(agent_id))
                    if skill_tools != "default":
                        return skill_tools
                    return "default"
    for row in role_agent_rows():
        if row.get("agent_id") == agent_id:
            return transport_tools_argument_for_role(row)
    return "default"


def validate_tools_argument_for_role(row: dict[str, Any], tools: str) -> str:
    requested_tools = normalize_allowed_tools(tools)
    if not requested_tools:
        return ""
    invalid_tools = [tool for tool in requested_tools if tool not in AGENT_DISPATCH_ALLOWED_TOOL_NAMES]
    if invalid_tools:
        allowed = ", ".join(sorted(AGENT_DISPATCH_ALLOWED_TOOL_NAMES))
        return f"unsupported agent-dispatch tools: {','.join(invalid_tools)}; allowed={allowed}"
    role_tools = normalize_allowed_tools(row.get("allowed_tools", []))
    if not role_tools:
        agent_id = normalize_cell(row.get("agent_id", ""))
        registry_row = role_agent_row_for(agent_id) if agent_id else {}
        role_tools = normalize_allowed_tools(registry_row.get("allowed_tools", [])) if registry_row else []
    if not role_tools:
        role_tools = skill_allowed_tools(normalize_cell(row.get("agent_id", "")))
    transport_tools = normalize_allowed_tools(transport_tools_argument_for_role(row))
    effective_role_tools = list(role_tools)
    for tool in transport_tools:
        if tool not in effective_role_tools:
            effective_role_tools.append(tool)
    if effective_role_tools:
        outside_profile = [tool for tool in requested_tools if tool not in effective_role_tools]
        if outside_profile:
            return (
                f"agent-dispatch tools outside role profile for {row.get('agent_id', '<unknown>')}: "
                f"{','.join(outside_profile)}; allowed={','.join(effective_role_tools)}"
            )
    return ""


GIT_OPERATION_AGENT_IDS = {
    "commit",
    "git-publisher",
    "git-workspace-prep",
    "pull",
    "push",
}
GIT_OPERATION_COMMANDS = {
    "add",
    "am",
    "apply",
    "branch",
    "checkout",
    "cherry-pick",
    "clean",
    "commit",
    "merge",
    "mv",
    "pull",
    "push",
    "rebase",
    "reset",
    "restore",
    "revert",
    "rm",
    "stash",
    "switch",
    "tag",
}


def role_allows_git_operations(row: dict[str, Any]) -> bool:
    agent_id = normalize_cell(row.get("agent_id") or row.get("role_id"))
    if agent_id in GIT_OPERATION_AGENT_IDS:
        return True
    value = row.get("git_operations_allowed")
    if value is None:
        return False
    return truthy_input(value)


def prompt_requests_git_operation(prompt: str) -> bool:
    for line in prompt.splitlines():
        normalized = line.strip().strip("`").lower()
        match = re.search(r"\bgit\s+([a-z][a-z0-9-]*)\b", normalized)
        if match and match.group(1) in GIT_OPERATION_COMMANDS:
            return True
    return False


def validate_git_operation_for_role(row: dict[str, Any], prompt: str) -> str:
    if prompt_requests_git_operation(prompt) and not role_allows_git_operations(row):
        agent_id = normalize_cell(row.get("agent_id") or row.get("role_id") or "<unknown>")
        allowed = ",".join(sorted(GIT_OPERATION_AGENT_IDS))
        return f"agent-dispatch git operation forbidden for {agent_id}; route via git role: {allowed}"
    return ""


def load_role_agent_registry(path: Path | None = None) -> dict[str, Any]:
    path = path or ROLE_AGENT_REGISTRY
    if not path.exists():
        return {}
    parsed = load_yaml_config(path)
    if not isinstance(parsed, dict):
        raise ValueError(f"role-agent registry must contain an object: {path}")
    return parsed


def _required_string_list_config(parsed: dict[str, Any], key: str, path: Path) -> list[str]:
    value = parsed.get(key)
    if not isinstance(value, list) or not value:
        raise ValueError(f"completion chain config {key} must be a non-empty list: {path}")
    items: list[str] = []
    for index, item in enumerate(value):
        if not isinstance(item, str) or not item.strip():
            raise ValueError(f"completion chain config {key}[{index}] must be a non-empty string: {path}")
        items.append(item.strip())
    return items


def _optional_bool_config(value: Any, *, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return normalize_cell(value).lower() not in {"", "0", "false", "no", "off"}


def _positive_seconds_config(value: Any, *, default: float, label: str, path: Path) -> float:
    if value is None or normalize_cell(value) == "":
        return default
    try:
        seconds = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"completion chain config {label} must be a positive number: {path}") from exc
    if seconds <= 0:
        raise ValueError(f"completion chain config {label} must be positive: {path}")
    return seconds


def _seconds_map_config(value: Any, *, label: str, path: Path) -> dict[str, float]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError(f"completion chain config {label} must be an object: {path}")
    normalized: dict[str, float] = {}
    for key, raw_seconds in value.items():
        normalized_key_value = normalize_cell(key)
        if not normalized_key_value:
            raise ValueError(f"completion chain config {label} keys must be non-empty: {path}")
        if raw_seconds is None or normalize_cell(raw_seconds) == "":
            raise ValueError(f"completion chain config {label}.{normalized_key_value} must be positive: {path}")
        normalized[normalized_key_value] = _positive_seconds_config(
            raw_seconds,
            default=1,
            label=f"{label}.{normalized_key_value}",
            path=path,
        )
    return normalized


def normalize_gate_sla_config(parsed: dict[str, Any], path: Path) -> dict[str, Any]:
    raw = parsed.get("gate_sla") or {}
    if raw and not isinstance(raw, dict):
        raise ValueError(f"completion chain config gate_sla must be an object: {path}")
    notification_classes = raw.get("notification_classes") or ["silent", "flow_alert", "approval_wait", "done"]
    if (
        not isinstance(notification_classes, list)
        or not notification_classes
        or any(not isinstance(item, str) or not item.strip() for item in notification_classes)
    ):
        raise ValueError(f"completion chain config gate_sla.notification_classes must be a non-empty string list: {path}")
    classes = [item.strip() for item in notification_classes]
    required_classes = {"silent", "flow_alert", "approval_wait", "done"}
    missing_classes = sorted(required_classes - set(classes))
    if missing_classes:
        raise ValueError(
            "completion chain config gate_sla.notification_classes missing required classes: "
            + ", ".join(missing_classes)
        )
    def class_value(key: str, default: str) -> str:
        value = normalize_cell(raw.get(key) or default)
        if value not in classes:
            raise ValueError(f"completion chain config gate_sla.{key} must be one of {classes}: {path}")
        return value

    return {
        "default_pending_seconds": _positive_seconds_config(
            raw.get("default_pending_seconds"),
            default=900.0,
            label="gate_sla.default_pending_seconds",
            path=path,
        ),
        "role_pending_seconds": _seconds_map_config(raw.get("role_pending_seconds"), label="gate_sla.role_pending_seconds", path=path),
        "hop_pending_seconds": _seconds_map_config(raw.get("hop_pending_seconds"), label="gate_sla.hop_pending_seconds", path=path),
        "notification_classes": classes,
        "breach_notification_class": class_value("breach_notification_class", "flow_alert"),
        "dead_letter_notification_class": class_value("dead_letter_notification_class", "flow_alert"),
        "approval_wait_notification_class": class_value("approval_wait_notification_class", "approval_wait"),
        "done_notification_class": class_value("done_notification_class", "done"),
        "idle_notification_class": class_value("idle_notification_class", "silent"),
    }


def load_completion_chain_config(path: Path | None = None) -> dict[str, Any]:
    path = path or COMPLETION_CHAIN_CONFIG
    if not path.exists():
        raise ValueError(f"completion chain config missing: {path}")
    parsed = load_yaml_config(path)
    if not isinstance(parsed, dict):
        raise ValueError(f"completion chain config must contain an object: {path}")
    schema_version = parsed.get("schema_version")
    if schema_version != 1:
        raise ValueError(f"completion chain config schema_version must be 1: {path}")
    required_keys = (
        "completion_chain",
        "valid_routing_directors",
        "completion_gate_required_hops",
        "pre_final_required_sections",
        "main_agent_evidence_roles",
        "main_agent_executor_roles",
    )
    lists = {
        key: _required_string_list_config(parsed, key, path)
        for key in required_keys
    }
    missing_required_hops = [
        hop
        for hop in lists["completion_gate_required_hops"]
        if hop not in lists["completion_chain"]
    ]
    if missing_required_hops:
        raise ValueError(
            "completion chain config completion_gate_required_hops not present in completion_chain: "
            + ", ".join(missing_required_hops)
        )
    auto_handoffs = parsed.get("auto_queue_handoffs") or []
    if not isinstance(auto_handoffs, list):
        raise ValueError(f"completion chain config auto_queue_handoffs must be a list: {path}")
    normalized_handoffs: list[dict[str, Any]] = []
    for index, item in enumerate(auto_handoffs):
        if not isinstance(item, dict):
            raise ValueError(f"completion chain config auto_queue_handoffs[{index}] must be an object: {path}")
        from_role = normalize_cell(item.get("from_role"))
        to_role = normalize_cell(item.get("to_role"))
        if not from_role or not to_role:
            raise ValueError(f"completion chain config auto_queue_handoffs[{index}] requires from_role and to_role: {path}")
        handoff_type = normalize_cell(item.get("handoff_type") or item.get("type") or "queue")
        if handoff_type not in {"queue", "command", "command_then_queue", "command+queue", "command_queue"}:
            raise ValueError(
                f"completion chain config auto_queue_handoffs[{index}] unsupported handoff_type: {handoff_type}"
            )
        normalized_handoffs.append(
            {
                "from_role": from_role,
                "to_role": to_role,
                "on_status": normalize_cell(item.get("on_status") or "done"),
                "enabled": bool(item.get("enabled", True)),
                "handoff_type": handoff_type,
                "command": normalize_cell(item.get("command") or item.get("to_command")),
                "command_owner_role": normalize_cell(item.get("command_owner_role") or item.get("commandOwnerRole")),
                "queue_after_command": _optional_bool_config(item.get("queue_after_command") or item.get("queueAfterCommand"), default=False),
                "required_report_result": normalize_cell(item.get("required_report_result")),
                "required_handoff_to": normalize_cell(item.get("required_handoff_to")),
                "precheck_command": normalize_cell(item.get("precheck_command")),
                "command_gate_role": normalize_cell(item.get("command_gate_role") or item.get("gate_role")),
                "command_flow_phase": normalize_cell(item.get("command_flow_phase") or item.get("flow_phase")),
                "publication_gate_phase": normalize_cell(item.get("publication_gate_phase")),
                "auto_finalization_check": _optional_bool_config(
                    item.get("auto_finalization_check")
                    or item.get("autoFinalizationCheck")
                    or item.get("run_finalization_check")
                    or item.get("runFinalizationCheck"),
                    default=False,
                ),
                "auto_final_transport_render_check": _optional_bool_config(
                    item.get("auto_final_transport_render_check")
                    or item.get("autoFinalTransportRenderCheck")
                    or item.get("run_final_transport_render_check")
                    or item.get("runFinalTransportRenderCheck"),
                    default=False,
                ),
                "style_profile": normalize_cell(item.get("style_profile") or item.get("styleProfile")),
                "require_next_phase_allowed": _optional_bool_config(
                    item.get("require_next_phase_allowed"),
                    default=bool(normalize_cell(item.get("precheck_command"))),
                ),
            }
        )
    assessor_policy = parsed.get("assessor_integration_policy") or {}
    if assessor_policy and not isinstance(assessor_policy, dict):
        raise ValueError(f"completion chain config assessor_integration_policy must be an object: {path}")
    return {
        "schema_version": schema_version,
        **lists,
        "auto_queue_handoffs": normalized_handoffs,
        "assessor_integration_policy": assessor_policy,
        "gate_sla": normalize_gate_sla_config(parsed, path),
    }


def load_gate_output_schemas(path: Path | None = None) -> dict[str, Any]:
    path = path or GATE_OUTPUT_SCHEMAS_CONFIG
    if not path.exists():
        raise ValueError(f"gate output schema config missing: {path}")
    parsed = load_yaml_config(path)
    if not isinstance(parsed, dict):
        raise ValueError(f"gate output schema config must contain an object: {path}")
    if parsed.get("schema_version") != 1:
        raise ValueError(f"gate output schema config schema_version must be 1: {path}")
    sections = parsed.get("sections")
    if not isinstance(sections, dict) or not sections:
        raise ValueError(f"gate output schema config sections must be a non-empty object: {path}")
    for section_name, section_schema in sections.items():
        if not isinstance(section_name, str) or not section_name.strip():
            raise ValueError(f"gate output schema config section name must be non-empty: {path}")
        if not isinstance(section_schema, dict):
            raise ValueError(f"gate output schema config section must be an object: {section_name}")
        fields = section_schema.get("fields")
        if fields is not None and not isinstance(fields, dict):
            raise ValueError(f"gate output schema config fields must be an object: {section_name}")
        for field_name, field_schema in (fields or {}).items():
            if not isinstance(field_name, str) or not field_name.strip():
                raise ValueError(f"gate output schema config field name must be non-empty: {section_name}")
            if not isinstance(field_schema, dict):
                raise ValueError(f"gate output schema config field must be an object: {section_name}.{field_name}")
            labels = field_schema.get("labels")
            if labels is not None and (
                not isinstance(labels, list)
                or not labels
                or any(not isinstance(label, str) or not label.strip() for label in labels)
            ):
                raise ValueError(f"gate output schema config labels must be a non-empty string list: {section_name}.{field_name}")
    return {"schema_version": 1, "sections": sections}


def active_role_model_rows() -> list[dict[str, str]]:
    return [
        row
        for row in parse_registry()
        if row.get("status") == "active"
    ]


def role_agent_template_value(template: Any, *, role_id: str, team: str, org_instance_id: str) -> str:
    value = str(template or "")
    return (
        value.replace("{role_id}", role_id)
        .replace("{agent_id}", role_id)
        .replace("{team}", team)
        .replace("{org_instance_id}", org_instance_id)
        .replace("{organization_instance_id}", org_instance_id)
    )


def role_agent_rows(*, organization_instance_id: str = "{org_instance_id}") -> list[dict[str, Any]]:
    registry = load_role_agent_registry()
    defaults = registry.get("defaults") if isinstance(registry.get("defaults"), dict) else {}
    overrides = registry.get("agents") if isinstance(registry.get("agents"), dict) else {}
    role_layers = registry.get("role_layers") if isinstance(registry.get("role_layers"), dict) else {}
    rows: list[dict[str, Any]] = []
    for model_row in active_role_model_rows():
        role_id = model_row["agent_id"]
        team = model_row.get("team", "")
        override = overrides.get(role_id, {}) if isinstance(overrides, dict) else {}
        if not isinstance(override, dict):
            override = {}
        model_ref = str(override.get("model_registry_ref") or role_id)
        model_source = registry_row_for(model_ref)
        if not model_source:
            raise ValueError(f"role-agent registry model_registry_ref not found: {role_id} -> {model_ref}")
        inbox_template = override.get("inbox_path") or defaults.get("inbox_path") or "inbox/{role_id}.yaml"
        report_template = override.get("report_dir") or defaults.get("report_dir") or "reports/{role_id}"
        queue_consumer = truthy_input(override.get("queue_consumer", defaults.get("queue_consumer", False)))
        queue_finalizer = normalize_cell(override.get("queue_finalizer") or defaults.get("queue_finalizer"))
        report_write_mode = normalize_cell(override.get("report_write_mode") or defaults.get("report_write_mode"))
        allowed_tools = normalize_allowed_tools(override.get("allowed_tools", defaults.get("allowed_tools", [])))
        role_layer = normalize_cell(override.get("role_layer") or role_layers.get(role_id) or defaults.get("role_layer"))
        if role_layer not in STATIC_ROLE_LAYERS:
            allowed_layers = ", ".join(sorted(STATIC_ROLE_LAYERS))
            raise ValueError(
                f"role-agent registry role_layer missing/invalid for {role_id}: {role_layer or '<missing>'}; "
                f"allowed={allowed_layers}"
            )
        context_dirs = [
            expand_config_path_value(item)
            for item in normalize_string_list(override.get("context_dirs", defaults.get("context_dirs", [])))
        ]
        context_dirs = [item for item in context_dirs if item]
        git_operations_allowed = role_id in GIT_OPERATION_AGENT_IDS or truthy_input(
            override.get("git_operations_allowed", defaults.get("git_operations_allowed", False))
        )
        if queue_consumer:
            if queue_finalizer not in ALLOWED_QUEUE_FINALIZERS:
                raise ValueError(f"role-agent registry queue_finalizer invalid for {role_id}: {queue_finalizer or '<missing>'}")
            if report_write_mode not in ALLOWED_REPORT_WRITE_MODES:
                raise ValueError(f"role-agent registry report_write_mode invalid for {role_id}: {report_write_mode or '<missing>'}")
            if not allowed_tools:
                raise ValueError(f"role-agent registry allowed_tools missing for queue consumer: {role_id}")
            declared_allowed_tools = skill_allowed_tools(role_id)
            if not declared_allowed_tools and not role_is_migrated_to_sahai(role_id):
                raise ValueError(f"role SKILL allowed-tools missing for queue consumer: {role_id}")
            if declared_allowed_tools and allowed_tools != declared_allowed_tools:
                raise ValueError(
                    "role-agent registry allowed_tools mismatch for "
                    f"{role_id}: registry={allowed_tools} skill={declared_allowed_tools}"
                )
        rows.append(
            {
                "role_id": role_id,
                "agent_id": role_id,
                "organization_instance_id": organization_instance_id,
                "team": team,
                "role_layer": role_layer,
                "model_registry_ref": model_ref,
                "provider": model_source.get("provider", ""),
                "intended_model": model_source.get("primary_model", ""),
                "fallback_models": model_source.get("fallback_models", ""),
                "execution_mode": model_source.get("execution_mode", ""),
                "always_active": bool_cell(model_source.get("always_active", "false")),
                "queue_consumer": queue_consumer,
                "queue_finalizer": queue_finalizer,
                "report_write_mode": report_write_mode,
                "allowed_tools": allowed_tools,
                "context_dirs": context_dirs,
                "git_operations_allowed": git_operations_allowed,
                "inbox_path": role_agent_template_value(
                    inbox_template,
                    role_id=role_id,
                    team=team,
                    org_instance_id=organization_instance_id,
                ),
                "report_dir": role_agent_template_value(
                    report_template,
                    role_id=role_id,
                    team=team,
                    org_instance_id=organization_instance_id,
                ),
            }
        )
    return rows


def role_agent_row_for(role_id: str, *, organization_instance_id: str = "{org_instance_id}") -> dict[str, Any]:
    for row in role_agent_rows(organization_instance_id=organization_instance_id):
        if row.get("role_id") == role_id or row.get("agent_id") == role_id:
            return row
    return {}


def organization_id(session_id: str) -> str:
    digest = hashlib.sha1(session_id.encode("utf-8")).hexdigest()[:12]
    return f"org-{digest}"


def current_timestamp() -> str:
    return dt.datetime.now(dt.timezone.utc).astimezone().isoformat(timespec="seconds")


def file_sha1(path: Path) -> str:
    return hashlib.sha1(path.read_bytes()).hexdigest()


def policy_digest_entries(sources: dict[str, Path] | None = None) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for policy_id, path in (sources or POLICY_DIGEST_SOURCES).items():
        entry: dict[str, Any] = {
            "policy_id": policy_id,
            "path": str(path),
            "status": "missing",
            "sha1": "",
            "byte_count": 0,
        }
        try:
            data = path.read_bytes()
        except FileNotFoundError:
            pass
        except OSError as exc:
            entry["status"] = "unreadable"
            entry["error"] = f"{type(exc).__name__}: {exc}"
        else:
            entry["status"] = "ready"
            entry["sha1"] = hashlib.sha1(data).hexdigest()
            entry["byte_count"] = len(data)
        entries.append(entry)
    return entries


def policy_digest_sha1(entries: list[dict[str, Any]]) -> str:
    material = json.dumps(entries, ensure_ascii=False, sort_keys=True)
    return hashlib.sha1(material.encode("utf-8")).hexdigest()


def policy_digest_status(entries: list[dict[str, Any]]) -> str:
    return "ready" if all(entry.get("status") == "ready" for entry in entries) else "partial"


def path_for_policy_digest_display(path: str) -> str:
    home = str(Path.home())
    if path == home:
        return "~"
    if path.startswith(home + os.sep):
        return "~" + path[len(home) :]
    return path


def render_policy_digest_skill_block(entries: list[dict[str, Any]]) -> str:
    status = policy_digest_status(entries)
    digest = policy_digest_sha1(entries)
    policy_rows = "\n".join(
        "| {policy_id} | `{status}` | `{sha1}` | {byte_count} | `{path}` |".format(
            policy_id=entry.get("policy_id", ""),
            status=entry.get("status", ""),
            sha1=entry.get("sha1", ""),
            byte_count=entry.get("byte_count", 0),
            path=path_for_policy_digest_display(str(entry.get("path", ""))),
        )
        for entry in entries
    )
    return f"""{POLICY_DIGEST_SKILL_BLOCK_BEGIN}
## ITB Policy Digest Snapshot

This block is generated by `infra-team-bootstrap sync-policy-digest-skills`.
Use the digest for routine freshness checks; read full policy bodies only when this digest changes, required judgment evidence is missing, or human approval is needed.
Narration policy: act on routine flow checks silently; surface only anomaly or approval blockers as `[FLOW-ALERT]`.

| Field | Value |
|---|---|
| policy_digest_status | `{status}` |
| policy_digest_sha1 | `{digest}` |

| Policy | Status | SHA1 | Bytes | Source |
|---|---|---:|---:|---|
{policy_rows or "| none | `missing` | `` | 0 | `` |"}
{POLICY_DIGEST_SKILL_BLOCK_END}
"""


def replace_policy_digest_skill_block(skill_text: str, block: str) -> str:
    has_begin = POLICY_DIGEST_SKILL_BLOCK_BEGIN in skill_text
    has_end = POLICY_DIGEST_SKILL_BLOCK_END in skill_text
    if has_begin != has_end:
        raise ValueError("incomplete ITB policy digest managed block")
    normalized_block = block.rstrip() + "\n"
    if not has_begin:
        return skill_text.rstrip() + "\n\n" + normalized_block
    start = skill_text.index(POLICY_DIGEST_SKILL_BLOCK_BEGIN)
    end = skill_text.index(POLICY_DIGEST_SKILL_BLOCK_END, start) + len(POLICY_DIGEST_SKILL_BLOCK_END)
    suffix = skill_text[end:]
    if suffix.startswith("\n") and not suffix.startswith("\n\n"):
        suffix = suffix[1:]
    return skill_text[:start] + normalized_block + suffix


def managed_role_skill_rows(
    rows: list[dict[str, str]] | None = None,
    *,
    include_reference_roles: bool = False,
) -> list[dict[str, str]]:
    registry_rows = rows if rows is not None else parse_registry()
    managed: list[dict[str, str]] = []
    for row in registry_rows:
        status = normalize_cell(row.get("status"))
        if status == "active":
            managed.append(row)
        elif include_reference_roles and status == "reference":
            managed.append(row)
    return managed


def sync_policy_digest_skills(
    *,
    runtime: str,
    state_root: Path,
    hook_input: dict[str, Any],
) -> dict[str, Any]:
    session_id, session_source = resolve_session_id(state_root, hook_input)
    if not session_id:
        session_id = "policy-digest-sync"
    session_dir = state_root / safe_id(session_id)
    session_dir.mkdir(parents=True, exist_ok=True)
    now = current_timestamp()
    entries = policy_digest_entries()
    block = render_policy_digest_skill_block(entries)
    requested_role_ids = normalize_string_list(
        hook_input.get("role_ids")
        or hook_input.get("role_id")
        or hook_input.get("agent_ids")
        or hook_input.get("agent_id")
    )
    requested = set(requested_role_ids)
    dry_run = bool(hook_input.get("dry_run"))
    include_reference_roles = truthy_input(
        hook_input.get("include_reference_roles")
        or hook_input.get("includeReferenceRoles")
        or hook_input.get("include_compatibility_reference_roles")
        or hook_input.get("includeCompatibilityReferenceRoles"),
        default=False,
    )
    updated: list[str] = []
    unchanged: list[str] = []
    missing: list[str] = []
    skipped_migrated: list[str] = []
    errors: list[dict[str, str]] = []

    target_rows = managed_role_skill_rows(include_reference_roles=include_reference_roles)
    for row in target_rows:
        agent_id = normalize_cell(row.get("agent_id"))
        if requested and agent_id not in requested:
            continue
        skill_path = role_definition_path(agent_id)
        if not skill_path.exists():
            missing.append(agent_id)
            continue
        try:
            original = skill_path.read_text(encoding="utf-8")
            rendered = replace_policy_digest_skill_block(original, block)
        except Exception as exc:
            errors.append({"agent_id": agent_id, "error": f"{type(exc).__name__}: {exc}"})
            continue
        if rendered == original:
            unchanged.append(agent_id)
            continue
        updated.append(agent_id)
        if not dry_run:
            tmp = skill_path.with_name(f".{skill_path.name}.{uuid.uuid4().hex}.tmp")
            tmp.write_text(rendered, encoding="utf-8")
            os.replace(tmp, skill_path)

    unresolved_requested = sorted(requested - {normalize_cell(row.get("agent_id")) for row in target_rows})
    if unresolved_requested:
        for role_id in unresolved_requested:
            errors.append({"agent_id": role_id, "error": "role_id is not an active Team Role"})

    status = "failed" if errors or missing else "ready"
    summary = {
        "ts": now,
        "runtime": runtime,
        "event_type": "policy_digest_skill_sync",
        "session_id": session_id,
        "session_source": session_source,
        "status": status,
        "dry_run": dry_run,
        "include_reference_roles": include_reference_roles,
        "policy_digest_status": policy_digest_status(entries),
        "policy_digest_sha1": policy_digest_sha1(entries),
        "policy_digest": entries,
        "target_count": len(updated) + len(unchanged) + len(missing) + len(skipped_migrated),
        "updated_count": len(updated),
        "unchanged_count": len(unchanged),
        "missing_count": len(missing),
        "skipped_migrated_count": len(skipped_migrated),
        "error_count": len(errors),
        "updated_roles": updated,
        "unchanged_roles": unchanged,
        "missing_roles": missing,
        "skipped_migrated_roles": skipped_migrated,
        "errors": errors,
    }
    append_jsonl_atomic(session_dir / "policy-digest-sync-events.jsonl", summary)
    output = {"policyDigestSkillSync": summary}
    if status != "ready":
        output["decision"] = "block"
        output["reason"] = "policy digest skill sync failed"
    return output


def append_jsonl_unlocked(path: Path, entry: dict[str, Any]) -> None:
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(json_event_safe(entry), ensure_ascii=False, sort_keys=True) + "\n")


def append_jsonl(path: Path, entry: dict[str, Any]) -> None:
    append_jsonl_atomic(path, entry)


def json_event_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): json_event_safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [json_event_safe(item) for item in value]
    if isinstance(value, tuple):
        return [json_event_safe(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    records: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        data = json.loads(line)
        if isinstance(data, dict):
            records.append(data)
    return records


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json_yaml(path: Path, data: Any) -> None:
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def atomic_write_text(path: Path, text: str) -> None:
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def report_file_integrity(path: Path) -> dict[str, Any]:
    data = path.read_bytes()
    text = data.decode("utf-8")
    return {
        "sha256": hashlib.sha256(data).hexdigest(),
        "line_count": len(text.splitlines()),
        "byte_count": len(data),
    }


def append_jsonl_atomic(path: Path, entry: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_name(f"{path.name}.lock.d")
    acquire_queue_lock(lock_path)
    try:
        append_jsonl_unlocked(path, entry)
    finally:
        release_queue_lock(lock_path)


def read_json_yaml(path: Path) -> Any:
    return load_yaml_config(path)


def queue_root_for(session_dir: Path, hook_input: dict[str, Any] | None = None) -> Path:
    hook_input = hook_input or {}
    value = (
        hook_input.get("queue_root")
        or hook_input.get("queueRoot")
        or os.environ.get("ITB_QUEUE_ROOT")
    )
    if value:
        return Path(str(value)).expanduser()
    return session_dir / "queue"


def queue_component_errors(value: str, field_name: str) -> list[str]:
    if not value:
        return [f"{field_name} is required"]
    if value in {".", ".."}:
        return [f"{field_name} must not be a relative path marker"]
    if safe_id(value) != value:
        return [f"{field_name} contains unsafe path characters: {value}"]
    return []


def queue_relative_path_errors(value: str, field_name: str) -> list[str]:
    if not value:
        return [f"{field_name} is required"]
    path = Path(value)
    if path.is_absolute():
        return [f"{field_name} must be relative to queue_root"]
    errors: list[str] = []
    for part in path.parts:
        errors.extend(queue_component_errors(part, field_name))
    return errors


def process_is_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def stale_queue_lock_reason(lock_path: Path, *, stale_after_seconds: float) -> str:
    if stale_after_seconds <= 0 or not lock_path.exists():
        return ""
    try:
        age_seconds = time.time() - lock_path.stat().st_mtime
    except OSError:
        return ""
    if age_seconds < stale_after_seconds:
        return ""
    owner_path = lock_path / "owner.json"
    if not owner_path.exists():
        return f"lock age {age_seconds:.1f}s exceeded {stale_after_seconds:.1f}s and has no owner metadata"
    try:
        owner = read_json(owner_path)
    except (OSError, json.JSONDecodeError):
        return f"lock age {age_seconds:.1f}s exceeded {stale_after_seconds:.1f}s and owner metadata is unreadable"
    pid = int(owner.get("pid") or 0) if isinstance(owner, dict) else 0
    if pid and process_is_alive(pid):
        return ""
    return f"lock age {age_seconds:.1f}s exceeded {stale_after_seconds:.1f}s and owner pid {pid or '<missing>'} is not alive"


def try_reclaim_stale_queue_lock(lock_path: Path, *, stale_after_seconds: float) -> bool:
    reason = stale_queue_lock_reason(lock_path, stale_after_seconds=stale_after_seconds)
    if not reason:
        return False
    marker = lock_path.with_name(f"{lock_path.name}.reclaiming.{os.getpid()}")
    try:
        lock_path.rename(marker)
    except OSError:
        return False
    try:
        for child in marker.iterdir():
            if child.is_file():
                child.unlink()
        marker.rmdir()
    except OSError:
        return False
    return True


def acquire_directory_lock(
    lock_path: Path,
    *,
    owner: dict[str, Any] | None = None,
    timeout_seconds: float = 5.0,
    stale_after_seconds: float = 300.0,
) -> dict[str, Any]:
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + timeout_seconds
    owner_data = {
        "pid": os.getpid(),
        "created_at": current_timestamp(),
        "stale_after_seconds": stale_after_seconds,
    }
    if owner:
        owner_data.update(owner)
    while True:
        try:
            lock_path.mkdir()
            write_json_yaml(lock_path / "owner.json", owner_data)
            return owner_data
        except FileExistsError:
            if try_reclaim_stale_queue_lock(lock_path, stale_after_seconds=stale_after_seconds):
                continue
            if time.monotonic() >= deadline:
                raise TimeoutError(f"queue lock timeout: {lock_path}")
            time.sleep(0.05)


def acquire_queue_lock(lock_path: Path, *, timeout_seconds: float = 5.0, stale_after_seconds: float = 300.0) -> None:
    acquire_directory_lock(
        lock_path,
        owner={"lock_type": "queue"},
        timeout_seconds=timeout_seconds,
        stale_after_seconds=stale_after_seconds,
    )


def release_queue_lock(lock_path: Path) -> None:
    try:
        owner_path = lock_path / "owner.json"
        if owner_path.exists():
            owner_path.unlink()
        lock_path.rmdir()
    except FileNotFoundError:
        return


def resolved_path(value: Any) -> Path:
    return Path(str(value)).expanduser().resolve(strict=False)



def shared_file_root_for(path: Path) -> Path | None:
    for root in SHARED_FILE_ROOTS:
        resolved_root = root.expanduser().resolve(strict=False)
        if path_is_within(path, resolved_root):
            return resolved_root
    return None


def shared_resource_id_from_input(hook_input: dict[str, Any]) -> str:
    explicit = normalize_cell(hook_input.get("resource_id") or hook_input.get("resourceId"))
    if explicit:
        return explicit
    repo_root = normalize_cell(hook_input.get("repo_root") or hook_input.get("repoRoot"))
    if repo_root:
        return f"repo:{resolved_path(repo_root)}"
    target_path = normalize_cell(hook_input.get("target_path") or hook_input.get("targetPath"))
    if target_path:
        target = resolved_path(target_path)
        return f"shared-file:{target}"
    return ""


def shared_lock_path(state_root: Path, resource_id: str) -> Path:
    digest = hashlib.sha1(resource_id.encode("utf-8")).hexdigest()[:12]
    label = safe_id(resource_id)[:72].strip("._-") or "resource"
    return state_root / "shared-locks" / f"{label}-{digest}.lock.d"


def read_lock_owner(lock_path: Path) -> dict[str, Any]:
    owner_path = lock_path / "owner.json"
    if not owner_path.exists():
        return {}
    data = read_json(owner_path)
    return data if isinstance(data, dict) else {}


def shared_lock_event_base(
    *,
    runtime: str,
    state_root: Path,
    hook_input: dict[str, Any],
    event_type: str,
) -> tuple[Path, dict[str, Any]]:
    session_id, session_source = resolve_session_id(state_root, hook_input)
    if not session_id:
        session_id = "shared-serializer"
    session_dir = state_root / safe_id(session_id)
    session_dir.mkdir(parents=True, exist_ok=True)
    return session_dir, {
        "ts": current_timestamp(),
        "runtime": runtime,
        "event_type": event_type,
        "session_id": session_id,
        "session_source": session_source,
    }


def shared_resource_lock_output(
    *,
    runtime: str,
    state_root: Path,
    hook_input: dict[str, Any],
) -> dict[str, Any]:
    session_dir, event = shared_lock_event_base(
        runtime=runtime,
        state_root=state_root,
        hook_input=hook_input,
        event_type="shared_resource_lock",
    )
    action = normalize_cell(hook_input.get("action") or "status")
    resource_id = shared_resource_id_from_input(hook_input)
    if not resource_id:
        return {"decision": "block", "reason": "shared-resource-lock requires resource_id, repo_root, or target_path"}
    lock_path = shared_lock_path(state_root, resource_id)
    lease_id = normalize_cell(hook_input.get("lease_id") or hook_input.get("leaseId"))
    if action == "acquire" and not lease_id:
        lease_id = f"lease-{uuid.uuid4().hex}"
    timeout_seconds = bounded_float_input(
        hook_input.get("timeout_seconds") or hook_input.get("timeoutSeconds"),
        default=5.0,
        minimum=0.0,
        maximum=3600.0,
    )
    stale_after_seconds = bounded_float_input(
        hook_input.get("stale_after_seconds") or hook_input.get("staleAfterSeconds"),
        default=900.0,
        minimum=1.0,
        maximum=86400.0,
    )
    event.update(
        {
            "action": action,
            "resource_id": resource_id,
            "lock_path": str(lock_path),
            "lease_id": lease_id,
        }
    )
    try:
        if action == "acquire":
            owner = acquire_directory_lock(
                lock_path,
                owner={
                    "lock_type": "shared_resource",
                    "resource_id": resource_id,
                    "lease_id": lease_id,
                    "holder": normalize_cell(hook_input.get("holder") or hook_input.get("role_id") or hook_input.get("agent_id")),
                    "purpose": normalize_cell(hook_input.get("purpose")),
                },
                timeout_seconds=timeout_seconds,
                stale_after_seconds=stale_after_seconds,
            )
            event.update({"result": "acquired", "owner": owner})
        elif action == "release":
            owner = read_lock_owner(lock_path)
            if not owner:
                raise ValueError("shared resource lock is not held")
            if not lease_id:
                raise ValueError("shared resource lock release requires lease_id")
            if lease_id and normalize_cell(owner.get("lease_id")) != lease_id:
                raise ValueError("lease_id does not match lock owner")
            release_queue_lock(lock_path)
            event.update({"result": "released", "owner": owner})
        elif action == "status":
            owner = read_lock_owner(lock_path)
            event.update({"result": "held" if owner else "free", "owner": owner})
        else:
            raise ValueError(f"unsupported shared-resource-lock action: {action}")
    except Exception as exc:
        event.update({"result": "blocked", "error": f"{type(exc).__name__}: {exc}"})
        append_jsonl_atomic(session_dir / "shared-serializer-events.jsonl", event)
        return {"decision": "block", "reason": event["error"], "sharedResourceLock": event}
    append_jsonl_atomic(session_dir / "shared-serializer-events.jsonl", event)
    return {"sharedResourceLock": event}


def file_sha256_if_exists(path: Path) -> str:
    if not path.exists():
        return ""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def shared_file_update_output(
    *,
    runtime: str,
    state_root: Path,
    hook_input: dict[str, Any],
) -> dict[str, Any]:
    session_dir, event = shared_lock_event_base(
        runtime=runtime,
        state_root=state_root,
        hook_input=hook_input,
        event_type="shared_file_update",
    )
    target_value = normalize_cell(hook_input.get("target_path") or hook_input.get("targetPath"))
    if not target_value:
        return {"decision": "block", "reason": "shared-file-update requires target_path"}
    target_path = resolved_path(target_value)
    shared_root = shared_file_root_for(target_path)
    if shared_root is None:
        return {"decision": "block", "reason": f"target_path is outside shared file roots: {target_path}"}
    operation = normalize_cell(hook_input.get("operation") or "append")
    resource_id = shared_resource_id_from_input(hook_input) or f"shared-root:{shared_root}"
    lock_path = shared_lock_path(state_root, resource_id)
    timeout_seconds = bounded_float_input(
        hook_input.get("timeout_seconds") or hook_input.get("timeoutSeconds"),
        default=5.0,
        minimum=0.0,
        maximum=3600.0,
    )
    stale_after_seconds = bounded_float_input(
        hook_input.get("stale_after_seconds") or hook_input.get("staleAfterSeconds"),
        default=900.0,
        minimum=1.0,
        maximum=86400.0,
    )
    expected_sha256 = normalize_cell(hook_input.get("expected_sha256") or hook_input.get("expectedSha256"))
    append_text = hook_input.get("append_text") if "append_text" in hook_input else hook_input.get("appendText")
    replace_text = hook_input.get("replace_text") if "replace_text" in hook_input else hook_input.get("replaceText")
    event.update(
        {
            "operation": operation,
            "target_path": str(target_path),
            "shared_root": str(shared_root),
            "resource_id": resource_id,
            "lock_path": str(lock_path),
            "expected_sha256": expected_sha256,
        }
    )
    try:
        acquire_directory_lock(
            lock_path,
            owner={
                "lock_type": "shared_file_update",
                "resource_id": resource_id,
                "target_path": str(target_path),
                "holder": normalize_cell(hook_input.get("holder") or hook_input.get("role_id") or hook_input.get("agent_id")),
                "purpose": normalize_cell(hook_input.get("purpose")),
            },
            timeout_seconds=timeout_seconds,
            stale_after_seconds=stale_after_seconds,
        )
        try:
            before_sha256 = file_sha256_if_exists(target_path)
            if expected_sha256 and expected_sha256 != before_sha256:
                raise ValueError("expected_sha256 does not match current file")
            target_path.parent.mkdir(parents=True, exist_ok=True)
            if operation == "append":
                if append_text is None:
                    raise ValueError("append operation requires append_text")
                with target_path.open("a", encoding="utf-8") as fh:
                    fh.write(str(append_text))
            elif operation == "replace":
                if replace_text is None:
                    raise ValueError("replace operation requires replace_text")
                tmp = target_path.with_name(f".{target_path.name}.{uuid.uuid4().hex}.tmp")
                tmp.write_text(str(replace_text), encoding="utf-8")
                os.replace(tmp, target_path)
            else:
                raise ValueError(f"unsupported shared-file-update operation: {operation}")
            after_sha256 = file_sha256_if_exists(target_path)
            event.update(
                {
                    "result": "updated",
                    "before_sha256": before_sha256,
                    "after_sha256": after_sha256,
                    "byte_count": target_path.stat().st_size if target_path.exists() else 0,
                }
            )
        finally:
            release_queue_lock(lock_path)
    except Exception as exc:
        event.update({"result": "blocked", "error": f"{type(exc).__name__}: {exc}"})
        append_jsonl_atomic(session_dir / "shared-serializer-events.jsonl", event)
        return {"decision": "block", "reason": event["error"], "sharedFileUpdate": event}
    append_jsonl_atomic(session_dir / "shared-serializer-events.jsonl", event)
    return {"sharedFileUpdate": event}


def load_inbox(path: Path, role_id: str) -> dict[str, Any]:
    if not path.exists():
        return {"envelope_version": "1", "role_id": role_id, "messages": []}
    data = read_json_yaml(path)
    if not isinstance(data, dict):
        raise ValueError(f"inbox must contain an object: {path}")
    messages = data.get("messages")
    if not isinstance(messages, list):
        raise ValueError(f"inbox messages must be a list: {path}")
    return data


def append_inbox_message(
    path: Path,
    role_id: str,
    message: dict[str, Any],
    queue_root: Path,
    *,
    task_payload_path: Path | None = None,
    task_payload_data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    lock_path = queue_root / "locks" / "enqueue.lock"
    acquire_queue_lock(lock_path)
    try:
        inbox = load_inbox(path, role_id)
        inbox["envelope_version"] = str(inbox.get("envelope_version") or "1")
        inbox["role_id"] = role_id
        inbox.setdefault("messages", [])
        if any(item.get("message_id") == message["message_id"] for item in inbox["messages"] if isinstance(item, dict)):
            raise ValueError(f"duplicate message_id for {role_id}: {message['message_id']}")
        if task_payload_path and task_payload_data is not None:
            if task_payload_path.exists():
                raise ValueError(f"task payload already exists: {task_payload_path}")
            write_json_yaml(task_payload_path, task_payload_data)
        inbox["messages"].append(message)
        write_json_yaml(path, inbox)
        return inbox
    finally:
        release_queue_lock(lock_path)


def update_inbox_message(
    path: Path,
    role_id: str,
    message_id: str,
    queue_root: Path,
    updates: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    lock_path = queue_root / "locks" / "enqueue.lock"
    acquire_queue_lock(lock_path)
    try:
        inbox = load_inbox(path, role_id)
        for item in inbox.get("messages", []):
            if isinstance(item, dict) and item.get("message_id") == message_id:
                item.update(updates)
                prune_terminal_inbox_messages(inbox, role_id, queue_root)
                write_json_yaml(path, inbox)
                return inbox, item
        raise ValueError(f"message_id not found for {role_id}: {message_id}")
    finally:
        release_queue_lock(lock_path)


def prune_terminal_inbox_messages(inbox: dict[str, Any], role_id: str, queue_root: Path) -> None:
    keep_count = bounded_int_input(
        os.environ.get("ITB_INBOX_TERMINAL_KEEP"),
        default=50,
        minimum=0,
        maximum=10000,
    )
    messages = inbox.get("messages")
    if not isinstance(messages, list):
        return
    terminal_indexes = [
        index
        for index, item in enumerate(messages)
        if isinstance(item, dict) and normalize_cell(item.get("status")) in {"done", "failed"}
    ]
    archive_count = max(0, len(terminal_indexes) - keep_count)
    if archive_count <= 0:
        return
    archived_at = current_timestamp()
    archive_indexes = set(terminal_indexes[:archive_count])
    archive_path = queue_root / "archive" / "inbox" / f"{role_id}.jsonl"
    retained_messages: list[Any] = []
    for index, item in enumerate(messages):
        if index in archive_indexes and isinstance(item, dict):
            append_jsonl_atomic(
                archive_path,
                {
                    "archived_at": archived_at,
                    "role_id": role_id,
                    "message": item,
                },
            )
        else:
            retained_messages.append(item)
    inbox["messages"] = retained_messages
    inbox["terminal_archive"] = {
        "archive_path": str(archive_path),
        "last_archived_at": archived_at,
        "terminal_keep_count": keep_count,
        "last_archived_count": archive_count,
    }


def first_pending_message(path: Path, role_id: str) -> dict[str, Any] | None:
    inbox = load_inbox(path, role_id)
    for item in inbox.get("messages", []):
        if isinstance(item, dict) and item.get("status") == "pending":
            return dict(item)
    return None


def pending_inbox_messages(path: Path, role_id: str) -> list[dict[str, Any]]:
    inbox = load_inbox(path, role_id)
    return [
        dict(item)
        for item in inbox.get("messages", [])
        if isinstance(item, dict) and normalize_cell(item.get("status") or "pending") == "pending"
    ]


def claim_pending_message(
    path: Path,
    role_id: str,
    queue_root: Path,
    *,
    now: str,
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    lock_path = queue_root / "locks" / "enqueue.lock"
    acquire_queue_lock(lock_path)
    try:
        inbox = load_inbox(path, role_id)
        for item in inbox.get("messages", []):
            if isinstance(item, dict) and item.get("status") == "pending":
                item["status"] = "processing"
                item["processing_started_at"] = now
                write_json_yaml(path, inbox)
                return dict(item), inbox
        return None, inbox
    finally:
        release_queue_lock(lock_path)


def queue_message_by_id(path: Path, role_id: str, message_id: str) -> dict[str, Any]:
    inbox = load_inbox(path, role_id)
    for item in inbox.get("messages", []):
        if isinstance(item, dict) and item.get("message_id") == message_id:
            return dict(item)
    raise ValueError(f"message_id not found for {role_id}: {message_id}")


def iso_seconds_delta(later: str, earlier: str) -> float:
    try:
        later_dt = dt.datetime.fromisoformat(later)
        earlier_dt = dt.datetime.fromisoformat(earlier)
    except ValueError:
        return 0.0
    return max(0.0, (later_dt - earlier_dt).total_seconds())


def iso_datetime_or_none(value: str) -> dt.datetime | None:
    text = normalize_cell(value)
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = dt.datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed


def iso_age_seconds(now: str, earlier: str) -> float | None:
    now_dt = iso_datetime_or_none(now)
    earlier_dt = iso_datetime_or_none(earlier)
    if now_dt is None or earlier_dt is None:
        return None
    return max(0.0, (now_dt - earlier_dt).total_seconds())


def message_pending_latency_seconds(message: dict[str, Any], now: str) -> float:
    created_at = normalize_cell(message.get("created_at"))
    if not created_at:
        return 0.0
    return iso_seconds_delta(now, created_at)


def gate_sla_threshold_seconds(role_id: str, message: dict[str, Any]) -> tuple[float, str]:
    hop_key = f"{normalize_cell(message.get('from_role')) or 'unknown'}->{normalize_cell(message.get('to_role')) or role_id}"
    hop_thresholds = GATE_SLA.get("hop_pending_seconds") if isinstance(GATE_SLA.get("hop_pending_seconds"), dict) else {}
    role_thresholds = GATE_SLA.get("role_pending_seconds") if isinstance(GATE_SLA.get("role_pending_seconds"), dict) else {}
    if hop_key in hop_thresholds:
        return float(hop_thresholds[hop_key]), f"hop:{hop_key}"
    if role_id in role_thresholds:
        return float(role_thresholds[role_id]), f"role:{role_id}"
    return float(GATE_SLA.get("default_pending_seconds") or 900.0), "default"


def gate_sla_status_for_message(role_id: str, message: dict[str, Any], now: str) -> dict[str, Any]:
    pending_seconds = message_pending_latency_seconds(message, now)
    threshold_seconds, threshold_source = gate_sla_threshold_seconds(role_id, message)
    breached = threshold_seconds > 0 and pending_seconds >= threshold_seconds
    return {
        "queued_ts": normalize_cell(message.get("created_at")),
        "completed_ts": normalize_cell(message.get("done_at") or message.get("failed_at")),
        "sla_pending_seconds": round(pending_seconds, 3),
        "sla_threshold_seconds": round(threshold_seconds, 3),
        "sla_threshold_source": threshold_source,
        "sla_breached": breached,
        "sla_breach_reason": (
            f"pending {round(pending_seconds, 3)}s >= SLA {round(threshold_seconds, 3)}s"
            if breached
            else ""
        ),
    }


def queue_nudge_cooldown_status(message: dict[str, Any], now: str, hook_input: dict[str, Any]) -> dict[str, Any]:
    cooldown_seconds = bounded_float_input(
        hook_input.get("nudge_cooldown_seconds")
        or hook_input.get("nudgeCooldownSeconds")
        or os.environ.get("ITB_QUEUE_WATCH_NUDGE_COOLDOWN_SECONDS"),
        default=60.0,
        minimum=0.0,
        maximum=3600.0,
    )
    last_nudged_at = normalize_cell(message.get("last_nudged_at"))
    now_dt = iso_datetime_or_none(now)
    last_nudged_dt = iso_datetime_or_none(last_nudged_at) if last_nudged_at else None
    age_seconds = (now_dt - last_nudged_dt).total_seconds() if now_dt and last_nudged_dt else None
    future_clock_skew = age_seconds is not None and age_seconds < 0
    cooldown_active = (
        cooldown_seconds > 0
        and age_seconds is not None
        and not future_clock_skew
        and age_seconds < cooldown_seconds
    )
    return {
        "cooldown_seconds": round(cooldown_seconds, 3),
        "last_nudged_at": last_nudged_at,
        "last_nudge_age_seconds": round(age_seconds, 3) if age_seconds is not None else "",
        "future_clock_skew": future_clock_skew,
        "cooldown_active": cooldown_active,
        "cooldown_remaining_seconds": (
            round(max(0.0, cooldown_seconds - float(age_seconds or 0.0)), 3)
            if cooldown_active
            else 0.0
        ),
    }








def notification_class_for_event(
    *,
    event_type: str = "",
    result: str = "",
    status: str = "",
    decision: str = "",
    approval_required: bool = False,
    errors: list[str] | None = None,
    sla_breached: bool = False,
) -> str:
    normalized_values = {
        normalized_publication_value(event_type),
        normalized_publication_value(result),
        normalized_publication_value(status),
        normalized_publication_value(decision),
    }
    if approval_required or normalized_values.intersection({"approval_required", "approval_wait", "waiting_human"}):
        return normalize_cell(GATE_SLA.get("approval_wait_notification_class") or "approval_wait")
    if errors or sla_breached or normalized_values.intersection(
        {
            "ambiguous",
            "block",
            "blocked",
            "blocked_by_precheck",
            "dead_letter",
            "dead_lettered",
            "error",
            "failed",
            "flow_alert",
            "completed_with_errors",
            "partial_error",
            "sla_breach",
            "sla_breached",
        }
    ):
        if normalized_values.intersection({"dead_letter", "dead_lettered"}):
            return normalize_cell(GATE_SLA.get("dead_letter_notification_class") or "flow_alert")
        return normalize_cell(GATE_SLA.get("breach_notification_class") or "flow_alert")
    if normalized_values.intersection(
        {
            "complete",
            "completed",
            "done",
            "gate_precheck_passed",
            "pass",
            "quality_ok",
            "ready_for_gate_verdict",
            "recovered",
        }
    ):
        return normalize_cell(GATE_SLA.get("done_notification_class") or "done")
    return normalize_cell(GATE_SLA.get("idle_notification_class") or "silent")


PROMPT_SUBMIT_CHAIN_KEYS = ("prompt_submit_chain_id", "promptSubmitChainId")


def prompt_submit_chain_id_from_mapping(value: Any) -> str:
    if not isinstance(value, dict):
        return ""
    for key in PROMPT_SUBMIT_CHAIN_KEYS:
        chain_id = normalize_cell(value.get(key))
        if chain_id:
            return chain_id
    context = value.get("auto_handoff_context")
    if isinstance(context, dict):
        for key in PROMPT_SUBMIT_CHAIN_KEYS:
            chain_id = normalize_cell(context.get(key))
            if chain_id:
                return chain_id
    payload = value.get("payload")
    if isinstance(payload, dict):
        for key in PROMPT_SUBMIT_CHAIN_KEYS:
            chain_id = normalize_cell(payload.get(key))
            if chain_id:
                return chain_id
        context = payload.get("auto_handoff_context")
        if isinstance(context, dict):
            for key in PROMPT_SUBMIT_CHAIN_KEYS:
                chain_id = normalize_cell(context.get(key))
                if chain_id:
                    return chain_id
    return ""


def queue_message_prompt_submit_chain_id(message: dict[str, Any]) -> str:
    return prompt_submit_chain_id_from_mapping(message)


def stamp_payload_prompt_submit_chain_id(payload: dict[str, Any], chain_id: str) -> None:
    chain_id = normalize_cell(chain_id)
    if not chain_id:
        return
    payload["prompt_submit_chain_id"] = chain_id
    context = payload.get("auto_handoff_context")
    if not isinstance(context, dict):
        context = {}
    context.setdefault("prompt_submit_chain_id", chain_id)
    payload["auto_handoff_context"] = context


def append_queue_metric(
    *,
    session_dir: Path,
    queue_root: Path,
    runtime: str,
    session_id: str,
    organization_instance_id: str,
    role_id: str,
    message: dict[str, Any],
    event_type: str,
    result: str,
    now: str,
    duration_seconds: float = 0.0,
    retry_count: int = 0,
    extra: dict[str, Any] | None = None,
) -> None:
    metric = {
        "ts": now,
        "runtime": runtime,
        "session_id": session_id,
        "organization_instance_id": organization_instance_id,
        "role_id": role_id,
        "from_role": normalize_cell(message.get("from_role")),
        "to_role": normalize_cell(message.get("to_role")) or role_id,
        "hop_key": f"{normalize_cell(message.get('from_role')) or 'unknown'}->{normalize_cell(message.get('to_role')) or role_id}",
        "task_id": normalize_cell(message.get("task_id")),
        "message_id": normalize_cell(message.get("message_id")),
        "event_type": event_type,
        "result": result,
        "pending_latency_sec": message_pending_latency_seconds(message, now),
        "duration_sec": round(max(0.0, duration_seconds), 3),
        "retry_count": retry_count,
    }
    prompt_submit_chain_id = queue_message_prompt_submit_chain_id(message)
    if prompt_submit_chain_id:
        metric["prompt_submit_chain_id"] = prompt_submit_chain_id
    sla = gate_sla_status_for_message(role_id, message, now)
    metric.update(sla)
    if extra:
        metric.update(extra)
    if not normalize_cell(metric.get("notification_class")):
        metric["notification_class"] = notification_class_for_event(
            event_type=event_type,
            result=result,
            sla_breached=bool(metric.get("sla_breached")),
        )
    append_jsonl_atomic(queue_root / "metrics" / f"{role_id}.jsonl", metric)
    append_jsonl_atomic(session_dir / "gate-metrics.jsonl", metric)


def append_gate_command_metric(
    *,
    session_dir: Path,
    queue_root: Path,
    runtime: str,
    session_id: str,
    organization_instance_id: str,
    role_id: str,
    from_role: str,
    to_role: str,
    task_id: str,
    message_id: str,
    result: str,
    started_at: str,
    completed_at: str,
    duration_seconds: float,
    command: str,
    extra: dict[str, Any] | None = None,
) -> None:
    duration = round(max(0.001, duration_seconds), 3)
    metric = {
        "ts": completed_at,
        "runtime": runtime,
        "session_id": session_id,
        "organization_instance_id": organization_instance_id,
        "role_id": role_id,
        "agent_id": role_id,
        "from_role": from_role,
        "to_role": to_role,
        "hop_key": f"{from_role or 'unknown'}->{to_role or role_id}",
        "task_id": task_id,
        "message_id": message_id,
        "request_id": command,
        "event_type": "finalized",
        "result": result,
        "usage_source": "builder_command",
        "effective_model": "deterministic",
        "completion_source": f"{command}_command",
        "started_at": started_at,
        "completed_at": completed_at,
        "duration_sec": duration,
        "pending_latency_sec": 0.0,
        "retry_count": 0,
        "command": command,
    }
    metric.update(
        gate_sla_status_for_message(
            role_id,
            {
                "from_role": from_role,
                "to_role": to_role,
                "created_at": started_at,
                "done_at": completed_at,
            },
            completed_at,
        )
    )
    if extra:
        metric.update(extra)
    metric["notification_class"] = notification_class_for_event(
        event_type="gate_command",
        result=result,
        sla_breached=bool(metric.get("sla_breached")),
    )
    append_jsonl_atomic(queue_root / "metrics" / f"{role_id}.jsonl", metric)
    append_jsonl_atomic(session_dir / "gate-metrics.jsonl", metric)


def optional_metric_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        try:
            return int(float(str(value)))
        except (TypeError, ValueError):
            return None


def nested_metric_int(data: dict[str, Any], paths: list[tuple[str, ...]]) -> int | None:
    for path in paths:
        current: Any = data
        for key in path:
            if not isinstance(current, dict) or key not in current:
                current = None
                break
            current = current[key]
        parsed = optional_metric_int(current)
        if parsed is not None:
            return parsed
    return None


def collect_provider_usage_metrics(data: dict[str, Any]) -> dict[str, int]:
    fields: dict[str, int] = {}
    input_tokens = nested_metric_int(
        data,
        [
            ("input_tokens",),
            ("inputTokens",),
            ("prompt_tokens",),
            ("promptTokens",),
            ("usage", "input_tokens"),
            ("usage", "inputTokens"),
            ("usage", "prompt_tokens"),
            ("usage", "promptTokens"),
            ("message", "usage", "input_tokens"),
            ("message", "usage", "inputTokens"),
            ("message", "usage", "prompt_tokens"),
            ("response", "usage", "input_tokens"),
            ("response", "usage", "inputTokens"),
            ("response", "usage", "prompt_tokens"),
            ("metrics", "input_tokens"),
            ("metrics", "inputTokens"),
        ],
    )
    output_tokens = nested_metric_int(
        data,
        [
            ("output_tokens",),
            ("outputTokens",),
            ("completion_tokens",),
            ("completionTokens",),
            ("usage", "output_tokens"),
            ("usage", "outputTokens"),
            ("usage", "completion_tokens"),
            ("usage", "completionTokens"),
            ("message", "usage", "output_tokens"),
            ("message", "usage", "outputTokens"),
            ("message", "usage", "completion_tokens"),
            ("response", "usage", "output_tokens"),
            ("response", "usage", "outputTokens"),
            ("response", "usage", "completion_tokens"),
            ("metrics", "output_tokens"),
            ("metrics", "outputTokens"),
        ],
    )
    total_tokens = nested_metric_int(
        data,
        [
            ("total_tokens",),
            ("totalTokens",),
            ("usage", "total_tokens"),
            ("usage", "totalTokens"),
            ("message", "usage", "total_tokens"),
            ("message", "usage", "totalTokens"),
            ("response", "usage", "total_tokens"),
            ("response", "usage", "totalTokens"),
            ("metrics", "total_tokens"),
            ("metrics", "totalTokens"),
        ],
    )
    duration_api_ms = nested_metric_int(
        data,
        [
            ("duration_api_ms",),
            ("durationApiMs",),
            ("api_duration_ms",),
            ("apiDurationMs",),
            ("duration_ms",),
            ("durationMs",),
            ("metrics", "duration_api_ms"),
            ("metrics", "durationApiMs"),
            ("response", "duration_api_ms"),
            ("response", "durationApiMs"),
            ("message", "duration_api_ms"),
            ("message", "durationApiMs"),
        ],
    )
    num_turns = nested_metric_int(
        data,
        [
            ("num_turns",),
            ("numTurns",),
            ("turn_count",),
            ("turnCount",),
            ("metrics", "num_turns"),
            ("metrics", "numTurns"),
            ("response", "num_turns"),
            ("response", "numTurns"),
            ("message", "num_turns"),
            ("message", "numTurns"),
        ],
    )
    turn_duration_ms = nested_metric_int(
        data,
        [
            ("turn_duration_ms",),
            ("turnDurationMs",),
            ("metrics", "turn_duration_ms"),
            ("metrics", "turnDurationMs"),
            ("response", "turn_duration_ms"),
            ("response", "turnDurationMs"),
            ("message", "turn_duration_ms"),
            ("message", "turnDurationMs"),
        ],
    )
    if input_tokens is not None:
        fields["input_tokens"] = input_tokens
    if output_tokens is not None:
        fields["output_tokens"] = output_tokens
    if total_tokens is not None:
        fields["total_tokens"] = total_tokens
    if duration_api_ms is not None:
        fields["duration_api_ms"] = duration_api_ms
    if turn_duration_ms is not None:
        fields["turn_duration_ms"] = turn_duration_ms
    if num_turns is not None:
        fields["num_turns"] = num_turns
    return fields


def provider_turn_duration_metric_fields(record: dict[str, Any]) -> dict[str, int]:
    if (
        normalize_cell(record.get("type")) != "system"
        or normalize_cell(record.get("subtype")) != "turn_duration"
    ):
        return {}
    turn_duration_ms = nested_metric_int(
        record,
        [
            ("turn_duration_ms",),
            ("turnDurationMs",),
            ("duration_ms",),
            ("durationMs",),
        ],
    )
    return {"turn_duration_ms": turn_duration_ms} if turn_duration_ms is not None else {}


def provider_transcript_records(path: Path) -> list[dict[str, Any]]:
    try:
        stat = path.stat()
        if not path.is_file():
            return []
        max_bytes = env_int_default("ITB_PROVIDER_USAGE_TRANSCRIPT_MAX_BYTES", 2 * 1024 * 1024)
        if max_bytes <= 0:
            return []
        offset = max(0, stat.st_size - max_bytes)
        with path.open("rb") as fh:
            if offset:
                fh.seek(offset)
            raw_bytes = fh.read(max_bytes)
    except OSError:
        return []
    raw = raw_bytes.decode("utf-8", errors="replace").strip()
    if not raw:
        return []
    if not offset:
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, dict):
            return [parsed]
        if isinstance(parsed, list):
            return [item for item in parsed if isinstance(item, dict)]

    lines = raw.splitlines()
    if offset and lines:
        lines = lines[1:]
    records: list[dict[str, Any]] = []
    for line in lines:
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict):
            records.append(event)
    return records


def provider_transcript_usage_metric_fields_from_records(records: list[dict[str, Any]]) -> dict[str, int]:
    fields: dict[str, int] = {}
    for record in records:
        record_fields = collect_provider_usage_metrics(record)
        if (
            normalize_cell(record.get("type")) == "system"
            and normalize_cell(record.get("subtype")) == "turn_duration"
        ):
            record_fields.pop("duration_api_ms", None)
            record_fields.update(provider_turn_duration_metric_fields(record))
        for key, value in record_fields.items():
            fields[key] = value
    if "total_tokens" not in fields and ("input_tokens" in fields or "output_tokens" in fields):
        fields["total_tokens"] = int(fields.get("input_tokens") or 0) + int(fields.get("output_tokens") or 0)
    return fields


def provider_transcript_usage_metric_fields(evidence: dict[str, Any]) -> dict[str, int]:
    path_text = normalize_cell(evidence.get("transcript_path") or evidence.get("transcriptPath"))
    if not path_text:
        return {}
    transcript_path = Path(os.path.expanduser(path_text))
    return provider_transcript_usage_metric_fields_from_records(provider_transcript_records(transcript_path))


def provider_transcript_metadata_fields_from_records(records: list[dict[str, Any]]) -> dict[str, str]:
    fields: dict[str, str] = {}
    for record in records:
        request_id = normalize_cell(
            record.get("requestId")
            or record.get("request_id")
            or str_from_nested(record, [("message", "requestId"), ("message", "request_id")])
        )
        effective_model = normalize_cell(
            record.get("model")
            or record.get("effective_model")
            or record.get("effectiveModel")
            or str_from_nested(record, [("message", "model"), ("message", "effective_model"), ("message", "effectiveModel")])
        )
        if request_id:
            fields["request_id"] = request_id
        if effective_model:
            fields["effective_model"] = effective_model
    return fields


def provider_transcript_metadata_fields(path_text: str) -> dict[str, str]:
    if not normalize_cell(path_text):
        return {}
    transcript_path = Path(os.path.expanduser(path_text))
    return provider_transcript_metadata_fields_from_records(provider_transcript_records(transcript_path))


def claude_project_dir_name_for_cwd(cwd: Path) -> str:
    return re.sub(r"[^A-Za-z0-9_-]", "-", str(cwd.expanduser()))


def claude_project_transcript_dir_for_cwd(cwd: Path) -> Path:
    return Path.home() / ".claude" / "projects" / claude_project_dir_name_for_cwd(cwd)


def claude_transcript_cwd_candidates_for_role_report(
    *,
    state_root: Path,
    session_id: str,
    role_id: str,
    preferred_cwds: list[str | Path] | None = None,
) -> list[Path]:
    candidates: list[Path] = []
    seen: set[str] = set()

    def add_candidate(value: object) -> None:
        text = normalize_cell(value)
        if not text:
            return
        path = Path(text).expanduser()
        key = str(path)
        if key in seen:
            return
        seen.add(key)
        candidates.append(path)

    for cwd_value in preferred_cwds or []:
        add_candidate(cwd_value)
    session_dir = state_root / safe_id(session_id)
    bootstrap_state = read_json_object_if_exists(session_dir / "bootstrap.json")
    add_candidate(bootstrap_state.get("cwd"))
    add_candidate(state_root / safe_id(session_id) / "provider-state" / safe_id(role_id) / "claude")
    return candidates


ROLE_REPORT_INTERACTIVE_REQUEST_PLACEHOLDERS = {"", "interactive-role-report"}


def provider_transcript_text_has_exact_token(record: dict[str, Any], token: str) -> bool:
    normalized_token = normalize_cell(token)
    if not normalized_token:
        return False
    message = record.get("message") if isinstance(record.get("message"), dict) else {}
    record_text = message.get("content") if isinstance(message.get("content"), str) else ""
    pattern = rf"(?<![A-Za-z0-9_.-]){re.escape(normalized_token)}(?![A-Za-z0-9_.-])"
    return re.search(pattern, record_text) is not None


def provider_transcript_record_cwd(record: dict[str, Any]) -> str:
    return normalize_cell(record.get("cwd") or str_from_nested(record, [("message", "cwd")]))


def provider_transcript_record_timestamp(record: dict[str, Any]) -> str:
    return normalize_cell(record.get("timestamp") or str_from_nested(record, [("message", "timestamp")]))


def provider_transcript_record_is_human_prompt(record: dict[str, Any]) -> bool:
    if normalize_cell(record.get("type")) != "user":
        return False
    message = record.get("message")
    if not isinstance(message, dict):
        return False
    return isinstance(message.get("content"), str)


def provider_transcript_record_matches_role_message(
    record: dict[str, Any],
    *,
    provider_cwd: Path,
    message_id: str,
    task_id: str,
    message_created_at: str,
) -> bool:
    if not provider_transcript_record_is_human_prompt(record):
        return False
    record_cwd = provider_transcript_record_cwd(record)
    if record_cwd != str(provider_cwd):
        return False
    token = normalize_cell(message_id) or normalize_cell(task_id)
    if not provider_transcript_text_has_exact_token(record, token):
        return False
    created_dt = iso_datetime_or_none(message_created_at)
    if created_dt is None:
        return True
    record_dt = iso_datetime_or_none(provider_transcript_record_timestamp(record))
    if record_dt is None:
        return False
    stale_tolerance = env_int_default("ITB_CLAUDE_TRANSCRIPT_STALE_TOLERANCE_SECONDS", 300)
    return record_dt >= created_dt - dt.timedelta(seconds=max(0, stale_tolerance))


def provider_transcript_role_message_window(records: list[dict[str, Any]], match_index: int) -> list[dict[str, Any]]:
    end_index = len(records)
    for index in range(match_index + 1, len(records)):
        if provider_transcript_record_is_human_prompt(records[index]):
            end_index = index
            break
    return records[match_index:end_index]


def role_report_transcript_match_for_path(
    path: Path,
    *,
    provider_cwd: Path,
    message_id: str,
    task_id: str,
    message_created_at: str,
    supplied_request_id: str,
) -> tuple[str, list[dict[str, Any]], dict[str, str], dict[str, int]]:
    records = provider_transcript_records(path)
    match_indices = [
        index
        for index, record in enumerate(records)
        if provider_transcript_record_matches_role_message(
            record,
            provider_cwd=provider_cwd,
            message_id=message_id,
            task_id=task_id,
            message_created_at=message_created_at,
        )
    ]
    if not match_indices:
        return "not_found", [], {}, {}
    window_records = provider_transcript_role_message_window(records, match_indices[-1])
    metadata_fields = provider_transcript_metadata_fields_from_records(window_records)
    normalized_supplied_request_id = normalize_cell(supplied_request_id)
    if normalized_supplied_request_id not in ROLE_REPORT_INTERACTIVE_REQUEST_PLACEHOLDERS:
        transcript_request_id = normalize_cell(metadata_fields.get("request_id"))
        if not transcript_request_id:
            return "request_id_missing", [], {}, {}
        if transcript_request_id != normalized_supplied_request_id:
            return "request_id_mismatch", [], {}, {}
    return "found", window_records, metadata_fields, provider_transcript_usage_metric_fields_from_records(window_records)


def discover_claude_transcript_path_for_role_report(
    *,
    state_root: Path,
    session_id: str,
    role_id: str,
    message_id: str,
    task_id: str,
    message_created_at: str = "",
    supplied_request_id: str = "",
    candidate_cwds: list[str | Path] | None = None,
) -> tuple[str, str, dict[str, str], dict[str, int]]:
    max_files = env_int_default("ITB_CLAUDE_TRANSCRIPT_DISCOVERY_MAX_FILES", 24)
    if max_files <= 0:
        return "", "disabled", {}, {}
    matches: list[tuple[Path, dict[str, str], dict[str, int]]] = []
    rejected_statuses: list[str] = []
    project_dir_seen = False
    unreadable_seen = False
    for provider_cwd in claude_transcript_cwd_candidates_for_role_report(
        state_root=state_root,
        session_id=session_id,
        role_id=role_id,
        preferred_cwds=candidate_cwds,
    ):
        project_dir = claude_project_transcript_dir_for_cwd(provider_cwd)
        if not project_dir.is_dir():
            continue
        project_dir_seen = True
        try:
            candidates = sorted(
                [path for path in project_dir.glob("*.jsonl") if path.is_file()],
                key=lambda path: path.stat().st_mtime,
                reverse=True,
            )[:max_files]
        except OSError:
            unreadable_seen = True
            continue
        for path in candidates:
            status, _window_records, metadata_fields, usage_fields = role_report_transcript_match_for_path(
                path,
                provider_cwd=provider_cwd,
                message_id=message_id,
                task_id=task_id,
                message_created_at=message_created_at,
                supplied_request_id=supplied_request_id,
            )
            if status == "found":
                matches.append((path, metadata_fields, usage_fields))
            elif status not in {"not_found"}:
                rejected_statuses.append(status)
    if len(matches) == 1:
        path, metadata_fields, usage_fields = matches[0]
        return str(path), "found", metadata_fields, usage_fields
    if len(matches) > 1:
        return "", "ambiguous", {}, {}
    if rejected_statuses:
        return "", rejected_statuses[-1], {}, {}
    if not project_dir_seen:
        return "", "project_dir_missing", {}, {}
    if unreadable_seen:
        return "", "project_dir_unreadable", {}, {}
    return "", "not_found", {}, {}


def enrich_role_report_provider_evidence_from_claude_transcript(
    provider_evidence: dict[str, Any],
    *,
    state_root: Path,
    session_id: str,
    role_id: str,
    message: dict[str, Any],
) -> None:
    provider = normalize_cell(provider_evidence.get("provider")).lower()
    intended_model = normalize_cell(provider_evidence.get("intended_model")).lower()
    effective_model = normalize_cell(provider_evidence.get("effective_model")).lower()
    expects_claude = provider == "anthropic" or intended_model.startswith("claude-") or effective_model.startswith("claude-")
    if not expects_claude or normalize_cell(provider_evidence.get("transcript_path") or provider_evidence.get("transcriptPath")):
        return
    transcript_path, status, metadata_fields, usage_fields = discover_claude_transcript_path_for_role_report(
        state_root=state_root,
        session_id=session_id,
        role_id=role_id,
        message_id=normalize_cell(message.get("message_id")),
        task_id=normalize_cell(message.get("task_id")),
        message_created_at=normalize_cell(message.get("created_at")),
        supplied_request_id=normalize_cell(provider_evidence.get("request_id")),
        candidate_cwds=[
            normalize_cell(
                provider_evidence.get("launch_cwd")
                or provider_evidence.get("provider_launch_cwd")
                or provider_evidence.get("provider_cwd")
            ),
            normalize_cell(provider_evidence.get("workspace_cwd") or provider_evidence.get("provider_workspace_cwd")),
        ],
    )
    provider_evidence["transcript_discovery_status"] = status
    provider_evidence["transcript_discovery_source"] = "claude_project_cwd_message_id_exact"
    if not transcript_path:
        return
    provider_evidence["transcript_path"] = transcript_path
    provider_evidence["usage_source"] = "claude_transcript_jsonl"
    provider_evidence["transcript_usage_scope"] = "matched_turn"
    for key, value in metadata_fields.items():
        if value and (key != "request_id" or normalize_cell(provider_evidence.get("request_id")) in {"", "interactive-role-report"}):
            provider_evidence[key] = value
    for key, value in usage_fields.items():
        if not normalize_cell(provider_evidence.get(key)):
            provider_evidence[key] = value


def provider_usage_metric_fields(evidence: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(evidence, dict):
        return {}
    direct_fields = collect_provider_usage_metrics(evidence)
    transcript_fields: dict[str, int] = {}
    if (
        normalize_cell(evidence.get("transcript_usage_scope")) != "matched_turn"
        and any(
            key not in direct_fields
            for key in ("input_tokens", "output_tokens", "duration_api_ms", "turn_duration_ms", "num_turns")
        )
    ):
        transcript_fields = provider_transcript_usage_metric_fields(evidence)
    input_tokens = direct_fields.get("input_tokens", transcript_fields.get("input_tokens"))
    output_tokens = direct_fields.get("output_tokens", transcript_fields.get("output_tokens"))
    total_tokens = direct_fields.get("total_tokens", transcript_fields.get("total_tokens"))
    duration_api_ms = direct_fields.get("duration_api_ms", transcript_fields.get("duration_api_ms"))
    turn_duration_ms = direct_fields.get("turn_duration_ms", transcript_fields.get("turn_duration_ms"))
    num_turns = direct_fields.get("num_turns", transcript_fields.get("num_turns"))
    fields: dict[str, Any] = {}
    if input_tokens is not None:
        fields["input_tokens"] = input_tokens
    if output_tokens is not None:
        fields["output_tokens"] = output_tokens
    if input_tokens is not None or output_tokens is not None:
        fields["total_tokens"] = int(input_tokens or 0) + int(output_tokens or 0)
    elif total_tokens is not None:
        fields["total_tokens"] = total_tokens
    if duration_api_ms is not None:
        fields["duration_api_ms"] = duration_api_ms
    if turn_duration_ms is not None:
        fields["turn_duration_ms"] = turn_duration_ms
    if num_turns is not None:
        fields["num_turns"] = num_turns
    return fields


def gate_latency_report_enrichment_enabled(hook_input: dict[str, Any]) -> bool:
    value = hook_input.get("enrich_provider_evidence")
    if value is None:
        value = hook_input.get("enrichProviderEvidence")
    if value is None:
        value = hook_input.get("enrich_report_evidence")
    if value is None:
        value = hook_input.get("enrichReportEvidence")
    return truthy_input(value, default=True)


def gate_latency_metric_role_id(metric: dict[str, Any]) -> str:
    return normalize_cell(metric.get("role_id") or metric.get("agent_id"))


def gate_latency_metric_key(metric: dict[str, Any]) -> tuple[str, str, str]:
    return (
        gate_latency_metric_role_id(metric),
        normalize_cell(metric.get("task_id")),
        normalize_cell(metric.get("message_id")),
    )


def gate_latency_metric_needs_usage_enrichment(metric: dict[str, Any]) -> bool:
    return any(
        optional_metric_int(metric.get(key)) is None
        for key in ("input_tokens", "output_tokens", "total_tokens", "duration_api_ms", "turn_duration_ms", "num_turns")
    )


def gate_latency_queued_created_at_by_key(metrics: list[dict[str, Any]]) -> dict[tuple[str, str, str], str]:
    created_at_by_key: dict[tuple[str, str, str], str] = {}
    for metric in metrics:
        if normalize_cell(metric.get("event_type")) != "queued":
            continue
        key = gate_latency_metric_key(metric)
        if not all(key):
            continue
        created_at = normalize_cell(metric.get("created_at") or metric.get("queued_at") or metric.get("queued_ts") or metric_timestamp(metric))
        if created_at and key not in created_at_by_key:
            created_at_by_key[key] = created_at
    return created_at_by_key


def gate_latency_task_payload_created_at(queue_root: Path, metric: dict[str, Any]) -> str:
    task_id = normalize_cell(metric.get("task_id"))
    message_id = normalize_cell(metric.get("message_id"))
    if not task_id or not message_id:
        return ""
    if queue_component_errors(task_id, "task_id") or queue_component_errors(message_id, "message_id"):
        return ""
    payload_path = queue_root / "tasks" / task_id / f"{message_id}.yaml"
    try:
        payload_path.resolve().relative_to(queue_root.resolve())
    except (OSError, ValueError):
        return ""
    if not payload_path.exists():
        return ""
    try:
        payload = read_json_yaml(payload_path)
    except (OSError, ValueError, json.JSONDecodeError):
        return ""
    if not isinstance(payload, dict):
        return ""
    return normalize_cell(payload.get("created_at") or payload.get("createdAt"))


def gate_latency_inbox_message_created_at(queue_root: Path, metric: dict[str, Any]) -> str:
    role_id, _task_id, message_id = gate_latency_metric_key(metric)
    if not role_id or not message_id:
        return ""
    candidates: list[Path] = [queue_root / "inbox" / f"{role_id}.yaml"]
    try:
        role_row = role_agent_row_for(role_id)
    except Exception:
        role_row = {}
    inbox_ref = normalize_cell(role_row.get("inbox_path")) if isinstance(role_row, dict) else ""
    if inbox_ref:
        candidate = queue_root / inbox_ref
        if candidate not in candidates:
            candidates.append(candidate)
    for path in candidates:
        if not path.exists():
            continue
        try:
            inbox = read_json_yaml(path)
        except (OSError, ValueError, json.JSONDecodeError):
            continue
        if not isinstance(inbox, dict):
            continue
        for item in inbox.get("messages", []):
            if isinstance(item, dict) and normalize_cell(item.get("message_id")) == message_id:
                return normalize_cell(item.get("created_at") or item.get("createdAt"))
    return ""


def gate_latency_metric_message_created_at(
    *,
    queue_root: Path,
    metric: dict[str, Any],
    queued_metric_created_at: str = "",
) -> str:
    return (
        normalize_cell(
            metric.get("message_created_at")
            or metric.get("messageCreatedAt")
            or metric.get("queued_at")
            or metric.get("queued_ts")
        )
        or normalize_cell(queued_metric_created_at)
        or gate_latency_task_payload_created_at(queue_root, metric)
        or gate_latency_inbox_message_created_at(queue_root, metric)
    )


def gate_latency_role_report_candidate_paths(queue_root: Path, metric: dict[str, Any]) -> list[Path]:
    candidates: list[Path] = []
    seen: set[Path] = set()

    def add_candidate(path: Path) -> None:
        if path in seen:
            return
        seen.add(path)
        candidates.append(path)

    report_path_value = normalize_cell(metric.get("report_path") or metric.get("report_ref") or metric.get("reportPath"))
    if report_path_value:
        report_path = Path(os.path.expanduser(report_path_value))
        if report_path.is_absolute():
            try:
                report_path.resolve().relative_to(queue_root.resolve())
            except (OSError, ValueError):
                pass
            else:
                add_candidate(report_path)
        else:
            try:
                add_candidate(safe_queue_relative_path(queue_root, report_path_value, "metric_report_path"))
            except ValueError:
                pass

    role_id = gate_latency_metric_role_id(metric)
    task_id = normalize_cell(metric.get("task_id"))
    if not role_id or not task_id:
        return candidates
    report_dir = queue_root / "reports" / role_id / task_id
    if not report_dir.is_dir():
        return candidates
    max_files = env_int_default("ITB_GATE_LATENCY_REPORT_ENRICHMENT_MAX_FILES", 64)
    if max_files <= 0:
        return candidates
    try:
        report_files = sorted(
            [
                path
                for pattern in ("*.yaml", "*.yml", "*.json")
                for path in report_dir.glob(pattern)
                if path.is_file()
            ],
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )[:max_files]
    except OSError:
        return candidates
    for path in report_files:
        add_candidate(path)
    return candidates


def gate_latency_role_report_matches_metric(report: dict[str, Any], metric: dict[str, Any]) -> bool:
    role_id = gate_latency_metric_role_id(metric)
    task_id = normalize_cell(metric.get("task_id"))
    message_id = normalize_cell(metric.get("message_id"))
    if normalize_cell(report.get("report_type")) not in {"", "role_queue_report", "role_agent_worker_report"}:
        return False
    if role_id and normalize_cell(report.get("from_role")) != role_id:
        return False
    if task_id and normalize_cell(report.get("task_id")) != task_id:
        return False
    if message_id and normalize_cell(report.get("message_id")) != message_id:
        return False
    return bool(role_id and task_id and message_id)


def gate_latency_role_report_for_metric(
    queue_root: Path,
    metric: dict[str, Any],
) -> tuple[dict[str, Any], str]:
    for path in gate_latency_role_report_candidate_paths(queue_root, metric):
        try:
            report = read_json_yaml(path)
        except (OSError, ValueError, json.JSONDecodeError):
            continue
        if isinstance(report, dict) and gate_latency_role_report_matches_metric(report, metric):
            return report, str(path)
    return {}, ""


def gate_latency_report_provider_evidence(report: dict[str, Any]) -> dict[str, Any]:
    evidence = report.get("provider_evidence") if isinstance(report.get("provider_evidence"), dict) else {}
    if not evidence and isinstance(report.get("evidence"), dict):
        evidence = report.get("evidence")
    return dict(evidence) if isinstance(evidence, dict) else {}


def gate_latency_enrich_provider_evidence_from_report(
    *,
    state_root: Path,
    metric: dict[str, Any],
    report: dict[str, Any],
    message_created_at: str = "",
) -> dict[str, Any]:
    evidence = gate_latency_report_provider_evidence(report)
    evidence.setdefault("provider", normalize_cell(metric.get("provider")))
    evidence.setdefault("effective_model", metric_effective_model(metric))
    evidence.setdefault("usage_source", normalize_cell(metric.get("usage_source")))
    evidence.setdefault("transcript_path", normalize_cell(metric.get("transcript_path") or metric.get("transcriptPath")))
    evidence.setdefault("session_id", normalize_cell(metric.get("session_id")))
    session_id = normalize_cell(evidence.get("session_id") or metric.get("session_id"))
    role_id = gate_latency_metric_role_id(metric)
    if session_id and role_id:
        enrich_role_report_provider_evidence_from_claude_transcript(
            evidence,
            state_root=state_root,
            session_id=session_id,
            role_id=role_id,
            message={
                "message_id": normalize_cell(metric.get("message_id") or report.get("message_id")),
                "task_id": normalize_cell(metric.get("task_id") or report.get("task_id")),
                "created_at": normalize_cell(message_created_at),
            },
        )
    return evidence


def gate_latency_enrich_metric_from_report(
    *,
    state_root: Path,
    queue_root: Path,
    metric: dict[str, Any],
    message_created_at: str = "",
) -> tuple[dict[str, Any], dict[str, Any]]:
    if not gate_latency_metric_needs_usage_enrichment(metric) and normalize_cell(metric.get("transcript_path")):
        return metric, {"result": "skipped_complete"}
    report, report_path = gate_latency_role_report_for_metric(queue_root, metric)
    if not report:
        return metric, {"result": "report_not_found"}

    evidence = gate_latency_enrich_provider_evidence_from_report(
        state_root=state_root,
        metric=metric,
        report=report,
        message_created_at=message_created_at,
    )
    usage_fields = provider_usage_metric_fields(evidence)
    enriched = dict(metric)
    changed_fields: list[str] = []
    for key, value in usage_fields.items():
        if optional_metric_int(enriched.get(key)) is None:
            enriched[key] = value
            changed_fields.append(key)

    for key in ("request_id", "usage_source", "effective_model", "transcript_path"):
        value = normalize_cell(evidence.get(key))
        if value and (key != "usage_source" or changed_fields or not normalize_cell(enriched.get(key))):
            if normalize_cell(enriched.get(key)) != value:
                enriched[key] = value
                changed_fields.append(key)

    if changed_fields:
        enriched["metric_enrichment_source"] = "queue_report_provider_evidence"
        enriched["metric_enrichment_report_path"] = report_path
        if normalize_cell(evidence.get("transcript_discovery_status")):
            enriched["metric_enrichment_transcript_discovery_status"] = normalize_cell(
                evidence.get("transcript_discovery_status")
            )
    return enriched, {
        "result": "enriched" if changed_fields else "matched_no_usage",
        "report_path": report_path,
        "changed_fields": sorted(set(changed_fields)),
        "transcript_discovery_status": normalize_cell(evidence.get("transcript_discovery_status")),
    }


def enrich_gate_latency_metrics_from_reports(
    *,
    state_root: Path,
    queue_root: Path,
    metrics: list[dict[str, Any]],
    enabled: bool,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    summary = {
        "enabled": enabled,
        "checked_metric_count": 0,
        "matched_report_count": 0,
        "enriched_metric_count": 0,
        "token_enriched_count": 0,
        "api_duration_enriched_count": 0,
        "turn_duration_enriched_count": 0,
        "transcript_found_count": 0,
    }
    if not enabled:
        return metrics, summary

    enriched_metrics: list[dict[str, Any]] = []
    queued_created_at_by_key = gate_latency_queued_created_at_by_key(metrics)
    for metric in metrics:
        role_id = gate_latency_metric_role_id(metric)
        if not role_id.startswith(("gate-", "teams-project-manager")) or normalize_cell(metric.get("event_type")) != "finalized":
            enriched_metrics.append(metric)
            continue
        summary["checked_metric_count"] += 1
        before_has_token = any(
            optional_metric_int(metric.get(key)) is not None
            for key in ("input_tokens", "output_tokens", "total_tokens")
        )
        before_has_api_duration = optional_metric_int(metric.get("duration_api_ms")) is not None
        before_has_turn_duration = optional_metric_int(metric.get("turn_duration_ms")) is not None
        enriched, enrichment = gate_latency_enrich_metric_from_report(
            state_root=state_root,
            queue_root=queue_root,
            metric=metric,
            message_created_at=gate_latency_metric_message_created_at(
                queue_root=queue_root,
                metric=metric,
                queued_metric_created_at=queued_created_at_by_key.get(gate_latency_metric_key(metric), ""),
            ),
        )
        if enrichment.get("report_path"):
            summary["matched_report_count"] += 1
        if enrichment.get("result") == "enriched":
            summary["enriched_metric_count"] += 1
        after_has_token = any(
            optional_metric_int(enriched.get(key)) is not None
            for key in ("input_tokens", "output_tokens", "total_tokens")
        )
        after_has_api_duration = optional_metric_int(enriched.get("duration_api_ms")) is not None
        after_has_turn_duration = optional_metric_int(enriched.get("turn_duration_ms")) is not None
        if not before_has_token and after_has_token:
            summary["token_enriched_count"] += 1
        if not before_has_api_duration and after_has_api_duration:
            summary["api_duration_enriched_count"] += 1
        if not before_has_turn_duration and after_has_turn_duration:
            summary["turn_duration_enriched_count"] += 1
        if normalize_cell(enrichment.get("transcript_discovery_status")) == "found":
            summary["transcript_found_count"] += 1
        enriched_metrics.append(enriched)
    return enriched_metrics, summary


def append_agent_dispatch_metric(
    *,
    session_dir: Path,
    queue_root: Path,
    runtime: str,
    session_id: str,
    organization_instance_id: str,
    agent_id: str,
    request_id: str,
    source_agent: str,
    usage_source: str,
    effective_model: str,
    result: str,
    started_at: str,
    completed_at: str,
    duration_seconds: float,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
    duration_api_ms: int | None = None,
    turn_duration_ms: int | None = None,
    num_turns: int | None = None,
    completion_source: str = "",
) -> None:
    metric = {
        "ts": completed_at,
        "runtime": runtime,
        "session_id": session_id,
        "organization_instance_id": organization_instance_id,
        "role_id": agent_id,
        "agent_id": agent_id,
        "request_id": request_id,
        "source_agent": source_agent,
        "from_role": source_agent,
        "to_role": agent_id,
        "hop_key": f"{source_agent or 'unknown'}->{agent_id}",
        "event_type": "agent_dispatch",
        "result": result,
        "usage_source": usage_source,
        "effective_model": effective_model,
        "started_at": started_at,
        "completed_at": completed_at,
        "duration_sec": round(max(0.0, duration_seconds), 3),
    }
    if input_tokens is not None:
        metric["input_tokens"] = input_tokens
    if output_tokens is not None:
        metric["output_tokens"] = output_tokens
    if input_tokens is not None or output_tokens is not None:
        metric["total_tokens"] = int(input_tokens or 0) + int(output_tokens or 0)
    if duration_api_ms is not None:
        metric["duration_api_ms"] = duration_api_ms
    if turn_duration_ms is not None:
        metric["turn_duration_ms"] = turn_duration_ms
    if num_turns is not None:
        metric["num_turns"] = num_turns
    if completion_source:
        metric["completion_source"] = completion_source
    append_jsonl_atomic(queue_root / "metrics" / f"{agent_id}.jsonl", metric)
    append_jsonl_atomic(session_dir / "gate-metrics.jsonl", metric)


def percentile(values: list[float], ratio: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = int(round((len(ordered) - 1) * ratio))
    return round(ordered[min(max(index, 0), len(ordered) - 1)], 3)


def metric_effective_model(metric: dict[str, Any]) -> str:
    model = normalize_cell(metric.get("effective_model") or metric.get("intended_model") or metric.get("model"))
    if model:
        return model
    role_id = normalize_cell(metric.get("role_id") or metric.get("agent_id"))
    row = registry_row_for(role_id) if role_id else {}
    return normalize_cell(row.get("primary_model"))


def latency_variant(metric: dict[str, Any]) -> str:
    usage_source = normalize_cell(metric.get("usage_source")).lower()
    model = metric_effective_model(metric).lower()
    permission_mode = normalize_cell(metric.get("permission_mode") or metric.get("permissionMode")).lower()
    if usage_source == "builder_command" or model == "deterministic":
        return "builder_command"
    if usage_source == "codex_exec_json" or "codex_exec" in usage_source:
        return "codex_exec_json"
    if model.startswith("gpt-") or usage_source.startswith("codex_"):
        return "codex_interactive"
    if "haiku" in model:
        permission = "acceptEdits" if permission_mode in {"", "acceptedits"} else permission_mode
        return f"claude_haiku_{permission}"
    if "sonnet" in model:
        return "claude_sonnet_interactive"
    if "opus" in model:
        return "claude_opus_interactive"
    if "claude" in usage_source:
        return "claude_interactive"
    return usage_source or "unknown"


def latency_metric_duration_sec(metric: dict[str, Any]) -> float:
    duration = float(metric.get("duration_sec") or 0.0)
    if duration > 0:
        return duration
    pending_latency = float(metric.get("pending_latency_sec") or 0.0)
    if pending_latency > 0:
        return pending_latency
    queued_at = normalize_cell(metric.get("queued_ts") or metric.get("queued_at") or metric.get("started_at"))
    completed_at = normalize_cell(metric.get("completed_ts") or metric.get("completed_at") or metric.get("ts"))
    if queued_at and completed_at:
        return iso_seconds_delta(completed_at, queued_at)
    return 0.0


def gate_latency_is_terminal_or_response_metric(metric: dict[str, Any]) -> bool:
    event_type = normalize_cell(metric.get("event_type"))
    result = normalize_cell(metric.get("result"))
    return event_type in {"finalized", "agent_dispatch"} or result in {
        "done",
        "provider_response_ready",
        "provider_response_timeout",
        "report_recovered",
    }


def gate_latency_summary_rows(metrics: list[dict[str, Any]]) -> list[dict[str, Any]]:
    buckets: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for metric in metrics:
        role_id = normalize_cell(metric.get("role_id") or metric.get("agent_id"))
        if not role_id.startswith(("gate-", "teams-project-manager")):
            continue
        if metric.get("event_type") == "agent_dispatch" and normalize_cell(metric.get("result")) == "provider_request_sent":
            continue
        result = normalize_cell(metric.get("result"))
        if not gate_latency_is_terminal_or_response_metric(metric):
            continue
        duration = latency_metric_duration_sec(metric)
        if duration <= 0:
            continue
        variant = latency_variant(metric)
        buckets.setdefault((role_id, variant), []).append(metric)
    rows: list[dict[str, Any]] = []
    for (role_id, variant), items in sorted(buckets.items()):
        durations = [latency_metric_duration_sec(item) for item in items]
        pending = [float(item.get("pending_latency_sec") or 0.0) for item in items if float(item.get("pending_latency_sec") or 0.0) > 0]
        input_tokens = [int(item.get("input_tokens") or 0) for item in items if optional_metric_int(item.get("input_tokens")) is not None]
        output_tokens = [int(item.get("output_tokens") or 0) for item in items if optional_metric_int(item.get("output_tokens")) is not None]
        total_tokens = [int(item.get("total_tokens") or 0) for item in items if optional_metric_int(item.get("total_tokens")) is not None]
        api_durations = [int(item.get("duration_api_ms") or 0) for item in items if optional_metric_int(item.get("duration_api_ms")) is not None]
        turn_durations = [int(item.get("turn_duration_ms") or 0) for item in items if optional_metric_int(item.get("turn_duration_ms")) is not None]
        token_sample_count = sum(
            1
            for item in items
            if any(optional_metric_int(item.get(key)) is not None for key in ("input_tokens", "output_tokens", "total_tokens"))
        )
        api_duration_sample_count = sum(1 for item in items if optional_metric_int(item.get("duration_api_ms")) is not None)
        turn_duration_sample_count = sum(1 for item in items if optional_metric_int(item.get("turn_duration_ms")) is not None)
        success_count = sum(1 for item in items if normalize_cell(item.get("result")) in {"done", "provider_response_ready", "report_recovered"})
        rows.append(
            {
                "role_id": role_id,
                "variant": variant,
                "sample_count": len(items),
                "success_count": success_count,
                "duration_avg_sec": round(sum(durations) / len(durations), 3),
                "duration_p50_sec": percentile(durations, 0.50),
                "duration_p90_sec": percentile(durations, 0.90),
                "duration_min_sec": round(min(durations), 3),
                "duration_max_sec": round(max(durations), 3),
                "pending_p50_sec": percentile(pending, 0.50),
                "input_tokens_total": sum(input_tokens),
                "output_tokens_total": sum(output_tokens),
                "total_tokens_total": sum(total_tokens) if total_tokens else sum(input_tokens) + sum(output_tokens),
                "duration_api_ms_avg": round(sum(api_durations) / len(api_durations), 3) if api_durations else 0,
                "turn_duration_ms_avg": round(sum(turn_durations) / len(turn_durations), 3) if turn_durations else 0,
                "token_sample_count": token_sample_count,
                "missing_token_sample_count": len(items) - token_sample_count,
                "api_duration_sample_count": api_duration_sample_count,
                "missing_api_duration_sample_count": len(items) - api_duration_sample_count,
                "turn_duration_sample_count": turn_duration_sample_count,
                "missing_turn_duration_sample_count": len(items) - turn_duration_sample_count,
                "usage_sources": sorted({normalize_cell(item.get("usage_source")) for item in items if normalize_cell(item.get("usage_source"))}),
                "effective_models": sorted({metric_effective_model(item) for item in items if metric_effective_model(item)}),
            }
        )
    return rows


def metric_timestamp(metric: dict[str, Any]) -> str:
    return normalize_cell(metric.get("ts") or metric.get("completed_at") or metric.get("started_at"))


def metric_timestamp_sort_key(metric: dict[str, Any]) -> str:
    return metric_timestamp(metric) or "9999-99-99T99:99:99"


def task_latency_timeline_rows(metrics: list[dict[str, Any]]) -> list[dict[str, Any]]:
    tasks: dict[str, dict[str, Any]] = {}
    for metric in sorted(metrics, key=metric_timestamp_sort_key):
        task_id = normalize_cell(metric.get("task_id"))
        if not task_id:
            continue
        role_id = normalize_cell(metric.get("role_id") or metric.get("agent_id"))
        event_type = normalize_cell(metric.get("event_type"))
        if not role_id or event_type not in {"queued", "finalized", "watch_nudge", "dead_letter", "agent_dispatch"}:
            continue
        task = tasks.setdefault(task_id, {"task_id": task_id, "timestamps": [], "hops": {}})
        ts = metric_timestamp(metric)
        if ts:
            task["timestamps"].append(ts)
        message_or_request_id = normalize_cell(metric.get("message_id") or metric.get("request_id"))
        hop_id = f"{role_id}:{message_or_request_id or event_type}"
        hop = task["hops"].setdefault(
            hop_id,
            {
                "role_id": role_id,
                "from_role": normalize_cell(metric.get("from_role") or metric.get("source_agent")),
                "to_role": normalize_cell(metric.get("to_role")) or role_id,
                "message_id": normalize_cell(metric.get("message_id")),
                "request_id": normalize_cell(metric.get("request_id")),
                "queued_at": "",
                "completed_at": "",
                "last_event_at": "",
                "result": "",
                "duration_sec": 0.0,
                "pending_latency_sec": 0.0,
                "retry_count": 0,
                "events": [],
            },
        )
        hop["events"].append(event_type)
        hop["last_event_at"] = ts or hop["last_event_at"]
        hop["result"] = normalize_cell(metric.get("result")) or hop["result"]
        hop["duration_sec"] = max(float(hop.get("duration_sec") or 0.0), float(metric.get("duration_sec") or 0.0))
        hop["pending_latency_sec"] = max(
            float(hop.get("pending_latency_sec") or 0.0),
            float(metric.get("pending_latency_sec") or 0.0),
        )
        hop["retry_count"] = max(int(hop.get("retry_count") or 0), int(metric.get("retry_count") or 0))
        if event_type == "queued":
            hop["queued_at"] = ts or hop["queued_at"]
        if event_type in {"finalized", "dead_letter", "agent_dispatch"}:
            hop["completed_at"] = ts or hop["completed_at"]

    rows: list[dict[str, Any]] = []
    for task_id, task in sorted(tasks.items()):
        timestamps = sorted(ts for ts in task["timestamps"] if ts)
        hops: list[dict[str, Any]] = []
        for hop in task["hops"].values():
            queued_at = normalize_cell(hop.get("queued_at"))
            completed_at = normalize_cell(hop.get("completed_at") or hop.get("last_event_at"))
            wall_sec = iso_seconds_delta(completed_at, queued_at) if queued_at and completed_at else float(hop.get("duration_sec") or 0.0)
            hops.append(
                {
                    "role_id": hop.get("role_id", ""),
                    "from_role": hop.get("from_role", ""),
                    "to_role": hop.get("to_role", ""),
                    "message_id": hop.get("message_id", ""),
                    "request_id": hop.get("request_id", ""),
                    "queued_at": queued_at,
                    "completed_at": completed_at,
                    "wall_sec": round(max(0.0, wall_sec), 3),
                    "duration_sec": round(float(hop.get("duration_sec") or 0.0), 3),
                    "pending_latency_sec": round(float(hop.get("pending_latency_sec") or 0.0), 3),
                    "retry_count": int(hop.get("retry_count") or 0),
                    "result": hop.get("result", ""),
                    "events": hop.get("events", []),
                }
            )
        hops.sort(key=lambda item: item.get("queued_at") or item.get("completed_at") or "")
        total_wall_sec = iso_seconds_delta(timestamps[-1], timestamps[0]) if len(timestamps) >= 2 else 0.0
        slowest = max(hops, key=lambda item: float(item.get("wall_sec") or item.get("duration_sec") or 0.0), default={})
        rows.append(
            {
                "task_id": task_id,
                "hop_count": len(hops),
                "total_wall_sec": round(total_wall_sec, 3),
                "slowest_hop_role": slowest.get("role_id", ""),
                "slowest_hop_wall_sec": slowest.get("wall_sec", 0.0),
                "hops": hops,
            }
        )
    return rows


def gate_latency_duration_bucket(metrics: list[dict[str, Any]], predicate: Callable[[dict[str, Any]], bool]) -> dict[str, Any]:
    items = [
        metric
        for metric in metrics
        if predicate(metric)
        and gate_latency_is_terminal_or_response_metric(metric)
        and normalize_cell(metric.get("result")) in {"done", "provider_response_ready", "report_recovered"}
        and latency_metric_duration_sec(metric) > 0
    ]
    durations = [latency_metric_duration_sec(item) for item in items]
    return {
        "sample_count": len(items),
        "duration_p50_sec": percentile(durations, 0.50),
        "duration_p90_sec": percentile(durations, 0.90),
        "duration_avg_sec": round(sum(durations) / len(durations), 3) if durations else 0.0,
        "duration_min_sec": round(min(durations), 3) if durations else 0.0,
        "duration_max_sec": round(max(durations), 3) if durations else 0.0,
        "variants": sorted({latency_variant(item) for item in items}),
        "effective_models": sorted({metric_effective_model(item) for item in items if metric_effective_model(item)}),
        "usage_sources": sorted({normalize_cell(item.get("usage_source")) for item in items if normalize_cell(item.get("usage_source"))}),
    }


def gate_latency_duration_bucket_from_durations(durations: list[float], *, sample_count: int | None = None) -> dict[str, Any]:
    return {
        "sample_count": len(durations) if sample_count is None else sample_count,
        "duration_p50_sec": percentile(durations, 0.50),
        "duration_p90_sec": percentile(durations, 0.90),
        "duration_avg_sec": round(sum(durations) / len(durations), 3) if durations else 0.0,
        "duration_min_sec": round(min(durations), 3) if durations else 0.0,
        "duration_max_sec": round(max(durations), 3) if durations else 0.0,
    }


def gate_latency_prompt_submit_target_seconds(hook_input: dict[str, Any]) -> float:
    return bounded_float_input(
        hook_input.get("prompt_submit_target_seconds")
        or hook_input.get("promptSubmitTargetSeconds")
        or hook_input.get("target_seconds")
        or hook_input.get("targetSeconds"),
        default=10.0,
        minimum=0.1,
        maximum=3600.0,
    )


def gate_latency_sla_config_seconds(kind: str, key: str, default: float) -> float:
    source = GATE_SLA.get(kind) if isinstance(GATE_SLA.get(kind), dict) else {}
    try:
        return float(source.get(key) or default)
    except (TypeError, ValueError):
        return default


def is_prompt_submit_gpf_metric(metric: dict[str, Any]) -> bool:
    role_id = gate_latency_metric_role_id(metric)
    if role_id != "gate-prompt-formatter":
        return False
    from_role = normalize_cell(metric.get("from_role") or metric.get("source_agent"))
    task_id = normalize_cell(metric.get("task_id"))
    return from_role.endswith("user-prompt-submit") or task_id.startswith("ENTRY-")


def is_gtc_scaffold_builder_metric(metric: dict[str, Any]) -> bool:
    role_id = gate_latency_metric_role_id(metric)
    if role_id != "gate-task-creator":
        return False
    usage_source = normalize_cell(metric.get("usage_source"))
    return (
        usage_source == "builder_command"
        or normalize_cell(metric.get("completion_source")) == "gtc-scaffold_command"
        or normalize_cell(metric.get("command")) == "gtc-scaffold"
        or metric_effective_model(metric) == "deterministic"
    )


def is_gtc_llm_baseline_metric(metric: dict[str, Any]) -> bool:
    return gate_latency_metric_role_id(metric) == "gate-task-creator" and not is_gtc_scaffold_builder_metric(metric)


def gate_latency_chain_key(metric: dict[str, Any]) -> str:
    keys = gate_latency_chain_keys(metric)
    return keys[0] if keys else ""


def gate_latency_chain_keys(metric: dict[str, Any]) -> list[str]:
    explicit = normalize_cell(metric.get("prompt_submit_chain_id") or metric.get("promptSubmitChainId"))
    if explicit:
        return [explicit]
    keys: list[str] = []
    for key in (
        "source_ref",
        "sourceRef",
        "report_path",
        "report_ref",
        "task_id",
        "source_task_id",
        "sourceTaskId",
    ):
        value = normalize_cell(metric.get(key))
        if value and value not in keys:
            keys.append(value)
    return keys


def gate_latency_prompt_submit_canonical_key(gpf_metric: dict[str, Any], fallback_key: str) -> str:
    explicit = normalize_cell(gpf_metric.get("prompt_submit_chain_id") or gpf_metric.get("promptSubmitChainId"))
    if explicit:
        return explicit
    return normalize_cell(
        gpf_metric.get("task_id")
        or gpf_metric.get("source_task_id")
        or gpf_metric.get("sourceTaskId")
        or fallback_key
    )


def append_gate_latency_chain_metric(chain: dict[str, list[dict[str, Any]]], leg: str, metric: dict[str, Any]) -> None:
    items = chain.setdefault(leg, [])
    if all(existing is not metric for existing in items):
        items.append(metric)


def gate_latency_success_duration_metrics(metrics: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        metric
        for metric in metrics
        if gate_latency_is_terminal_or_response_metric(metric)
        and normalize_cell(metric.get("result")) in {"done", "provider_response_ready", "report_recovered"}
        and latency_metric_duration_sec(metric) > 0
    ]


def gate_latency_prompt_submit_chains(metrics: list[dict[str, Any]]) -> dict[str, dict[str, list[dict[str, Any]]]]:
    alias_chains: dict[str, dict[str, list[dict[str, Any]]]] = {}
    for metric in gate_latency_success_duration_metrics(metrics):
        keys = gate_latency_chain_keys(metric)
        if not keys:
            continue
        if is_prompt_submit_gpf_metric(metric):
            leg = "gpf"
        elif is_gtc_scaffold_builder_metric(metric):
            leg = "gtc_scaffold"
        elif is_gtc_llm_baseline_metric(metric):
            leg = "gtc_llm_baseline"
        else:
            continue
        for key in keys:
            append_gate_latency_chain_metric(alias_chains.setdefault(key, {}), leg, metric)

    chains: dict[str, dict[str, list[dict[str, Any]]]] = {}
    for alias_key, chain in alias_chains.items():
        gpf_items = chain.get("gpf", [])
        if not gpf_items:
            continue
        for gpf_metric in gpf_items:
            canonical_key = gate_latency_prompt_submit_canonical_key(gpf_metric, alias_key)
            if not canonical_key:
                continue
            merged = chains.setdefault(canonical_key, {})
            append_gate_latency_chain_metric(merged, "gpf", gpf_metric)
            for leg in ("gtc_scaffold", "gtc_llm_baseline"):
                for metric in chain.get(leg, []):
                    append_gate_latency_chain_metric(merged, leg, metric)
    return {
        key: chain
        for key, chain in chains.items()
        if chain.get("gpf") and (chain.get("gtc_scaffold") or chain.get("gtc_llm_baseline"))
    }


def gate_latency_chain_leg_duration(chains: dict[str, dict[str, list[dict[str, Any]]]], leg: str) -> dict[str, Any]:
    durations = [
        latency_metric_duration_sec(items[0])
        for items in (chain.get(leg, []) for chain in chains.values())
        if items
    ]
    return gate_latency_duration_bucket_from_durations(durations)


def gate_latency_chain_total_durations(
    chains: dict[str, dict[str, list[dict[str, Any]]]],
    gtc_leg: str,
) -> tuple[list[float], list[dict[str, Any]]]:
    durations: list[float] = []
    samples: list[dict[str, Any]] = []
    for chain_id, chain in sorted(chains.items()):
        gpf_items = chain.get("gpf", [])
        gtc_items = chain.get(gtc_leg, [])
        if not gpf_items or not gtc_items:
            continue
        gpf_duration = latency_metric_duration_sec(gpf_items[0])
        gtc_duration = latency_metric_duration_sec(gtc_items[0])
        total = round(gpf_duration + gtc_duration, 3)
        durations.append(total)
        samples.append(
            {
                "chain_id": chain_id,
                "gpf_duration_sec": round(gpf_duration, 3),
                "gtc_duration_sec": round(gtc_duration, 3),
                "total_sec": total,
                "gtc_leg": gtc_leg,
            }
        )
    return durations, samples


def gate_latency_verdict(value: float, threshold: float, *, ready: bool) -> str:
    if not ready:
        return "insufficient_samples"
    return "pass" if value <= threshold else "fail"


def gate_latency_prompt_submit_comparison(metrics: list[dict[str, Any]], hook_input: dict[str, Any]) -> dict[str, Any]:
    chains = gate_latency_prompt_submit_chains(metrics)
    gpf = gate_latency_chain_leg_duration(chains, "gpf")
    gtc_scaffold = gate_latency_chain_leg_duration(chains, "gtc_scaffold")
    gtc_llm_baseline = gate_latency_chain_leg_duration(chains, "gtc_llm_baseline")
    deterministic_totals, deterministic_samples = gate_latency_chain_total_durations(chains, "gtc_scaffold")
    baseline_totals, baseline_samples = gate_latency_chain_total_durations(chains, "gtc_llm_baseline")
    deterministic_ready = bool(deterministic_totals)
    baseline_ready = bool(baseline_totals)
    deterministic_total_p50 = percentile(deterministic_totals, 0.50)
    baseline_total_p50 = percentile(baseline_totals, 0.50)
    target_seconds = gate_latency_prompt_submit_target_seconds(hook_input)
    default_sla = float(GATE_SLA.get("default_pending_seconds") or 900.0)
    gpf_sla_seconds = gate_latency_sla_config_seconds("role_pending_seconds", "gate-prompt-formatter", default_sla)
    gtc_handoff_sla_seconds = gate_latency_sla_config_seconds(
        "hop_pending_seconds",
        "gate-task-creator->teams-project-manager",
        default_sla,
    )
    combined_sla_seconds = round(gpf_sla_seconds + gtc_handoff_sla_seconds, 3)
    baseline_speedup_ratio = (
        round(baseline_total_p50 / deterministic_total_p50, 3)
        if deterministic_ready and baseline_ready and deterministic_total_p50 > 0
        else 0.0
    )
    if not deterministic_ready:
        speedup_verdict = "insufficient_deterministic_samples"
    elif not baseline_ready:
        speedup_verdict = "baseline_missing"
    elif baseline_speedup_ratio > 1:
        speedup_verdict = "faster_than_llm_baseline"
    else:
        speedup_verdict = "not_faster_than_llm_baseline"
    return {
        "status": "ready" if deterministic_ready else "insufficient_samples",
        "entrypoint": "legacy_prompt_chain",
        "target_seconds": target_seconds,
        "target_verdict": gate_latency_verdict(deterministic_total_p50, target_seconds, ready=deterministic_ready),
        "combined_sla_seconds": combined_sla_seconds,
        "sla_verdict": gate_latency_verdict(deterministic_total_p50, combined_sla_seconds, ready=deterministic_ready),
        "gpf_sla_seconds": gpf_sla_seconds,
        "gtc_handoff_sla_seconds": gtc_handoff_sla_seconds,
        "deterministic_total_p50_sec": deterministic_total_p50,
        "deterministic_total_sample_count": len(deterministic_totals),
        "llm_baseline_total_p50_sec": baseline_total_p50,
        "llm_baseline_total_sample_count": len(baseline_totals),
        "baseline_speedup_ratio": baseline_speedup_ratio,
        "speedup_verdict": speedup_verdict,
        "chain_count": len(chains),
        "deterministic_samples": deterministic_samples[:10],
        "llm_baseline_samples": baseline_samples[:10],
        "gpf": gpf,
        "gtc_scaffold": gtc_scaffold,
        "gtc_llm_baseline": gtc_llm_baseline,
    }


def render_gate_latency_report(summary: dict[str, Any]) -> str:
    rows = summary.get("rows") or []
    row_text = "\n".join(
        "| {role_id} | `{variant}` | {sample_count} | {success_count} | {duration_p50_sec} | {duration_p90_sec} | {duration_avg_sec} | {pending_p50_sec} | {token_sample_count} | {total_tokens_total} | {api_duration_sample_count} | {duration_api_ms_avg} | {turn_duration_sample_count} | {turn_duration_ms_avg} | {models} |".format(
            role_id=row.get("role_id", ""),
            variant=row.get("variant", ""),
            sample_count=row.get("sample_count", 0),
            success_count=row.get("success_count", 0),
            duration_p50_sec=row.get("duration_p50_sec", 0),
            duration_p90_sec=row.get("duration_p90_sec", 0),
            duration_avg_sec=row.get("duration_avg_sec", 0),
            pending_p50_sec=row.get("pending_p50_sec", 0),
            token_sample_count=row.get("token_sample_count", 0),
            total_tokens_total=row.get("total_tokens_total", 0),
            api_duration_sample_count=row.get("api_duration_sample_count", 0),
            duration_api_ms_avg=row.get("duration_api_ms_avg", 0),
            turn_duration_sample_count=row.get("turn_duration_sample_count", 0),
            turn_duration_ms_avg=row.get("turn_duration_ms_avg", 0),
            models=", ".join(row.get("effective_models", [])),
        )
        for row in rows
    )
    timelines = summary.get("task_timelines") or []
    timeline_text = "\n".join(
        "| {task_id} | {hop_count} | {total_wall_sec} | `{slowest_hop_role}` | {slowest_hop_wall_sec} |".format(
            task_id=row.get("task_id", ""),
            hop_count=row.get("hop_count", 0),
            total_wall_sec=row.get("total_wall_sec", 0),
            slowest_hop_role=row.get("slowest_hop_role", ""),
            slowest_hop_wall_sec=row.get("slowest_hop_wall_sec", 0),
        )
        for row in timelines
    )
    missing = ", ".join(summary.get("missing_required_variants") or []) or "none"
    prompt_submit = summary.get("prompt_submit_comparison") or {}
    return f"""# Gate Latency Comparison

| Field | Value |
|---|---|
| status | `{summary.get('status', '')}` |
| session_id | `{summary.get('session_id', '')}` |
| metrics_path | `{summary.get('metrics_path', '')}` |
| metric_count | {summary.get('metric_count', 0)} |
| compared_sample_count | {summary.get('compared_sample_count', 0)} |
| report_enrichment | `{summary.get('report_enrichment', {}).get('enriched_metric_count', 0)} enriched / {summary.get('report_enrichment', {}).get('checked_metric_count', 0)} checked` |
| missing_required_variants | `{missing}` |

| Role | Variant | Samples | Success | p50 sec | p90 sec | avg sec | pending p50 sec | token samples | total tokens | API samples | avg API ms | Turn samples | avg turn ms | Models |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|
{row_text or "| none | `none` | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |  |"}

## PromptSubmit Comparison

| Field | Value |
|---|---|
| status | `{prompt_submit.get('status', '')}` |
| entrypoint | `{prompt_submit.get('entrypoint', '')}` |
| target_seconds | {prompt_submit.get('target_seconds', 0)} |
| target_verdict | `{prompt_submit.get('target_verdict', '')}` |
| combined_sla_seconds | {prompt_submit.get('combined_sla_seconds', 0)} |
| sla_verdict | `{prompt_submit.get('sla_verdict', '')}` |
| deterministic_total_p50_sec | {prompt_submit.get('deterministic_total_p50_sec', 0)} |
| llm_baseline_total_p50_sec | {prompt_submit.get('llm_baseline_total_p50_sec', 0)} |
| baseline_speedup_ratio | {prompt_submit.get('baseline_speedup_ratio', 0)} |
| speedup_verdict | `{prompt_submit.get('speedup_verdict', '')}` |
| gpf_samples | {(prompt_submit.get('gpf') or {}).get('sample_count', 0)} |
| gtc_scaffold_samples | {(prompt_submit.get('gtc_scaffold') or {}).get('sample_count', 0)} |
| gtc_llm_baseline_samples | {(prompt_submit.get('gtc_llm_baseline') or {}).get('sample_count', 0)} |

## Task Timeline

| Task | Hops | total wall sec | Slowest hop | Slowest wall sec |
|---|---:|---:|---|---:|
{timeline_text or "| none | 0 | 0 | `` | 0 |"}
"""


def gate_latency_report_output(
    *,
    runtime: str,
    state_root: Path,
    hook_input: dict[str, Any],
) -> dict[str, Any]:
    session_id, session_source = resolve_session_id(state_root, hook_input)
    if not session_id:
        session_id = "latency-report"
    session_dir = state_root / safe_id(session_id)
    metrics_path_value = normalize_cell(hook_input.get("metrics_path") or hook_input.get("metricsPath"))
    metrics_path = Path(metrics_path_value).expanduser() if metrics_path_value else session_dir / "gate-metrics.jsonl"
    metrics = read_jsonl(metrics_path)
    queue_root = queue_root_for(session_dir, hook_input)
    metrics, report_enrichment = enrich_gate_latency_metrics_from_reports(
        state_root=state_root,
        queue_root=queue_root,
        metrics=metrics,
        enabled=gate_latency_report_enrichment_enabled(hook_input),
    )
    rows = gate_latency_summary_rows(metrics)
    task_timelines = task_latency_timeline_rows(metrics)
    prompt_submit_comparison = gate_latency_prompt_submit_comparison(metrics, hook_input)
    required_variants = normalize_string_list(
        hook_input.get("required_variants") or hook_input.get("requiredVariants")
    ) or ["builder_command", "claude_haiku_acceptEdits", "claude_sonnet_interactive", "codex_exec_json"]
    covered_variants = sorted({row["variant"] for row in rows})
    missing_required_variants = [variant for variant in required_variants if variant not in covered_variants]
    status = "ready" if rows and not missing_required_variants else "insufficient_samples" if rows else "no_samples"
    report_id = f"gate-latency-comparison-{dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
    report_dir = session_dir / "reports" / "gate-latency"
    report_path = report_dir / f"{report_id}.md"
    json_path = report_dir / f"{report_id}.json"
    summary = {
        "ts": current_timestamp(),
        "runtime": runtime,
        "event_type": "gate_latency_comparison",
        "session_id": session_id,
        "session_source": session_source,
        "status": status,
        "metrics_path": str(metrics_path),
        "metric_count": len(metrics),
        "compared_sample_count": sum(row["sample_count"] for row in rows),
        "report_enrichment": report_enrichment,
        "required_variants": required_variants,
        "covered_variants": covered_variants,
        "missing_required_variants": missing_required_variants,
        "rows": rows,
        "prompt_submit_comparison": prompt_submit_comparison,
        "task_timelines": task_timelines,
        "report_path": str(report_path),
        "json_path": str(json_path),
    }
    report_dir.mkdir(parents=True, exist_ok=True)
    write_json_yaml(json_path, summary)
    report_path.write_text(render_gate_latency_report(summary), encoding="utf-8")
    append_jsonl_atomic(session_dir / "gate-latency-comparison-events.jsonl", summary)
    return {"gateLatencyComparison": summary}


def merge_roster_agent_row(
    roster_path: Path,
    fallback_roster: list[Any],
    agent_id: str,
    updated_row: dict[str, Any],
) -> list[Any]:
    try:
        latest_roster = read_json(roster_path) if roster_path.exists() else fallback_roster
    except (OSError, json.JSONDecodeError):
        latest_roster = fallback_roster
    if not isinstance(latest_roster, list):
        latest_roster = fallback_roster

    merged: list[Any] = []
    row_found = False
    for item in latest_roster:
        if isinstance(item, dict) and item.get("agent_id") == agent_id:
            item = item | updated_row
            row_found = True
        merged.append(item)
    if not row_found:
        merged.append(updated_row)
    return merged


def write_json_yaml_locked(path: Path, data: Any, *, lock_path: Path | None = None) -> None:
    lock_path = lock_path or path.with_name(f"{path.name}.lock.d")
    acquire_queue_lock(lock_path)
    try:
        write_json_yaml(path, data)
    finally:
        release_queue_lock(lock_path)


def merge_json_object_locked(
    path: Path,
    fallback: dict[str, Any],
    updates: dict[str, Any],
    *,
    lock_path: Path | None = None,
) -> dict[str, Any]:
    lock_path = lock_path or path.with_name(f"{path.name}.lock.d")
    acquire_queue_lock(lock_path)
    try:
        try:
            current = read_json(path) if path.exists() else fallback
        except (OSError, json.JSONDecodeError):
            current = fallback
        if not isinstance(current, dict):
            current = fallback
        merged = dict(current)
        merged.update(updates)
        write_json_yaml(path, merged)
        return merged
    finally:
        release_queue_lock(lock_path)


def merge_roster_agent_row_locked(
    roster_path: Path,
    fallback_roster: list[Any],
    agent_id: str,
    updated_row: dict[str, Any],
) -> list[Any]:
    lock_path = roster_path.with_name(f"{roster_path.name}.lock.d")
    acquire_queue_lock(lock_path)
    try:
        roster = merge_roster_agent_row(roster_path, fallback_roster, agent_id, updated_row)
        write_json_yaml(roster_path, roster)
        return roster
    finally:
        release_queue_lock(lock_path)


def merge_roster_agent_rows_locked(
    roster_path: Path,
    fallback_roster: list[Any],
    updated_rows: list[dict[str, Any]],
) -> list[Any]:
    lock_path = roster_path.with_name(f"{roster_path.name}.lock.d")
    acquire_queue_lock(lock_path)
    try:
        try:
            roster = read_json(roster_path) if roster_path.exists() else fallback_roster
        except (OSError, json.JSONDecodeError):
            roster = fallback_roster
        if not isinstance(roster, list):
            roster = fallback_roster
        updates_by_agent = {
            normalize_cell(row.get("agent_id")): row
            for row in updated_rows
            if normalize_cell(row.get("agent_id"))
        }
        merged: list[Any] = []
        seen: set[str] = set()
        for item in roster:
            if isinstance(item, dict):
                agent_id = normalize_cell(item.get("agent_id"))
                if agent_id in updates_by_agent:
                    item = item | updates_by_agent[agent_id]
                    seen.add(agent_id)
            merged.append(item)
        for agent_id, row in updates_by_agent.items():
            if agent_id not in seen:
                merged.append(row)
        write_json_yaml(roster_path, merged)
        return merged
    finally:
        release_queue_lock(lock_path)


def safe_queue_relative_path(queue_root: Path, value: str, field_name: str) -> Path:
    errors = queue_relative_path_errors(value, field_name)
    if errors:
        raise ValueError("; ".join(errors))
    path = queue_root / value
    try:
        path.resolve().relative_to(queue_root.resolve())
    except ValueError as exc:
        raise ValueError(f"{field_name} escapes queue_root: {value}") from exc
    return path


def validate_queue_message(message: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    for key in ("message_id", "from_role", "to_role", "task_id", "created_at", "status", "payload"):
        if key not in message or message[key] is None or message[key] == "":
            errors.append(f"message missing required field: {key}")
    if message.get("status") not in QUEUE_STATUS_VALUES:
        errors.append(f"message status must be one of {sorted(QUEUE_STATUS_VALUES)}")
    if not isinstance(message.get("payload"), dict):
        errors.append("message payload must be an object")
    return errors


def normalize_cell(value: Any) -> str:
    return str(value or "").strip().strip("`")


def value_present(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    return True


def evidence_result_status(value: Any) -> str:
    return normalize_cell(value).lower().split(";", 1)[0].strip()


def truthy_status(value: Any) -> bool:
    return normalize_cell(value).lower() in {"true", "yes", "ok", "complete", "completed", "done"}


def falsy_status(value: Any) -> bool:
    return normalize_cell(value).lower() in {"false", "no", "none", "not_applicable", "n/a", "0", "clear"}


def truthy_input(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    return normalize_cell(value).lower() in {"true", "yes", "1", "on"}


INVALID_PUBLICATION_TERMINALS = {
    "deferred_not_requested",
    "not_requested",
    "publication_deferred_not_requested",
    "commit_deferred_not_requested",
}
CONTROLLED_MICRO_FLOW_MODE_VALUES = {
    "controlled_micro_flow",
    "controlled_micro_task",
}
LOCAL_CONTROLLED_MICRO_FLOW_USAGE_SOURCE = "local_controlled_micro_flow"
LOCAL_CONTROLLED_MICRO_FLOW_RESULTS = {
    "local_controlled_micro_flow",
    "controlled_micro_flow_complete",
}
COMMIT_REQUIRED_VALUES = {"true", "yes", "required", "expected"}
ALLOWED_PUBLICATION_FLOWS = {
    "not_required",
    "commit_only",
    "commit_and_push",
    "push_branch",
    "branch_push",
    "pull_request",
    "pr",
    "merge_to_main_and_push",
    "merge_to_default_and_push",
    "vault_direct_write",
}
ALLOWED_BRANCH_ACTIONS = {
    "not_required",
    "create_branch",
    "checkout_existing",
    "reuse_current_branch",
    "worktree",
    "none",
}
FALSE_OR_EMPTY_PUBLICATION_VALUES = {
    "",
    "false",
    "no",
    "none",
    "missing",
    "not_applicable",
    "not_available",
    "pending",
    "blocked",
}


def normalized_key(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "_", normalize_cell(value).lower()).strip("_")


def normalized_publication_value(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "_", normalize_cell(value).lower()).strip("_")


def normalized_branch_name(value: Any) -> str:
    branch = normalize_cell(value).strip("`")
    for prefix in ("refs/heads/", "origin/"):
        if branch.startswith(prefix):
            branch = branch[len(prefix):]
    return branch


def is_github_pull_request_url(value: Any) -> bool:
    url = normalize_cell(value)
    return bool(re.fullmatch(r"https://github\.com/[^/\s]+/[^/\s]+/pull/[0-9]+", url))


def git_publication_pr_verification_errors(publication_result: dict[str, str], pr_url: str) -> list[str]:
    errors: list[str] = []
    pr_verified = table_value(publication_result, "pr_verified", "PR Verified")
    if not truthy_status(pr_verified):
        errors.append("pr_required true but Git Publication Result pr_verified is not true")

    verification_source = table_value(
        publication_result,
        "pr_verification_source",
        "PR Verification Source",
    )
    if normalized_publication_value(verification_source) != "gh_pr_view":
        errors.append("pr_required true but Git Publication Result pr_verification_source is not gh_pr_view")

    verified_url = table_value(publication_result, "pr_verified_url", "PR Verified URL")
    if not is_github_pull_request_url(verified_url):
        errors.append("pr_required true but Git Publication Result pr_verified_url is not a GitHub pull request URL")
    elif normalize_cell(verified_url) != normalize_cell(pr_url):
        errors.append("pr_required true but Git Publication Result pr_verified_url does not match pr_url")

    return errors


def table_value(table: dict[str, str], *keys: str) -> str:
    wanted = {normalized_key(key) for key in keys}
    for key, value in table.items():
        if normalized_key(key) in wanted:
            return normalize_cell(value)
    return ""


def table_has_meaningful_key(table: dict[str, Any], *keys: str) -> bool:
    wanted = {normalized_key(key) for key in keys}
    for key, value in table.items():
        if normalized_key(key) not in wanted:
            continue
        if isinstance(value, bool):
            return True
        return bool(normalize_cell(value))
    return False


def report_dict_section(report: dict[str, Any], *keys: str) -> dict[str, Any]:
    wanted = {normalized_key(key) for key in keys}
    for key, value in report.items():
        if normalized_key(key) in wanted and isinstance(value, dict):
            return value
    for parent_key in (
        "quality_evaluation",
        "qualityEvaluation",
        "Quality Evaluation",
        "completion_gate",
        "completionGate",
        "Completion Gate",
    ):
        parent = report.get(parent_key)
        if not isinstance(parent, dict):
            continue
        for key, value in parent.items():
            if normalized_key(key) in wanted and isinstance(value, dict):
                return value
    return {}


def report_git_publication_manifest(report: dict[str, Any]) -> dict[str, Any]:
    return report_dict_section(
        report,
        "git_publication_manifest",
        "gitPublicationManifest",
        "Git Publication Manifest",
        "publication_manifest",
        "publicationManifest",
    )


def report_task_change_manifest(report: dict[str, Any]) -> dict[str, Any]:
    return report_dict_section(
        report,
        "task_change_manifest",
        "taskChangeManifest",
        "Task Change Manifest",
    )


def gate_schema_field_value(table: dict[str, str], field_name: str, field_schema: dict[str, Any]) -> str:
    labels = field_schema.get("labels")
    if not isinstance(labels, list) or not labels:
        labels = [field_name]
    return table_value(table, *[str(label) for label in labels])


def validate_gate_output_section_schema(section_name: str, table: dict[str, str]) -> list[str]:
    errors: list[str] = []
    section_schema = GATE_OUTPUT_SECTION_SCHEMAS.get(section_name)
    if not isinstance(section_schema, dict):
        return errors
    fields = section_schema.get("fields")
    if not isinstance(fields, dict):
        fields = {}
    for field_name, raw_field_schema in fields.items():
        if not isinstance(raw_field_schema, dict):
            continue
        value = gate_schema_field_value(table, str(field_name), raw_field_schema)
        label = normalize_cell((raw_field_schema.get("labels") or [field_name])[0])
        if raw_field_schema.get("required") and not normalize_cell(value):
            errors.append(f"{section_name} missing required field: {label}")
            continue
        if raw_field_schema.get("truthy") and not truthy_status(value):
            errors.append(f"{section_name} {normalized_key(label)} is not true")
        if raw_field_schema.get("falsy") and not falsy_status(value):
            errors.append(f"{section_name} {normalized_key(label)} is not false")
        if raw_field_schema.get("forbidden_truthy") and truthy_status(value):
            errors.append(f"{section_name} {normalized_key(label)} must not be true")
        expected = normalize_cell(raw_field_schema.get("equals"))
        if expected and normalize_cell(value) != expected:
            errors.append(f"{section_name} {normalized_key(label)} is not {expected}")
        normalized_expected = normalized_publication_value(raw_field_schema.get("normalized_equals"))
        if normalized_expected and normalized_publication_value(value) != normalized_expected:
            errors.append(f"{section_name} {normalized_key(label)} is not {normalized_expected}")

    main_role_values = {
        normalized_publication_value(role)
        for role in (MAIN_AGENT_EVIDENCE_ROLES | MAIN_AGENT_EXECUTOR_ROLES)
    }
    forbidden_source_fields = section_schema.get("forbidden_main_role_source_fields")
    if isinstance(forbidden_source_fields, list):
        for field_name in forbidden_source_fields:
            source_value = normalized_publication_value(table_value(table, str(field_name)))
            if source_value and source_value in main_role_values:
                errors.append(f"{section_name} cannot be self-certified by main agent")
                break
    return errors


def has_meaningful_publication_value(value: Any) -> bool:
    return normalized_publication_value(value) not in FALSE_OR_EMPTY_PUBLICATION_VALUES


AGENT_DISPATCH_ALLOWED_PERMISSION_MODES = {"acceptEdits", "auto", "default", "plan"}
AGENT_DISPATCH_ALLOWED_TOOL_NAMES = {"Agent", "Bash", "Edit", "Glob", "Grep", "Read", "Write"}
GATE_ENTRY_AGENT_ID = "gate-prompt-formatter"
INTERACTIVE_READINESS_BLOCKING_STATUSES = {
    "blocked_provider_approval",
    "blocked_provider_onboarding",
    "blocked_trust_prompt",
    "prompt_busy",
    "prompt_not_ready",
}
QUEUE_STATUS_VALUES = {"pending", "processing", "done", "failed", "archived"}
LOCAL_STUB_USAGE_SOURCE = "role_agent_worker_local_stub"
_COMPLETION_CHAIN_CONFIG = load_completion_chain_config()
_GATE_OUTPUT_SCHEMA_CONFIG = load_gate_output_schemas()
MAIN_AGENT_EXECUTOR_ROLES = set(_COMPLETION_CHAIN_CONFIG["main_agent_executor_roles"])
INVALID_PUBLICATION_USAGE_SOURCES = {
    "",
    LOCAL_STUB_USAGE_SOURCE,
    "bootstrap_metadata_only",
    "main_agent_local",
    "self_certified",
}
EXECUTION_PREFLIGHT_REQUIRED_CHECKS = (
    "organization_instance_bootstrapped",
    "gate_intake_envelope_created",
    "task_detail_created_or_updated",
    "task_index_synced",
    "kanban_synced",
    "project_manager_handoff_created",
    "review_line_defined",
    "team_roster_recorded",
    "active_set_declared",
    "queue_evidence_recorded",
)
TASK_DETAIL_LINE_CAP_DEFAULT = 220
TASK_DETAIL_APPEND_STATUS_VALUES = {
    "pending",
    "pass",
    "block",
    "ambiguous",
    "ready",
    "complete",
    "incomplete",
    "quality_ok",
    "needs_rework",
    "not_required",
    "waiting_human",
    "failed",
}
VALID_ROUTING_DIRECTORS = set(_COMPLETION_CHAIN_CONFIG["valid_routing_directors"])
COMPLETION_CHAIN = tuple(_COMPLETION_CHAIN_CONFIG["completion_chain"])
COMPLETION_GATE_REQUIRED_HOPS = tuple(_COMPLETION_CHAIN_CONFIG["completion_gate_required_hops"])
PRE_FINAL_REQUIRED_SECTIONS = tuple(_COMPLETION_CHAIN_CONFIG["pre_final_required_sections"])
MAIN_AGENT_EVIDENCE_ROLES = set(_COMPLETION_CHAIN_CONFIG["main_agent_evidence_roles"])
AUTO_QUEUE_HANDOFFS = tuple(_COMPLETION_CHAIN_CONFIG.get("auto_queue_handoffs", []))
ASSESSOR_INTEGRATION_POLICY = dict(_COMPLETION_CHAIN_CONFIG.get("assessor_integration_policy", {}))
GATE_SLA = dict(_COMPLETION_CHAIN_CONFIG.get("gate_sla", {}))
GATE_OUTPUT_SECTION_SCHEMAS = dict(_GATE_OUTPUT_SCHEMA_CONFIG["sections"])
TEAM_ROUTING_DIRECTOR_BY_TEAM = {
    "backend": "tech-director",
    "business": "business-director",
    "content": "contents-director",
    "contents": "contents-director",
    "engineering": "tech-director",
    "frontend": "tech-director",
    "infra": "infra-director",
    "infrastructure": "infra-director",
    "mobile": "tech-director",
    "qa": "tech-director",
    "security": "tech-director",
    "tech": "tech-director",
    "technical": "tech-director",
}
GATE_ENTRY_SKIP_PROMPTS = {
    "a",
    "b",
    "c",
    "n",
    "no",
    "ok",
    "y",
    "yes",
    "はい",
    "いいえ",
    "うん",
    "おk",
    "再開",
    "再開して",
    "続けて",
}
GATE_ENTRY_TASK_MARKERS = (
    "commit",
    "fix",
    "implement",
    "review",
    "run",
    "test",
    "update",
    "コミット",
    "レビュー",
    "確認",
    "計画",
    "実装",
    "修正",
    "作成",
    "更新",
    "追加",
    "削除",
    "調査",
    "整理",
    "対応",
    "直し",
    "直して",
    "提案",
    "読み込",
    "要件",
)


def bounded_int_input(value: Any, *, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return min(max(parsed, minimum), maximum)


def bounded_float_input(value: Any, *, default: float, minimum: float, maximum: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return min(max(parsed, minimum), maximum)


def split_markdown_row(line: str) -> list[str]:
    return [cell.strip() for cell in line.strip().strip("|").split("|")]


def markdown_section(text: str, heading: str) -> str:
    marker = f"## {heading}"
    start = text.find(marker)
    if start < 0:
        return ""
    next_start = text.find("\n## ", start + len(marker))
    if next_start < 0:
        return text[start:]
    return text[start:next_start]


def replace_markdown_section(text: str, heading: str, replacement: str) -> str:
    marker = f"## {heading}"
    rendered = replacement.rstrip() + "\n"
    start = text.find(marker)
    if start < 0:
        return text.rstrip() + "\n\n" + rendered if text.strip() else rendered
    next_start = text.find("\n## ", start + len(marker))
    prefix = text[:start].rstrip()
    suffix = text[next_start + 1 :] if next_start >= 0 else ""
    parts = []
    if prefix:
        parts.append(prefix)
    parts.append(rendered.rstrip())
    if suffix.strip():
        parts.append(suffix.lstrip("\n"))
    return "\n\n".join(parts).rstrip() + "\n"


def task_detail_line_cap(hook_input: dict[str, Any] | None = None) -> int:
    hook_input = hook_input or {}
    return bounded_int_input(
        hook_input.get("task_detail_line_cap")
        or hook_input.get("taskDetailLineCap")
        or os.environ.get("ITB_TASK_DETAIL_LINE_CAP")
        or TASK_DETAIL_LINE_CAP_DEFAULT,
        default=TASK_DETAIL_LINE_CAP_DEFAULT,
        minimum=80,
        maximum=2000,
    )


def task_detail_line_lint(text: str, phase: str, hook_input: dict[str, Any] | None = None) -> tuple[list[str], list[str]]:
    line_cap = task_detail_line_cap(hook_input)
    line_count = len(text.splitlines())
    message = f"Task Detail line count {line_count} exceeds cap {line_cap}; move verbose evidence to report files and keep task.md as a thin index"
    if line_count <= line_cap:
        return [], []
    if normalize_flow_phase(phase) == "pre_final_response":
        return [message], []
    return [], [message]


def key_value_table(section_text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in section_text.splitlines():
        if not line.startswith("|"):
            continue
        cells = split_markdown_row(line)
        if len(cells) < 2:
            continue
        key = cells[0]
        if not key or key.lower() in {"field", "check", "---"} or set(key) <= {"-"}:
            continue
        values[key] = cells[1]
    return values


def markdown_table_rows(section_text: str) -> list[dict[str, str]]:
    headers: list[str] | None = None
    rows: list[dict[str, str]] = []
    for line in section_text.splitlines():
        if not line.startswith("|"):
            continue
        cells = markdown_table_cells(line)
        if not cells:
            continue
        if set("".join(cells)) <= {"-"}:
            continue
        if headers is None:
            headers = cells
            continue
        if len(cells) != len(headers):
            continue
        rows.append(dict(zip(headers, cells)))
    return rows


def has_post_publication_completion_artifacts(text: str) -> bool:
    completion_gate = key_value_table(markdown_section(text, "Completion Gate"))
    return (
        bool(markdown_section(text, "Finalization Check"))
        or bool(markdown_section(text, "Guardian Verdict"))
        or bool(markdown_section(text, "Completion Envelope"))
        or bool(markdown_section(text, "Final Transport Render Check"))
        or truthy_status(
            table_value(
                completion_gate,
                "Finalization Status Checked",
                "finalization_status_checked",
                "Guardian Status Checked",
                "guardian_status_checked",
            )
        )
        or truthy_status(table_value(completion_gate, "Vault Final Update", "vault_final_update"))
    )


def git_publication_finalization_errors(
    publication_result: dict[str, str],
    *,
    push_required: bool,
) -> list[str]:
    errors: list[str] = []
    finalization_status = normalized_publication_value(
        table_value(publication_result, "finalization_status", "Finalization Status")
    )
    if finalization_status not in {"complete", "not_required", "separated"}:
        errors.append("Git Publication Result finalization_status is missing or invalid after completion artifacts")
        return errors

    if finalization_status == "complete":
        finalization_commit_hashes = table_value(
            publication_result,
            "finalization_commit_hashes",
            "Finalization Commit Hashes",
            "finalization_commit_hash",
            "Finalization Commit Hash",
        )
        if not has_meaningful_publication_value(finalization_commit_hashes):
            errors.append("Git Publication Result finalization_commit_hashes is missing")
        if push_required:
            finalization_push_status = normalized_publication_value(
                table_value(publication_result, "finalization_push_status", "Finalization Push Status")
            )
            if finalization_push_status != "complete":
                errors.append("push_required true but Git Publication Result finalization_push_status is not complete")
            finalization_remote_branch = table_value(
                publication_result,
                "finalization_remote_branch",
                "Finalization Remote Branch",
            )
            if not has_meaningful_publication_value(finalization_remote_branch):
                errors.append("push_required true but Git Publication Result finalization_remote_branch is missing")
        return errors

    finalization_reason = table_value(
        publication_result,
        "finalization_not_required_reason",
        "Finalization Not Required Reason",
        "finalization_separation_reason",
        "Finalization Separation Reason",
    )
    if not has_meaningful_publication_value(finalization_reason):
        errors.append("Git Publication Result finalization not-required/separated reason is missing")
    return errors


def gate_list_values(value: Any) -> list[str]:
    if isinstance(value, list):
        raw_items = value
    else:
        raw_items = str(value or "").replace("\n", ",").split(",")
    return [normalize_cell(item).strip("`") for item in raw_items if normalize_cell(item).strip("`")]


def scalar_report_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (list, tuple)):
        return ", ".join(normalize_cell(item) for item in value if normalize_cell(item))
    if isinstance(value, dict):
        return ""
    return normalize_cell(value)


def dict_table_value(data: dict[str, Any], *keys: str) -> str:
    wanted = {normalized_key(key) for key in keys}
    for key, value in data.items():
        if normalized_key(key) in wanted:
            return scalar_report_value(value)
    return ""


def report_payload_table(data: Any, *, nested_keys: tuple[str, ...]) -> dict[str, str]:
    if not isinstance(data, dict):
        return {}
    tables: list[dict[str, Any]] = [data]
    for key in nested_keys:
        nested = data.get(key)
        if isinstance(nested, dict):
            tables.append(nested)
    result: dict[str, str] = {}
    for table in tables:
        for key, value in table.items():
            normalized = scalar_report_value(value)
            if normalized:
                result[str(key)] = normalized
    return result


def publication_result_report_table(publication_result: dict[str, str]) -> tuple[dict[str, str], list[str]]:
    report_path_value = table_value(publication_result, "Report Path", "report_path")
    if not report_path_value or normalized_publication_value(report_path_value) in {"none", "missing"}:
        return {}, []
    report_path = Path(report_path_value).expanduser()
    if not report_path.exists():
        return {}, [f"Git Publication Result report_path does not exist: {report_path}"]
    expected_sha = table_value(publication_result, "Report SHA256", "report_sha256")
    if expected_sha and expected_sha not in {"missing", "none"}:
        actual_sha = file_sha256_if_exists(report_path)
        if actual_sha and actual_sha != expected_sha:
            return {}, ["Git Publication Result report_sha256 does not match report_path"]
    try:
        data = read_json_yaml(report_path)
    except Exception as exc:
        return {}, [f"Git Publication Result report_path unreadable: {report_path}: {type(exc).__name__}: {exc}"]
    if not isinstance(data, dict):
        return {}, [f"Git Publication Result report_path must contain an object: {report_path}"]
    return report_payload_table(
        data,
        nested_keys=(
            "git_publication_result",
            "gitPublicationResult",
            "Git Publication Result",
            "publication_result",
            "publicationResult",
        ),
    ), []


def task_detail_compact_task_id(
    *,
    hook_input: dict[str, Any],
    task_detail_path: Path | None,
    task_detail_text: str,
) -> str:
    task_id = normalize_cell(hook_input.get("task_id") or hook_input.get("taskId"))
    if task_id:
        return task_id
    for section_name in (
        "Team Completion Check",
        "Finalization Check",
        "Task Change Manifest",
        "Quality Evaluation",
        "Project Manager Handoff",
        "Metadata",
    ):
        table = key_value_table(markdown_section(task_detail_text, section_name))
        task_id = table_value(table, "task_id", "Task ID", "Parent Task", "Parent Task ID", "parent_task")
        if task_id:
            return task_id
    if task_detail_path is not None:
        parent_name = task_detail_path.parent.name
        if parent_name.startswith("TSK-"):
            return parent_name.split("-", 2)[0] + "-" + parent_name.split("-", 2)[1]
        return task_detail_path.stem
    return "unknown-task"


def gate_command_status(errors: list[str], warnings: list[str]) -> str:
    if errors:
        return "block"
    if any("ambiguous" in item.lower() for item in warnings):
        return "ambiguous"
    return "pass"


def gate_command_missing_evidence(errors: list[str]) -> list[str]:
    markers = ("missing", "not true", "not complete", "unreadable", "does not exist", "not found")
    return [item for item in errors if any(marker in item.lower() for marker in markers)]


def gate_command_blockers(errors: list[str]) -> list[str]:
    markers = ("blocker", "blocked", "approval", "human")
    return [item for item in errors if any(marker in item.lower() for marker in markers)]


def gate_command_next_action(command: str, status: str) -> str:
    if command == "team-completion-check":
        return (
            "queue_gate_task_evaluator"
            if status == "pass"
            else "return_to_teams_project_manager_with_missing_evidence"
        )
    if command == "finalization-check":
        return (
            "handoff_to_main_transport_renderer"
            if status == "pass"
            else "return_to_gate_task_evaluator_or_vault_final_update_with_missing_evidence"
        )
    if command == "vault-final-update":
        return "run_finalization_check" if status == "complete" else "repair_compact_gate_artifacts"
    if command == "final-transport-render-check":
        return "render_final_response" if status == "complete" else "repair_final_transport_render_evidence"
    if command == "evaluator-precheck":
        return (
            "dispatch_thin_gate_task_evaluator_verdict"
            if status == "pass"
            else "return_to_prior_role_with_validation_errors"
        )
    return "proceed" if status == "pass" else "repair_blocking_evidence"


def gate_command_llm_dispatch_policy(command: str, status: str) -> str:
    if command == "team-completion-check":
        return "skip_assessor_runtime_queue_evaluator" if status == "pass" else "skip_assessor_and_evaluator_dispatch"
    if command == "finalization-check":
        return "skip_guardian_runtime_render_final" if status == "pass" else "skip_guardian_and_final_renderer"
    if command == "vault-final-update":
        return "run_builder_finalization_check" if status == "complete" else "skip_finalization_until_vault_rollup_repaired"
    if command == "final-transport-render-check":
        return "render_main_transport_response" if status == "complete" else "skip_main_transport_renderer"
    if command == "evaluator-precheck":
        return "allow_thin_evaluator_verdict" if status == "pass" else "skip_evaluator_dispatch"
    return "allow_next_dispatch" if status == "pass" else "skip_next_dispatch"


def normalized_gate_command_artifact_payload(payload: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(payload)
    command = normalize_cell(normalized.get("command"))
    status = normalize_cell(normalized.get("status"))
    if not command or not status:
        return normalized
    success = status in {"pass", "complete"}
    normalized.setdefault("missing_evidence", [])
    normalized.setdefault("blockers", gate_command_blockers(normalize_string_list(normalized.get("validation_errors"))))
    normalized.setdefault("next_phase_allowed", success)
    normalized.setdefault("next_action", gate_command_next_action(command, status))
    normalized.setdefault("llm_dispatch_policy", gate_command_llm_dispatch_policy(command, status))
    return normalized


def gate_command_artifact_path(session_dir: Path, task_id: str, artifact_name: str) -> Path:
    return session_dir / "gates" / safe_id(task_id or "unknown-task") / f"{artifact_name}.json"


def write_gate_command_artifact(
    *,
    session_dir: Path,
    task_id: str,
    artifact_name: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    artifact_path = gate_command_artifact_path(session_dir, task_id, artifact_name)
    payload = normalized_gate_command_artifact_payload(payload)
    payload = payload | {"artifact_path": str(artifact_path)}
    write_json_yaml(artifact_path, payload)
    return payload


def team_completion_command_payload(
    *,
    runtime: str,
    session_id: str,
    task_id: str,
    task_detail_path: Path | None,
    task_detail_text: str,
    errors: list[str],
    warnings: list[str],
    now: str,
) -> dict[str, Any]:
    team_routing = key_value_table(markdown_section(task_detail_text, "Team Routing Decision"))
    completion = key_value_table(markdown_section(task_detail_text, "Team Completion Check"))
    required_teams = gate_list_values(
        table_value(completion, "Required Teams", "required_teams")
        or ", ".join(
            item
            for item in (
                table_value(team_routing, "Main Team", "main_team"),
                table_value(team_routing, "Supporting Teams", "supporting_teams"),
                table_value(team_routing, "Review Evidence Teams", "review_evidence_teams"),
            )
            if item
        )
    )
    all_director_reports_complete = truthy_status(
        table_value(
            completion,
            "All Director Reports Complete",
            "all_director_reports_complete",
            "All Team Tasks Done",
            "all_team_tasks_done",
        )
    )
    completed_teams_value = table_value(completion, "Completed Teams", "completed_teams")
    if not completed_teams_value and all_director_reports_complete:
        completed_teams_value = table_value(completion, "Required Teams", "required_teams")
    completed_teams = gate_list_values(completed_teams_value)
    missing_teams = gate_list_values(table_value(completion, "Missing Teams", "missing_teams"))
    if required_teams and not missing_teams:
        missing_teams = [team for team in required_teams if team not in completed_teams]
    status = gate_command_status(errors, warnings)
    return {
        "schema_version": 1,
        "command": "team-completion-check",
        "runtime": runtime,
        "session_id": session_id,
        "task_id": task_id,
        "task_detail_path": str(task_detail_path) if task_detail_path else "",
        "created_at": now,
        "status": status,
        "completed_teams": completed_teams,
        "missing_teams": missing_teams,
        "missing_evidence": gate_command_missing_evidence(errors),
        "blockers": gate_command_blockers(errors),
        "next_phase_allowed": status == "pass",
        "next_action": gate_command_next_action("team-completion-check", status),
        "llm_dispatch_policy": gate_command_llm_dispatch_policy("team-completion-check", status),
        "handoff_to": "gate-task-evaluator" if status == "pass" else table_value(completion, "Handoff To", "handoff_to"),
        "validation_errors": errors,
        "validation_warnings": warnings,
        "reason": "; ".join(errors or warnings) if errors or warnings else "team completion evidence is ready",
    }


def finalization_command_payload(
    *,
    runtime: str,
    session_id: str,
    task_id: str,
    task_detail_path: Path | None,
    task_detail_text: str,
    errors: list[str],
    warnings: list[str],
    now: str,
) -> dict[str, Any]:
    finalization = key_value_table(markdown_section(task_detail_text, "Finalization Check"))
    completion_gate = key_value_table(markdown_section(task_detail_text, "Completion Gate"))
    status = gate_command_status(errors, warnings)
    notification_class = notification_class_for_event(
        event_type="finalization_check",
        status=status,
        errors=errors,
    )
    return {
        "schema_version": 1,
        "command": "finalization-check",
        "runtime": runtime,
        "session_id": session_id,
        "task_id": task_id,
        "task_detail_path": str(task_detail_path) if task_detail_path else "",
        "created_at": now,
        "status": status,
        "completed_teams": [],
        "missing_teams": [],
        "missing_evidence": gate_command_missing_evidence(errors),
        "blockers": gate_command_blockers(errors),
        "next_phase_allowed": status == "pass",
        "next_action": gate_command_next_action("finalization-check", status),
        "llm_dispatch_policy": gate_command_llm_dispatch_policy("finalization-check", status),
        "git_publication_closed": not any("Git Publication Result" in item for item in errors),
        "vault_final_update": truthy_status(
            table_value(finalization, "Vault Final Update", "vault_final_update")
            or table_value(completion_gate, "Vault Final Update", "vault_final_update")
        ),
        "task_index_synced": truthy_status(table_value(finalization, "Task Index Synced", "task_index_synced")),
        "kanban_synced": truthy_status(table_value(finalization, "Kanban Synced", "kanban_synced")),
        "completion_envelope_ready": bool(markdown_section(task_detail_text, "Completion Envelope")),
        "transport_render_ready": bool(markdown_section(task_detail_text, "Final Transport Render Check")),
        "handoff_to": "main_transport_renderer" if status == "pass" else table_value(finalization, "Handoff To", "handoff_to"),
        "validation_errors": errors,
        "validation_warnings": warnings,
        "notification_class": notification_class,
        "reason": "; ".join(errors or warnings) if errors or warnings else "finalization evidence is complete",
    }


def evaluation_precheck_command_payload(
    *,
    runtime: str,
    session_id: str,
    task_id: str,
    task_detail_path: Path | None,
    repo_root: Path | None,
    git_diff_status: str,
    git_status_lines: list[str],
    shortcut: str,
    llm_scope: str,
    suggested_task_change_manifest: dict[str, Any],
    suggested_git_publication_manifest: dict[str, Any],
    errors: list[str],
    warnings: list[str],
    now: str,
) -> dict[str, Any]:
    status = gate_command_status(errors, warnings)
    return {
        "schema_version": 1,
        "command": "evaluator-precheck",
        "runtime": runtime,
        "session_id": session_id,
        "task_id": task_id,
        "task_detail_path": str(task_detail_path) if task_detail_path else "",
        "repo_root": str(repo_root) if repo_root else "",
        "created_at": now,
        "status": status,
        "completed_teams": [],
        "missing_teams": [],
        "missing_evidence": gate_command_missing_evidence(errors),
        "blockers": gate_command_blockers(errors),
        "next_phase_allowed": status == "pass",
        "next_action": gate_command_next_action("evaluator-precheck", status),
        "llm_dispatch_policy": gate_command_llm_dispatch_policy("evaluator-precheck", status),
        "git_diff_status": git_diff_status,
        "git_status_lines": git_status_lines,
        "shortcut": shortcut,
        "llm_scope": llm_scope,
        "suggested_task_change_manifest": suggested_task_change_manifest,
        "suggested_git_publication_manifest": suggested_git_publication_manifest,
        "validation_errors": errors,
        "validation_warnings": warnings,
        "reason": "; ".join(errors or warnings) if errors or warnings else "evaluation precheck is ready",
    }


def hook_task_detail_path(hook_input: dict[str, Any]) -> Path | None:
    value = (
        hook_input.get("task_detail_path")
        or hook_input.get("taskDetailPath")
        or hook_input.get("task_path")
        or os.environ.get("ITB_TASK_DETAIL_PATH")
    )
    if not value:
        return None
    return Path(str(value)).expanduser()


FLOW_PHASE_VALUES = {"pre_execution", "post_routing", "pre_final_response"}


def normalize_flow_phase(value: Any, default: str = "pre_execution") -> str:
    normalized = str(value or default).strip().lower().replace("-", "_")
    if normalized in FLOW_PHASE_VALUES:
        return normalized
    return default


def hook_flow_phase_raw(hook_input: dict[str, Any]) -> Any:
    return hook_input.get("flow_phase") or hook_input.get("flowPhase") or os.environ.get("ITB_FLOW_PHASE")


def flow_phase_validation_error(value: Any) -> str:
    if value is None or normalize_cell(value) == "":
        return ""
    normalized = str(value).strip().lower().replace("-", "_")
    if normalized in FLOW_PHASE_VALUES:
        return ""
    allowed = ", ".join(sorted(FLOW_PHASE_VALUES))
    return f"unsupported flow_phase: {value}; allowed={allowed}"


def hook_flow_phase(hook_input: dict[str, Any]) -> str:
    return normalize_flow_phase(hook_flow_phase_raw(hook_input))


def active_task_path(session_dir: Path) -> Path:
    return session_dir / "active-task.json"


def load_active_task(session_dir: Path) -> tuple[dict[str, Any] | None, list[str], list[str]]:
    path = active_task_path(session_dir)
    if not path.exists():
        return None, [], []
    try:
        active_task = read_json(path)
    except (OSError, json.JSONDecodeError) as exc:
        return None, [f"active-task.json unreadable: {exc}"], []
    if not isinstance(active_task, dict):
        return None, ["active-task.json must contain an object"], []
    status = normalize_cell(active_task.get("status")).lower()
    if status in {"", "active", "in_progress", "ready", "waiting_human"}:
        return active_task, [], []
    if status in {"closed", "cleared", "done", "complete", "completed", "inactive"}:
        return None, [], [f"active-task.json ignored because status={status}"]
    return None, [f"active-task.json has unsupported status: {status}"], []


def active_task_detail_path(active_task: dict[str, Any]) -> Path | None:
    value = (
        active_task.get("task_detail_path")
        or active_task.get("taskDetailPath")
        or active_task.get("task_path")
    )
    if not value:
        return None
    return Path(str(value)).expanduser()


def active_task_flow_phase(active_task: dict[str, Any]) -> str:
    return normalize_flow_phase(active_task.get("flow_phase") or active_task.get("flowPhase"))


def task_detail_status(task_detail_path: Path) -> str:
    try:
        text = task_detail_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""

    frontmatter_match = re.match(r"^---\n(.*?)\n---", text, flags=re.S)
    if frontmatter_match:
        for line in frontmatter_match.group(1).splitlines():
            if line.startswith("status:"):
                return normalize_cell(line.split(":", 1)[1]).strip('"').strip("'").lower()

    metadata = key_value_table(markdown_section(text, "Metadata"))
    return normalize_cell(metadata.get("Status")).lower()


def task_detail_is_complete(task_detail_path: Path) -> bool:
    return task_detail_status(task_detail_path) in {"done", "complete", "completed", "closed"}


def controlled_micro_flow_policy_errors(text: str) -> list[str]:
    section = markdown_section(text, "Controlled Micro-Flow")
    if not section:
        return ["Controlled Micro-Flow section missing"]

    table = key_value_table(section)
    mode = normalized_publication_value(
        table_value(table, "Workflow Mode", "Mode", "flow_mode", "workflow_mode")
    )
    risk_tier = normalized_publication_value(table_value(table, "Risk Tier", "risk_tier"))
    organization_policy = normalized_publication_value(
        table_value(table, "Organization Policy", "organization_policy")
    )
    strict_escalation_checked = table_value(
        table,
        "Strict Flow Escalation Checked",
        "strict_flow_escalation_checked",
    )
    local_gate_evidence_allowed = table_value(
        table,
        "Local Gate Evidence Allowed",
        "local_gate_evidence_allowed",
    )
    external_provider_dispatch = normalized_publication_value(
        table_value(table, "External Provider Dispatch", "external_provider_dispatch")
    )
    escalation_required = table_value(table, "Escalation Required", "escalation_required")
    escalation_triggers = normalized_publication_value(
        table_value(table, "Escalation Triggers", "escalation_triggers")
    )

    errors: list[str] = []
    if mode not in CONTROLLED_MICRO_FLOW_MODE_VALUES:
        errors.append("Controlled Micro-Flow Workflow Mode is not controlled_micro_flow")
    if risk_tier != "low":
        errors.append("Controlled Micro-Flow Risk Tier is not low")
    if organization_policy not in {"preserved", "organization_policy_preserved"}:
        errors.append("Controlled Micro-Flow Organization Policy is not preserved")
    if not truthy_status(strict_escalation_checked):
        errors.append("Controlled Micro-Flow Strict Flow Escalation Checked is not true")
    if not truthy_status(local_gate_evidence_allowed):
        errors.append("Controlled Micro-Flow Local Gate Evidence Allowed is not true")
    if external_provider_dispatch not in {"not_required_for_micro_flow", "not_required"}:
        errors.append("Controlled Micro-Flow External Provider Dispatch is not not_required_for_micro_flow")
    if truthy_status(escalation_required):
        errors.append("Controlled Micro-Flow Escalation Required is true")
    if escalation_triggers and escalation_triggers not in {"none", "none_present", "not_applicable", "n_a"}:
        errors.append("Controlled Micro-Flow Escalation Triggers is not none")
    return errors


def controlled_micro_flow_policy_allows_local_evidence(task_detail_path: Path) -> tuple[bool, list[str]]:
    try:
        text = task_detail_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return False, [f"task detail unreadable for Controlled Micro-Flow: {exc}"]

    errors = controlled_micro_flow_policy_errors(text)
    return not errors, errors


def is_local_controlled_micro_flow_evidence(row: dict[str, str]) -> bool:
    usage_source = normalized_publication_value(row.get("Usage Source", ""))
    result = evidence_result_status(row.get("Result", ""))
    return (
        usage_source == LOCAL_CONTROLLED_MICRO_FLOW_USAGE_SOURCE
        or result in LOCAL_CONTROLLED_MICRO_FLOW_RESULTS
    )


def clear_active_task_with_event(
    *,
    session_dir: Path,
    runtime: str,
    session_id: str,
    now: str,
    reason: str,
    active_task: dict[str, Any] | None = None,
    validation_errors: list[str] | None = None,
) -> dict[str, Any]:
    path = active_task_path(session_dir)
    existed = path.exists()
    if existed:
        path.unlink()
    event = {
        "ts": now,
        "runtime": runtime,
        "event_type": "active_task",
        "session_id": session_id,
        "action": "recovery_clear",
        "result": "active_task_recovery_cleared",
        "reason": reason,
        "active_task_existed": existed,
        "active_task_path": str(path),
    }
    if active_task:
        event.update(
            {
                "task_id": normalize_cell(active_task.get("task_id") or active_task.get("taskId")),
                "task_detail_path": normalize_cell(active_task.get("task_detail_path") or active_task.get("taskDetailPath")),
                "flow_phase": active_task_flow_phase(active_task),
                "owner_role": normalize_cell(active_task.get("owner_role") or active_task.get("ownerRole")),
                "last_gate": normalize_cell(active_task.get("last_gate") or active_task.get("lastGate")),
            }
        )
    if validation_errors:
        event["validation_errors"] = validation_errors
    append_jsonl_atomic(session_dir / "active-task-events.jsonl", event)
    return event


def git_publication_gate_errors(text: str) -> list[str]:
    errors: list[str] = []
    completion_gate = key_value_table(markdown_section(text, "Completion Gate"))
    evaluation = key_value_table(markdown_section(text, "Quality Evaluation"))
    change_manifest = key_value_table(markdown_section(text, "Task Change Manifest"))
    publication_manifest_text = markdown_section(text, "Git Publication Manifest")
    publication_manifest = key_value_table(publication_manifest_text)
    publication_result_text = markdown_section(text, "Git Publication Result")
    publication_result = key_value_table(publication_result_text)
    publication_result_report, publication_result_report_errors = publication_result_report_table(publication_result)
    errors.extend(publication_result_report_errors)
    if publication_result_report:
        publication_result = publication_result | publication_result_report

    commit_required_source = ""
    commit_required_value = ""
    for source, table in (
        ("Task Change Manifest", change_manifest),
        ("Git Publication Manifest", publication_manifest),
        ("Quality Evaluation", evaluation),
        ("Completion Gate", completion_gate),
    ):
        value = table_value(table, "commit_required", "Commit Required")
        if value:
            commit_required_source = source
            commit_required_value = value
            break
    commit_required = normalized_publication_value(commit_required_value) in COMMIT_REQUIRED_VALUES
    push_required_source = ""
    push_required_value = ""
    pr_required_source = ""
    pr_required_value = ""
    for source, table in (
        ("Task Change Manifest", change_manifest),
        ("Git Publication Manifest", publication_manifest),
        ("Quality Evaluation", evaluation),
        ("Completion Gate", completion_gate),
    ):
        if not push_required_value:
            value = table_value(table, "push_required", "Push Required")
            if value:
                push_required_source = source
                push_required_value = value
        if not pr_required_value:
            value = table_value(table, "pr_required", "PR Required", "pull_request_required", "Pull Request Required")
            if value:
                pr_required_source = source
                pr_required_value = value
    push_required = normalized_publication_value(push_required_value) in COMMIT_REQUIRED_VALUES
    pr_required = normalized_publication_value(pr_required_value) in COMMIT_REQUIRED_VALUES

    publication_required_value = (
        table_value(completion_gate, "publication_required", "Publication Required")
        or table_value(publication_manifest, "publication_required", "Publication Required")
    )
    quality_publication_manifest = table_value(evaluation, "Git Publication Manifest", "git_publication_manifest")
    publication_required = (
        commit_required
        or push_required
        or pr_required
        or bool(publication_manifest_text)
        or normalized_publication_value(publication_required_value) in COMMIT_REQUIRED_VALUES
        or normalized_publication_value(quality_publication_manifest) == "present"
    )
    publication_flow = (
        table_value(publication_manifest, "publication_flow", "Publication Flow")
        or table_value(publication_result, "publication_flow", "Publication Flow")
    )
    normalized_flow = normalized_publication_value(publication_flow)
    if normalized_flow and normalized_flow not in ALLOWED_PUBLICATION_FLOWS:
        errors.append(f"Git Publication Manifest uses unsupported publication_flow: {publication_flow}")
    branch_action = (
        table_value(publication_manifest, "branch_action", "Branch Action")
        or table_value(publication_result, "branch_action", "Branch Action")
        or table_value(change_manifest, "branch_action", "Branch Action")
    )
    normalized_branch_action = normalized_publication_value(branch_action)
    if normalized_branch_action and normalized_branch_action not in ALLOWED_BRANCH_ACTIONS:
        errors.append(f"Git Publication Manifest uses unsupported branch_action: {branch_action}")
    if normalized_branch_action == "none" and (commit_required or push_required or pr_required):
        errors.append("branch_action none is invalid when git publication is required")
    if normalized_flow == "vault_direct_write":
        vault_direct_write_approved = (
            table_value(publication_manifest, "vault_direct_write_approved", "Vault Direct Write Approved")
            or table_value(publication_manifest, "vault_direct_write", "Vault Direct Write")
            or table_value(publication_result, "vault_direct_write_approved", "Vault Direct Write Approved")
            or table_value(publication_result, "vault_direct_write", "Vault Direct Write")
        )
        if not truthy_status(vault_direct_write_approved):
            errors.append("publication_flow vault_direct_write requires explicit vault_direct_write approval")
        if commit_required or push_required or pr_required:
            errors.append("publication_flow vault_direct_write cannot be combined with git publication required")

    publication_blob = normalized_publication_value(
        " ".join(
            [
                publication_result_text,
                table_value(evaluation, "Reasons"),
                table_value(publication_result, "Reasons"),
                table_value(publication_result, "blocked_reason", "Blocked Reason"),
                table_value(publication_result, "commit_status", "Commit Status"),
                table_value(publication_result, "git_publication_status", "Git Publication Status"),
                table_value(publication_result, "publication_status", "Publication Status"),
            ]
        )
    )
    for invalid_status in sorted(INVALID_PUBLICATION_TERMINALS):
        if invalid_status in publication_blob:
            errors.append(f"Git Publication Result uses invalid terminal status: {invalid_status}")

    git_publication_required = commit_required or push_required or pr_required
    if git_publication_required and publication_result_text:
        executor_role = table_value(publication_result, "executor_role", "Executor Role", "executor", "Executor")
        if not has_meaningful_publication_value(executor_role):
            errors.append("Git Publication Result executor_role is missing")
        elif normalized_publication_value(executor_role) in MAIN_AGENT_EXECUTOR_ROLES:
            errors.append("Git Publication Result executor_role cannot be main agent")

        executor_session_id = table_value(
            publication_result,
            "executor_session_id",
            "Executor Session ID",
            "provider_session_id",
            "Provider Session ID",
            "session_id",
            "Session ID",
        )
        if not has_meaningful_publication_value(executor_session_id):
            errors.append("Git Publication Result executor_session_id is missing")

        usage_source = table_value(publication_result, "usage_source", "Usage Source")
        normalized_usage_source = normalized_publication_value(usage_source)
        if normalized_usage_source in INVALID_PUBLICATION_USAGE_SOURCES:
            errors.append("Git Publication Result usage_source is missing or not provider-backed")

    if commit_required:
        if not publication_result_text:
            source = f" from {commit_required_source}" if commit_required_source else ""
            errors.append(f"Git Publication Result missing while commit is required{source}")
            return errors

        commit_status = normalized_publication_value(table_value(publication_result, "commit_status", "Commit Status"))
        if commit_status != "complete":
            errors.append("commit_required true but Git Publication Result commit_status is not complete")

        commit_hashes = table_value(publication_result, "commit_hashes", "Commit Hashes", "commit_hash", "Commit Hash")
        if not has_meaningful_publication_value(commit_hashes):
            errors.append("commit_required true but Git Publication Result commit_hashes is missing")

        diff_matches = (
            table_value(
                publication_result,
                "committed_diff_matches_snapshot",
                "Committed Diff Matches Snapshot",
            )
            or table_value(
                change_manifest,
                "committed_diff_matches_snapshot",
                "Committed Diff Matches Snapshot",
            )
        )
        if not truthy_status(diff_matches):
            errors.append("commit_required true but committed_diff_matches_snapshot is not true")

    if push_required:
        if not publication_result_text:
            source = f" from {push_required_source}" if push_required_source else ""
            errors.append(f"Git Publication Result missing while push is required{source}")
            return errors

        push_status = normalized_publication_value(table_value(publication_result, "push_status", "Push Status"))
        if push_status != "complete":
            errors.append("push_required true but Git Publication Result push_status is not complete")

        remote_branch = table_value(publication_result, "remote_branch", "Remote Branch")
        if not has_meaningful_publication_value(remote_branch):
            errors.append("push_required true but Git Publication Result remote_branch is missing")
        if normalized_flow in {"merge_to_main_and_push", "merge_to_default_and_push"}:
            default_branch = (
                table_value(publication_manifest, "default_branch", "Default Branch")
                or table_value(publication_result, "default_branch", "Default Branch")
            )
            if not has_meaningful_publication_value(default_branch):
                errors.append("merge_to_main_and_push publication requires default_branch")
            elif has_meaningful_publication_value(remote_branch) and (
                normalized_branch_name(remote_branch) != normalized_branch_name(default_branch)
            ):
                errors.append("merge_to_main_and_push publication remote_branch does not match default_branch")

    if pr_required:
        if not publication_result_text:
            source = f" from {pr_required_source}" if pr_required_source else ""
            errors.append(f"Git Publication Result missing while PR is required{source}")
            return errors

        pr_status = normalized_publication_value(
            table_value(publication_result, "pr_status", "PR Status", "pull_request_status", "Pull Request Status")
        )
        if pr_status not in {"created", "complete"}:
            errors.append("pr_required true but Git Publication Result pr_status is not created or complete")

        pr_url = table_value(publication_result, "pr_url", "PR URL", "pull_request_url", "Pull Request URL")
        if not has_meaningful_publication_value(pr_url):
            errors.append("pr_required true but Git Publication Result pr_url is missing")
        elif not is_github_pull_request_url(pr_url):
            errors.append("pr_required true but Git Publication Result pr_url is not a GitHub pull request URL")
        else:
            errors.extend(git_publication_pr_verification_errors(publication_result, pr_url))

    if publication_required and publication_result_text:
        publication_status = normalized_publication_value(
            table_value(
                publication_result,
                "git_publication_status",
                "Git Publication Status",
                "publication_status",
                "Publication Status",
            )
        )
        if publication_status and publication_status not in {"complete", "not_required"}:
            errors.append("Git Publication Result status is not complete or not_required")
        if commit_required and publication_status and publication_status != "complete":
            errors.append("commit_required true but Git Publication Result status is not complete")

    if (
        git_publication_required
        and publication_result_text
        and has_post_publication_completion_artifacts(text)
        and normalized_publication_value(
            table_value(publication_result, "git_publication_status", "Git Publication Status")
        )
        == "complete"
    ):
        errors.extend(git_publication_finalization_errors(publication_result, push_required=push_required))

    return errors


def git_publication_manifest_handoff_errors(report: dict[str, Any], task_detail_text: str) -> list[str]:
    errors: list[str] = []
    publication_manifest = report_git_publication_manifest(report)
    if not publication_manifest and task_detail_text:
        publication_manifest = key_value_table(markdown_section(task_detail_text, "Git Publication Manifest"))
    if not publication_manifest:
        return ["Git Publication Manifest missing for git-publisher handoff"]

    commit_required = normalized_publication_value(
        table_value(publication_manifest, "commit_required", "Commit Required")
    ) in COMMIT_REQUIRED_VALUES
    push_required = normalized_publication_value(
        table_value(publication_manifest, "push_required", "Push Required")
    ) in COMMIT_REQUIRED_VALUES
    pr_required = normalized_publication_value(
        table_value(publication_manifest, "pr_required", "PR Required", "pull_request_required", "Pull Request Required")
    ) in COMMIT_REQUIRED_VALUES
    publication_required = (
        commit_required
        or push_required
        or pr_required
        or normalized_publication_value(
            table_value(publication_manifest, "publication_required", "Publication Required")
        ) in COMMIT_REQUIRED_VALUES
    )
    if not publication_required:
        errors.append("git-publisher handoff requires publication_required true or commit/push/pr required")

    handoff_to = table_value(publication_manifest, "handoff_to", "Handoff To")
    if normalized_publication_value(handoff_to) != "git_publisher":
        errors.append("Git Publication Manifest handoff_to is not git-publisher")

    required_fields = (
        ("task_id", ("task_id", "Task ID")),
        ("repo_root", ("repo_root", "Repo Root")),
        ("branch_plan", ("branch_plan", "Branch Plan")),
        ("commit_required", ("commit_required", "Commit Required")),
        ("push_required", ("push_required", "Push Required")),
        ("pr_required", ("pr_required", "PR Required", "pull_request_required", "Pull Request Required")),
        ("publication_policy", ("publication_policy", "Publication Policy")),
        ("publication_flow", ("publication_flow", "Publication Flow")),
    )
    for field_name, labels in required_fields:
        if not table_has_meaningful_key(publication_manifest, *labels):
            errors.append(f"Git Publication Manifest {field_name} is missing for git-publisher handoff")

    publication_flow = table_value(publication_manifest, "publication_flow", "Publication Flow")
    normalized_flow = normalized_publication_value(publication_flow)
    if normalized_flow and normalized_flow not in ALLOWED_PUBLICATION_FLOWS:
        errors.append(f"Git Publication Manifest uses unsupported publication_flow: {publication_flow}")

    branch_action = table_value(publication_manifest, "branch_action", "Branch Action")
    normalized_branch_action = normalized_publication_value(branch_action)
    if normalized_branch_action and normalized_branch_action not in ALLOWED_BRANCH_ACTIONS:
        errors.append(f"Git Publication Manifest uses unsupported branch_action: {branch_action}")
    if normalized_branch_action == "none" and (commit_required or push_required or pr_required):
        errors.append("branch_action none is invalid when git publication is required")

    if commit_required:
        task_change_manifest = (
            table_value(publication_manifest, "task_change_manifest", "Task Change Manifest")
            or table_value(report_task_change_manifest(report), "task_id", "Task ID")
            or table_value(key_value_table(markdown_section(task_detail_text, "Task Change Manifest")), "task_id", "Task ID")
        )
        if not task_change_manifest:
            errors.append("commit_required true but Task Change Manifest is missing for git-publisher handoff")

    return errors


def validate_task_flow_artifact(
    task_detail_path: Path,
    phase: str,
    *,
    optional_pre_final_sections: tuple[str, ...] = (),
) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []
    try:
        text = task_detail_path.read_text(encoding="utf-8")
    except OSError as exc:
        return [f"task detail unreadable: {exc}"], warnings

    line_errors, line_warnings = task_detail_line_lint(text, phase)
    errors.extend(line_errors)
    warnings.extend(line_warnings)

    project_manager_handoff = key_value_table(markdown_section(text, "Project Manager Handoff"))
    if phase in {"pre_execution", "post_routing", "pre_final_response"}:
        if not project_manager_handoff:
            errors.append("Project Manager Handoff missing")
        else:
            if normalize_cell(project_manager_handoff.get("Handoff To")) != "teams-project-manager":
                errors.append("Project Manager Handoff does not target teams-project-manager")
            if normalize_cell(project_manager_handoff.get("Handoff Status")) != "sent_to_project_manager":
                errors.append("Project Manager Handoff status is not sent_to_project_manager")

        execution_preflight = key_value_table(markdown_section(text, "Execution Preflight"))
        if not execution_preflight:
            errors.append("Execution Preflight section missing")
        else:
            for check in EXECUTION_PREFLIGHT_REQUIRED_CHECKS:
                if not truthy_status(execution_preflight.get(check)):
                    errors.append(f"Execution Preflight check not true: {check}")

    team_routing = key_value_table(markdown_section(text, "Team Routing Decision"))
    if phase in {"post_routing", "pre_final_response"}:
        director = normalize_cell(team_routing.get("Handoff To Director"))
        if not team_routing:
            errors.append("Team Routing Decision missing")
        elif director not in VALID_ROUTING_DIRECTORS:
            errors.append("Team Routing Decision missing valid director handoff")
        completion_gate = normalize_cell(team_routing.get("Completion Gate"))
        if any(hop not in completion_gate for hop in COMPLETION_GATE_REQUIRED_HOPS):
            errors.append("Team Routing Decision does not preserve Completion Gate")

    completion_gate_table = key_value_table(markdown_section(text, "Completion Gate"))
    if phase == "pre_final_response":
        optional_sections = {normalize_cell(section) for section in optional_pre_final_sections}
        for section in PRE_FINAL_REQUIRED_SECTIONS:
            if normalize_cell(section) in optional_sections:
                continue
            if not markdown_section(text, section):
                errors.append(f"{section} missing for pre_final_response")

        if not completion_gate_table:
            errors.append("Completion Gate section missing")
        else:
            finalization_checked = table_value(
                completion_gate_table,
                "Finalization Status Checked",
                "finalization_status_checked",
                "Guardian Status Checked",
                "guardian_status_checked",
            )
            finalization_status = table_value(
                completion_gate_table,
                "Finalization Status",
                "finalization_status",
                "Guardian Status",
                "guardian_status",
            )
            if not truthy_status(finalization_checked):
                errors.append("Finalization Status Checked is not true")
            if normalized_publication_value(finalization_status) != "complete":
                errors.append("Finalization Status is not complete")
            if not truthy_status(completion_gate_table.get("Vault Final Update")):
                errors.append("Vault Final Update is not complete")

        for schema_section_name in GATE_OUTPUT_SECTION_SCHEMAS:
            section_table = key_value_table(markdown_section(text, schema_section_name))
            if section_table:
                errors.extend(validate_gate_output_section_schema(schema_section_name, section_table))

        role_execution_rows = markdown_table_rows(markdown_section(text, "Role Execution Evidence"))
        if not role_execution_rows:
            errors.append("Role Execution Evidence table missing for pre_final_response")
        else:
            valid_role_rows = 0
            for row in role_execution_rows:
                role_id = normalize_cell(
                    row.get("Role")
                    or row.get("role")
                    or row.get("Role ID")
                    or row.get("role_id")
                    or row.get("Agent")
                    or row.get("agent")
                )
                result = normalize_cell(row.get("Result") or row.get("result") or row.get("Status") or row.get("status")).lower()
                usage_source = normalize_cell(
                    row.get("Usage Source")
                    or row.get("usage_source")
                    or row.get("Provider Evidence")
                    or row.get("provider_evidence")
                ).lower()
                if not role_id:
                    errors.append("Role Execution Evidence row missing role_id")
                    continue
                if role_id in MAIN_AGENT_EVIDENCE_ROLES:
                    errors.append(f"Role Execution Evidence cannot use main agent as executor: {role_id}")
                    continue
                if role_id.startswith("gate-") or role_id == "teams-project-manager":
                    continue
                if result not in {"complete", "completed", "done", "passed", "success"}:
                    errors.append(f"Role Execution Evidence not complete for {role_id}")
                    continue
                if usage_source in {"", LOCAL_STUB_USAGE_SOURCE, "main_agent_local", "self_certified"}:
                    errors.append(f"Role Execution Evidence missing provider-backed usage_source for {role_id}")
                    continue
                valid_role_rows += 1
            if valid_role_rows == 0:
                errors.append("Role Execution Evidence has no completed non-gate execution role")

        errors.extend(git_publication_gate_errors(text))

        invocation_errors, invocation_warnings = validate_required_task_invocations(
            task_detail_path,
            required_agents=(
                "gate-prompt-formatter",
                "gate-task-creator",
                "teams-project-manager",
                "gate-task-evaluator",
            ),
        )
        errors.extend(invocation_errors)
        warnings.extend(invocation_warnings)

    return errors, warnings


def env_flag(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() not in {"", "0", "false", "no", "off"}


def env_int(name: str) -> int | None:
    value = os.environ.get(name, "").strip()
    if not value:
        return None
    try:
        parsed = int(value)
    except ValueError:
        return None
    return parsed if parsed >= 0 else None


def env_int_default(name: str, default: int) -> int:
    parsed = env_int(name)
    return default if parsed is None else parsed


def hook_prompt_text(hook_input: dict[str, Any]) -> str:
    return str(
        hook_input.get("prompt")
        or hook_input.get("user_prompt")
        or hook_input.get("userPrompt")
        or hook_input.get("message")
        or ""
    )


def gate_entry_prompt_is_task_like(hook_input: dict[str, Any]) -> bool:
    if truthy_input(hook_input.get("force_gate_entry") or hook_input.get("forceGateEntry")):
        return True
    prompt = hook_prompt_text(hook_input).strip()
    if not prompt:
        return False
    lowered = prompt.lower()
    if lowered.startswith("<task-notification>") or lowered.startswith("<task_notification>"):
        return False
    compact = re.sub(r"\s+", "", lowered)
    if compact in GATE_ENTRY_SKIP_PROMPTS:
        return False
    if len(compact) <= 2:
        return False
    if any(marker in lowered for marker in GATE_ENTRY_TASK_MARKERS):
        return True
    return len(prompt) >= env_int_default("ITB_GATE_ENTRY_TASK_LIKE_MIN_CHARS", 24)


PRE_GPF_STRICT_KEYWORDS = (
    "rm -rf",
    "delete",
    "destroy",
    "drop table",
    "reset --hard",
    "force push",
    "git push",
    "commit",
    "deploy",
    "release",
    "publish",
    "production",
    "security",
    "vulnerability",
    "secret",
    "token",
    "password",
    "credential",
    "auth",
    "permission",
    "policy",
    "legal",
    "contract",
    "削除",
    "消して",
    "破壊",
    "リセット",
    "コミット",
    "プッシュ",
    "デプロイ",
    "公開",
    "本番",
    "セキュリティ",
    "脆弱性",
    "秘密",
    "トークン",
    "パスワード",
    "認証",
    "認可",
    "権限",
    "ポリシー",
    "規約",
    "法務",
    "契約",
)
PRE_GPF_WRITE_KEYWORDS = (
    "fix",
    "modify",
    "edit",
    "update",
    "implement",
    "create",
    "write",
    "refactor",
    "修正",
    "変更",
    "編集",
    "更新",
    "実装",
    "作成",
    "追加",
    "反映",
    "直して",
    "書いて",
)
PRE_GPF_READ_ONLY_KEYWORDS = (
    "what",
    "why",
    "how",
    "explain",
    "describe",
    "status",
    "summarize",
    "教えて",
    "説明",
    "要約",
    "確認",
    "状態",
    "ステータス",
    "どこ",
    "なぜ",
    "何",
    "調査",
    "レビュー",
    "チェック",
)
PRE_GPF_INSPECTION_KEYWORDS = (
    "research",
    "review",
    "audit",
    "inspect",
    "analyze",
    "compare",
    "diff",
    "git diff",
    "git status",
    "file",
    "code",
    "repo",
    "log",
    "error",
    "test",
    "調査",
    "レビュー",
    "監査",
    "分析",
    "比較",
    "差分",
    "未コミット",
    "ファイル",
    "コード",
    "リポジトリ",
    "ログ",
    "エラー",
    "テスト",
    "変更点",
    "変更内容",
)
PRE_GPF_CONTEXTUAL_WRITE_KEYWORDS = (
    "update",
    "変更",
    "更新",
)
PRE_GPF_PUBLICATION_STRICT_KEYWORDS = (
    "git push",
    "commit",
    "push",
    "コミット",
    "プッシュ",
)
PRE_GPF_NEGATED_PUBLICATION_PHRASES = (
    "do not commit",
    "don't commit",
    "without commit",
    "no commit",
    "commitなし",
    "commit なし",
    "commitしない",
    "commit はしない",
    "do not push",
    "don't push",
    "without push",
    "no push",
    "pushなし",
    "push なし",
    "pushしない",
    "push はしない",
    "コミットなし",
    "コミット なし",
    "コミットしない",
    "コミットはしない",
    "コミットしません",
    "コミット不要",
    "プッシュなし",
    "プッシュ なし",
    "プッシュしない",
    "プッシュはしない",
    "プッシュしません",
    "プッシュ不要",
)


def keyword_hits(text: str, keywords: tuple[str, ...]) -> list[str]:
    return [keyword for keyword in keywords if keyword in text]


def effective_classifier_strict_hits(text: str) -> tuple[list[str], list[str]]:
    hits = keyword_hits(text, PRE_GPF_STRICT_KEYWORDS)
    if not hits or not any(phrase in text for phrase in PRE_GPF_NEGATED_PUBLICATION_PHRASES):
        return hits, []
    effective: list[str] = []
    suppressed: list[str] = []
    for hit in hits:
        if hit in PRE_GPF_PUBLICATION_STRICT_KEYWORDS:
            suppressed.append(hit)
        else:
            effective.append(hit)
    return effective, suppressed


def effective_classifier_write_hits(
    text: str,
    write_hits: list[str],
    *,
    has_read_only_signal: bool,
    inspection_hits: list[str],
) -> tuple[list[str], list[str]]:
    if not write_hits:
        return [], []
    if not has_read_only_signal or not inspection_hits:
        return write_hits, []
    effective: list[str] = []
    suppressed: list[str] = []
    for hit in write_hits:
        if hit in PRE_GPF_CONTEXTUAL_WRITE_KEYWORDS:
            suppressed.append(hit)
        else:
            effective.append(hit)
    return effective, suppressed


def prompt_looks_single_deliverable(prompt: str) -> bool:
    stripped_lines = [line.strip() for line in prompt.splitlines() if line.strip()]
    bullet_lines = [line for line in stripped_lines if line.startswith(("-", "*", "1.", "2.", "3."))]
    if len(bullet_lines) > 1:
        return False
    lowered = prompt.lower()
    multi_markers = (" and also ", " as well as ", "それと", "加えて", "さらに", "複数", "全部")
    return not any(marker in lowered for marker in multi_markers)


def normalize_classifier_workflow_mode(value: Any) -> str:
    normalized = normalized_publication_value(value)
    if normalized in {"micro", "micro_flow", "controlled_micro_task"}:
        return "controlled_micro_flow"
    if normalized in {"controlled_micro_flow", "strict_flow", "standard_flow"}:
        return normalized
    return ""


def classify_gate_entry_prompt(hook_input: dict[str, Any]) -> dict[str, Any]:
    prompt = hook_prompt_text(hook_input).strip()
    explicit_workflow = normalize_classifier_workflow_mode(
        hook_input.get("workflow_mode") or hook_input.get("workflowMode")
    )
    explicit_risk = normalized_publication_value(hook_input.get("risk_tier") or hook_input.get("riskTier"))
    if truthy_input(hook_input.get("skip_pre_gpf_classifier") or hook_input.get("skipPreGpfClassifier")):
        return {
            "schema_version": 1,
            "classifier": "pre_gpf_deterministic",
            "classifier_version": "1",
            "source": "disabled",
            "workflow_mode": explicit_workflow or "strict_flow",
            "risk_tier": explicit_risk or "normal",
            "read_only": False,
            "single_deliverable": False,
            "approval_required": True,
            "fast_path_candidate": "disabled",
            "escalation_triggers": ["classifier_disabled"],
            "reasons": ["pre-GPF classifier disabled"],
        }
    lowered = prompt.lower()
    compact = re.sub(r"\s+", "", lowered)
    strict_hits, suppressed_strict_hits = effective_classifier_strict_hits(lowered)
    raw_write_hits = keyword_hits(lowered, PRE_GPF_WRITE_KEYWORDS)
    read_hits = keyword_hits(lowered, PRE_GPF_READ_ONLY_KEYWORDS)
    inspection_hits = keyword_hits(lowered, PRE_GPF_INSPECTION_KEYWORDS)
    question_like = "?" in prompt or "？" in prompt or bool(read_hits)
    write_hits, suppressed_write_hits = effective_classifier_write_hits(
        lowered,
        raw_write_hits,
        has_read_only_signal=question_like,
        inspection_hits=inspection_hits,
    )
    single_deliverable = prompt_looks_single_deliverable(prompt)
    max_micro_chars = env_int_default("ITB_PRE_GPF_MICRO_MAX_CHARS", 140)
    bounded = len(compact) <= max_micro_chars
    read_only = question_like and not write_hits and not strict_hits
    inspection_required = read_only and bool(inspection_hits)
    reasons: list[str] = []
    if explicit_workflow or explicit_risk:
        reasons.append("hook_input_override")
    if strict_hits:
        reasons.append("strict keyword present")
    if write_hits:
        reasons.append("write/edit intent present")
    if suppressed_strict_hits:
        reasons.append("negated publication trigger ignored")
    if suppressed_write_hits:
        reasons.append("contextual write keyword treated as read-only")
    if not single_deliverable:
        reasons.append("multiple deliverables suspected")
    if not bounded:
        reasons.append(f"prompt length exceeds micro threshold {max_micro_chars}")
    if inspection_required:
        reasons.append("read-only inspection requires standard Gate flow")
    if read_only and not inspection_required and single_deliverable and bounded:
        workflow_mode = "controlled_micro_flow"
        risk_tier = "low"
        fast_path_candidate = "read_only_no_diff_single_team"
        reasons.append("bounded read-only single-deliverable prompt")
    elif read_only:
        workflow_mode = "standard_flow"
        risk_tier = "normal"
        fast_path_candidate = "not_eligible"
        reasons.append("read-only standard flow")
    else:
        workflow_mode = "strict_flow"
        risk_tier = "high" if strict_hits else "normal"
        fast_path_candidate = "not_eligible"
        if not reasons:
            reasons.append("default strict flow")
    if explicit_workflow:
        if workflow_mode == "strict_flow" and explicit_workflow != "strict_flow":
            reasons.append("non-strict workflow override ignored because strict flow is required")
        else:
            workflow_mode = explicit_workflow
    if explicit_risk:
        risk_tier = explicit_risk
    approval_required = bool(strict_hits)
    return {
        "schema_version": 1,
        "classifier": "pre_gpf_deterministic",
        "classifier_version": "1",
        "source": "hook_input_override" if explicit_workflow or explicit_risk else "deterministic_keywords",
        "workflow_mode": workflow_mode,
        "risk_tier": risk_tier,
        "read_only": read_only,
        "single_deliverable": single_deliverable,
        "bounded_prompt": bounded,
        "inspection_required": inspection_required,
        "approval_required": approval_required,
        "fast_path_candidate": fast_path_candidate,
        "escalation_triggers": strict_hits + write_hits,
        "suppressed_escalation_triggers": suppressed_strict_hits + suppressed_write_hits,
        "read_only_signals": read_hits,
        "inspection_signals": inspection_hits,
        "reasons": reasons,
    }


def micro_fast_path_disabled(hook_input: dict[str, Any]) -> bool:
    if not env_flag("ITB_MICRO_FAST_PATH", default=True):
        return True
    if truthy_input(hook_input.get("skip_micro_fast_path") or hook_input.get("skipMicroFastPath")):
        return True
    if truthy_input(hook_input.get("force_gate_entry_queue") or hook_input.get("forceGateEntryQueue")):
        return True
    if truthy_input(hook_input.get("force_gate_entry_dispatch") or hook_input.get("forceGateEntryDispatch")):
        return True
    return False


def micro_fast_path_git_status(hook_input: dict[str, Any]) -> tuple[str, str, list[str], list[str]]:
    explicit = normalized_publication_value(hook_input.get("git_diff_status") or hook_input.get("gitDiffStatus"))
    repo_root_value = hook_input.get("repo_root") or hook_input.get("repoRoot")
    cwd_value = hook_input.get("cwd") or os.getcwd()
    probe_root = Path(str(repo_root_value or cwd_value)).expanduser()
    completed = subprocess.run(
        ["git", "-C", str(probe_root), "rev-parse", "--show-toplevel"],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if completed.returncode != 0:
        if explicit and explicit not in {"no_diff", "clean", "none", "not_applicable", "no_repo"}:
            return explicit, "", [], [f"git_diff_status is not no_diff: {explicit}"]
        return "no_repo", "", [], []
    repo_root = completed.stdout.strip()
    porcelain, errors = git_status_porcelain(Path(repo_root))
    if errors:
        return "unknown", repo_root, [], errors
    lines = [line for line in porcelain.splitlines() if line.strip()]
    if lines:
        return "dirty", repo_root, lines, [f"git status is dirty with {len(lines)} entries"]
    return "no_diff", repo_root, [], []


def micro_fast_path_task_id(session_id: str, hook_input: dict[str, Any]) -> str:
    explicit = normalize_cell(hook_input.get("task_id") or hook_input.get("taskId"))
    if explicit:
        return explicit
    prompt = hook_prompt_text(hook_input)
    digest = hashlib.sha1(f"{session_id}\n{prompt}".encode("utf-8")).hexdigest()[:12]
    return f"MICRO-{safe_id(session_id)}-{digest}"


def micro_fast_path_verdict(
    *,
    runtime: str,
    session_dir: Path,
    session_id: str,
    organization_instance_id: str,
    hook_input: dict[str, Any],
    classifier: dict[str, Any],
    now: str,
) -> dict[str, Any]:
    errors: list[str] = []
    warnings: list[str] = []
    if micro_fast_path_disabled(hook_input):
        errors.append("micro fast-path disabled or forced Gate entry requested")
    if normalize_cell(classifier.get("workflow_mode")) != "controlled_micro_flow":
        errors.append("classifier workflow_mode is not controlled_micro_flow")
    if normalized_publication_value(classifier.get("risk_tier")) != "low":
        errors.append("classifier risk_tier is not low")
    if normalize_cell(classifier.get("fast_path_candidate")) != "read_only_no_diff_single_team":
        errors.append("classifier fast_path_candidate is not read_only_no_diff_single_team")
    if not bool(classifier.get("read_only")):
        errors.append("classifier did not mark prompt read_only")
    if not bool(classifier.get("single_deliverable")):
        errors.append("classifier did not mark prompt single_deliverable")
    if bool(classifier.get("approval_required")):
        errors.append("classifier requires approval")
    if hook_task_detail_path(hook_input) is not None:
        errors.append("task_detail_path present; active task must use normal Gate flow")

    git_diff_status, repo_root, git_status_lines, git_errors = micro_fast_path_git_status(hook_input)
    if git_diff_status not in {"no_diff", "no_repo"}:
        errors.append(f"git_diff_status is not no_diff: {git_diff_status}")
    errors.extend(git_errors)

    status = "block" if errors else "pass"
    task_id = micro_fast_path_task_id(session_id, hook_input)
    base_payload = {
        "schema_version": 1,
        "runtime": runtime,
        "session_id": session_id,
        "organization_instance_id": organization_instance_id,
        "task_id": task_id,
        "created_at": now,
        "status": status,
        "workflow_mode": normalize_cell(classifier.get("workflow_mode")),
        "risk_tier": normalize_cell(classifier.get("risk_tier")),
        "fast_path_candidate": normalize_cell(classifier.get("fast_path_candidate")),
        "role_provider_turns": 0 if status == "pass" else None,
        "git_diff_status": git_diff_status,
        "repo_root": repo_root,
        "git_status_lines": git_status_lines,
        "validation_errors": errors,
        "validation_warnings": warnings,
    }
    if status != "pass":
        return base_payload | {
            "result": "not_eligible",
            "next_entrypoint": "gate-prompt-formatter",
            "reason": "; ".join(errors),
        }

    tpm_payload = write_gate_command_artifact(
        session_dir=session_dir,
        task_id=task_id,
        artifact_name="tpm_completion",
        payload=base_payload
        | {
            "command": "team-completion-check",
            "completed_teams": ["gate"],
            "missing_teams": [],
            "missing_evidence": [],
            "blockers": [],
            "next_phase_allowed": True,
            "handoff_to": "gate-task-evaluator",
            "reason": "controlled micro-flow read-only/no-diff/single-team prompt",
        },
    )
    evaluation_payload = write_gate_command_artifact(
        session_dir=session_dir,
        task_id=task_id,
        artifact_name="evaluation",
        payload=base_payload
        | {
            "command": "micro-fast-path-evaluation",
            "evaluation_status": "quality_ok",
            "requirements_satisfied": True,
            "reviews_satisfied": True,
            "validation_satisfied": True,
            "human_approval_satisfied": "not_required",
            "commit_required": False,
            "push_required": False,
            "pr_required": False,
            "next_phase_allowed": True,
            "handoff_to": "finalization-check",
            "reason": "read-only prompt requires no task-owned diff publication",
        },
    )
    finalization_payload = write_gate_command_artifact(
        session_dir=session_dir,
        task_id=task_id,
        artifact_name="finalization",
        payload=base_payload
        | {
            "command": "finalization-check",
            "git_publication_closed": True,
            "vault_final_update": "not_required_read_only_fast_path",
            "task_index_synced": "not_required_read_only_fast_path",
            "kanban_synced": "not_required_read_only_fast_path",
            "completion_envelope_ready": True,
            "transport_render_ready": True,
            "next_phase_allowed": True,
            "handoff_to": "main_transport_renderer",
            "notification_class": notification_class_for_event(
                event_type="finalization_check",
                status="pass",
            ),
            "reason": "controlled micro-flow completed without role provider turns",
        },
    )
    completion_envelope = write_gate_command_artifact(
        session_dir=session_dir,
        task_id=task_id,
        artifact_name="completion_envelope",
        payload=base_payload
        | {
            "result": "micro_fast_path_complete",
            "completion_status": "ready_for_main_transport",
            "facts_preserved": True,
            "no_new_task_judgment": True,
            "worker_persona_leakage": False,
            "style_profile": "main_transport_renderer_default",
            "next_entrypoint": "main_transport_renderer",
            "notification_class": notification_class_for_event(
                event_type="micro_fast_path",
                result="done",
            ),
            "tpm_completion_artifact_path": tpm_payload["artifact_path"],
            "evaluation_artifact_path": evaluation_payload["artifact_path"],
            "finalization_artifact_path": finalization_payload["artifact_path"],
        },
    )
    return completion_envelope | {
        "tpm_completion_artifact_path": tpm_payload["artifact_path"],
        "evaluation_artifact_path": evaluation_payload["artifact_path"],
        "finalization_artifact_path": finalization_payload["artifact_path"],
    }


def gate_entry_dispatch_enabled(*, runtime: str, hook_input: dict[str, Any]) -> bool:
    if runtime != "codex":
        return False
    if not env_flag("ITB_GATE_ENTRY_DISPATCH", default=False):
        return False
    if truthy_input(hook_input.get("skip_gate_entry_dispatch") or hook_input.get("skipGateEntryDispatch")):
        return False
    if truthy_input(hook_input.get("force_gate_entry_dispatch") or hook_input.get("forceGateEntryDispatch")):
        return bool(hook_prompt_text(hook_input).strip())
    return gate_entry_prompt_is_task_like(hook_input)


def gate_entry_queue_enabled(*, runtime: str, hook_input: dict[str, Any]) -> bool:
    if runtime not in {"codex", "claude"}:
        return False
    if gate_entry_dispatch_enabled(runtime=runtime, hook_input=hook_input):
        return False
    if not env_flag("ITB_GATE_ENTRY_QUEUE", default=True):
        return False
    if truthy_input(hook_input.get("skip_gate_entry_queue") or hook_input.get("skipGateEntryQueue")):
        return False
    if truthy_input(hook_input.get("force_gate_entry_queue") or hook_input.get("forceGateEntryQueue")):
        return bool(hook_prompt_text(hook_input).strip())
    return gate_entry_prompt_is_task_like(hook_input)


def gate_entry_auto_gtc_enabled(hook_input: dict[str, Any]) -> bool:
    if truthy_input(hook_input.get("skip_gate_entry_auto_gtc") or hook_input.get("skipGateEntryAutoGtc")):
        return False
    return env_flag("ITB_GATE_ENTRY_AUTO_GTC", default=True)


def gate_entry_codex_exec_enabled(row: dict[str, Any] | None, hook_input: dict[str, Any]) -> bool:
    if truthy_input(hook_input.get("skip_gate_entry_codex_exec") or hook_input.get("skipGateEntryCodexExec")):
        return False
    forced = truthy_input(hook_input.get("force_gate_entry_codex_exec") or hook_input.get("forceGateEntryCodexExec"))
    if not row:
        return False
    if agent_runtime(row) != ("codex_exec", "codex"):
        return forced
    return forced or env_flag("ITB_GATE_ENTRY_CODEX_EXEC", default=True)


def gate_entry_roster_row(session_dir: Path) -> dict[str, Any] | None:
    roster_path = session_dir / "roster.json"
    if not roster_path.exists():
        return None
    roster = read_json(roster_path)
    if not isinstance(roster, list):
        return None
    for row in roster:
        if isinstance(row, dict) and row.get("agent_id") == GATE_ENTRY_AGENT_ID:
            return row
    return None


def gate_entry_task_id(session_id: str, hook_input: dict[str, Any]) -> str:
    explicit = normalize_cell(
        hook_input.get("task_id")
        or hook_input.get("taskId")
        or hook_input.get("entry_task_id")
        or hook_input.get("entryTaskId")
    )
    if explicit:
        return explicit
    entry_id = normalize_cell(
        hook_input.get("entry_id")
        or hook_input.get("entryId")
        or hook_input.get("request_id")
        or hook_input.get("requestId")
        or f"entry-{uuid.uuid4().hex}"
    )
    return f"ENTRY-{safe_id(session_id)}-{safe_id(entry_id)}"


def gate_entry_message_id(hook_input: dict[str, Any]) -> str:
    explicit = normalize_cell(hook_input.get("message_id") or hook_input.get("messageId"))
    return explicit or f"msg-{uuid.uuid4().hex}"


def gate_entry_report_id(hook_input: dict[str, Any]) -> str:
    explicit = normalize_cell(hook_input.get("report_id") or hook_input.get("reportId"))
    return explicit or f"rep-{uuid.uuid4().hex}"


def format_gate_entry_queue_context(queue_output: dict[str, Any]) -> tuple[str, str]:
    queue = queue_output.get("roleQueue", {})
    result = normalize_cell(queue.get("result") or queue_output.get("decision") or "")
    reason = normalize_cell(queue_output.get("reason"))
    context = f"""## Gate Entry Queue

| Field | Value |
|---|---|
| result | `{result}` |
| role_id | `{normalize_cell(queue.get('role_id'))}` |
| task_id | `{normalize_cell(queue.get('task_id'))}` |
| message_id | `{normalize_cell(queue.get('message_id'))}` |
| queue_root | `{normalize_cell(queue.get('queue_root'))}` |
| inbox_path | `{normalize_cell(queue.get('inbox_path'))}` |
| task_payload_path | `{normalize_cell(queue.get('task_payload_path'))}` |
| nudge_result | `{normalize_cell((queue.get('nudge') or {}).get('result') if isinstance(queue.get('nudge'), dict) else '')}` |

The human prompt has been normalized into the `gate-prompt-formatter` role queue.
Do not perform the requested task directly; wait for the Gate workflow artifacts.
"""
    if reason:
        context += f"\nQueue error: {reason}\n"
    return context, result


def build_gate_entry_dispatch_prompt(
    *,
    user_prompt: str,
    task_detail_path: str,
    flow_phase: str,
    task_context_source: str,
    classifier: dict[str, Any] | None = None,
) -> str:
    task_context = task_detail_path or "none"
    classifier = classifier if isinstance(classifier, dict) else {}
    workflow_mode = normalize_cell(classifier.get("workflow_mode")) or "strict_flow"
    risk_tier = normalize_cell(classifier.get("risk_tier")) or "normal"
    fast_path_candidate = normalize_cell(classifier.get("fast_path_candidate")) or "not_eligible"
    return f"""Create a compact Gate Intake Envelope for gate-task-creator.
Speed matters: think briefly, do not browse, do not read files, do not write files, do not plan, and do not perform the requested task.
Use the deterministic pre-GPF classifier below as the default. You may escalate to `strict_flow`; do not downgrade a strict classifier to micro.
Return only this YAML shape, then the required done marker.

envelope_version: "2"
source_type: human_prompt
original_request: |
  [preserve the human prompt exactly]
intent_summary: "[one concise sentence]"
desired_outcome:
  deliverables: ["..."]
  done_criteria: ["..."]
scope:
  in: ["..."]
  out: ["..."]
approval_required: false
approval_reason: "none"
workflow_mode: {workflow_mode}
risk_tier: {risk_tier}
task_units:
  - unit_id: unit-1
    title: "..."
    main_team: gate
    assignee: gate-task-creator
    priority: P0
    done_criteria: ["..."]
routing_hint: "teams-project-manager"
review_requirements: ["domain_review", "independent_review"]
vault_update_targets: ["Agents-Vault"]
missing_information: []
risks: []
handoff_notes:
  gate-task-creator: "create task detail and hand off to teams-project-manager"
improvement_log:
  - "none"

Original Request:
{user_prompt.strip()}

Runtime Context:
- runtime: codex
- task_detail_path: {task_context}
- task_context_source: {task_context_source or "none"}
- flow_phase: {flow_phase}
- classifier_workflow_mode: {workflow_mode}
- classifier_risk_tier: {risk_tier}
- classifier_fast_path_candidate: {fast_path_candidate}
"""


def format_gate_entry_dispatch_context(dispatch_output: dict[str, Any]) -> tuple[str, str]:
    dispatch = dispatch_output.get("agentDispatch", {})
    result = normalize_cell(dispatch.get("result") or dispatch_output.get("decision") or "")
    request_id = normalize_cell(dispatch.get("request_id"))
    effective_model = normalize_cell(dispatch.get("effective_model"))
    usage_source = normalize_cell(dispatch.get("usage_source"))
    response = str(dispatch.get("response") or "").strip()
    reason = normalize_cell(dispatch_output.get("reason") or dispatch.get("error"))
    response_block = response[:6000]
    if response and len(response) > len(response_block):
        response_block += "\n\n[truncated by ITB preflight]"

    context = f"""## Gate Entry Dispatch

| Field | Value |
|---|---|
| agent | `{GATE_ENTRY_AGENT_ID}` |
| result | `{result or 'unknown'}` |
| request_id | `{request_id}` |
| effective_model | `{effective_model}` |
| usage_source | `{usage_source}` |
| reason | `{reason}` |

Gate Intake Envelope from `{GATE_ENTRY_AGENT_ID}`:

```markdown
{response_block or '(no response captured)'}
```

`gate-task-creator` must consume this envelope before task work starts.
"""
    return context, result


def validate_gate_entry_response(response: str) -> list[str]:
    stripped = response.strip()
    if not stripped:
        return ["Gate entry dispatch returned no response text"]

    yaml_markers = (
        "envelope_version",
        "source_type",
        "original_request",
        "intent_summary",
        "desired_outcome",
        "done_criteria",
        "approval_required",
        "workflow_mode",
        "task_units",
        "routing_hint",
        "review_requirements",
        "vault_update_targets",
        "handoff_notes",
    )
    normalized = stripped.lower()
    yaml_missing = [marker for marker in yaml_markers if marker not in normalized]
    if not yaml_missing:
        return []

    required_markers = (
        "# normalized request",
        "## original request",
        "## intent",
        "## desired outcome",
        "## scope",
        "## task units",
        "## handoff notes",
    )
    missing = [marker for marker in required_markers if marker not in normalized]
    if not missing:
        return []

    compact_markers = (
        "deliverables",
        "done_criteria",
        "requires_human_approval",
        "handoff notes",
        "gate-task-creator",
    )
    compact_missing = [marker for marker in compact_markers if marker not in normalized]
    if not compact_missing:
        return []

    return [
        "Gate entry dispatch response is missing required Gate Intake Envelope sections: "
        + ", ".join(missing)
        + "; compact fields missing: "
        + ", ".join(compact_missing)
        + "; yaml fields missing: "
        + ", ".join(yaml_missing)
    ]


def yaml_block(value: str, indent: str = "  ") -> str:
    lines = (value or "").splitlines() or [""]
    return "\n".join(f"{indent}{line}" for line in lines)


def compact_title(value: str, fallback: str = "Gate intake") -> str:
    cleaned = " ".join((value or "").split())
    if not cleaned:
        return fallback
    return cleaned[:80]


def gate_entry_response_is_repairable(response: str) -> bool:
    normalized = response.lower()
    return any(marker in normalized for marker in ("routing_hint", "handoff_notes", "main_team", "assignee", "done_criteria"))


def normalize_gate_entry_response(response: str, user_prompt: str) -> tuple[str, bool, list[str]]:
    if not validate_gate_entry_response(response):
        return response.strip(), False, []
    if not gate_entry_response_is_repairable(response):
        return response.strip(), False, ["provider response is not repairable as Gate Intake Envelope"]
    title = compact_title(user_prompt, fallback="Gate intake")
    repaired = f"""envelope_version: "2"
source_type: human_prompt
original_request: |
{yaml_block(user_prompt)}
intent_summary: "{title}"
desired_outcome:
  deliverables: ["Task Detail and Project Manager Handoff"]
  done_criteria: ["gate-task-creator can create the task without reinterpreting the human prompt"]
scope:
  in: ["Normalize and create Gate task artifacts"]
  out: ["Perform specialist task work in gate-prompt-formatter"]
approval_required: false
approval_reason: "none"
workflow_mode: strict_flow
risk_tier: normal
task_units:
  - unit_id: unit-1
    title: "{title}"
    main_team: gate
    assignee: gate-task-creator
    priority: P0
    done_criteria: ["Task Detail is created and handed off to teams-project-manager"]
routing_hint: "teams-project-manager"
review_requirements: ["domain_review", "independent_review"]
vault_update_targets: ["Agents-Vault"]
missing_information: []
risks: []
handoff_notes:
  gate-task-creator: "Use original_request as source of truth; provider returned a partial envelope fragment."
improvement_log:
  - "adapter_repaired_partial_gate_intake_envelope"
provider_fragment: |
{yaml_block(response)}
"""
    repair_errors = validate_gate_entry_response(repaired)
    return repaired.strip(), True, repair_errors


GTC_ENVELOPE_REQUIRED_FIELDS = (
    "envelope_version",
    "source_type",
    "original_request",
    "intent_summary",
    "desired_outcome",
    "scope",
    "approval_required",
    "workflow_mode",
    "task_units",
    "routing_hint",
    "review_requirements",
    "vault_update_targets",
)


def quote_yaml_block_scalars(raw: str) -> str:
    lines = raw.splitlines()
    rendered: list[str] = []
    index = 0
    block_re = re.compile(r"^(\s*)([^:#][^:]*):\s*\|\s*$")
    while index < len(lines):
        line = lines[index]
        match = block_re.match(line)
        if not match:
            rendered.append(line)
            index += 1
            continue
        base_indent = len(match.group(1))
        key = match.group(2).strip()
        index += 1
        block_lines: list[str] = []
        while index < len(lines):
            child = lines[index]
            child_indent = len(child) - len(child.lstrip(" "))
            if child.strip() and child_indent <= base_indent:
                break
            strip_width = min(len(child), base_indent + 2)
            block_lines.append(child[strip_width:] if len(child) >= strip_width else "")
            index += 1
        rendered.append(f"{' ' * base_indent}{key}: {json.dumps(chr(10).join(block_lines), ensure_ascii=False)}")
    return "\n".join(rendered) + ("\n" if raw.endswith("\n") else "")


def expand_inline_sequence_mappings(raw: str) -> str:
    rendered: list[str] = []
    item_re = re.compile(r"^(\s*)-\s+([^:#][^:]*):\s*(.*)$")
    for line in raw.splitlines():
        match = item_re.match(line)
        if not match:
            rendered.append(line)
            continue
        indent, key, value = match.groups()
        rendered.append(f"{indent}- ")
        rendered.append(f"{indent}  {key.strip()}: {value.strip()}")
    return "\n".join(rendered) + ("\n" if raw.endswith("\n") else "")


def normalize_gate_intake_yaml_for_basic_parser(raw: str) -> str:
    return expand_inline_sequence_mappings(quote_yaml_block_scalars(raw))


def parse_gate_intake_envelope_text(raw: str) -> tuple[dict[str, Any], list[str]]:
    text = raw.strip()
    if not text:
        return {}, ["Gate Intake Envelope is empty"]
    try:
        if _pyyaml is not None:
            parsed = _pyyaml.safe_load(text)
        else:
            parsed = parse_basic_yaml_config(
                normalize_gate_intake_yaml_for_basic_parser(text),
                Path("<gate-intake-envelope>"),
            )
    except Exception as exc:
        return {}, [f"Gate Intake Envelope parse failed: {exc}"]
    if not isinstance(parsed, dict):
        return {}, ["Gate Intake Envelope must parse to an object"]
    return parsed, []


def gate_intake_envelope_from_input(hook_input: dict[str, Any]) -> tuple[dict[str, Any], str, list[str]]:
    raw_envelope = hook_input.get("gate_intake_envelope") or hook_input.get("gateIntakeEnvelope")
    if isinstance(raw_envelope, dict):
        return dict(raw_envelope), "hook_input.gate_intake_envelope", []
    if isinstance(raw_envelope, str) and raw_envelope.strip():
        parsed, errors = parse_gate_intake_envelope_text(raw_envelope)
        return parsed, "hook_input.gate_intake_envelope", errors

    report_path_value = (
        hook_input.get("gate_intake_report_path")
        or hook_input.get("gateIntakeReportPath")
        or hook_input.get("source_report_path")
        or hook_input.get("sourceReportPath")
    )
    if report_path_value:
        report_path = Path(str(report_path_value)).expanduser()
        if not report_path.exists():
            return {}, str(report_path), [f"Gate Intake Envelope report path does not exist: {report_path}"]
        report = read_json_yaml(report_path)
        if not isinstance(report, dict):
            return {}, str(report_path), ["Gate Intake Envelope report must parse to an object"]
        report_envelope = report.get("gate_intake_envelope") or report.get("gateIntakeEnvelope")
        if isinstance(report_envelope, dict):
            return dict(report_envelope), str(report_path), []
        if isinstance(report_envelope, str) and report_envelope.strip():
            parsed, errors = parse_gate_intake_envelope_text(report_envelope)
            return parsed, str(report_path), errors
        return {}, str(report_path), ["Gate Intake Envelope report has no gate_intake_envelope field"]

    return {}, "", ["gtc-scaffold requires gate_intake_envelope or gate_intake_report_path"]


def list_field_values(value: Any) -> list[str]:
    if isinstance(value, list):
        return [normalize_cell(item) for item in value if normalize_cell(item)]
    if isinstance(value, dict):
        return [f"{key}: {item}" for key, item in value.items() if normalize_cell(item)]
    return [normalize_cell(item) for item in str(value or "").replace("\n", ",").split(",") if normalize_cell(item)]


def nested_list_field(envelope: dict[str, Any], key: str, nested_key: str) -> list[str]:
    value = envelope.get(key)
    if isinstance(value, dict):
        return list_field_values(value.get(nested_key))
    return []


def first_task_unit(envelope: dict[str, Any]) -> dict[str, Any]:
    units = envelope.get("task_units")
    if isinstance(units, list):
        for unit in units:
            if isinstance(unit, dict):
                return dict(unit)
    return {}


def gtc_missing_envelope_fields(envelope: dict[str, Any]) -> list[str]:
    missing = [field for field in GTC_ENVELOPE_REQUIRED_FIELDS if field not in envelope or envelope.get(field) in ("", None, [])]
    desired = envelope.get("desired_outcome")
    if not isinstance(desired, dict):
        if "desired_outcome" not in missing:
            missing.append("desired_outcome")
    else:
        for field in ("deliverables", "done_criteria"):
            if not list_field_values(desired.get(field)):
                missing.append(f"desired_outcome.{field}")
    scope = envelope.get("scope")
    if not isinstance(scope, dict):
        if "scope" not in missing:
            missing.append("scope")
    else:
        for field in ("in", "out"):
            if not list_field_values(scope.get(field)):
                missing.append(f"scope.{field}")
    return list(dict.fromkeys(missing))


def gtc_initial_status(envelope: dict[str, Any], missing_fields: list[str], hook_input: dict[str, Any]) -> str:
    explicit = normalized_publication_value(hook_input.get("status") or hook_input.get("initial_status") or hook_input.get("initialStatus"))
    if explicit in {"ready", "waiting_human", "blocked", "triage", "in_progress"}:
        return explicit
    if truthy_input(envelope.get("approval_required")):
        return "waiting_human"
    if missing_fields or list_field_values(envelope.get("missing_information")):
        return "triage"
    return "ready"


def safe_task_slug(value: str) -> str:
    lowered = value.lower()
    tokens = re.findall(r"[a-z0-9]+", lowered)
    slug = "-".join(tokens[:8]).strip("-")
    return slug or "task"


def markdown_table_cell(value: Any) -> str:
    return normalize_cell(value).replace("|", "\\|").replace("\n", "<br>")


def markdown_bullets(values: list[str], fallback: str = "pending") -> str:
    items = [item for item in values if normalize_cell(item)]
    if not items:
        items = [fallback]
    return "\n".join(f"- {item}" for item in items)


def markdown_inline_list(values: list[str]) -> str:
    return ", ".join(values) if values else "none"


def next_task_id_from_vault(vault_root: Path) -> str:
    max_id = 0
    index_path = vault_root / "00-Inbox&Tasks/Task-Index.md"
    sources: list[str] = []
    if index_path.exists():
        sources.append(index_path.read_text(encoding="utf-8"))
    projects_root = vault_root / "01-Projects"
    if projects_root.exists():
        for path in projects_root.glob("**/TSK-*"):
            sources.append(path.name)
    for source in sources:
        for match in re.finditer(r"TSK-(\d{4,})", source):
            max_id = max(max_id, int(match.group(1)))
    return f"TSK-{max(max_id + 1, 1001):04d}"


def task_detail_wikilink(vault_root: Path, task_detail_path: Path) -> str:
    try:
        relative = task_detail_path.resolve().relative_to(vault_root.resolve())
    except ValueError:
        relative = task_detail_path
    text = str(relative).replace("\\", "/")
    if text.endswith(".md"):
        text = text[:-3]
    return text


def gtc_kanban_section_for_status(status: str) -> str:
    return {
        "ready": "Ready",
        "triage": "Inbox",
        "inbox": "Inbox",
        "in_progress": "In Progress",
        "waiting_human": "Waiting Human",
        "blocked": "Waiting Human",
        "done": "Done",
    }.get(normalized_publication_value(status), "Inbox")


def append_unique_markdown_line(path: Path, line: str) -> bool:
    if path.exists():
        text = path.read_text(encoding="utf-8")
    else:
        text = ""
    if line in text:
        return False
    if text and not text.endswith("\n"):
        text += "\n"
    text += line + "\n"
    atomic_write_text(path, text)
    return True


def ensure_kanban_entry(path: Path, section: str, line: str) -> bool:
    if path.exists():
        text = path.read_text(encoding="utf-8")
    else:
        text = "---\ntype: kanban\n---\n\n# Kanban\n\n"
    if line in text:
        return False
    heading = f"## {section}"
    if heading not in text:
        if text and not text.endswith("\n"):
            text += "\n"
        text += f"\n{heading}\n\n{line}\n"
        atomic_write_text(path, text)
        return True
    start = text.index(heading)
    next_heading = text.find("\n## ", start + len(heading))
    insert_at = len(text) if next_heading < 0 else next_heading
    prefix = text[:insert_at].rstrip() + "\n\n"
    suffix = text[insert_at:]
    atomic_write_text(path, prefix + line + "\n" + suffix)
    return True


def gtc_routing_director_for_team(main_team: str, assignee: str = "") -> str:
    normalized_team = normalized_publication_value(main_team)
    if normalized_team in VALID_ROUTING_DIRECTORS:
        return normalized_team
    mapped = TEAM_ROUTING_DIRECTOR_BY_TEAM.get(normalized_team)
    if mapped:
        return mapped
    normalized_assignee = normalized_publication_value(assignee)
    if normalized_assignee in VALID_ROUTING_DIRECTORS:
        return normalized_assignee
    return "teams-project-manager"


def task_detail_report_link(vault_root: Path, report_path: Path | None) -> str:
    if report_path is None:
        return "none"
    try:
        relative = report_path.resolve().relative_to(vault_root.resolve())
    except ValueError:
        return str(report_path)
    text = str(relative).replace("\\", "/")
    if text.endswith(".md"):
        text = text[:-3]
    return f"[[{text}]]"


def task_detail_thin_section_text(
    *,
    section: str,
    status: str,
    summary: str,
    report_path: Path | None,
    report_sha256: str,
    report_link: str,
    updated_at: str,
    owner_role: str,
) -> str:
    return f"""## {section}

| Field | Value |
|---|---|
| Status | {markdown_table_cell(status)} |
| Summary | {markdown_table_cell(summary or "see report")} |
| Report | {markdown_table_cell(report_link)} |
| Report Path | {markdown_table_cell(str(report_path) if report_path else "none")} |
| Report SHA256 | {markdown_table_cell(report_sha256 or "missing")} |
| Updated At | {markdown_table_cell(updated_at)} |
| Owner Role | {markdown_table_cell(owner_role or "unknown")} |
"""


def queue_context_task_detail_path(
    *,
    session_dir: Path,
    queue_root: Path,
    message: dict[str, Any],
    hook_input: dict[str, Any] | None = None,
) -> tuple[Path | None, str]:
    hook_input = hook_input or {}
    payload = message.get("payload") if isinstance(message.get("payload"), dict) else {}
    auto_context = payload.get("auto_handoff_context") if isinstance(payload.get("auto_handoff_context"), dict) else {}
    sources = (
        ("payload.auto_handoff_context", auto_context),
        ("payload", payload),
        ("message", message),
    )
    for source_name, source in sources:
        for key in ("task_detail_path", "taskDetailPath", "task_path", "taskPath", "context_ref", "contextRef"):
            value = source.get(key)
            if not normalize_cell(value):
                continue
            path = Path(str(value)).expanduser()
            if not path.is_absolute():
                queue_candidate = queue_root / path
                session_candidate = session_dir / path
                if queue_candidate.exists():
                    path = queue_candidate
                elif session_candidate.exists():
                    path = session_candidate
            return path, f"{source_name}.{key}"

    active_task, _active_errors, _active_warnings = load_active_task(session_dir)
    if active_task:
        active_path = active_task_detail_path(active_task)
        if active_path is not None:
            return active_path, "active-task.json"
    task_detail_path = hook_task_detail_path(hook_input)
    if task_detail_path is not None:
        return task_detail_path, "hook_input"
    return None, "missing"


def report_section_value(report: dict[str, Any], section: dict[str, Any], *keys: str) -> str:
    return dict_table_value(section, *keys) or dict_table_value(report, *keys)


def required_teams_from_task_detail(task_detail_text: str, completion_section: dict[str, str]) -> list[str]:
    routing = key_value_table(markdown_section(task_detail_text, "Team Routing Decision"))
    required_value = table_value(completion_section, "Required Teams", "required_teams")
    if not required_value:
        required_value = ", ".join(
            item
            for item in (
                table_value(routing, "Main Team", "main_team"),
                table_value(routing, "Supporting Teams", "supporting_teams"),
                table_value(routing, "Review Evidence Teams", "review_evidence_teams"),
            )
            if item
        )
    return gate_list_values(required_value)


def team_completion_check_section_text(
    *,
    completion_status: str,
    required_teams: list[str],
    completed_teams: list[str],
    report_ref: str,
    report_path: str,
    report_sha256: str,
    updated_at: str,
) -> str:
    return f"""## Team Completion Check

| Field | Value |
|---|---|
| Completion Status | {markdown_table_cell(completion_status)} |
| Required Teams | {markdown_table_cell(markdown_inline_list(required_teams) if required_teams else "")} |
| Completed Teams | {markdown_table_cell(markdown_inline_list(completed_teams) if completed_teams else "")} |
| Missing Teams |  |
| All Director Reports Complete | true |
| Handoff To | gate-task-evaluator |
| Source Report | {markdown_table_cell(report_ref or report_path or "unknown")} |
| Report Path | {markdown_table_cell(report_path or "unknown")} |
| Report SHA256 | {markdown_table_cell(report_sha256 or "missing")} |
| Updated At | {markdown_table_cell(updated_at)} |
| Owner Role | teams-project-manager |
"""


def maybe_update_tpm_team_completion_check(
    *,
    runtime: str,
    session_dir: Path,
    queue_root: Path,
    role_id: str,
    message: dict[str, Any],
    report: dict[str, Any],
    finalized: dict[str, Any],
    hook_input: dict[str, Any] | None,
    now: str,
    dry_run: bool = False,
) -> dict[str, Any]:
    if role_id != "teams-project-manager":
        return {"result": "skipped_not_tpm"}
    if not isinstance(report, dict):
        return {"result": "skipped_missing_report"}

    completion_report = report_dict_section(
        report,
        "team_completion_check",
        "teamCompletionCheck",
        "Team Completion Check",
        "completion_assessment",
        "Completion Assessment",
    )
    completion_status = normalized_publication_value(
        report_section_value(
            report,
            completion_report,
            "Completion Status",
            "completion_status",
            "Assessment Status",
            "assessment_status",
            "result",
        )
    )
    if completion_status != "ready_for_evaluation":
        return {"result": "skipped_result_mismatch", "completion_status": completion_status}

    task_detail_path, path_source = queue_context_task_detail_path(
        session_dir=session_dir,
        queue_root=queue_root,
        message=message,
        hook_input=hook_input,
    )
    if task_detail_path is None:
        return {"result": "blocked_missing_task_detail_path", "path_source": path_source}
    if not task_detail_path.exists():
        return {
            "result": "blocked_task_detail_missing",
            "task_detail_path": str(task_detail_path),
            "path_source": path_source,
        }

    try:
        task_detail_text = task_detail_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return {
            "result": "blocked_task_detail_unreadable",
            "task_detail_path": str(task_detail_path),
            "error": str(exc),
        }

    existing_completion = key_value_table(markdown_section(task_detail_text, "Team Completion Check"))
    expected_task_id = normalize_cell(message.get("task_id") or finalized.get("task_id"))
    actual_task_id = task_detail_compact_task_id(
        hook_input={},
        task_detail_path=task_detail_path,
        task_detail_text=task_detail_text,
    )
    if expected_task_id and actual_task_id and expected_task_id != actual_task_id:
        return {
            "result": "blocked_task_detail_task_id_mismatch",
            "task_id": expected_task_id,
            "task_detail_task_id": actual_task_id,
            "task_detail_path": str(task_detail_path),
            "path_source": path_source,
        }
    existing_schema_errors = validate_gate_output_section_schema("Team Completion Check", existing_completion)
    if existing_completion and not existing_schema_errors:
        return {
            "result": "skipped_already_ready",
            "task_detail_path": str(task_detail_path),
            "path_source": path_source,
        }

    all_complete_value = report_section_value(
        report,
        completion_report,
        "All Director Reports Complete",
        "all_director_reports_complete",
        "All Team Tasks Done",
        "all_team_tasks_done",
    )
    if not all_complete_value:
        return {
            "result": "blocked_missing_director_completion_evidence",
            "reason": "TPM ready_for_evaluation report must include All Director Reports Complete: true before updating Task Detail",
            "task_detail_path": str(task_detail_path),
            "path_source": path_source,
        }
    if not truthy_status(all_complete_value):
        return {"result": "skipped_director_reports_incomplete", "all_director_reports_complete": all_complete_value}

    required_teams = gate_list_values(
        report_section_value(report, completion_report, "Required Teams", "required_teams")
    ) or required_teams_from_task_detail(task_detail_text, existing_completion)
    completed_teams = gate_list_values(
        report_section_value(report, completion_report, "Completed Teams", "completed_teams")
    )
    missing_completed_teams = [team for team in required_teams if team not in completed_teams]
    if required_teams and missing_completed_teams:
        return {
            "result": "blocked_completed_teams_incomplete",
            "required_teams": required_teams,
            "completed_teams": completed_teams,
            "missing_teams": missing_completed_teams,
            "task_detail_path": str(task_detail_path),
            "path_source": path_source,
        }
    if not required_teams:
        return {
            "result": "blocked_required_teams_missing",
            "task_detail_path": str(task_detail_path),
            "path_source": path_source,
        }

    integrity = finalized.get("report_integrity") if isinstance(finalized.get("report_integrity"), dict) else {}
    report_path = normalize_cell(finalized.get("report_path"))
    report_ref = normalize_cell(finalized.get("report_ref"))
    rendered_section = team_completion_check_section_text(
        completion_status="ready_for_evaluation",
        required_teams=required_teams,
        completed_teams=completed_teams,
        report_ref=report_ref,
        report_path=report_path,
        report_sha256=normalize_cell(integrity.get("sha256")),
        updated_at=now,
    )
    updated_text = replace_markdown_section(task_detail_text, "Team Completion Check", rendered_section)
    line_errors, line_warnings = task_detail_line_lint(updated_text, "post_routing")
    result = "dry_run" if dry_run else "updated"
    if line_errors:
        result = "blocked_line_lint"
    if not dry_run and not line_errors:
        atomic_write_text(task_detail_path, updated_text)
    validation_errors: list[str] = []
    validation_warnings: list[str] = []
    if not dry_run and not line_errors:
        validation_errors, validation_warnings = validate_task_flow_artifact(task_detail_path, "post_routing")
        if validation_errors:
            result = "updated_with_validation_errors"

    event = {
        "ts": now,
        "runtime": runtime,
        "event_type": "team_completion_check_update",
        "role_id": role_id,
        "task_id": normalize_cell(message.get("task_id")),
        "message_id": normalize_cell(message.get("message_id")),
        "result": result,
        "task_detail_path": str(task_detail_path),
        "path_source": path_source,
        "required_teams": required_teams,
        "completed_teams": completed_teams,
        "report_ref": report_ref,
        "dry_run": dry_run,
        "validation_errors": validation_errors or line_errors,
        "validation_warnings": validation_warnings or line_warnings,
    }
    if not dry_run:
        append_jsonl_atomic(session_dir / "queue-events.jsonl", event)
    return event


def task_detail_append_output(*, runtime: str, state_root: Path, hook_input: dict[str, Any]) -> dict[str, Any]:
    session_id = current_session_id(state_root, hook_input) or "unknown-session"
    session_dir = state_root / safe_id(session_id)
    session_dir.mkdir(parents=True, exist_ok=True)
    now = current_timestamp()
    dry_run = truthy_input(hook_input.get("dry_run") or hook_input.get("dryRun"))
    auto_vault_final_update = truthy_input(
        hook_input.get("auto_vault_final_update")
        or hook_input.get("autoVaultFinalUpdate")
        or hook_input.get("run_vault_final_update")
        or hook_input.get("runVaultFinalUpdate")
    )
    task_detail_path = hook_task_detail_path(hook_input)
    section = normalize_cell(hook_input.get("section") or hook_input.get("section_name") or hook_input.get("sectionName"))
    status = normalized_publication_value(hook_input.get("status") or hook_input.get("section_status") or hook_input.get("sectionStatus"))
    summary = normalize_cell(hook_input.get("summary") or hook_input.get("section_summary") or hook_input.get("sectionSummary"))
    owner_role = normalize_cell(hook_input.get("owner_role") or hook_input.get("role_id") or hook_input.get("roleId"))
    vault_root = Path(str(hook_input.get("vault_root") or hook_input.get("vaultRoot") or AGENTS_VAULT_ROOT)).expanduser()
    report_path_value = hook_input.get("report_path") or hook_input.get("reportPath")
    report_path = Path(str(report_path_value)).expanduser() if report_path_value else None
    report_sha256 = normalize_cell(hook_input.get("report_sha256") or hook_input.get("reportSha256"))
    line_cap_phase = normalize_flow_phase(
        hook_input.get("line_cap_phase") or hook_input.get("lineCapPhase") or hook_input.get("flow_phase") or "pre_execution"
    )
    errors: list[str] = []
    warnings: list[str] = []

    if task_detail_path is None:
        errors.append("task-detail-append requires task_detail_path")
    elif not task_detail_path.exists():
        errors.append(f"task_detail_path does not exist: {task_detail_path}")
    if not section:
        errors.append("task-detail-append requires section")
    elif "\n" in section or section.startswith("#"):
        errors.append("task-detail-append section must be a plain second-level heading name")
    if not status:
        errors.append("task-detail-append requires status")
    elif not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", status):
        errors.append(f"task-detail-append status is not a compact enum: {status}")
    if report_path is None:
        errors.append("task-detail-append requires report_path")
    elif report_path.exists():
        computed_sha256 = file_sha256_if_exists(report_path)
        if report_sha256 and report_sha256 != computed_sha256:
            errors.append("report_sha256 does not match report_path")
        report_sha256 = computed_sha256
    elif not report_sha256:
        errors.append(f"report_path does not exist and report_sha256 was not supplied: {report_path}")

    if errors:
        payload = {
            "schema_version": 1,
            "result": "blocked",
            "runtime": runtime,
            "session_id": session_id,
            "task_detail_path": str(task_detail_path) if task_detail_path else "",
            "section": section,
            "status": status,
            "report_path": str(report_path) if report_path else "",
            "report_sha256": report_sha256,
            "validation_errors": errors,
            "validation_warnings": warnings,
            "dry_run": dry_run,
        }
        return {"decision": "block", "reason": "; ".join(errors), "taskDetailAppend": payload}

    assert task_detail_path is not None
    text = task_detail_path.read_text(encoding="utf-8")
    report_link = task_detail_report_link(vault_root, report_path)
    rendered_section = task_detail_thin_section_text(
        section=section,
        status=status,
        summary=summary,
        report_path=report_path,
        report_sha256=report_sha256,
        report_link=report_link,
        updated_at=now,
        owner_role=owner_role,
    )
    updated_text = replace_markdown_section(text, section, rendered_section)
    line_errors, line_warnings = task_detail_line_lint(updated_text, line_cap_phase, hook_input)
    warnings.extend(line_warnings)
    publication_gate_errors: list[str] = []
    if auto_vault_final_update and normalized_key(section) == "git_publication_result":
        publication_gate_errors = git_publication_gate_errors(updated_text)
        errors.extend(publication_gate_errors)
    line_count = len(updated_text.splitlines())
    line_cap = task_detail_line_cap(hook_input)
    result = "dry_run" if dry_run else "updated"
    if line_errors:
        result = "blocked"
        errors.extend(line_errors)

    payload = {
        "schema_version": 1,
        "result": result,
        "runtime": runtime,
        "session_id": session_id,
        "task_detail_path": str(task_detail_path),
        "section": section,
        "status": status,
        "summary": summary,
        "report_path": str(report_path) if report_path else "",
        "report_sha256": report_sha256,
        "report_link": report_link,
        "line_count": line_count,
        "line_cap": line_cap,
        "line_cap_phase": line_cap_phase,
        "validation_errors": errors,
        "validation_warnings": warnings,
        "auto_vault_final_update": auto_vault_final_update,
        "publication_gate_errors": publication_gate_errors,
        "dry_run": dry_run,
        "preview": rendered_section if dry_run else "",
    }
    task_id = task_detail_compact_task_id(hook_input=hook_input, task_detail_path=task_detail_path, task_detail_text=updated_text)
    payload["task_id"] = task_id

    if errors:
        return {"decision": "block", "reason": "; ".join(errors), "taskDetailAppend": payload}
    if not dry_run:
        atomic_write_text(task_detail_path, updated_text)
        vault_final_update_result: dict[str, Any] = {}
        if auto_vault_final_update and normalized_key(section) == "git_publication_result":
            vault_final_update_result = vault_final_update_output(
                runtime=runtime,
                state_root=state_root,
                hook_input={
                    "session_id": session_id,
                    "task_id": task_id,
                    "task_detail_path": str(task_detail_path),
                    "owner_role": "vault_final_update",
                    "source": "task-detail-append:auto_vault_final_update",
                    "skip_auto_queue_handoff": True,
                },
            )
            payload["vault_final_update"] = vault_final_update_result
            if vault_final_update_result.get("decision") == "block":
                payload["result"] = "updated_vault_final_update_blocked"
        artifact = write_gate_command_artifact(
            session_dir=session_dir,
            task_id=task_id,
            artifact_name=f"task_detail_append_{safe_id(section)}",
            payload=payload,
        )
        payload = artifact
        append_jsonl(
            session_dir / "task-detail-append-events.jsonl",
            {
                "ts": now,
                "runtime": runtime,
                "event_type": "task_detail_append",
                "session_id": session_id,
                "task_id": task_id,
                "task_detail_path": str(task_detail_path),
                "section": section,
                "status": status,
                "line_count": line_count,
                "line_cap": line_cap,
                "auto_vault_final_update": auto_vault_final_update,
                "vault_final_update_result": (
                    normalize_cell((payload.get("vault_final_update") or {}).get("decision"))
                    or normalize_cell(((payload.get("vault_final_update") or {}).get("vaultFinalUpdate") or {}).get("result"))
                ),
            },
        )
        if vault_final_update_result.get("decision") == "block":
            return {
                "decision": "block",
                "reason": normalize_cell(vault_final_update_result.get("reason")) or "vault-final-update blocked",
                "taskDetailAppend": payload,
            }
    return {"taskDetailAppend": payload}


VAULT_FINAL_UPDATE_ARTIFACT_NAMES = (
    "tpm_completion",
    "evaluation",
    "finalization",
    "completion_envelope",
)


def vault_final_update_input_paths(hook_input: dict[str, Any]) -> list[str]:
    paths: list[str] = []
    for key in (
        "artifact_paths",
        "artifactPaths",
        "gate_artifact_paths",
        "gateArtifactPaths",
        "tpm_completion_artifact_path",
        "tpmCompletionArtifactPath",
        "evaluation_artifact_path",
        "evaluationArtifactPath",
        "finalization_artifact_path",
        "finalizationArtifactPath",
        "completion_envelope_artifact_path",
        "completionEnvelopeArtifactPath",
    ):
        paths.extend(normalize_string_list(hook_input.get(key)))
    deduped: list[str] = []
    seen: set[str] = set()
    for item in paths:
        path = str(Path(item).expanduser())
        if path not in seen:
            seen.add(path)
            deduped.append(path)
    return deduped


def discover_vault_final_update_artifact_paths(session_dir: Path, task_id: str) -> list[Path]:
    gate_dir = session_dir / "gates" / safe_id(task_id or "unknown-task")
    paths: list[Path] = []
    for name in VAULT_FINAL_UPDATE_ARTIFACT_NAMES:
        path = gate_dir / f"{name}.json"
        if path.exists():
            paths.append(path)
    return paths


def compact_gate_artifact_summary(path: Path) -> tuple[dict[str, Any], list[str]]:
    errors: list[str] = []
    if not path.exists():
        return {
            "path": str(path),
            "exists": False,
            "sha256": "",
            "command": "",
            "status": "missing",
            "result": "missing",
            "next_action": "",
            "notification_class": "",
        }, [f"gate artifact does not exist: {path}"]
    try:
        data = read_json_yaml(path)
    except Exception as exc:
        data = {}
        errors.append(f"gate artifact unreadable: {path}: {type(exc).__name__}: {exc}")
    if not isinstance(data, dict):
        data = {}
        errors.append(f"gate artifact must contain an object: {path}")
    status = normalize_cell(data.get("status") or data.get("completion_status") or data.get("result") or "unknown")
    if status in {"block", "blocked", "failed", "error"}:
        errors.append(f"gate artifact status is blocking: {path}: {status}")
    return {
        "path": str(path),
        "exists": True,
        "sha256": file_sha256_if_exists(path),
        "command": normalize_cell(data.get("command") or data.get("completion_status") or data.get("result")),
        "status": status,
        "result": normalize_cell(data.get("result")),
        "next_action": normalize_cell(data.get("next_action") or data.get("nextAction")),
        "notification_class": normalize_cell(data.get("notification_class") or data.get("notificationClass")),
    }, errors


def vault_final_update_rollup_section(
    *,
    status: str,
    summary: str,
    artifact_path: Path,
    artifact_sha256: str,
    gate_artifacts: list[dict[str, Any]],
    updated_at: str,
    owner_role: str,
) -> str:
    commands = [
        f"{item.get('command') or Path(str(item.get('path', ''))).stem}:{item.get('status', '')}"
        for item in gate_artifacts
    ]
    artifact_refs = [f"{Path(str(item.get('path', ''))).name}:{item.get('sha256', '')[:12]}" for item in gate_artifacts]
    notification_classes = sorted({normalize_cell(item.get("notification_class")) for item in gate_artifacts if normalize_cell(item.get("notification_class"))})
    notification_class_text = markdown_inline_list(notification_classes) if notification_classes else "none"
    return f"""## Vault Final Update

| Field | Value |
|---|---|
| Status | {markdown_table_cell(status)} |
| Summary | {markdown_table_cell(summary)} |
| Gate Artifacts | {markdown_table_cell(markdown_inline_list(commands))} |
| Artifact Hashes | {markdown_table_cell(markdown_inline_list(artifact_refs))} |
| Rollup Artifact | {markdown_table_cell(str(artifact_path))} |
| Rollup SHA256 | {markdown_table_cell(artifact_sha256)} |
| Notification Classes | {markdown_table_cell(notification_class_text)} |
| Updated At | {markdown_table_cell(updated_at)} |
| Owner Role | {markdown_table_cell(owner_role or "vault_final_update")} |
"""


def vault_final_update_output(*, runtime: str, state_root: Path, hook_input: dict[str, Any]) -> dict[str, Any]:
    session_id = current_session_id(state_root, hook_input) or "unknown-session"
    session_dir = state_root / safe_id(session_id)
    session_dir.mkdir(parents=True, exist_ok=True)
    now = current_timestamp()
    dry_run = truthy_input(hook_input.get("dry_run") or hook_input.get("dryRun"))
    auto_finalization_check = truthy_input(
        hook_input.get("auto_finalization_check")
        or hook_input.get("autoFinalizationCheck")
        or hook_input.get("run_finalization_check")
        or hook_input.get("runFinalizationCheck")
    )
    auto_final_transport_render_check = truthy_input(
        hook_input.get("auto_final_transport_render_check")
        or hook_input.get("autoFinalTransportRenderCheck")
        or hook_input.get("run_final_transport_render_check")
        or hook_input.get("runFinalTransportRenderCheck")
    )
    task_detail_path = hook_task_detail_path(hook_input)
    owner_role = normalize_cell(hook_input.get("owner_role") or hook_input.get("ownerRole") or "vault_final_update")
    errors: list[str] = []
    warnings: list[str] = []
    task_detail_text = ""
    if task_detail_path is None:
        errors.append("vault-final-update requires task_detail_path")
    elif not task_detail_path.exists():
        errors.append(f"task_detail_path does not exist: {task_detail_path}")
    else:
        try:
            task_detail_text = task_detail_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            errors.append(f"task detail unreadable: {exc}")
            task_detail_text = ""
    task_id = task_detail_compact_task_id(
        hook_input=hook_input,
        task_detail_path=task_detail_path,
        task_detail_text=task_detail_text,
    )
    input_paths = [Path(item).expanduser() for item in vault_final_update_input_paths(hook_input)]
    artifact_paths = input_paths or discover_vault_final_update_artifact_paths(session_dir, task_id)
    if not artifact_paths:
        errors.append("vault-final-update requires at least one compact gate artifact")
    summaries: list[dict[str, Any]] = []
    for path in artifact_paths:
        summary, artifact_errors = compact_gate_artifact_summary(path)
        summaries.append(summary)
        errors.extend(artifact_errors)
    status = "complete" if not errors else "blocked"
    summary_text = normalize_cell(hook_input.get("summary") or hook_input.get("rollup_summary") or hook_input.get("rollupSummary"))
    if not summary_text:
        summary_text = (
            "Compact gate artifacts rolled up once for Vault final update."
            if status == "complete"
            else "Vault final update blocked; see validation errors."
        )
    rollup_payload = {
        "schema_version": 1,
        "command": "vault-final-update",
        "runtime": runtime,
        "session_id": session_id,
        "task_id": task_id,
        "task_detail_path": str(task_detail_path) if task_detail_path else "",
        "created_at": now,
        "status": status,
        "summary": summary_text,
        "gate_artifacts": summaries,
        "validation_errors": errors,
        "validation_warnings": warnings,
        "next_action": "run_finalization_check" if status == "complete" else "repair_compact_gate_artifacts",
        "handoff_to": "finalization-check" if status == "complete" else "gate-task-evaluator",
        "dry_run": dry_run,
        "auto_finalization_check": auto_finalization_check,
        "auto_final_transport_render_check": auto_final_transport_render_check,
    }
    artifact_path = gate_command_artifact_path(session_dir, task_id, "vault_final_update")
    rollup_with_path = normalized_gate_command_artifact_payload(rollup_payload) | {"artifact_path": str(artifact_path)}
    artifact_sha256 = ""
    rendered_section = vault_final_update_rollup_section(
        status=status,
        summary=summary_text,
        artifact_path=artifact_path,
        artifact_sha256="dry_run" if dry_run else "pending",
        gate_artifacts=summaries,
        updated_at=now,
        owner_role=owner_role,
    )
    if task_detail_text and task_detail_path is not None:
        updated_text = replace_markdown_section(task_detail_text, "Vault Final Update", rendered_section)
        line_errors, line_warnings = task_detail_line_lint(updated_text, "pre_final_response", hook_input)
        errors.extend(line_errors)
        warnings.extend(line_warnings)
        rollup_with_path["line_count"] = len(updated_text.splitlines())
        rollup_with_path["line_cap"] = task_detail_line_cap(hook_input)
        rollup_with_path["validation_errors"] = errors
        rollup_with_path["validation_warnings"] = warnings
        if line_errors:
            status = "blocked"
            rollup_with_path["status"] = status
        elif dry_run:
            rollup_with_path["preview"] = rendered_section
    if not dry_run:
        rollup_artifact_payload = dict(rollup_with_path)
        rollup_artifact_payload.pop("artifact_sha256", None)
        write_json_yaml(artifact_path, rollup_artifact_payload)
        artifact_sha256 = file_sha256_if_exists(artifact_path)
        rollup_with_path["artifact_sha256"] = artifact_sha256
    finalization_check_output: dict[str, Any] = {}
    finalization_block_reason = ""
    if task_detail_text and task_detail_path is not None and not dry_run and not errors:
        rendered_section = vault_final_update_rollup_section(
            status=status,
            summary=summary_text,
            artifact_path=artifact_path,
            artifact_sha256=artifact_sha256,
            gate_artifacts=summaries,
            updated_at=now,
            owner_role=owner_role,
        )
        updated_text = replace_markdown_section(task_detail_text, "Vault Final Update", rendered_section)
        atomic_write_text(task_detail_path, updated_text)
        if auto_finalization_check:
            finalization_check_output = gate_precheck_output(
                runtime=runtime,
                state_root=state_root,
                hook_input={
                    "session_id": session_id,
                    "task_id": task_id,
                    "task_detail_path": str(task_detail_path),
                    "flow_phase": "pre_final_response",
                    "auto_final_transport_render_check": auto_final_transport_render_check,
                    "style_profile": hook_input.get("style_profile")
                    or hook_input.get("styleProfile")
                    or "main_transport_renderer_default",
                    "owner_role": "finalization-check",
                    "last_gate": "finalization-check",
                    "source": "vault-final-update:auto_finalization_check",
                    "skip_auto_queue_handoff": True,
                },
                gate_role="finalization-check",
            )
            rollup_with_path["finalization_check"] = json_event_safe(finalization_check_output)
            if finalization_check_output.get("decision") == "block":
                finalization_block_reason = (
                    normalize_cell(finalization_check_output.get("reason"))
                    or "finalization-check blocked after vault-final-update"
                )
                rollup_with_path["finalization_check_status"] = "block"
            else:
                rollup_with_path["finalization_check_status"] = "pass"
            rollup_artifact_payload = dict(rollup_with_path)
            rollup_artifact_payload.pop("artifact_sha256", None)
            write_json_yaml(artifact_path, rollup_artifact_payload)
            artifact_sha256 = file_sha256_if_exists(artifact_path)
            rollup_with_path["artifact_sha256"] = artifact_sha256
            latest_text = task_detail_path.read_text(encoding="utf-8")
            rendered_section = vault_final_update_rollup_section(
                status=status,
                summary=summary_text,
                artifact_path=artifact_path,
                artifact_sha256=artifact_sha256,
                gate_artifacts=summaries,
                updated_at=now,
                owner_role=owner_role,
            )
            atomic_write_text(task_detail_path, replace_markdown_section(latest_text, "Vault Final Update", rendered_section))
    if errors:
        output = {"decision": "block", "reason": "; ".join(errors), "vaultFinalUpdate": rollup_with_path}
    elif finalization_block_reason:
        output = {
            "decision": "block",
            "reason": finalization_block_reason,
            "vaultFinalUpdate": rollup_with_path
            | {"result": "updated_finalization_blocked", "artifact_sha256": artifact_sha256},
        }
    else:
        result = "dry_run" if dry_run else "updated"
        if finalization_check_output:
            result = "updated_finalization_passed"
        output = {"vaultFinalUpdate": rollup_with_path | {"result": result, "artifact_sha256": artifact_sha256}}
    if not dry_run:
        event_result = "vault_final_update_blocked" if errors else "vault_final_update_updated"
        if finalization_block_reason:
            event_result = "vault_final_update_updated_finalization_blocked"
        elif finalization_check_output:
            event_result = "vault_final_update_updated_finalization_passed"
        append_jsonl_atomic(
            session_dir / "vault-final-update-events.jsonl",
            {
                "ts": now,
                "runtime": runtime,
                "event_type": "vault_final_update",
                "session_id": session_id,
                "task_id": task_id,
                "task_detail_path": str(task_detail_path) if task_detail_path else "",
                "status": status,
                "result": event_result,
                "artifact_path": str(artifact_path),
                "auto_finalization_check": auto_finalization_check,
                "auto_final_transport_render_check": auto_final_transport_render_check,
                "finalization_check": json_event_safe(finalization_check_output) if finalization_check_output else {},
                "validation_errors": errors,
                "validation_warnings": warnings,
            },
        )
    return output


def final_transport_render_check_section(
    *,
    style_profile: str,
    updated_at: str,
    finalization_artifact_path: Path | None,
    completion_envelope_artifact_path: Path | None,
) -> str:
    extra_rows = ""
    if finalization_artifact_path is not None:
        extra_rows += f"| Finalization Artifact | {markdown_table_cell(str(finalization_artifact_path))} |\n"
    if completion_envelope_artifact_path is not None:
        extra_rows += f"| Completion Envelope Artifact | {markdown_table_cell(str(completion_envelope_artifact_path))} |\n"
    return f"""## Final Transport Render Check

| Field | Value |
|---|---|
| Renderer | main_transport_renderer |
| Source Envelope | Completion Envelope |
| Source Finalization Check | Finalization Check |
| Facts Preserved | true |
| No New Task Judgment | true |
| Worker Persona Leakage | false |
| Style Profile | {markdown_table_cell(style_profile or "main_transport_renderer_default")} |
| Safety Exception | false |
| Updated At | {markdown_table_cell(updated_at)} |
{extra_rows}"""


def final_transport_render_check_output(*, runtime: str, state_root: Path, hook_input: dict[str, Any]) -> dict[str, Any]:
    session_id = current_session_id(state_root, hook_input) or "unknown-session"
    session_dir = state_root / safe_id(session_id)
    session_dir.mkdir(parents=True, exist_ok=True)
    now = current_timestamp()
    dry_run = truthy_input(hook_input.get("dry_run") or hook_input.get("dryRun"))
    task_detail_path = hook_task_detail_path(hook_input)
    style_profile = normalize_cell(
        hook_input.get("style_profile")
        or hook_input.get("styleProfile")
        or "main_transport_renderer_default"
    )
    errors: list[str] = []
    warnings: list[str] = []
    task_detail_text = ""
    if task_detail_path is None:
        errors.append("final-transport-render-check requires task_detail_path")
    elif not task_detail_path.exists():
        errors.append(f"task_detail_path does not exist: {task_detail_path}")
    else:
        try:
            task_detail_text = task_detail_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            errors.append(f"task detail unreadable: {exc}")
            task_detail_text = ""

    task_id = task_detail_compact_task_id(
        hook_input=hook_input,
        task_detail_path=task_detail_path,
        task_detail_text=task_detail_text,
    )
    finalization_artifact_path_value = normalize_cell(
        hook_input.get("finalization_artifact_path") or hook_input.get("finalizationArtifactPath")
    )
    finalization_artifact_path = (
        Path(finalization_artifact_path_value).expanduser()
        if finalization_artifact_path_value
        else gate_command_artifact_path(session_dir, task_id, "finalization")
    )
    completion_envelope_artifact_path_value = normalize_cell(
        hook_input.get("completion_envelope_artifact_path") or hook_input.get("completionEnvelopeArtifactPath")
    )
    completion_envelope_artifact_path = (
        Path(completion_envelope_artifact_path_value).expanduser()
        if completion_envelope_artifact_path_value
        else gate_command_artifact_path(session_dir, task_id, "completion_envelope")
    )
    if not finalization_artifact_path.exists():
        errors.append(f"finalization artifact does not exist: {finalization_artifact_path}")
    else:
        try:
            finalization_artifact = read_json_yaml(finalization_artifact_path)
        except Exception as exc:
            finalization_artifact = {}
            errors.append(f"finalization artifact unreadable: {type(exc).__name__}: {exc}")
        if not isinstance(finalization_artifact, dict):
            finalization_artifact = {}
            errors.append("finalization artifact must contain an object")
        if normalize_cell(finalization_artifact.get("command")) != "finalization-check":
            errors.append("finalization artifact command is not finalization-check")
        if normalize_cell(finalization_artifact.get("status")) != "pass":
            errors.append("finalization artifact status is not pass")
        if normalize_cell(finalization_artifact.get("handoff_to")) != "main_transport_renderer":
            errors.append("finalization artifact handoff_to is not main_transport_renderer")

    if task_detail_text:
        completion_envelope = key_value_table(markdown_section(task_detail_text, "Completion Envelope"))
        finalization = key_value_table(markdown_section(task_detail_text, "Finalization Check"))
        if not completion_envelope:
            errors.append("Completion Envelope missing")
        else:
            errors.extend(validate_gate_output_section_schema("Completion Envelope", completion_envelope))
        if not finalization:
            errors.append("Finalization Check missing")
        else:
            finalization_status = table_value(finalization, "Finalization Status", "finalization_status")
            if normalized_publication_value(finalization_status) != "complete":
                errors.append("Finalization Check status is not complete")

    rendered_section = final_transport_render_check_section(
        style_profile=style_profile,
        updated_at=now,
        finalization_artifact_path=finalization_artifact_path if finalization_artifact_path.exists() else None,
        completion_envelope_artifact_path=completion_envelope_artifact_path if completion_envelope_artifact_path.exists() else None,
    )
    rendered_table = key_value_table(rendered_section)
    errors.extend(validate_gate_output_section_schema("Final Transport Render Check", rendered_table))
    updated_text = ""
    if task_detail_text and task_detail_path is not None:
        updated_text = replace_markdown_section(task_detail_text, "Final Transport Render Check", rendered_section)
        line_errors, line_warnings = task_detail_line_lint(updated_text, "pre_final_response", hook_input)
        errors.extend(line_errors)
        warnings.extend(line_warnings)

    status = "complete" if not errors else "blocked"
    artifact_path = gate_command_artifact_path(session_dir, task_id, "final_transport_render_check")
    payload = {
        "schema_version": 1,
        "command": "final-transport-render-check",
        "runtime": runtime,
        "session_id": session_id,
        "task_id": task_id,
        "task_detail_path": str(task_detail_path) if task_detail_path else "",
        "created_at": now,
        "status": status,
        "renderer": "main_transport_renderer",
        "source_envelope": "Completion Envelope",
        "source_finalization_check": "Finalization Check",
        "facts_preserved": True,
        "no_new_task_judgment": True,
        "worker_persona_leakage": False,
        "style_profile": style_profile,
        "safety_exception": False,
        "finalization_artifact_path": str(finalization_artifact_path),
        "completion_envelope_artifact_path": str(completion_envelope_artifact_path) if completion_envelope_artifact_path.exists() else "",
        "validation_errors": errors,
        "validation_warnings": warnings,
        "dry_run": dry_run,
        "handoff_to": "main_transport_renderer" if status == "complete" else "finalization-check",
        "next_action": "render_final_response" if status == "complete" else "repair_final_transport_render_evidence",
    }
    if updated_text:
        payload["line_count"] = len(updated_text.splitlines())
        payload["line_cap"] = task_detail_line_cap(hook_input)
    if errors:
        output = {"decision": "block", "reason": "; ".join(errors), "finalTransportRenderCheck": payload | {"artifact_path": str(artifact_path)}}
    else:
        if dry_run:
            payload["preview"] = rendered_section
            output = {"finalTransportRenderCheck": payload | {"result": "dry_run", "artifact_path": str(artifact_path)}}
        else:
            assert task_detail_path is not None
            atomic_write_text(task_detail_path, updated_text)
            payload = write_gate_command_artifact(
                session_dir=session_dir,
                task_id=task_id,
                artifact_name="final_transport_render_check",
                payload=payload,
            )
            append_jsonl_atomic(
                session_dir / "final-transport-render-check-events.jsonl",
                {
                    "ts": now,
                    "runtime": runtime,
                    "event_type": "final_transport_render_check",
                    "session_id": session_id,
                    "task_id": task_id,
                    "task_detail_path": str(task_detail_path),
                    "status": status,
                    "result": "final_transport_render_check_updated",
                    "artifact_path": payload["artifact_path"],
                    "validation_errors": errors,
                    "validation_warnings": warnings,
                },
            )
            output = {"finalTransportRenderCheck": payload | {"result": "updated"}}
    return output


def gtc_scaffold_task_detail_text(
    *,
    task_id: str,
    title: str,
    status: str,
    envelope: dict[str, Any],
    source_ref: str,
    now: str,
    task_detail_path: Path,
    vault_root: Path,
    session_id: str,
    organization_instance_id: str,
    queue_root: Path,
    missing_fields: list[str],
) -> str:
    unit = first_task_unit(envelope)
    main_team = normalize_cell(unit.get("main_team")) or normalize_cell(envelope.get("main_team")) or "gate"
    assignee = normalize_cell(unit.get("assignee")) or normalize_cell(envelope.get("assignee")) or "teams-project-manager"
    routing_director = gtc_routing_director_for_team(main_team, assignee)
    original_request = normalize_cell(envelope.get("original_request")) or "(missing original_request)"
    deliverables = nested_list_field(envelope, "desired_outcome", "deliverables")
    done_criteria = nested_list_field(envelope, "desired_outcome", "done_criteria")
    scope_in = nested_list_field(envelope, "scope", "in")
    scope_out = nested_list_field(envelope, "scope", "out")
    review_requirements = list_field_values(envelope.get("review_requirements"))
    vault_updates = list_field_values(envelope.get("vault_update_targets"))
    missing_information = list_field_values(envelope.get("missing_information")) + missing_fields
    risks = list_field_values(envelope.get("risks"))
    workflow_mode = normalize_classifier_workflow_mode(envelope.get("workflow_mode")) or "strict_flow"
    risk_tier = normalized_publication_value(envelope.get("risk_tier")) or ("low" if workflow_mode == "controlled_micro_flow" else "normal")
    completion_flow = " -> ".join(COMPLETION_CHAIN)
    detail_link = task_detail_wikilink(vault_root, task_detail_path)
    queue_root_text = str(queue_root)
    controlled_micro_section = ""
    if workflow_mode == "controlled_micro_flow":
        controlled_micro_section = f"""
## Controlled Micro-Flow

| Field | Value |
|---|---|
| Workflow Mode | controlled_micro_flow |
| Risk Tier | {risk_tier or "low"} |
| Organization Policy | preserved |
| Strict Flow Escalation Checked | true |
| Local Gate Evidence Allowed | true |
| External Provider Dispatch | not_required_for_micro_flow |
| Escalation Required | false |
| Escalation Triggers | none |
"""

    return f"""---
type: task-detail
task_id: {task_id}
main_team: {main_team}
assignee: {assignee}
status: {status}
source: {normalize_cell(envelope.get("source_type")) or "gate-intake-envelope"}
last_updated: {now[:10]}
requires_human_approval: {str(truthy_input(envelope.get("approval_required"))).lower()}
---

# {task_id} {title}

## Metadata

| Field | Value |
|---|---|
| Task ID | {task_id} |
| Title | {markdown_table_cell(title)} |
| Main Team | {markdown_table_cell(main_team)} |
| Assignee | {markdown_table_cell(assignee)} |
| Status | {markdown_table_cell(status)} |
| Source | {markdown_table_cell(envelope.get("source_type") or "gate-intake-envelope")} |
| Last Updated | {now[:10]} |
| Requires Human Approval | {str(truthy_input(envelope.get("approval_required"))).lower()} |
| Workflow Mode | {markdown_table_cell(workflow_mode)} |
| Risk Tier | {markdown_table_cell(risk_tier)} |

## Request Summary

| Field | Value |
|---|---|
| Original Request | {markdown_table_cell(original_request)} |
| Intent Summary | {markdown_table_cell(normalize_cell(envelope.get("intent_summary")) or "(missing intent_summary)")} |
| Scope In | {markdown_table_cell(markdown_inline_list(scope_in))} |
| Scope Out | {markdown_table_cell(markdown_inline_list(scope_out))} |
| Deliverables | {markdown_table_cell(markdown_inline_list(deliverables))} |
| Done Criteria | {markdown_table_cell(markdown_inline_list(done_criteria))} |
| Review Requirements | {markdown_table_cell(markdown_inline_list(review_requirements))} |
| Human Approval | {"required" if truthy_input(envelope.get("approval_required")) else "not_required"} |
| Vault Updates | {markdown_table_cell(markdown_inline_list(vault_updates) or "Agents-Vault")} |
| Missing Information | {markdown_table_cell(markdown_inline_list(missing_information))} |
| Risks | {markdown_table_cell(markdown_inline_list(risks))} |
{controlled_micro_section}
## Organization Instance

| Field | Value |
|---|---|
| organization_instance_id | {organization_instance_id} |
| chat_session_id | {session_id} |
| queue_root | {queue_root_text} |

## Organization Active Set

| role_id | agent_instance_id | organization_instance_id | context_scope | chat_session_id | project_id | lifecycle_status | active_for_task | notes |
|---|---|---|---|---|---|---|---|---|
| gate-prompt-formatter | gate-prompt-formatter@{session_id} | {organization_instance_id} | organization_instance | {session_id} | {task_detail_path.parent.parent.name} | active | true | entry role |
| gate-task-creator | gtc-scaffold@{session_id} | {organization_instance_id} | command_artifact | {session_id} | {task_detail_path.parent.parent.name} | command_only | false | builder scaffold owner |
| teams-project-manager | teams-project-manager@{session_id} | {organization_instance_id} | organization_instance | {session_id} | {task_detail_path.parent.parent.name} | active | true | next hop |

## Active Set

| Task Phase | Core Active | Task Active | Deferred Role | Reason |
|---|---|---|---|---|
| pre_execution | gate-prompt-formatter, teams-project-manager | {assignee} | gate-task-creator fallback, specialist teams | gtc-scaffold command created deterministic task artifacts |

## Invocation Evidence

| Agent | Provider | Intended Model | Effective Model | Request ID | Session ID | Usage Source | Transcript Path | Result |
|---|---|---|---|---|---|---|---|---|
| gate-prompt-formatter | registry | pending | pending | pending | {session_id} | queue_report | {markdown_table_cell(source_ref)} | Gate Intake Envelope created |
| gate-task-creator | builder | deterministic | deterministic | gtc-scaffold | {session_id} | builder_command | {markdown_table_cell(detail_link)} | Task scaffold created |

## Queue Evidence

| From Role | To Role | Message ID | Inbox Path | Payload Path | Report Path | Message Status | Report Status | Provider Evidence | Notes |
|---|---|---|---|---|---|---|---|---|---|
| gate-prompt-formatter | gtc-scaffold -> teams-project-manager | pending | pending | pending | {markdown_table_cell(source_ref)} | done | done | source envelope + builder command | compact source retained by scaffold; TPM queue created by auto-chain |

## Execution Preflight

| Check | Value | Evidence |
|---|---|---|
| organization_instance_bootstrapped | true | {session_id} |
| gate_intake_envelope_created | true | {markdown_table_cell(source_ref or "hook_input")} |
| task_detail_created_or_updated | true | {markdown_table_cell(detail_link)} |
| task_index_synced | true | Task-Index.md |
| kanban_synced | true | Kanban.md |
| project_manager_handoff_created | true | Project Manager Handoff |
| review_line_defined | true | Reviews |
| team_roster_recorded | true | Organization Active Set |
| active_set_declared | true | Active Set |
| queue_evidence_recorded | true | Queue Evidence |

## Team Routing Decision

| Field | Value |
|---|---|
| Main Team | {markdown_table_cell(main_team)} |
| Supporting Teams |  |
| Review Evidence Teams | {markdown_table_cell(main_team)} |
| Handoff To Director | {markdown_table_cell(routing_director)} |
| Completion Gate | {completion_flow} |
| Routing Source | gate-task-creator scaffold |

## Project Manager Handoff

| Field | Value |
|---|---|
| Handoff To | teams-project-manager |
| Handoff Status | sent_to_project_manager |
| Created Task | {task_id} |
| Source Envelope | {markdown_table_cell(source_ref or "hook_input.gate_intake_envelope")} |
| Review Requirements | {markdown_table_cell(markdown_inline_list(review_requirements))} |
| Approval Status | {"required" if truthy_input(envelope.get("approval_required")) else "not_required"} |
| Completion Gate | {completion_flow} |
| Notes | Task Detail, Task Index, Kanban, Active Task registered by gtc-scaffold |

## Team Completion Check

| Field | Value |
|---|---|
| Completion Status | pending |
| Required Teams | {markdown_table_cell(main_team)} |
| Completed Teams |  |
| Missing Teams | {markdown_table_cell(main_team)} |
| All Director Reports Complete | false |
| Handoff To | gate-task-evaluator |

## Completion Gate

| Field | Value |
|---|---|
| Completion Flow | {completion_flow} |
| Team Completion Check | pending |
| Evaluation Status | pending |
| Task Change Manifest | pending |
| Git Publication Manifest | pending |
| Commit Required | unknown |
| Push Required | unknown |
| PR Required | unknown |
| Git Publication Result | pending |
| Vault Final Update | pending |
| Finalization Status Checked | false |
| Finalization Status | pending |

"""


def gtc_scaffold_output(*, runtime: str, state_root: Path, hook_input: dict[str, Any]) -> dict[str, Any]:
    session_id = current_session_id(state_root, hook_input) or "unknown-session"
    session_dir = state_root / safe_id(session_id)
    session_dir.mkdir(parents=True, exist_ok=True)
    state_path = session_dir / "bootstrap.json"
    state = read_json(state_path) if state_path.exists() else {}
    organization_instance_id = normalize_cell(
        hook_input.get("organization_instance_id")
        or hook_input.get("organizationInstanceId")
        or state.get("organization_instance_id")
        or organization_id(session_id)
    )
    queue_root = queue_root_for(session_dir, hook_input)
    now = current_timestamp()
    started_at = now
    started_monotonic = time.monotonic()
    dry_run = truthy_input(hook_input.get("dry_run") or hook_input.get("dryRun"))
    vault_root = Path(str(hook_input.get("vault_root") or hook_input.get("vaultRoot") or AGENTS_VAULT_ROOT)).expanduser()
    project_name = normalize_cell(hook_input.get("project") or hook_input.get("project_name") or hook_input.get("projectName") or "AI-Agent-Organization")
    envelope, source_ref, envelope_errors = gate_intake_envelope_from_input(hook_input)
    prompt_submit_chain_id = prompt_submit_chain_id_from_mapping(hook_input)
    if not envelope:
        return {"decision": "block", "reason": "; ".join(envelope_errors), "gtcScaffold": {"result": "blocked", "errors": envelope_errors}}

    vault_lock_resource_id = f"gtc-scaffold-vault:{resolved_path(vault_root)}"
    vault_lock_path = shared_lock_path(state_root, vault_lock_resource_id)
    vault_lock_owner: dict[str, Any] = {}
    active_output: dict[str, Any] = {}
    if not dry_run:
        try:
            vault_lock_owner = acquire_directory_lock(
                vault_lock_path,
                owner={
                    "lock_type": "gtc_scaffold_vault",
                    "resource_id": vault_lock_resource_id,
                    "holder": "gtc-scaffold",
                    "session_id": session_id,
                    "organization_instance_id": organization_instance_id,
                    "purpose": "serialize Task ID allocation and Vault task artifact writes",
                },
                timeout_seconds=10.0,
                stale_after_seconds=900.0,
            )
        except Exception as exc:
            reason = f"gtc-scaffold shared Vault lock failed: {type(exc).__name__}: {exc}"
            return {
                "decision": "block",
                "reason": reason,
                "gtcScaffold": {
                    "result": "blocked",
                    "vault_root": str(vault_root),
                    "shared_lock": {
                        "resource_id": vault_lock_resource_id,
                        "lock_path": str(vault_lock_path),
                        "acquired": False,
                    },
                },
            }

    try:
        missing_fields = gtc_missing_envelope_fields(envelope)
        status = gtc_initial_status(envelope, missing_fields, hook_input)
        task_id = normalize_cell(hook_input.get("task_id") or hook_input.get("taskId")) or next_task_id_from_vault(vault_root)
        if not re.fullmatch(r"TSK-\d{4,}", task_id):
            return {"decision": "block", "reason": f"gtc-scaffold task_id is invalid: {task_id}", "gtcScaffold": {"result": "blocked"}}
        unit = first_task_unit(envelope)
        title = normalize_cell(hook_input.get("title") or unit.get("title") or envelope.get("intent_summary") or task_id)
        slug = safe_task_slug(normalize_cell(hook_input.get("slug")) or title)
        project_root = vault_root / "01-Projects" / project_name
        task_dir = project_root / f"{task_id}-{slug}"
        task_detail_path = task_dir / "task.md"
        if task_detail_path.exists() and not truthy_input(hook_input.get("update_existing") or hook_input.get("updateExisting")):
            reason = f"Task Detail already exists: {task_detail_path}"
            return {"decision": "block", "reason": reason, "gtcScaffold": {"result": "blocked", "task_detail_path": str(task_detail_path)}}

        task_text = gtc_scaffold_task_detail_text(
            task_id=task_id,
            title=title,
            status=status,
            envelope=envelope,
            source_ref=source_ref,
            now=now,
            task_detail_path=task_detail_path,
            vault_root=vault_root,
            session_id=session_id,
            organization_instance_id=organization_instance_id,
            queue_root=queue_root,
            missing_fields=missing_fields,
        )
        detail_link = task_detail_wikilink(vault_root, task_detail_path)
        index_path = vault_root / "00-Inbox&Tasks" / "Task-Index.md"
        kanban_path = vault_root / "00-Inbox&Tasks" / "Kanban.md"
        main_team = normalize_cell(unit.get("main_team")) or normalize_cell(envelope.get("main_team")) or "gate"
        assignee = normalize_cell(unit.get("assignee")) or normalize_cell(envelope.get("assignee")) or "teams-project-manager"
        index_line = f"| {task_id} | {markdown_table_cell(title)} | {main_team} | {assignee} | {status} | [[{detail_link}]] |"
        kanban_line = f"- [[{detail_link}|{task_id} {title}]]"

        if not dry_run:
            atomic_write_text(task_detail_path, task_text)
            index_changed = append_unique_markdown_line(index_path, index_line)
            kanban_changed = ensure_kanban_entry(kanban_path, gtc_kanban_section_for_status(status), kanban_line)
            active_output = active_task_output(
                runtime=runtime,
                state_root=state_root,
                hook_input={
                    "session_id": session_id,
                    "task_id": task_id,
                    "task_detail_path": str(task_detail_path),
                    "flow_phase": "pre_execution",
                    "owner_role": "gate-task-creator",
                    "last_gate": "gate-task-creator",
                    "source": "gtc-scaffold",
                },
            )
        else:
            index_changed = False
            kanban_changed = False
    finally:
        if vault_lock_owner:
            release_queue_lock(vault_lock_path)

    errors: list[str] = []
    warnings: list[str] = []
    if not dry_run:
        errors, warnings = validate_task_flow_artifact(task_detail_path, "pre_execution")
    if envelope_errors:
        warnings.extend(envelope_errors)
    result = "scaffolded_triage" if status == "triage" else "scaffolded"
    if errors:
        result = "scaffolded_with_validation_errors"
    payload = {
        "schema_version": 1,
        "result": result,
        "runtime": runtime,
        "session_id": session_id,
        "organization_instance_id": organization_instance_id,
        "task_id": task_id,
        "title": title,
        "status": status,
        "vault_root": str(vault_root),
        "project": project_name,
        "task_detail_path": str(task_detail_path),
        "task_index_path": str(index_path),
        "kanban_path": str(kanban_path),
        "task_index_changed": index_changed,
        "kanban_changed": kanban_changed,
        "active_task": active_output.get("activeTask") if isinstance(active_output, dict) else {},
        "source_ref": source_ref,
        "missing_envelope_fields": missing_fields,
        "validation_errors": errors,
        "validation_warnings": warnings,
        "dry_run": dry_run,
        "prompt_submit_chain_id": prompt_submit_chain_id,
        "preview": task_text if dry_run else "",
        "shared_lock": {
            "resource_id": vault_lock_resource_id,
            "lock_path": str(vault_lock_path),
            "acquired": bool(vault_lock_owner),
        },
    }
    if not dry_run:
        artifact = write_gate_command_artifact(
            session_dir=session_dir,
            task_id=task_id,
            artifact_name="gtc_scaffold",
            payload=payload,
        )
        payload = artifact
        append_jsonl(
            session_dir / "gtc-scaffold-events.jsonl",
            {
                "ts": now,
                "runtime": runtime,
                "event_type": "gtc_scaffold",
                "session_id": session_id,
                "organization_instance_id": organization_instance_id,
                "task_id": task_id,
                "result": result,
                "task_detail_path": str(task_detail_path),
                "status": status,
                "validation_error_count": len(errors),
            },
        )
        completed_at = current_timestamp()
        append_gate_command_metric(
            session_dir=session_dir,
            queue_root=queue_root,
            runtime=runtime,
            session_id=session_id,
            organization_instance_id=organization_instance_id,
            role_id="gate-task-creator",
            from_role="gate-task-creator",
            to_role="teams-project-manager",
            task_id=task_id,
            message_id=f"gtc-scaffold-{task_id}",
            result="failed" if errors or active_output.get("decision") == "block" else "done",
            started_at=started_at,
            completed_at=completed_at,
            duration_seconds=time.monotonic() - started_monotonic,
            command="gtc-scaffold",
            extra={
                "source_ref": source_ref,
                "prompt_submit_chain_id": prompt_submit_chain_id or source_ref or task_id,
                "task_detail_path": str(task_detail_path),
                "validation_error_count": len(errors),
            },
        )
    if active_output.get("decision") == "block":
        return {"decision": "block", "reason": active_output.get("reason", "active-task registration failed"), "gtcScaffold": payload}
    if errors:
        return {"decision": "block", "reason": "; ".join(errors), "gtcScaffold": payload}
    return {"gtcScaffold": payload}


def finalize_role_queue_report(
    *,
    runtime: str,
    session_dir: Path,
    queue_root: Path,
    inbox_path: Path,
    role_id: str,
    message_id: str,
    report_ref: str,
    result: str,
    status: str,
    summary: str,
    provider_evidence: dict[str, Any],
    gate_intake_envelope: str = "",
    validation: dict[str, Any] | None = None,
    blockers: list[str] | None = None,
    files_changed: list[str] | None = None,
    improvement_log: list[str] | None = None,
    report_extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    now = current_timestamp()
    message = queue_message_by_id(inbox_path, role_id, message_id)
    prompt_submit_chain_id = queue_message_prompt_submit_chain_id(message)
    report_path = safe_queue_relative_path(queue_root, report_ref, "report_path")
    status_value = "done" if status == "done" else "failed"
    report = {
        "report_version": "1",
        "report_id": report_path.stem,
        "report_type": "role_queue_report",
        "from_role": role_id,
        "task_id": normalize_cell(message.get("task_id")),
        "unit_id": normalize_cell(message.get("unit_id")),
        "message_id": message_id,
        "created_at": now,
        "result": result,
        "status": status_value,
        "summary": summary,
        "gate_intake_envelope": gate_intake_envelope,
        "files_changed": files_changed or [],
        "validation": validation or {},
        "blockers": blockers or [],
        "provider_evidence": provider_evidence,
        "improvement_log": improvement_log or [],
        "queue": {
            "queue_root": str(queue_root),
            "inbox_path": str(inbox_path),
            "report_path": report_ref,
        },
    }
    if prompt_submit_chain_id:
        report["prompt_submit_chain_id"] = prompt_submit_chain_id
    for key, value in (report_extra or {}).items():
        if key not in report:
            report[key] = value
    stamp_terminal_report_schema_validation(report, role_id=role_id, message_id=message_id)
    write_json_yaml(report_path, report)
    integrity = report_file_integrity(report_path)
    inbox_updates: dict[str, Any] = {
        "status": status_value,
        "report_path": report_ref,
        "report_sha256": integrity["sha256"],
        "report_line_count": integrity["line_count"],
        "report_byte_count": integrity["byte_count"],
    }
    if status_value == "done":
        inbox_updates["done_at"] = now
    else:
        inbox_updates["failed_at"] = now
        inbox_updates["error"] = "; ".join(blockers or []) or result
    update_inbox_message(inbox_path, role_id, message_id, queue_root, inbox_updates)
    append_jsonl_atomic(
        session_dir / "queue-events.jsonl",
        {
            "ts": now,
            "runtime": runtime,
            "event_type": "role_queue_finalize",
            "role_id": role_id,
            "task_id": normalize_cell(message.get("task_id")),
            "message_id": message_id,
            "result": status_value,
            "report_path": str(report_path),
            "report_integrity": integrity,
            "usage_source": normalize_cell(provider_evidence.get("usage_source")),
        },
    )
    metric_extra = provider_usage_metric_fields(provider_evidence) | {
        "usage_source": normalize_cell(provider_evidence.get("usage_source")),
        "effective_model": normalize_cell(provider_evidence.get("effective_model")),
        "transcript_path": normalize_cell(provider_evidence.get("transcript_path")),
        "report_path": str(report_path),
        "report_ref": report_ref,
    }
    if prompt_submit_chain_id:
        metric_extra["prompt_submit_chain_id"] = prompt_submit_chain_id
    append_queue_metric(
        session_dir=session_dir,
        queue_root=queue_root,
        runtime=runtime,
        session_id=normalize_cell(provider_evidence.get("session_id")) or normalize_cell(message.get("session_id")),
        organization_instance_id=normalize_cell(provider_evidence.get("organization_instance_id")),
        role_id=role_id,
        message=message,
        event_type="finalized",
        result=status_value,
        now=now,
        duration_seconds=float(provider_evidence.get("duration_sec") or 0.0),
        retry_count=int(provider_evidence.get("retry_count") or 0),
        extra=metric_extra,
    )
    output = {
        "result": status_value,
        "role_id": role_id,
        "message_id": message_id,
        "task_id": normalize_cell(message.get("task_id")),
        "report_path": str(report_path),
        "report_ref": report_ref,
        "report_integrity": integrity,
    }
    if prompt_submit_chain_id:
        output["prompt_submit_chain_id"] = prompt_submit_chain_id
    return output


def auto_queue_handoffs_for(role_id: str, status: str) -> list[dict[str, Any]]:
    handoffs: list[dict[str, Any]] = []
    for handoff in AUTO_QUEUE_HANDOFFS:
        if not isinstance(handoff, dict):
            continue
        if not handoff.get("enabled", True):
            continue
        if normalize_cell(handoff.get("from_role")) != role_id:
            continue
        if normalize_cell(handoff.get("on_status") or "done") != status:
            continue
        handoffs.append(dict(handoff))
    return handoffs


def auto_queue_handoff_for(role_id: str, status: str) -> dict[str, Any] | None:
    handoffs = auto_queue_handoffs_for(role_id, status)
    return handoffs[0] if handoffs else None


def auto_handoff_command_name(handoff: dict[str, Any]) -> str:
    command = normalize_cell(handoff.get("command") or handoff.get("to_command"))
    if command:
        return command
    return normalize_cell(handoff.get("to_role")).replace("_", "-")


def auto_handoff_report_data(finalized: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    report_path_raw = normalize_cell(finalized.get("report_path"))
    if not report_path_raw:
        return {}, ["auto handoff report_path missing"]
    report_path = Path(report_path_raw)
    if not report_path.exists():
        return {}, [f"auto handoff report_path does not exist: {report_path}"]
    try:
        data = read_json_yaml(report_path)
    except Exception as exc:
        return {}, [f"auto handoff report unreadable: {report_path}: {type(exc).__name__}: {exc}"]
    if not isinstance(data, dict):
        return {}, [f"auto handoff report must contain an object: {report_path}"]
    return data, []


def auto_handoff_report_handoff_values(report: dict[str, Any]) -> list[str]:
    values: list[str] = []
    for key in ("handoff_to", "handoffTo", "next_role", "nextRole"):
        value = normalize_cell(report.get(key))
        if value:
            values.append(value)
    for section_name in (
        "git_publication_manifest",
        "gitPublicationManifest",
        "Git Publication Manifest",
        "publication_manifest",
        "git_publication_result",
        "gitPublicationResult",
        "Git Publication Result",
        "publication_result",
        "publicationResult",
        "quality_evaluation",
        "qualityEvaluation",
        "Quality Evaluation",
    ):
        section = report.get(section_name)
        if not isinstance(section, dict):
            continue
        for key in ("handoff_to", "handoffTo", "Handoff To", "next_role", "nextRole", "Next Role"):
            value = normalize_cell(section.get(key))
            if value:
                values.append(value)
    return values


def auto_handoff_task_detail_handoff_values(task_detail_path: Path | None) -> tuple[list[str], list[str], str]:
    if task_detail_path is None:
        return [], [], ""
    if not task_detail_path.exists():
        return [], [f"auto handoff task_detail_path does not exist: {task_detail_path}"], ""
    try:
        text = task_detail_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return [], [f"auto handoff task detail unreadable: {task_detail_path}: {exc}"], ""
    values: list[str] = []
    for section_name in ("Git Publication Manifest", "Git Publication Result", "Quality Evaluation", "Completion Gate"):
        table = key_value_table(markdown_section(text, section_name))
        value = table_value(table, "handoff_to", "Handoff To", "next_role", "Next Role")
        if value:
            values.append(value)
    return values, [], text


def auto_handoff_condition_check(
    *,
    finalized: dict[str, Any],
    handoff: dict[str, Any],
    context: dict[str, Any],
) -> dict[str, Any]:
    required_report_result = normalize_cell(handoff.get("required_report_result"))
    required_handoff_to = normalize_cell(handoff.get("required_handoff_to"))
    if not required_report_result and not required_handoff_to:
        return {"result": "passed", "passed": True}

    report, report_errors = auto_handoff_report_data(finalized)
    if report_errors:
        return {
            "result": "blocked",
            "passed": False,
            "reason": "; ".join(report_errors),
            "validation_errors": report_errors,
        }

    if required_report_result:
        actual_result = normalize_cell(report.get("result"))
        if normalized_publication_value(actual_result) != normalized_publication_value(required_report_result):
            return {
                "result": "skipped",
                "passed": False,
                "reason": "required_report_result_mismatch",
                "required_report_result": required_report_result,
                "actual_report_result": actual_result,
            }

    if required_handoff_to:
        task_detail_path = hook_task_detail_path(context)
        task_detail_values, task_detail_errors, task_detail_text = auto_handoff_task_detail_handoff_values(task_detail_path)
        handoff_values = auto_handoff_report_handoff_values(report) + task_detail_values
        normalized_values = {normalized_publication_value(value) for value in handoff_values if normalize_cell(value)}
        if normalized_publication_value(required_handoff_to) not in normalized_values:
            return {
                "result": "skipped",
                "passed": False,
                "reason": "required_handoff_to_mismatch",
                "required_handoff_to": required_handoff_to,
                "actual_handoff_to": handoff_values,
                "validation_warnings": task_detail_errors,
            }
        publication_gate_phase = normalized_publication_value(handoff.get("publication_gate_phase"))
        if publication_gate_phase == "manifest":
            publication_errors = git_publication_manifest_handoff_errors(report, task_detail_text)
        elif task_detail_text:
            publication_errors = git_publication_gate_errors(task_detail_text)
        else:
            publication_errors = []
        if publication_errors:
            return {
                "result": "blocked",
                "passed": False,
                "reason": (
                    "git_publication_manifest_incomplete"
                    if publication_gate_phase == "manifest"
                    else "git_publication_gate_incomplete"
                ),
                "required_handoff_to": required_handoff_to,
                "actual_handoff_to": handoff_values,
                "validation_errors": publication_errors,
                "validation_warnings": task_detail_errors,
            }

    return {"result": "passed", "passed": True}


def auto_handoff_task_context_input(
    *,
    session_dir: Path,
    hook_input: dict[str, Any],
    finalized: dict[str, Any],
    handoff: dict[str, Any],
) -> dict[str, Any]:
    context = dict(hook_input)
    finalized_task_id = normalize_cell(finalized.get("task_id") or finalized.get("taskId"))
    task_id = finalized_task_id or normalize_cell(hook_input.get("task_id") or hook_input.get("taskId"))
    if finalized_task_id:
        context["task_id"] = finalized_task_id
    elif task_id:
        context.setdefault("task_id", task_id)
    if normalize_cell(handoff.get("command_flow_phase")):
        context["flow_phase"] = normalize_cell(handoff.get("command_flow_phase"))
    active_task, active_task_errors, active_task_warnings = load_active_task(session_dir)
    if active_task_errors:
        context.setdefault("_auto_handoff_context_errors", []).extend(active_task_errors)
    if active_task_warnings:
        context.setdefault("_auto_handoff_context_warnings", []).extend(active_task_warnings)
    if not hook_task_detail_path(context) and active_task:
        active_path = active_task_detail_path(active_task)
        if active_path:
            context["task_detail_path"] = str(active_path)
        if not normalize_cell(context.get("flow_phase")):
            context["flow_phase"] = active_task_flow_phase(active_task)
    if not prompt_submit_chain_id_from_mapping(context):
        finalized_chain_id = prompt_submit_chain_id_from_mapping(finalized)
        if finalized_chain_id:
            context["prompt_submit_chain_id"] = finalized_chain_id
    return context


def copy_auto_handoff_context_to_payload(payload: dict[str, Any], hook_input: dict[str, Any]) -> None:
    context: dict[str, Any] = {}
    for key in AUTO_HANDOFF_CONTEXT_KEYS:
        if key in hook_input:
            context[key] = hook_input[key]
    if context:
        payload["auto_handoff_context"] = context


def merge_auto_handoff_context_from_payload(
    hook_input: dict[str, Any],
    payload: dict[str, Any],
    *,
    payload_authoritative: bool = False,
) -> dict[str, Any]:
    context = payload.get("auto_handoff_context") if isinstance(payload.get("auto_handoff_context"), dict) else {}
    if payload_authoritative:
        merged = {key: value for key, value in hook_input.items() if key not in AUTO_HANDOFF_CONTEXT_KEYS}
    else:
        merged = dict(hook_input)
    if not context:
        return merged
    for key in AUTO_HANDOFF_CONTEXT_KEYS:
        if key in context:
            merged[key] = context[key]
    return merged


def copy_auto_handoff_context_from_context(hook_input: dict[str, Any], context: dict[str, Any]) -> None:
    for key in AUTO_HANDOFF_CONTEXT_KEYS:
        if key in context:
            hook_input[key] = context[key]


def auto_handoff_command_passed(command_output: dict[str, Any], *, require_next_phase_allowed: bool) -> tuple[bool, str]:
    gate_command = command_output.get("gateCommand") if isinstance(command_output.get("gateCommand"), dict) else {}
    gate_precheck = command_output.get("gatePrecheck") if isinstance(command_output.get("gatePrecheck"), dict) else {}
    vault_final_update = command_output.get("vaultFinalUpdate") if isinstance(command_output.get("vaultFinalUpdate"), dict) else {}
    gtc_scaffold = command_output.get("gtcScaffold") if isinstance(command_output.get("gtcScaffold"), dict) else {}
    status = normalize_cell(
        gate_command.get("status")
        or gate_precheck.get("precheck_status")
        or vault_final_update.get("status")
        or vault_final_update.get("result")
        or gtc_scaffold.get("result")
    ).lower()
    if status not in {"pass", "complete", "updated", "scaffolded", "scaffolded_triage"}:
        return False, status or "missing_command_status"
    if require_next_phase_allowed and gate_command and not truthy_status(gate_command.get("next_phase_allowed")):
        return False, "next_phase_not_allowed"
    if command_output.get("decision") == "block":
        return False, normalize_cell(command_output.get("reason")) or "command_blocked"
    return True, "pass"


def run_auto_handoff_precheck(
    *,
    runtime: str,
    state_root: Path,
    session_dir: Path,
    finalized: dict[str, Any],
    hook_input: dict[str, Any],
    handoff: dict[str, Any],
) -> dict[str, Any]:
    precheck_command = normalize_cell(handoff.get("precheck_command"))
    if not precheck_command:
        return {"result": "not_required"}
    command_input = auto_handoff_task_context_input(
        session_dir=session_dir,
        hook_input=hook_input,
        finalized=finalized,
        handoff=handoff,
    )
    command_input.setdefault("source", "auto_queue_handoff")
    command_input.setdefault("skip_auto_queue_handoff", True)
    gate_role = normalize_cell(handoff.get("command_gate_role") or precheck_command)
    if precheck_command in {"team-completion-check", "assessor-precheck"}:
        output = gate_precheck_output(
            runtime=runtime,
            state_root=state_root,
            hook_input=command_input,
            gate_role=gate_role if gate_role in {"team-completion-check", "gate-task-assessor"} else "team-completion-check",
        )
    elif precheck_command in {"finalization-check", "guardian-precheck"}:
        output = gate_precheck_output(
            runtime=runtime,
            state_root=state_root,
            hook_input=command_input,
            gate_role=gate_role if gate_role in {"finalization-check", "gate-task-guardian"} else "finalization-check",
        )
    elif precheck_command == "evaluator-precheck":
        output = evaluator_precheck_output(runtime=runtime, state_root=state_root, hook_input=command_input)
    else:
        return {"result": "unsupported_precheck_command", "precheck_command": precheck_command}
    require_next = _optional_bool_config(handoff.get("require_next_phase_allowed"), default=True)
    passed, reason = auto_handoff_command_passed(output, require_next_phase_allowed=require_next)
    return {
        "result": "passed" if passed else "blocked",
        "precheck_command": precheck_command,
        "gate_role": gate_role,
        "passed": passed,
        "reason": reason,
        "command_output": output,
    }


def run_auto_handoff_command(
    *,
    runtime: str,
    state_root: Path,
    session_dir: Path,
    finalized: dict[str, Any],
    hook_input: dict[str, Any],
    handoff: dict[str, Any],
) -> dict[str, Any]:
    command = auto_handoff_command_name(handoff)
    command_input = auto_handoff_task_context_input(
        session_dir=session_dir,
        hook_input=hook_input,
        finalized=finalized,
        handoff=handoff,
    )
    command_input.setdefault("source", "auto_command_handoff")
    command_input.setdefault("skip_auto_queue_handoff", True)
    command_owner_role = normalize_cell(handoff.get("command_owner_role") or handoff.get("commandOwnerRole"))
    command_input.setdefault("owner_role", command_owner_role or normalize_cell(handoff.get("to_role")) or command.replace("-", "_"))
    if truthy_input(hook_input.get("auto_chain_dry_run") or hook_input.get("autoChainDryRun")):
        command_input["dry_run"] = True
    if command in {"vault-final-update", "vault_final_update"}:
        if _optional_bool_config(handoff.get("auto_finalization_check"), default=False):
            command_input["auto_finalization_check"] = True
        if _optional_bool_config(handoff.get("auto_final_transport_render_check"), default=False):
            command_input["auto_final_transport_render_check"] = True
        style_profile = normalize_cell(handoff.get("style_profile") or handoff.get("styleProfile"))
        if style_profile:
            command_input["style_profile"] = style_profile
        output = vault_final_update_output(runtime=runtime, state_root=state_root, hook_input=command_input)
    elif command in {"gtc-scaffold", "gtc_scaffold"}:
        source_report_path = normalize_cell(finalized.get("report_path"))
        if source_report_path:
            command_input.setdefault("gate_intake_report_path", source_report_path)
            command_input.setdefault("source_report_path", source_report_path)
        command_task_id = normalize_cell(command_input.get("task_id") or command_input.get("taskId"))
        if command_task_id and not re.fullmatch(r"TSK-\d{4,}", command_task_id):
            command_input.pop("task_id", None)
            command_input.pop("taskId", None)
            command_input["entry_task_id"] = command_task_id
        command_input.setdefault("owner_role", "gate-task-creator")
        output = gtc_scaffold_output(runtime=runtime, state_root=state_root, hook_input=command_input)
    else:
        return {"result": "unsupported_command", "command": command, "passed": False}
    require_next = _optional_bool_config(handoff.get("require_next_phase_allowed"), default=False)
    passed, reason = auto_handoff_command_passed(output, require_next_phase_allowed=require_next)
    return {
        "result": "passed" if passed else "blocked",
        "command": command,
        "passed": passed,
        "reason": reason,
        "command_output": output,
    }


def auto_handoff_command_payload(command: dict[str, Any]) -> dict[str, Any]:
    output = command.get("command_output") if isinstance(command.get("command_output"), dict) else {}
    if isinstance(output.get("gtcScaffold"), dict):
        return dict(output["gtcScaffold"])
    if isinstance(output.get("vaultFinalUpdate"), dict):
        return dict(output["vaultFinalUpdate"])
    if isinstance(output.get("gateCommand"), dict):
        return dict(output["gateCommand"])
    return {}


def auto_handoff_command_task_id(command: dict[str, Any], fallback: str) -> str:
    payload = auto_handoff_command_payload(command)
    return normalize_cell(payload.get("task_id") or payload.get("taskId")) or fallback


def auto_handoff_command_task_detail_path(command: dict[str, Any]) -> str:
    payload = auto_handoff_command_payload(command)
    return normalize_cell(payload.get("task_detail_path") or payload.get("taskDetailPath"))


def command_handoff_should_queue(handoff: dict[str, Any], handoff_type: str) -> bool:
    if handoff_type in {"command_then_queue", "command+queue", "command_queue"}:
        return True
    return truthy_input(handoff.get("queue_after_command") or handoff.get("queueAfterCommand"))


def maybe_enqueue_auto_queue_handoff(
    *,
    runtime: str,
    state_root: Path,
    session_id: str,
    organization_instance_id: str,
    from_role: str,
    finalized: dict[str, Any],
    hook_input: dict[str, Any],
) -> dict[str, Any]:
    if truthy_input(hook_input.get("skip_auto_queue_handoff") or hook_input.get("skipAutoQueueHandoff")):
        return {"result": "skipped_by_input"}
    handoffs = auto_queue_handoffs_for(from_role, normalize_cell(finalized.get("result")))
    if not handoffs:
        return {"result": "not_configured"}
    task_id = normalize_cell(finalized.get("task_id"))
    if not task_id:
        return {"result": "skipped_missing_target"}
    report_ref = normalize_cell(finalized.get("report_ref"))
    session_dir = state_root / safe_id(session_id)
    skipped_candidates: list[dict[str, Any]] = []
    for handoff in handoffs:
        to_role = normalize_cell(handoff.get("to_role"))
        if not to_role:
            continue
        handoff_type = normalize_cell(handoff.get("handoff_type") or "queue")
        context = auto_handoff_task_context_input(
            session_dir=session_dir,
            hook_input=hook_input,
            finalized=finalized,
            handoff=handoff,
        )
        condition = auto_handoff_condition_check(finalized=finalized, handoff=handoff, context=context)
        if not truthy_status(condition.get("passed")):
            result = "blocked_by_condition" if condition.get("result") == "blocked" else "skipped_by_condition"
            event = {
                "ts": current_timestamp(),
                "runtime": runtime,
                "event_type": "auto_queue_handoff",
                "handoff_type": handoff_type,
                "session_id": session_id,
                "organization_instance_id": organization_instance_id,
                "from_role": from_role,
                "to_role": to_role,
                "task_id": task_id,
                "result": result,
                "condition": json_event_safe(condition),
                "queue_output": {},
                "assessor_integration_policy": ASSESSOR_INTEGRATION_POLICY,
            }
            if result == "skipped_by_condition":
                skipped_candidates.append(event)
                continue
            if skipped_candidates:
                event["skipped_candidates"] = json_event_safe(skipped_candidates)
            append_jsonl_atomic(session_dir / "queue-events.jsonl", event)
            return event
        precheck = run_auto_handoff_precheck(
            runtime=runtime,
            state_root=state_root,
            session_dir=session_dir,
            finalized=finalized,
            hook_input=hook_input,
            handoff=handoff,
        )
        if precheck.get("result") in {"blocked", "unsupported_precheck_command"}:
            event = {
                "ts": current_timestamp(),
                "runtime": runtime,
                "event_type": "auto_queue_handoff",
                "handoff_type": handoff_type,
                "session_id": session_id,
                "organization_instance_id": organization_instance_id,
                "from_role": from_role,
                "to_role": to_role,
                "task_id": task_id,
                "result": "blocked_by_precheck",
                "condition": json_event_safe(condition),
                "precheck": json_event_safe(precheck),
                "queue_output": {},
                "assessor_integration_policy": ASSESSOR_INTEGRATION_POLICY,
            }
            if skipped_candidates:
                event["skipped_candidates"] = json_event_safe(skipped_candidates)
            append_jsonl_atomic(session_dir / "queue-events.jsonl", event)
            return event
        if handoff_type in {"command", "command_then_queue", "command+queue", "command_queue"}:
            command = run_auto_handoff_command(
                runtime=runtime,
                state_root=state_root,
                session_dir=session_dir,
                finalized=finalized,
                hook_input=hook_input,
                handoff=handoff,
            )
            queue_output: dict[str, Any] = {}
            result = "command_passed" if truthy_status(command.get("passed")) else "blocked_by_command"
            if truthy_status(command.get("passed")) and command_handoff_should_queue(handoff, handoff_type):
                command_task_id = auto_handoff_command_task_id(command, task_id)
                command_task_detail_path = auto_handoff_command_task_detail_path(command)
                command_name = normalize_cell(command.get("command"))
                instruction = (
                    f"{from_role} completed `{task_id}` and `{command_name}` produced `{command_task_id}`.\n"
                    f"Continue the completion chain as `{to_role}` using the generated Task Detail.\n"
                )
                if command_task_detail_path:
                    instruction += f"task_detail_path: {command_task_detail_path}\n"
                payload = {
                    "type": "command_completion_chain_handoff",
                    "from_role": from_role,
                    "command_owner_role": normalize_cell(handoff.get("command_owner_role") or handoff.get("commandOwnerRole")),
                    "previous_report_ref": report_ref,
                    "previous_report_path": normalize_cell(finalized.get("report_path")),
                    "command": normalize_cell(command.get("command")),
                    "command_result": command,
                }
                queue_input = {
                    "session_id": session_id,
                    "organization_instance_id": organization_instance_id,
                    "role_id": to_role,
                    "from_role": normalize_cell(handoff.get("command_owner_role") or handoff.get("commandOwnerRole")) or from_role,
                    "task_id": command_task_id,
                    "message_id": f"auto-{safe_id(from_role)}-cmd-to-{safe_id(to_role)}-{uuid.uuid4().hex[:12]}",
                    "report_id": f"auto-{safe_id(to_role)}-{uuid.uuid4().hex[:12]}",
                    "instruction": instruction,
                    "payload": payload,
                }
                copy_auto_handoff_context_from_context(queue_input, context)
                if command_task_detail_path:
                    queue_input["context_ref"] = command_task_detail_path
                if truthy_input(hook_input.get("auto_chain_dry_run") or hook_input.get("autoChainDryRun")):
                    queue_input["dry_run"] = True
                queue_output = role_queue(runtime=runtime, state_root=state_root, hook_input=queue_input)
                result = normalize_cell((queue_output.get("roleQueue") or {}).get("result") or queue_output.get("decision") or "queued")
            event = {
                "ts": current_timestamp(),
                "runtime": runtime,
                "event_type": "auto_queue_handoff",
                "handoff_type": handoff_type,
                "session_id": session_id,
                "organization_instance_id": organization_instance_id,
                "from_role": from_role,
                "to_role": to_role,
                "task_id": task_id,
                "result": result,
                "condition": json_event_safe(condition),
                "precheck": json_event_safe(precheck),
                "command": json_event_safe(command),
                "queue_output": json_event_safe(queue_output),
                "assessor_integration_policy": ASSESSOR_INTEGRATION_POLICY,
            }
            command_task_id = auto_handoff_command_task_id(command, "")
            if command_task_id:
                event["command_task_id"] = command_task_id
            command_task_detail_path = auto_handoff_command_task_detail_path(command)
            if command_task_detail_path:
                event["command_task_detail_path"] = command_task_detail_path
            if skipped_candidates:
                event["skipped_candidates"] = json_event_safe(skipped_candidates)
            append_jsonl_atomic(session_dir / "queue-events.jsonl", event)
            return event
        instruction = (
            f"{from_role} completed `{task_id}` and wrote report `{report_ref}`.\n"
            f"Continue the completion chain as `{to_role}`.\n"
        )
        precheck_command = normalize_cell(handoff.get("precheck_command"))
        if precheck_command:
            instruction += f"Run or consume `{precheck_command}` evidence before producing the next gate artifact.\n"
        payload = {
            "type": "completion_chain_handoff",
            "from_role": from_role,
            "previous_report_ref": report_ref,
            "previous_report_path": normalize_cell(finalized.get("report_path")),
            "precheck_command": precheck_command,
            "precheck_result": precheck,
        }
        report_data, report_errors = auto_handoff_report_data(finalized)
        if not report_errors:
            publication_manifest = report_git_publication_manifest(report_data)
            if publication_manifest:
                payload["git_publication_manifest"] = publication_manifest
            task_change_manifest = report_task_change_manifest(report_data)
            if task_change_manifest:
                payload["task_change_manifest"] = task_change_manifest
        queue_input = {
            "session_id": session_id,
            "organization_instance_id": organization_instance_id,
            "role_id": to_role,
            "from_role": from_role,
            "task_id": task_id,
            "message_id": f"auto-{safe_id(from_role)}-to-{safe_id(to_role)}-{uuid.uuid4().hex[:12]}",
            "report_id": f"auto-{safe_id(to_role)}-{uuid.uuid4().hex[:12]}",
            "instruction": instruction,
            "payload": payload,
        }
        copy_auto_handoff_context_from_context(queue_input, context)
        if truthy_input(hook_input.get("auto_chain_dry_run") or hook_input.get("autoChainDryRun")):
            queue_input["dry_run"] = True
        output = role_queue(runtime=runtime, state_root=state_root, hook_input=queue_input)
        event = {
            "ts": current_timestamp(),
            "runtime": runtime,
            "event_type": "auto_queue_handoff",
            "handoff_type": handoff_type,
            "session_id": session_id,
            "organization_instance_id": organization_instance_id,
            "from_role": from_role,
            "to_role": to_role,
            "task_id": task_id,
            "result": normalize_cell((output.get("roleQueue") or {}).get("result") or output.get("decision") or "queued"),
            "queue_output": json_event_safe(output),
            "condition": json_event_safe(condition),
            "precheck": json_event_safe(precheck),
            "assessor_integration_policy": ASSESSOR_INTEGRATION_POLICY,
        }
        if skipped_candidates:
            event["skipped_candidates"] = json_event_safe(skipped_candidates)
        append_jsonl_atomic(session_dir / "queue-events.jsonl", event)
        return event

    event = skipped_candidates[-1] if skipped_candidates else {"result": "skipped_missing_target"}
    if skipped_candidates:
        event["skipped_candidates"] = json_event_safe(skipped_candidates)
    append_jsonl_atomic(session_dir / "queue-events.jsonl", event)
    return event


def role_report(*, runtime: str, state_root: Path, hook_input: dict[str, Any]) -> dict[str, Any]:
    if hook_input.get("_cli_report_json_error"):
        return {"decision": "block", "reason": f"role-report invalid report-json: {hook_input.get('_cli_report_json_error')}"}
    session_id = str(
        current_session_id(state_root, hook_input)
        or "unknown-session"
    )
    session_dir = state_root / safe_id(session_id)
    state_path = session_dir / "bootstrap.json"
    state = read_json(state_path) if state_path.exists() else {}
    organization_instance_id = str(
        state.get("organization_instance_id")
        or hook_input.get("organization_instance_id")
        or hook_input.get("organizationInstanceId")
        or organization_id(session_id)
    )
    role_id = normalize_cell(
        hook_input.get("role_id")
        or hook_input.get("roleId")
        or hook_input.get("agent_id")
        or hook_input.get("agentId")
    )
    message_id = normalize_cell(hook_input.get("message_id") or hook_input.get("messageId"))
    if not role_id:
        return {"decision": "block", "reason": "role-report requires role_id or agent_id"}
    if not message_id:
        return {"decision": "block", "reason": "role-report requires message_id"}
    report_body_keys = {
        "status",
        "result",
        "summary",
        "files_changed",
        "filesChanged",
        "validation",
        "blockers",
        "provider_evidence",
        "providerEvidence",
        "gate_intake_envelope",
        "gateIntakeEnvelope",
        "report_extra",
        "reportExtra",
    }
    if not any(key in hook_input for key in report_body_keys):
        return {"decision": "block", "reason": "role-report requires report JSON body on stdin"}

    role_row = role_agent_row_for(role_id, organization_instance_id=organization_instance_id)
    if not role_row:
        return {"decision": "block", "reason": f"role-agent registry has no active role: {role_id}"}
    if truthy_input(hook_input.get("queue_consumer_override") or hook_input.get("queueConsumerOverride")):
        role_row = dict(role_row)
        role_row["queue_consumer"] = True

    queue_root = queue_root_for(session_dir, hook_input)
    inbox_path = queue_root / str(role_row["inbox_path"])
    try:
        message = queue_message_by_id(inbox_path, role_id, message_id)
    except ValueError as exc:
        return {"decision": "block", "reason": str(exc)}

    payload = message.get("payload") if isinstance(message.get("payload"), dict) else {}
    report_ref = normalize_cell(
        hook_input.get("report_path")
        or hook_input.get("reportPath")
        or payload.get("report_path")
    )
    if not report_ref:
        return {"decision": "block", "reason": "role-report requires report_path or message payload.report_path"}

    raw_status = normalize_cell(hook_input.get("status") or "done").lower()
    status = "done" if raw_status in {"done", "complete", "completed", "success", "ok"} else "failed"
    provider_evidence = hook_input.get("provider_evidence")
    if not isinstance(provider_evidence, dict):
        provider_evidence = {}
    provider_evidence = dict(provider_evidence)
    provider_evidence.setdefault("provider", normalize_cell(role_row.get("provider")) or "unknown")
    provider_evidence.setdefault("intended_model", normalize_cell(role_row.get("intended_model")))
    provider_evidence.setdefault("effective_model", normalize_cell(hook_input.get("effective_model") or hook_input.get("effectiveModel") or role_row.get("intended_model")))
    provider_evidence.setdefault("provider_session_id", normalize_cell(hook_input.get("provider_session_id") or hook_input.get("providerSessionId") or session_id))
    provider_evidence.setdefault("request_id", normalize_cell(hook_input.get("request_id") or hook_input.get("requestId") or "interactive-role-report"))
    provider_evidence.setdefault("usage_source", normalize_cell(hook_input.get("usage_source") or hook_input.get("usageSource") or "provider_authored_role_report"))
    provider_evidence.setdefault("transcript_path", normalize_cell(hook_input.get("transcript_path") or hook_input.get("transcriptPath")))
    provider_evidence.setdefault("session_id", session_id)
    provider_evidence.setdefault("organization_instance_id", organization_instance_id)
    provider_evidence.setdefault("duration_sec", hook_input.get("duration_sec") or hook_input.get("durationSec") or 0)
    provider_evidence.setdefault("input_tokens", hook_input.get("input_tokens") or hook_input.get("inputTokens") or "")
    provider_evidence.setdefault("output_tokens", hook_input.get("output_tokens") or hook_input.get("outputTokens") or "")
    provider_evidence.setdefault("duration_api_ms", hook_input.get("duration_api_ms") or hook_input.get("durationApiMs") or "")
    provider_evidence.setdefault("num_turns", hook_input.get("num_turns") or hook_input.get("numTurns") or "")
    provider_evidence.setdefault("retry_count", hook_input.get("retry_count") or hook_input.get("retryCount") or message.get("retry_count") or 0)
    enrich_role_report_provider_evidence_from_claude_transcript(
        provider_evidence,
        state_root=state_root,
        session_id=session_id,
        role_id=role_id,
        message=message,
    )

    files_changed = hook_input.get("files_changed") or hook_input.get("filesChanged") or []
    blockers = hook_input.get("blockers") or []
    improvement_log = hook_input.get("improvement_log") or hook_input.get("improvementLog") or []
    if not isinstance(files_changed, list):
        files_changed = [str(files_changed)]
    if not isinstance(blockers, list):
        blockers = [str(blockers)]
    if not isinstance(improvement_log, list):
        improvement_log = [str(improvement_log)]

    validation = hook_input.get("validation") if isinstance(hook_input.get("validation"), dict) else {}
    validation = dict(validation)
    validation.setdefault("artifact_writer", "itb_atomic_queue_writer")
    validation.setdefault("inbox_status_updated_by", "role-report")

    report_extra = hook_input.get("report_extra") or hook_input.get("reportExtra") or {}
    if not isinstance(report_extra, dict):
        report_extra = {}

    try:
        finalized = finalize_role_queue_report(
            runtime=runtime,
            session_dir=session_dir,
            queue_root=queue_root,
            inbox_path=inbox_path,
            role_id=role_id,
            message_id=message_id,
            report_ref=report_ref,
            result=normalize_cell(hook_input.get("result") or ("role_report_created" if status == "done" else "role_report_failed")),
            status=status,
            summary=normalize_cell(hook_input.get("summary") or ""),
            provider_evidence=provider_evidence,
            gate_intake_envelope=str(hook_input.get("gate_intake_envelope") or hook_input.get("gateIntakeEnvelope") or ""),
            validation=validation,
            blockers=[str(item) for item in blockers],
            files_changed=[str(item) for item in files_changed],
            improvement_log=[str(item) for item in improvement_log],
            report_extra=report_extra,
        )
    except (TimeoutError, ValueError) as exc:
        return {"decision": "block", "reason": str(exc)}

    auto_handoff_input = merge_auto_handoff_context_from_payload(hook_input, payload, payload_authoritative=True)
    if truthy_input(payload.get("skip_auto_queue_handoff") or payload.get("skipAutoQueueHandoff")):
        auto_handoff_input = auto_handoff_input | {"skip_auto_queue_handoff": True}
    report_data, report_errors = auto_handoff_report_data(finalized)
    team_completion_update: dict[str, Any] = {}
    if not report_errors:
        team_completion_update = maybe_update_tpm_team_completion_check(
            runtime=runtime,
            session_dir=session_dir,
            queue_root=queue_root,
            role_id=role_id,
            message=message,
            report=report_data,
            finalized=finalized,
            hook_input=auto_handoff_input,
            now=current_timestamp(),
        )
        if normalize_cell(team_completion_update.get("task_detail_path")):
            auto_handoff_input.setdefault("task_detail_path", team_completion_update["task_detail_path"])
    auto_handoff = maybe_enqueue_auto_queue_handoff(
        runtime=runtime,
        state_root=state_root,
        session_id=session_id,
        organization_instance_id=organization_instance_id,
        from_role=role_id,
        finalized=finalized,
        hook_input=auto_handoff_input,
    )

    role_report_payload = finalized | {"auto_handoff": auto_handoff}
    if team_completion_update and team_completion_update.get("result") != "skipped_not_tpm":
        role_report_payload["team_completion_update"] = team_completion_update
    return {"roleReport": role_report_payload}


def finalize_gate_entry_dispatch_queue(
    *,
    runtime: str,
    session_dir: Path,
    queue_output: dict[str, Any],
    dispatch_output: dict[str, Any],
    response_errors: list[str],
) -> dict[str, Any]:
    queue = queue_output.get("roleQueue", {})
    dispatch = dispatch_output.get("agentDispatch", {})
    queue_root_raw = normalize_cell(queue.get("queue_root"))
    inbox_path_raw = normalize_cell(queue.get("inbox_path"))
    report_path_raw = normalize_cell(queue.get("report_path"))
    message_id = normalize_cell(queue.get("message_id"))
    role_id = normalize_cell(queue.get("role_id")) or GATE_ENTRY_AGENT_ID
    if not queue_root_raw or not inbox_path_raw or not report_path_raw or not message_id:
        return {"result": "skipped", "reason": "queue metadata missing"}
    queue_root = Path(queue_root_raw)
    inbox_path = Path(inbox_path_raw)
    report_ref = str(Path(report_path_raw).resolve().relative_to(queue_root.resolve()))
    response = str(dispatch.get("response") or "").strip()
    dispatch_result = normalize_cell(dispatch.get("result"))
    ready = dispatch_result == "provider_response_ready" and not response_errors
    provider_evidence = {
        "provider": normalize_cell(dispatch.get("provider")) or "anthropic",
        "intended_model": normalize_cell(dispatch.get("intended_model")) or normalize_cell(dispatch.get("effective_model")),
        "effective_model": normalize_cell(dispatch.get("effective_model")),
        "provider_session_id": normalize_cell(dispatch.get("target") or dispatch.get("provider_session_id")),
        "request_id": normalize_cell(dispatch.get("request_id")),
        "usage_source": normalize_cell(dispatch.get("usage_source")),
        "transcript_path": normalize_cell(dispatch.get("transcript_path")),
        "session_id": normalize_cell(dispatch.get("session_id")),
        "organization_instance_id": normalize_cell(dispatch.get("organization_instance_id")),
        "input_tokens": dispatch.get("input_tokens"),
        "output_tokens": dispatch.get("output_tokens"),
        "duration_api_ms": dispatch.get("duration_api_ms"),
        "duration_sec": dispatch.get("duration_sec"),
        "num_turns": dispatch.get("num_turns"),
    }
    blockers = response_errors[:]
    if not ready and not blockers:
        blockers.append(normalize_cell(dispatch_output.get("reason") or dispatch.get("error") or dispatch_result or "dispatch failed"))
    return finalize_role_queue_report(
        runtime=runtime,
        session_dir=session_dir,
        queue_root=queue_root,
        inbox_path=inbox_path,
        role_id=role_id,
        message_id=message_id,
        report_ref=report_ref,
        result="gate_intake_envelope_created" if ready else "gate_intake_envelope_failed",
        status="done" if ready else "failed",
        summary=(
            "Gate Intake Envelope captured from provider response and finalized by ITB atomic queue writer."
            if ready
            else "Gate Intake Envelope provider response failed validation and was finalized as failed."
        ),
        provider_evidence=provider_evidence,
        gate_intake_envelope=response,
        validation={
            "gate_intake_envelope_valid": ready,
            "response_validation_errors": response_errors,
            "artifact_writer": "itb_atomic_queue_writer",
        },
        blockers=blockers,
        files_changed=[],
        improvement_log=[
            "Gate entry report/inbox finalization is adapter-owned to avoid role-side file-write stalls.",
        ],
    )


def env_csv(name: str) -> set[str]:
    value = os.environ.get(name, "")
    return {item.strip() for item in value.split(",") if item.strip()}






def role_execution_prompt(row: dict[str, Any]) -> str:
    agent_id = row["agent_id"]
    skill_path = role_definition_path(agent_id)
    organization_instance_id = normalize_cell(row.get("organization_instance_id")) or "unknown-organization"
    return f"""You are `{agent_id}` in organization instance `{organization_instance_id}`.
Provider: {row.get('provider', '')}.
Intended model: {row.get('intended_model', '')}.
Execution mode: {row.get('execution_mode', '')}.
SKILL.md: {skill_path}
Read your SKILL.md, follow its Flow Contract, and write decisions, evidence, and handoffs to Agents-Vault.
Output discipline: act on flow instructions silently; report work content only. Use [FLOW-ALERT] once only for blockers or approval waits.
"""


def agent_runtime(row: dict[str, Any]) -> tuple[str, str] | None:
    provider = row.get("provider", "")
    execution_mode = row.get("execution_mode", "")
    if provider == "anthropic":
        return ("claude_cli", "claude")
    if provider == "openai" or execution_mode == "codex":
        return ("codex_exec", "codex")
    return None





def validate_provider_evidence(
    *,
    agent_id: str,
    provider: str,
    intended_model: str,
    effective_model: str,
    usage_source: str,
) -> list[str]:
    errors: list[str] = []
    normalized_provider = (provider or "").strip().lower()
    normalized_intended = (intended_model or "").strip().lower()
    normalized_effective = (effective_model or "").strip().lower()
    normalized_usage = (usage_source or "").strip().lower()

    expects_claude = normalized_provider == "anthropic" or normalized_intended.startswith("claude-")
    expects_openai = normalized_provider == "openai" or normalized_intended.startswith("gpt-")

    if expects_claude:
        if not normalized_effective.startswith("claude-"):
            errors.append(
                f"{agent_id}: provider mismatch; intended Claude/anthropic but effective_model={effective_model or '<empty>'}"
            )
        if "claude" not in normalized_usage:
            errors.append(
                f"{agent_id}: provider mismatch; intended Claude/anthropic but usage_source={usage_source or '<empty>'}"
            )
    elif expects_openai:
        if normalized_effective.startswith("claude-"):
            errors.append(
                f"{agent_id}: provider mismatch; intended OpenAI/Codex but effective_model={effective_model or '<empty>'}"
            )
        if normalized_usage and "claude" in normalized_usage:
            errors.append(
                f"{agent_id}: provider mismatch; intended OpenAI/Codex but usage_source={usage_source or '<empty>'}"
            )
    return errors


MODEL_TIER_RANKS = {
    "haiku": 1,
    "mini": 1,
    "spark": 1,
    "sonnet": 2,
    "gpt": 2,
    "opus": 3,
}


def model_tier_name(model: Any) -> str:
    normalized = normalize_cell(model).lower()
    if not normalized:
        return ""
    if "opus" in normalized:
        return "opus"
    if "sonnet" in normalized:
        return "sonnet"
    if "haiku" in normalized:
        return "haiku"
    if "mini" in normalized or "spark" in normalized:
        return "mini"
    if normalized.startswith("gpt-"):
        return "gpt"
    return ""


def model_tier_mismatch_warning(agent_id: str, intended_model: str, effective_model: str) -> str:
    intended_tier = model_tier_name(intended_model)
    effective_tier = model_tier_name(effective_model)
    if not intended_tier or not effective_tier or intended_tier == effective_tier:
        return ""
    intended_rank = MODEL_TIER_RANKS.get(intended_tier, 0)
    effective_rank = MODEL_TIER_RANKS.get(effective_tier, 0)
    direction = "higher" if effective_rank > intended_rank else "lower" if effective_rank < intended_rank else "different"
    return (
        f"{agent_id}: model tier mismatch; intended_model={intended_model or '<empty>'} "
        f"({intended_tier}) but effective_model={effective_model or '<empty>'} ({effective_tier}, {direction})"
    )


def provider_add_dirs(
    cwd: str,
    *,
    row: dict[str, Any] | None = None,
    extra_dirs: list[str | Path] | None = None,
) -> list[str]:
    env_value = os.environ.get("ITB_PROVIDER_ADD_DIRS", "")
    candidates: list[Path] = []
    if env_value.strip():
        for raw in env_value.replace(",", os.pathsep).split(os.pathsep):
            value = raw.strip()
            if value:
                candidates.append(Path(value).expanduser())
    else:
        context_dirs = row.get("context_dirs") if isinstance(row, dict) else None
        if context_dirs is None:
            candidates.extend(DEFAULT_PROVIDER_ADD_DIR_CANDIDATES)
        else:
            candidates.extend(Path(item).expanduser() for item in normalize_string_list(context_dirs))
    for extra_dir in extra_dirs or []:
        if extra_dir:
            candidates.append(Path(extra_dir).expanduser())

    cwd_path = Path(cwd).expanduser()
    result: list[str] = []
    seen: set[str] = {str(cwd_path)}
    for candidate in candidates:
        try:
            resolved = str(candidate.resolve())
        except OSError:
            resolved = str(candidate)
        if resolved in seen or not candidate.exists():
            continue
        seen.add(resolved)
        result.append(resolved)
    return result


def rough_token_estimate(byte_count: int) -> int:
    return max(0, (int(byte_count) + 3) // 4)


def context_surface_path_summary(
    path: Path,
    *,
    max_files: int,
    max_bytes: int,
    max_depth: int,
) -> dict[str, Any]:
    summary: dict[str, Any] = {
        "path": str(path),
        "status": "missing",
        "file_count": 0,
        "dir_count": 0,
        "sampled_bytes": 0,
        "rough_tokens": 0,
        "truncated": False,
        "truncation_reason": "",
        "max_files": max_files,
        "max_bytes": max_bytes,
        "max_depth": max_depth,
    }
    try:
        resolved = path.expanduser().resolve(strict=False)
    except OSError:
        resolved = path.expanduser()
    summary["path"] = str(resolved)
    if not resolved.exists():
        return summary
    if resolved.is_file():
        try:
            sampled = min(resolved.stat().st_size, max_bytes)
        except OSError as exc:
            return summary | {"status": "unreadable", "error": str(exc)}
        summary.update(
            {
                "status": "file",
                "file_count": 1,
                "sampled_bytes": sampled,
                "rough_tokens": rough_token_estimate(sampled),
                "truncated": sampled >= max_bytes,
                "truncation_reason": "max_bytes" if sampled >= max_bytes else "",
            }
        )
        return summary
    if not resolved.is_dir():
        return summary | {"status": "unsupported_path_type"}

    root_parts = len(resolved.parts)
    skipped_dir_count = 0
    try:
        for dirpath, dirnames, filenames in os.walk(resolved):
            current = Path(dirpath)
            relative_depth = max(0, len(current.parts) - root_parts)
            original_dir_count = len(dirnames)
            dirnames[:] = [
                name
                for name in dirnames
                if name not in CONTEXT_SURFACE_SKIP_DIR_NAMES and not name.startswith(".")
            ]
            skipped_dir_count += original_dir_count - len(dirnames)
            if relative_depth >= max_depth:
                skipped_dir_count += len(dirnames)
                dirnames[:] = []
            summary["dir_count"] += 1
            for filename in filenames:
                if summary["file_count"] >= max_files:
                    summary["truncated"] = True
                    summary["truncation_reason"] = "max_files"
                    break
                if summary["sampled_bytes"] >= max_bytes:
                    summary["truncated"] = True
                    summary["truncation_reason"] = "max_bytes"
                    break
                file_path = current / filename
                try:
                    stat = file_path.stat()
                except OSError:
                    continue
                if not file_path.is_file():
                    continue
                remaining = max(0, max_bytes - int(summary["sampled_bytes"]))
                summary["sampled_bytes"] += min(stat.st_size, remaining)
                summary["file_count"] += 1
            if summary["truncated"]:
                break
    except OSError as exc:
        return summary | {"status": "unreadable", "error": str(exc)}

    summary["status"] = "directory"
    summary["rough_tokens"] = rough_token_estimate(int(summary["sampled_bytes"]))
    summary["skipped_dir_count"] = skipped_dir_count
    return summary


def preflight_compaction_summary(session_dir: Path, *, max_events: int) -> dict[str, Any]:
    path = session_dir / "preflight-events.jsonl"
    summary: dict[str, Any] = {
        "event_path": str(path),
        "event_count": 0,
        "sampled_event_count": 0,
        "compacted_count": 0,
        "ready_count": 0,
        "degraded_count": 0,
        "blocked_count": 0,
        "compaction_hit_rate": 0.0,
    }
    if not path.exists():
        return summary
    lines = path.read_text(encoding="utf-8").splitlines()
    summary["event_count"] = len(lines)
    for line in lines[-max_events:]:
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict) or event.get("event_type") != "prompt_preflight":
            continue
        summary["sampled_event_count"] += 1
        if truthy_status(event.get("context_compacted")):
            summary["compacted_count"] += 1
        result = normalize_cell(event.get("result"))
        if result == "preflight_ready":
            summary["ready_count"] += 1
        elif result == "preflight_degraded":
            summary["degraded_count"] += 1
        elif result == "preflight_blocked":
            summary["blocked_count"] += 1
    if summary["sampled_event_count"]:
        summary["compaction_hit_rate"] = round(
            summary["compacted_count"] / summary["sampled_event_count"],
            4,
        )
    return summary


def context_surface_report_output(
    *,
    runtime: str,
    state_root: Path,
    hook_input: dict[str, Any],
) -> dict[str, Any]:
    session_id = str(current_session_id(state_root, hook_input) or "context-surface")
    session_dir = state_root / safe_id(session_id)
    state_path = session_dir / "bootstrap.json"
    state = read_json(state_path) if state_path.exists() else {}
    organization_instance_id = normalize_cell(
        state.get("organization_instance_id")
        or hook_input.get("organization_instance_id")
        or hook_input.get("organizationInstanceId")
        or organization_id(session_id)
    )
    cwd = normalize_cell(hook_input.get("cwd") or state.get("cwd") or os.getcwd())
    role_filter = normalize_cell(hook_input.get("role_id") or hook_input.get("roleId"))
    include_queue_root_value = (
        hook_input["include_queue_root"]
        if "include_queue_root" in hook_input
        else hook_input.get("includeQueueRoot")
    )
    include_queue_root = truthy_input(include_queue_root_value, default=True)
    max_files = bounded_int_input(hook_input.get("max_files") or hook_input.get("maxFiles"), default=2000, minimum=1, maximum=20000)
    max_bytes = bounded_int_input(
        hook_input.get("max_bytes") or hook_input.get("maxBytes"),
        default=2_000_000,
        minimum=1024,
        maximum=100_000_000,
    )
    max_depth = bounded_int_input(hook_input.get("max_depth") or hook_input.get("maxDepth"), default=6, minimum=0, maximum=32)
    max_events = bounded_int_input(
        hook_input.get("max_events") or hook_input.get("maxEvents"),
        default=2000,
        minimum=1,
        maximum=100000,
    )
    queue_root_raw = normalize_cell(state.get("queue_root"))
    extra_dirs: list[str | Path] = [queue_root_raw] if include_queue_root and queue_root_raw else []
    rows = role_agent_rows(organization_instance_id=organization_instance_id)
    if role_filter:
        rows = [row for row in rows if normalize_cell(row.get("agent_id")) == role_filter]
    role_reports: list[dict[str, Any]] = []
    total_sampled_bytes = 0
    total_rough_tokens = 0
    for row in rows:
        add_dirs = provider_add_dirs(cwd, row=row, extra_dirs=extra_dirs)
        path_summaries = [
            context_surface_path_summary(
                Path(add_dir),
                max_files=max_files,
                max_bytes=max_bytes,
                max_depth=max_depth,
            )
            for add_dir in add_dirs
        ]
        sampled_bytes = sum(int(item.get("sampled_bytes") or 0) for item in path_summaries)
        rough_tokens = sum(int(item.get("rough_tokens") or 0) for item in path_summaries)
        total_sampled_bytes += sampled_bytes
        total_rough_tokens += rough_tokens
        role_reports.append(
            {
                "agent_id": row.get("agent_id"),
                "provider": row.get("provider"),
                "execution_mode": row.get("execution_mode"),
                "queue_consumer": bool(row.get("queue_consumer")),
                "context_dir_count": len(row.get("context_dirs") or []),
                "effective_add_dir_count": len(add_dirs),
                "effective_add_dirs": add_dirs,
                "sampled_bytes": sampled_bytes,
                "rough_tokens": rough_tokens,
                "truncated": any(bool(item.get("truncated")) for item in path_summaries),
                "paths": path_summaries,
            }
        )

    compact_summary = preflight_compaction_summary(session_dir, max_events=max_events)
    payload = {
        "ts": current_timestamp(),
        "runtime": runtime,
        "command": "context-surface-report",
        "session_id": session_id,
        "organization_instance_id": organization_instance_id,
        "cwd": cwd,
        "role_filter": role_filter,
        "include_queue_root": include_queue_root,
        "limits": {
            "max_files": max_files,
            "max_bytes": max_bytes,
            "max_depth": max_depth,
            "max_events": max_events,
        },
        "role_count": len(role_reports),
        "roles_with_add_dirs": sum(1 for item in role_reports if int(item.get("effective_add_dir_count") or 0) > 0),
        "total_sampled_bytes": total_sampled_bytes,
        "total_rough_tokens": total_rough_tokens,
        "preflight_compaction": compact_summary,
        "roles": role_reports,
    }
    artifact_path = session_dir / "context-surface-report.json"
    write_json_yaml(artifact_path, payload)
    append_jsonl_atomic(session_dir / "context-surface-events.jsonl", payload)
    return {"contextSurfaceReport": payload | {"artifact_path": str(artifact_path)}}


def provider_permission_mode(value: Any = "") -> str:
    return normalize_cell(value or os.environ.get("ITB_PROVIDER_PERMISSION_MODE") or DEFAULT_PROVIDER_PERMISSION_MODE)


def provider_permission_mode_for_model(model: Any, value: Any = "") -> str:
    explicit = normalize_cell(value or os.environ.get("ITB_PROVIDER_PERMISSION_MODE"))
    if explicit:
        return explicit
    if claude_model_family(model) == "haiku":
        return "acceptEdits"
    return DEFAULT_PROVIDER_PERMISSION_MODE


def codex_approval_policy(value: Any = "") -> str:
    return normalize_cell(value or os.environ.get("ITB_CODEX_APPROVAL_POLICY") or DEFAULT_CODEX_APPROVAL_POLICY)


def claude_model_family(model: Any) -> str:
    normalized = normalize_cell(model).lower()
    for family in ("opus", "sonnet", "haiku"):
        if family in normalized:
            return family
    return "default"


def claude_effort_for_model(model: Any) -> str:
    explicit = normalize_cell(os.environ.get("ITB_CLAUDE_EFFORT"))
    if explicit:
        return explicit
    family = claude_model_family(model)
    if family == "opus":
        return normalize_cell(os.environ.get("ITB_CLAUDE_OPUS_EFFORT") or DEFAULT_CLAUDE_OPUS_EFFORT)
    if family in {"sonnet", "haiku"}:
        return normalize_cell(
            os.environ.get("ITB_CLAUDE_HAIKU_SONNET_EFFORT")
            or os.environ.get("ITB_CLAUDE_SONNET_HAIKU_EFFORT")
            or DEFAULT_CLAUDE_HAIKU_SONNET_EFFORT
        )
    return normalize_cell(os.environ.get("ITB_CLAUDE_DEFAULT_EFFORT") or DEFAULT_CLAUDE_HAIKU_SONNET_EFFORT)


def codex_model_for_agent(row: dict[str, Any]) -> str:
    return normalize_cell(os.environ.get("ITB_CODEX_MODEL") or DEFAULT_CODEX_MODEL or row.get("intended_model", ""))


def codex_reasoning_effort() -> str:
    return normalize_cell(os.environ.get("ITB_CODEX_REASONING_EFFORT") or DEFAULT_CODEX_REASONING_EFFORT)


def codex_service_tier() -> str:
    return normalize_cell(os.environ.get("ITB_CODEX_SERVICE_TIER") or DEFAULT_CODEX_SERVICE_TIER)


















def int_from_nested(data: dict[str, Any], paths: list[tuple[str, ...]]) -> int | None:
    for path in paths:
        current: Any = data
        for key in path:
            if not isinstance(current, dict) or key not in current:
                current = None
                break
            current = current[key]
        if isinstance(current, int):
            return current
        if isinstance(current, str) and current.isdigit():
            return int(current)
    return None


def str_from_nested(data: dict[str, Any], paths: list[tuple[str, ...]]) -> str:
    for path in paths:
        current: Any = data
        for key in path:
            if not isinstance(current, dict) or key not in current:
                current = None
                break
            current = current[key]
        if current:
            return str(current)
    return ""


def first_claude_fallback(row: dict[str, Any]) -> str:
    primary = row.get("intended_model", "")
    fallbacks = str(row.get("fallback_models", ""))
    for item in fallbacks.split(","):
        fallback = item.strip()
        if fallback.startswith("claude-") and fallback != primary:
            return fallback
    return ""


def claude_activation_budget(row: dict[str, Any], hook_input: dict[str, Any]) -> tuple[str, str]:
    explicit = hook_input.get("max_budget_usd") or hook_input.get("maxBudgetUsd")
    if explicit:
        return str(explicit), "hook_input"

    env_value = os.environ.get("ITB_PROVIDER_ACTIVATION_MAX_BUDGET_USD")
    if env_value:
        return env_value, "env"

    model = str(row.get("intended_model", "")).lower()
    for tier in ("opus", "sonnet", "haiku"):
        if tier in model:
            return CLAUDE_ACTIVATION_BUDGET_DEFAULTS_USD[tier], f"model_tier:{tier}"
    return CLAUDE_ACTIVATION_BUDGET_DEFAULTS_USD["default"], "model_tier:default"


def claude_activation_command(row: dict[str, Any], prompt: str, max_budget_usd: str) -> list[str]:
    model = row.get("intended_model", "")
    command = [
        "claude",
        "--safe-mode",
        "--print",
        "--output-format",
        "json",
        "--model",
        model,
        "--permission-mode",
        provider_permission_mode(),
        "--effort",
        claude_effort_for_model(model),
        "--no-session-persistence",
        "--max-budget-usd",
        max_budget_usd,
        "--append-system-prompt",
        role_execution_prompt(row),
        prompt,
    ]
    tools = claude_tools_argument_for_role(row)
    if tools:
        command[command.index("--no-session-persistence") : command.index("--no-session-persistence")] = ["--tools", tools]
    fallback_model = first_claude_fallback(row)
    if fallback_model:
        model_index = command.index("--model")
        command[model_index + 2 : model_index + 2] = ["--fallback-model", fallback_model]
    return command


def codex_activation_command(row: dict[str, Any], prompt: str, cwd: str) -> list[str]:
    model = row.get("intended_model", "") or DEFAULT_CODEX_MODEL
    effort = os.environ.get("ITB_CODEX_REASONING_EFFORT") or DEFAULT_CODEX_REASONING_EFFORT
    tier = os.environ.get("ITB_CODEX_SERVICE_TIER") or DEFAULT_CODEX_SERVICE_TIER
    return [
        "codex",
        "exec",
        "--ephemeral",
        "--ignore-user-config",
        "--ignore-rules",
        "--json",
        "--model",
        model,
        "--cd",
        cwd,
        "--sandbox",
        "workspace-write",
        "-c",
        f'model_reasoning_effort="{effort}"',
        "-c",
        f'service_tier="{tier}"',
        prompt,
    ]


def codex_event_text(event: dict[str, Any]) -> str:
    for key in ("result", "message", "text"):
        value = event.get(key)
        if isinstance(value, str) and value.strip():
            return value
    content = event.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict) and isinstance(item.get("text"), str):
                parts.append(item["text"])
            elif isinstance(item, str):
                parts.append(item)
        return "\n".join(part for part in parts if part.strip())
    return ""


def parse_codex_json_output(stdout: str) -> dict[str, Any]:
    result: dict[str, Any] = {}
    last_text = ""
    parsed_any = False
    for line in stdout.splitlines():
        if not line.strip():
            continue
        event = json.loads(line)
        if not isinstance(event, dict):
            continue
        parsed_any = True
        text = codex_event_text(event)
        if text:
            last_text = text
        if event.get("type") == "result" or event.get("subtype") in {"success", "error"}:
            result.update(event)
    if not parsed_any:
        return {}
    if last_text and not result.get("result"):
        result["result"] = last_text
    return result


def codex_exec_role_prompt(row: dict[str, Any], prompt: str) -> str:
    return f"""{role_execution_prompt(row).strip()}

Provider request:
{prompt.strip()}
"""


def codex_exec_agent_dispatch(*, runtime: str, state_root: Path, hook_input: dict[str, Any]) -> dict[str, Any]:
    session_id = str(
        hook_input.get("session_id")
        or hook_input.get("sessionId")
        or current_session_id(state_root, hook_input)
        or "unknown-session"
    )
    session_dir = state_root / safe_id(session_id)
    state_path = session_dir / "bootstrap.json"
    roster_path = session_dir / "roster.json"
    session_dir.mkdir(parents=True, exist_ok=True)
    now = current_timestamp()

    state = read_json(state_path) if state_path.exists() else {}
    if not isinstance(state, dict):
        state = {}
    organization_instance_id = str(
        state.get("organization_instance_id")
        or hook_input.get("organization_instance_id")
        or hook_input.get("organizationInstanceId")
        or organization_id(session_id)
    )
    state.setdefault("runtime", runtime)
    state.setdefault("session_id", session_id)
    state.setdefault("organization_instance_id", organization_instance_id)
    state.setdefault("cwd", str(hook_input.get("cwd") or os.getcwd()))
    state.setdefault("bootstrap_status", "headless_metadata")
    state.setdefault("readiness_scope", "metadata_only")
    roster = read_json(roster_path) if roster_path.exists() else role_agent_rows(
        organization_instance_id=organization_instance_id,
    )
    if not isinstance(roster, list):
        return {"decision": "block", "reason": "roster.json is not a list"}
    agent_id = normalize_cell(hook_input.get("agent_id") or hook_input.get("agentId"))
    prompt = str(hook_input.get("prompt") or "")
    if not agent_id:
        return {"decision": "block", "reason": "codex exec provider adapter requires agent_id"}
    if not prompt.strip():
        return {"decision": "block", "reason": "codex exec provider adapter requires prompt"}

    row = next((item for item in roster if isinstance(item, dict) and item.get("agent_id") == agent_id), None)
    if row is None:
        row = role_agent_row_for(agent_id, organization_instance_id=organization_instance_id)
        if not row:
            return {"decision": "block", "reason": f"agent not found in registry: {agent_id}"}
        roster.append(row)
    if agent_runtime(row) != ("codex_exec", "codex"):
        return {
            "decision": "block",
            "reason": (
                f"codex exec provider adapter requires OpenAI/codex role: "
                f"{agent_id} provider={row.get('provider', '')} execution_mode={row.get('execution_mode', '')}"
            ),
        }
    git_policy_error = validate_git_operation_for_role(row, prompt)
    if git_policy_error:
        return {"decision": "block", "reason": git_policy_error}
    if shutil.which("codex") is None:
        return {"decision": "block", "reason": "codex command not found"}

    cwd = str(hook_input.get("cwd") or state.get("cwd") or os.getcwd())
    request_id = normalize_cell(hook_input.get("request_id") or hook_input.get("requestId") or f"req-{uuid.uuid4().hex}")
    transcript_dir = session_dir / "provider-exec" / safe_id(agent_id)
    transcript_dir.mkdir(parents=True, exist_ok=True)
    transcript_path = transcript_dir / f"{safe_id(request_id)}.jsonl"
    command = codex_activation_command(row, codex_exec_role_prompt(row, prompt), cwd)
    started = time.monotonic()
    completed = subprocess.run(
        command,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=env_int("ITB_CODEX_EXEC_DISPATCH_TIMEOUT_SECONDS") or env_int("ITB_PROVIDER_ACTIVATION_TIMEOUT_SECONDS") or 120,
        check=False,
    )
    elapsed_seconds = time.monotonic() - started
    elapsed_ms = int(elapsed_seconds * 1000)
    transcript_path.write_text(completed.stdout, encoding="utf-8")
    if completed.returncode != 0:
        append_jsonl(
            session_dir / "invocation-evidence.jsonl",
            invocation_evidence_entry(
                ts=now,
                runtime=runtime,
                event_type="agent_dispatch",
                session_id=session_id,
                organization_instance_id=organization_instance_id,
                agent_id=agent_id,
                result="provider_process_failed",
                usage_source="codex_exec_json",
                effective_model=row.get("intended_model", ""),
                request_id=request_id,
                duration_api_ms=elapsed_ms,
                notes=completed.stderr.strip() or completed.stdout.strip(),
                extra={"transcript_path": str(transcript_path), "cwd": cwd},
            ),
        )
        return {"decision": "block", "reason": completed.stderr.strip() or completed.stdout.strip()}

    try:
        codex_result = parse_codex_json_output(completed.stdout)
    except json.JSONDecodeError as exc:
        return {"decision": "block", "reason": f"codex json output unreadable: {exc}"}

    input_tokens = int_from_nested(
        codex_result,
        [("usage", "input_tokens"), ("usage", "inputTokens"), ("input_tokens",), ("inputTokens",)],
    )
    output_tokens = int_from_nested(
        codex_result,
        [("usage", "output_tokens"), ("usage", "outputTokens"), ("output_tokens",), ("outputTokens",)],
    )
    codex_duration_api_ms = int_from_nested(
        codex_result,
        [("duration_api_ms",), ("durationApiMs",), ("metrics", "duration_api_ms")],
    )
    duration_api_ms = codex_duration_api_ms if codex_duration_api_ms is not None else elapsed_ms
    provider_session_id = str_from_nested(codex_result, [("session_id",), ("sessionId",)])
    output_request_id = str_from_nested(codex_result, [("request_id",), ("requestId",)]) or request_id
    effective_model = (
        str_from_nested(codex_result, [("model",), ("effective_model",), ("effectiveModel",)])
        or row.get("intended_model", "")
    )
    result_text = str(codex_result.get("result") or codex_result.get("message") or "").strip()
    num_turns = int_from_nested(codex_result, [("num_turns",), ("numTurns",)])
    has_inference_evidence = bool(
        result_text
        or (input_tokens is not None and input_tokens > 0)
        or (output_tokens is not None and output_tokens > 0)
        or (codex_duration_api_ms is not None and codex_duration_api_ms > 0)
        or (num_turns is not None and num_turns > 0)
    )
    result_name = "provider_response_ready" if has_inference_evidence and result_text else "provider_response_no_inference"
    row["last_seen_at"] = now
    row["last_request_id"] = output_request_id
    row["effective_model"] = effective_model
    row["session_id"] = provider_session_id
    row["usage_source"] = "codex_exec_json" if result_name == "provider_response_ready" else "codex_exec_json_no_inference"
    row["provider_status"] = result_name
    if result_name == "provider_response_ready":
        row["activation_status"] = "response_active"
        row["response_status"] = "invoked"
        row["notes"] = "Codex exec one-shot agent-dispatch completed with response evidence."
    else:
        row["response_status"] = "not_invoked"
        row["notes"] = "Codex exec one-shot agent-dispatch produced no response text or inference evidence."

    update_provider_response_state(state, roster)
    state["last_agent_dispatch_agent"] = agent_id
    state["last_agent_dispatch_at"] = now
    state["last_agent_dispatch_usage_source"] = row["usage_source"]
    if result_name == "provider_response_ready":
        state["readiness_scope"] = "response_evidence"

    write_json_yaml(roster_path, roster)
    write_json_yaml(state_path, state)
    append_jsonl_atomic(
        session_dir / "invocation-evidence.jsonl",
        invocation_evidence_entry(
            ts=now,
            runtime=runtime,
            event_type="agent_dispatch",
            session_id=session_id,
            organization_instance_id=organization_instance_id,
            agent_id=agent_id,
            result=result_name,
            usage_source=row["usage_source"],
            effective_model=effective_model,
            request_id=output_request_id,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            duration_api_ms=duration_api_ms,
            num_turns=num_turns,
            notes="Codex exec one-shot agent-dispatch completed." if result_name == "provider_response_ready" else "Codex exec one-shot produced no inference evidence.",
            extra={
                "provider_session_id": provider_session_id,
                "transcript_path": str(transcript_path),
                "stdout_result_present": bool(result_text),
                "cwd": cwd,
            },
        ),
    )
    if result_name != "provider_response_ready":
        return {
            "decision": "block",
            "reason": "codex exec provider adapter produced no response evidence",
            "agentDispatch": {
                "agent_id": agent_id,
                "request_id": output_request_id,
                "result": result_name,
                "usage_source": row["usage_source"],
                "effective_model": effective_model,
                "transcript_path": str(transcript_path),
            },
        }

    return {
        "agentDispatch": {
            "agent_id": agent_id,
            "request_id": output_request_id,
            "result": "provider_response_ready",
            "provider": "openai",
            "intended_model": row.get("intended_model", ""),
            "effective_model": effective_model,
            "usage_source": "codex_exec_json",
            "provider_session_id": provider_session_id,
            "session_id": session_id,
            "organization_instance_id": organization_instance_id,
            "transcript_path": str(transcript_path),
            "response": result_text,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "duration_api_ms": duration_api_ms,
            "duration_sec": round(max(0.0, elapsed_seconds), 3),
            "num_turns": num_turns,
        }
    }


def claude_cli_role_prompt(row: dict[str, Any], prompt: str) -> str:
    return f"""{role_execution_prompt(row).strip()}

Provider request:
{prompt.strip()}
"""


def claude_cli_agent_dispatch(*, runtime: str, state_root: Path, hook_input: dict[str, Any]) -> dict[str, Any]:
    session_id = str(
        hook_input.get("session_id")
        or hook_input.get("sessionId")
        or current_session_id(state_root, hook_input)
        or "unknown-session"
    )
    session_dir = state_root / safe_id(session_id)
    state_path = session_dir / "bootstrap.json"
    roster_path = session_dir / "roster.json"
    session_dir.mkdir(parents=True, exist_ok=True)
    now = current_timestamp()

    state_missing = not state_path.exists()
    roster_missing = not roster_path.exists()
    state = read_json(state_path) if not state_missing else {}
    if not isinstance(state, dict):
        state = {}
    organization_instance_id = str(
        state.get("organization_instance_id")
        or hook_input.get("organization_instance_id")
        or hook_input.get("organizationInstanceId")
        or organization_id(session_id)
    )
    state.setdefault("runtime", runtime)
    state.setdefault("session_id", session_id)
    state.setdefault("organization_instance_id", organization_instance_id)
    state.setdefault("cwd", str(hook_input.get("cwd") or os.getcwd()))
    state.setdefault("bootstrap_status", "headless_metadata")
    state.setdefault("readiness_scope", "metadata_only")
    roster = read_json(roster_path) if not roster_missing else role_agent_rows(
        organization_instance_id=organization_instance_id,
    )
    if not isinstance(roster, list):
        return {"decision": "block", "reason": "roster.json is not a list"}

    agent_id = normalize_cell(hook_input.get("agent_id") or hook_input.get("agentId"))
    prompt = str(hook_input.get("prompt") or "")
    if not agent_id:
        return {"decision": "block", "reason": "claude CLI provider adapter requires agent_id"}
    if not prompt.strip():
        return {"decision": "block", "reason": "claude CLI provider adapter requires prompt"}

    row = next((item for item in roster if isinstance(item, dict) and item.get("agent_id") == agent_id), None)
    if row is None:
        row = role_agent_row_for(agent_id, organization_instance_id=organization_instance_id)
        if not row:
            return {"decision": "block", "reason": f"agent not found in registry: {agent_id}"}
        roster.append(row)
    if agent_runtime(row) != ("claude_cli", "claude"):
        return {
            "decision": "block",
            "reason": (
                f"claude CLI provider adapter requires Anthropic/Claude role: "
                f"{agent_id} provider={row.get('provider', '')} execution_mode={row.get('execution_mode', '')}"
            ),
        }
    git_policy_error = validate_git_operation_for_role(row, prompt)
    if git_policy_error:
        return {"decision": "block", "reason": git_policy_error}
    if shutil.which("claude") is None:
        return {"decision": "block", "reason": "claude command not found"}

    request_id = normalize_cell(hook_input.get("request_id") or hook_input.get("requestId") or f"req-{uuid.uuid4().hex}")
    transcript_dir = session_dir / "provider-exec" / safe_id(agent_id)
    transcript_dir.mkdir(parents=True, exist_ok=True)
    transcript_path = transcript_dir / f"{safe_id(request_id)}.json"
    max_budget_usd, budget_source = claude_activation_budget(row, hook_input)
    command = claude_activation_command(row, claude_cli_role_prompt(row, prompt), max_budget_usd)
    started = time.monotonic()
    completed = subprocess.run(
        command,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=env_int("ITB_CLAUDE_CLI_DISPATCH_TIMEOUT_SECONDS") or env_int("ITB_PROVIDER_ACTIVATION_TIMEOUT_SECONDS") or 120,
        check=False,
    )
    elapsed_seconds = time.monotonic() - started
    elapsed_ms = int(elapsed_seconds * 1000)
    transcript_path.write_text(completed.stdout, encoding="utf-8")
    if completed.returncode != 0:
        append_jsonl_atomic(
            session_dir / "invocation-evidence.jsonl",
            invocation_evidence_entry(
                ts=now,
                runtime=runtime,
                event_type="agent_dispatch",
                session_id=session_id,
                organization_instance_id=organization_instance_id,
                agent_id=agent_id,
                result="provider_process_failed",
                usage_source="claude_print_json",
                effective_model=row.get("intended_model", ""),
                request_id=request_id,
                duration_api_ms=elapsed_ms,
                notes=completed.stderr.strip() or completed.stdout.strip(),
                extra={"transcript_path": str(transcript_path), "max_budget_usd": max_budget_usd, "budget_source": budget_source},
            ),
        )
        return {"decision": "block", "reason": completed.stderr.strip() or completed.stdout.strip()}

    try:
        claude_result = parse_claude_json_output(completed.stdout)
    except json.JSONDecodeError as exc:
        return {"decision": "block", "reason": f"claude json output unreadable: {exc}"}

    input_tokens = int_from_nested(
        claude_result,
        [("usage", "input_tokens"), ("usage", "inputTokens"), ("input_tokens",), ("inputTokens",)],
    )
    output_tokens = int_from_nested(
        claude_result,
        [("usage", "output_tokens"), ("usage", "outputTokens"), ("output_tokens",), ("outputTokens",)],
    )
    claude_duration_api_ms = int_from_nested(
        claude_result,
        [("duration_api_ms",), ("durationApiMs",), ("metrics", "duration_api_ms")],
    )
    duration_api_ms = claude_duration_api_ms if claude_duration_api_ms is not None else elapsed_ms
    provider_session_id = str_from_nested(claude_result, [("session_id",), ("sessionId",)])
    output_request_id = str_from_nested(claude_result, [("request_id",), ("requestId",)]) or request_id
    effective_model = str_from_nested(claude_result, [("model",), ("effective_model",), ("effectiveModel",)]) or row.get("intended_model", "")
    result_text = str(claude_result.get("result") or claude_result.get("message") or "").strip()
    num_turns = int_from_nested(claude_result, [("num_turns",), ("numTurns",)])
    has_inference_evidence = bool(
        result_text
        or (input_tokens is not None and input_tokens > 0)
        or (output_tokens is not None and output_tokens > 0)
        or (claude_duration_api_ms is not None and claude_duration_api_ms > 0)
        or (num_turns is not None and num_turns > 0)
    )
    result_name = "provider_response_ready" if has_inference_evidence and result_text else "provider_response_no_inference"
    row["last_seen_at"] = now
    row["last_request_id"] = output_request_id
    row["effective_model"] = effective_model
    row["session_id"] = provider_session_id
    row["usage_source"] = "claude_print_json" if result_name == "provider_response_ready" else "claude_print_json_no_inference"
    row["provider_status"] = result_name
    if result_name == "provider_response_ready":
        row["activation_status"] = "response_active"
        row["response_status"] = "invoked"
        row["notes"] = "Claude CLI one-shot agent-dispatch completed with response evidence."
    else:
        row["response_status"] = "not_invoked"
        row["notes"] = "Claude CLI one-shot agent-dispatch produced no response text or inference evidence."

    update_provider_response_state(state, roster)
    state["last_agent_dispatch_agent"] = agent_id
    state["last_agent_dispatch_at"] = now
    state["last_agent_dispatch_usage_source"] = row["usage_source"]
    if result_name == "provider_response_ready":
        state["readiness_scope"] = "response_evidence"

    write_json_yaml(roster_path, roster)
    write_json_yaml(state_path, state)
    append_jsonl_atomic(
        session_dir / "invocation-evidence.jsonl",
        invocation_evidence_entry(
            ts=now,
            runtime=runtime,
            event_type="agent_dispatch",
            session_id=session_id,
            organization_instance_id=organization_instance_id,
            agent_id=agent_id,
            result=result_name,
            usage_source=row["usage_source"],
            effective_model=effective_model,
            request_id=output_request_id,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            duration_api_ms=duration_api_ms,
            num_turns=num_turns,
            notes="Claude CLI one-shot agent-dispatch completed." if result_name == "provider_response_ready" else "Claude CLI one-shot produced no inference evidence.",
            extra={
                "provider_session_id": provider_session_id,
                "transcript_path": str(transcript_path),
                "stdout_result_present": bool(result_text),
                "max_budget_usd": max_budget_usd,
                "budget_source": budget_source,
            },
        ),
    )
    if result_name != "provider_response_ready":
        return {
            "decision": "block",
            "reason": "claude CLI provider adapter produced no response evidence",
            "agentDispatch": {
                "agent_id": agent_id,
                "request_id": output_request_id,
                "result": result_name,
                "usage_source": row["usage_source"],
                "effective_model": effective_model,
                "transcript_path": str(transcript_path),
            },
        }

    return {
        "agentDispatch": {
            "agent_id": agent_id,
            "request_id": output_request_id,
            "result": "provider_response_ready",
            "provider": "anthropic",
            "intended_model": row.get("intended_model", ""),
            "effective_model": effective_model,
            "usage_source": "claude_print_json",
            "provider_session_id": provider_session_id,
            "session_id": session_id,
            "organization_instance_id": organization_instance_id,
            "transcript_path": str(transcript_path),
            "response": result_text,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "duration_api_ms": duration_api_ms,
            "duration_sec": round(max(0.0, elapsed_seconds), 3),
            "num_turns": num_turns,
            "max_budget_usd": max_budget_usd,
            "budget_source": budget_source,
        }
    }


def agent_dispatch(*, runtime: str, state_root: Path, hook_input: dict[str, Any]) -> dict[str, Any]:
    session_id = str(
        hook_input.get("session_id")
        or hook_input.get("sessionId")
        or current_session_id(state_root, hook_input)
        or "unknown-session"
    )
    organization_instance_id = str(
        hook_input.get("organization_instance_id")
        or hook_input.get("organizationInstanceId")
        or organization_id(session_id)
    )
    agent_id = normalize_cell(hook_input.get("agent_id") or hook_input.get("agentId"))
    if not agent_id:
        return {"decision": "block", "reason": "agent-dispatch requires agent_id"}
    row = role_agent_row_for(agent_id, organization_instance_id=organization_instance_id)
    if not row:
        session_dir = state_root / safe_id(session_id)
        roster_path = session_dir / "roster.json"
        roster = read_json(roster_path) if roster_path.exists() else []
        if isinstance(roster, list):
            row = next((item for item in roster if isinstance(item, dict) and item.get("agent_id") == agent_id), {})
    provider_runtime = agent_runtime(row) if row else None
    if provider_runtime == ("codex_exec", "codex"):
        return codex_exec_agent_dispatch(runtime=runtime, state_root=state_root, hook_input=hook_input)
    if provider_runtime == ("claude_cli", "claude"):
        return claude_cli_agent_dispatch(runtime=runtime, state_root=state_root, hook_input=hook_input)
    return {"decision": "block", "reason": f"unsupported headless agent-dispatch provider for {agent_id}"}


def reset_response_evidence(
    row: dict[str, Any],
    now: str,
    note: str,
    usage_source: str = "claude_print_json_no_inference",
) -> None:
    always_active = row.get("always_active")
    if isinstance(always_active, bool):
        is_always_active = always_active
    else:
        is_always_active = bool_cell(str(always_active or "false"))
    row["activation_status"] = "metadata_ready" if is_always_active else "idle"
    row["response_status"] = "not_invoked"
    row["provider_status"] = "provider_no_inference"
    row["effective_model"] = ""
    row["session_id"] = ""
    row["last_request_id"] = ""
    row["usage_source"] = usage_source
    row["last_seen_at"] = now
    row["notes"] = note


def provider_response_ready_count(roster: list[Any]) -> int:
    return sum(1 for item in roster if isinstance(item, dict) and item.get("response_status") == "invoked")


def update_provider_response_state(state: dict[str, Any], roster: list[Any]) -> None:
    response_ready = provider_response_ready_count(roster)
    state["provider_response_ready_count"] = response_ready
    state["provider_response_scope"] = "response_evidence" if response_ready else "not_invoked"
    if response_ready == 0 and state.get("readiness_scope") == "response_evidence":
        state["readiness_scope"] = "metadata_only"


def parse_claude_json_output(stdout: str) -> dict[str, Any]:
    raw = stdout.strip()
    if not raw:
        return {}
    parsed = json.loads(raw)
    if isinstance(parsed, dict):
        return parsed
    return {"raw": parsed}


def provider_activate(*, runtime: str, state_root: Path, hook_input: dict[str, Any]) -> dict[str, Any]:
    session_id = current_session_id(state_root, hook_input) or "unknown-session"
    session_dir = state_root / safe_id(str(session_id))
    state_path = session_dir / "bootstrap.json"
    roster_path = session_dir / "roster.json"
    session_dir.mkdir(parents=True, exist_ok=True)
    now = current_timestamp()
    agent_id = str(hook_input.get("agent_id") or hook_input.get("agentId") or "gate-prompt-formatter")
    prompt = str(
        hook_input.get("prompt")
        or "Return a compact JSON object with keys agent_id, provider, and task_summary for ITB provider activation verification."
    )

    state_missing = not state_path.exists()
    roster_missing = not roster_path.exists()
    state = read_json(state_path) if not state_missing else {}
    if not isinstance(state, dict):
        return {"decision": "block", "reason": "bootstrap.json is not an object"}
    organization_instance_id = str(
        state.get("organization_instance_id")
        or hook_input.get("organization_instance_id")
        or hook_input.get("organizationInstanceId")
        or organization_id(str(session_id))
    )
    state.setdefault("runtime", runtime)
    state.setdefault("session_id", session_id)
    state.setdefault("organization_instance_id", organization_instance_id)
    state.setdefault("cwd", str(hook_input.get("cwd") or os.getcwd()))
    state.setdefault("bootstrap_status", "headless_metadata")
    state.setdefault("readiness_scope", "metadata_only")
    state.setdefault("outputs", {"state_dir": str(session_dir)})

    roster = read_json(roster_path) if not roster_missing else role_agent_rows(
        organization_instance_id=organization_instance_id,
    )
    if not isinstance(roster, list):
        return {"decision": "block", "reason": "roster.json is not a list"}

    row = next((item for item in roster if item.get("agent_id") == agent_id), None)
    if row is None:
        if roster_missing:
            row = role_agent_row_for(agent_id, organization_instance_id=organization_instance_id)
            if not row:
                return {"decision": "block", "reason": f"agent not found in registry: {agent_id}"}
            roster.append(row)
        else:
            return {"decision": "block", "reason": f"agent not found in roster: {agent_id}"}
    registry_row = registry_row_for(agent_id)
    if not row.get("fallback_models") and registry_row.get("fallback_models"):
        row["fallback_models"] = registry_row["fallback_models"]
    if state_missing:
        write_json_yaml(state_path, state)
    if roster_missing:
        write_json_yaml(roster_path, roster)
    max_budget_usd, budget_source = claude_activation_budget(row, hook_input)

    provider_runtime = agent_runtime(row)
    if provider_runtime == ("codex_exec", "codex"):
        if shutil.which("codex") is None:
            return {"decision": "block", "reason": "codex command not found"}
        cwd = str(hook_input.get("cwd") or state.get("cwd") or os.getcwd())
        command = codex_activation_command(row, prompt, cwd)
        started = time.monotonic()
        completed = subprocess.run(
            command,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=env_int("ITB_PROVIDER_ACTIVATION_TIMEOUT_SECONDS") or 120,
            check=False,
        )
        elapsed_ms = int((time.monotonic() - started) * 1000)
        if completed.returncode != 0:
            append_jsonl_atomic(
                session_dir / "invocation-evidence.jsonl",
                invocation_evidence_entry(
                    ts=now,
                    runtime=runtime,
                    event_type="provider_activation",
                    session_id=str(session_id),
                    organization_instance_id=state.get("organization_instance_id", organization_id(str(session_id))),
                    agent_id=agent_id,
                    result="provider_activation_failed",
                    usage_source="codex_exec_json",
                    effective_model=row.get("intended_model", ""),
                    notes=completed.stderr.strip() or completed.stdout.strip(),
                    duration_api_ms=elapsed_ms,
                ),
            )
            return {"decision": "block", "reason": completed.stderr.strip() or completed.stdout.strip()}

        try:
            codex_result = parse_codex_json_output(completed.stdout)
        except json.JSONDecodeError as exc:
            return {"decision": "block", "reason": f"codex json output unreadable: {exc}"}

        input_tokens = int_from_nested(
            codex_result,
            [("usage", "input_tokens"), ("usage", "inputTokens"), ("input_tokens",), ("inputTokens",)],
        )
        output_tokens = int_from_nested(
            codex_result,
            [("usage", "output_tokens"), ("usage", "outputTokens"), ("output_tokens",), ("outputTokens",)],
        )
        codex_duration_api_ms = int_from_nested(
            codex_result,
            [("duration_api_ms",), ("durationApiMs",), ("metrics", "duration_api_ms")],
        )
        duration_api_ms = codex_duration_api_ms if codex_duration_api_ms is not None else elapsed_ms
        provider_session_id = str_from_nested(codex_result, [("session_id",), ("sessionId",)])
        request_id = str_from_nested(codex_result, [("request_id",), ("requestId",)])
        effective_model = (
            str_from_nested(codex_result, [("model",), ("effective_model",), ("effectiveModel",)])
            or row.get("intended_model", "")
        )
        result_text = str(codex_result.get("result") or codex_result.get("message") or "")
        num_turns = int_from_nested(codex_result, [("num_turns",), ("numTurns",)])
        has_inference_evidence = bool(
            result_text.strip()
            or (input_tokens is not None and input_tokens > 0)
            or (output_tokens is not None and output_tokens > 0)
            or (codex_duration_api_ms is not None and codex_duration_api_ms > 0)
            or (num_turns is not None and num_turns > 0)
        )
        if not has_inference_evidence:
            reset_response_evidence(
                row,
                now,
                "Codex exec returned no inference evidence; previous response evidence, if any, was invalidated.",
                usage_source="codex_exec_json_no_inference",
            )
            update_provider_response_state(state, roster)
            state["last_provider_activation_agent"] = agent_id
            state["last_provider_activation_at"] = now
            state["last_provider_activation_usage_source"] = "codex_exec_json_no_inference"
            write_json_yaml(roster_path, roster)
            write_json_yaml(state_path, state)
            append_jsonl(
                session_dir / "invocation-evidence.jsonl",
                invocation_evidence_entry(
                    ts=now,
                    runtime=runtime,
                    event_type="provider_activation",
                    session_id=str(session_id),
                    organization_instance_id=state.get("organization_instance_id", organization_id(str(session_id))),
                    agent_id=agent_id,
                    result="provider_activation_no_inference",
                    usage_source="codex_exec_json",
                    effective_model=effective_model,
                    request_id=request_id or "unavailable",
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    duration_api_ms=duration_api_ms,
                    num_turns=num_turns,
                    notes="Codex exec returned success but no result, tokens, turns, or API duration.",
                    extra={
                        "provider_session_id": provider_session_id,
                        "stdout_result_present": bool(result_text),
                    },
                ),
            )
            return {
                "decision": "block",
                "reason": "codex provider activation produced no inference evidence",
            }

        row["activation_status"] = "response_active"
        row["response_status"] = "invoked"
        row["provider_status"] = "provider_response_ready"
        row["effective_model"] = effective_model
        row["session_id"] = provider_session_id
        row["last_request_id"] = request_id
        row["usage_source"] = "codex_exec_json"
        row["last_seen_at"] = now
        row["notes"] = "Codex exec provider activation produced runtime response evidence."

        update_provider_response_state(state, roster)
        state["readiness_scope"] = "response_evidence"
        state["last_provider_activation_agent"] = agent_id
        state["last_provider_activation_at"] = now
        state["last_provider_activation_usage_source"] = "codex_exec_json"

        write_json_yaml(roster_path, roster)
        write_json_yaml(state_path, state)
        append_jsonl(
            session_dir / "invocation-evidence.jsonl",
            invocation_evidence_entry(
                ts=now,
                runtime=runtime,
                event_type="provider_activation",
                session_id=str(session_id),
                organization_instance_id=state.get("organization_instance_id", organization_id(str(session_id))),
                agent_id=agent_id,
                result="provider_response_ready",
                usage_source="codex_exec_json",
                effective_model=effective_model,
                request_id=request_id or "unavailable",
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                duration_api_ms=duration_api_ms,
                num_turns=num_turns,
                notes="Codex exec activation completed and usage evidence was recorded.",
                extra={
                    "provider_session_id": provider_session_id,
                    "stdout_result_present": bool(result_text),
                },
            ),
        )
        return {
            "hookSpecificOutput": {
                "hookEventName": "ProviderActivation",
                "additionalContext": f"Codex provider activation complete for `{agent_id}` with `{effective_model}`.",
            },
            "activation": {
                "agent_id": agent_id,
                "provider": "openai",
                "effective_model": effective_model,
                "session_id": provider_session_id,
                "request_id": request_id,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "usage_source": "codex_exec_json",
            },
        }
    if provider_runtime != ("claude_cli", "claude"):
        return {
            "decision": "block",
            "reason": f"agent is not routed to Claude provider: {agent_id} provider={row.get('provider', '')}",
        }
    if shutil.which("claude") is None:
        return {"decision": "block", "reason": "claude command not found"}

    command = claude_activation_command(row, prompt, max_budget_usd)
    started = time.monotonic()
    completed = subprocess.run(
        command,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=env_int("ITB_PROVIDER_ACTIVATION_TIMEOUT_SECONDS") or 120,
        check=False,
    )
    elapsed_ms = int((time.monotonic() - started) * 1000)
    if completed.returncode != 0:
        append_jsonl(
            session_dir / "invocation-evidence.jsonl",
            invocation_evidence_entry(
                ts=now,
                runtime=runtime,
                event_type="provider_activation",
                session_id=str(session_id),
                organization_instance_id=state.get("organization_instance_id", organization_id(str(session_id))),
                agent_id=agent_id,
                result="provider_activation_failed",
                usage_source="claude_print_json",
                effective_model=row.get("intended_model", ""),
                notes=completed.stderr.strip() or completed.stdout.strip(),
                duration_api_ms=elapsed_ms,
                extra={
                    "max_budget_usd": max_budget_usd,
                    "budget_source": budget_source,
                },
            ),
        )
        return {"decision": "block", "reason": completed.stderr.strip() or completed.stdout.strip()}

    try:
        claude_result = parse_claude_json_output(completed.stdout)
    except json.JSONDecodeError as exc:
        return {"decision": "block", "reason": f"claude json output unreadable: {exc}"}

    input_tokens = int_from_nested(
        claude_result,
        [("usage", "input_tokens"), ("usage", "inputTokens"), ("input_tokens",), ("inputTokens",)],
    )
    output_tokens = int_from_nested(
        claude_result,
        [("usage", "output_tokens"), ("usage", "outputTokens"), ("output_tokens",), ("outputTokens",)],
    )
    claude_duration_api_ms = int_from_nested(
        claude_result,
        [("duration_api_ms",), ("durationApiMs",), ("metrics", "duration_api_ms")],
    )
    duration_api_ms = claude_duration_api_ms if claude_duration_api_ms is not None else elapsed_ms
    provider_session_id = str_from_nested(claude_result, [("session_id",), ("sessionId",)])
    request_id = str_from_nested(claude_result, [("request_id",), ("requestId",)])
    effective_model = str_from_nested(claude_result, [("model",), ("effective_model",), ("effectiveModel",)]) or row.get("intended_model", "")
    result_text = str(claude_result.get("result") or claude_result.get("message") or "")
    num_turns = int_from_nested(claude_result, [("num_turns",), ("numTurns",)])
    has_inference_evidence = bool(
        result_text.strip()
        or (input_tokens is not None and input_tokens > 0)
        or (output_tokens is not None and output_tokens > 0)
        or (claude_duration_api_ms is not None and claude_duration_api_ms > 0)
        or (num_turns is not None and num_turns > 0)
    )
    if not has_inference_evidence:
        reset_response_evidence(
            row,
            now,
            "Claude --print returned no inference evidence; previous response evidence, if any, was invalidated.",
        )
        update_provider_response_state(state, roster)
        state["last_provider_activation_agent"] = agent_id
        state["last_provider_activation_at"] = now
        state["last_provider_activation_usage_source"] = "claude_print_json_no_inference"
        write_json_yaml(roster_path, roster)
        write_json_yaml(state_path, state)
        append_jsonl(
            session_dir / "invocation-evidence.jsonl",
            invocation_evidence_entry(
                ts=now,
                runtime=runtime,
                event_type="provider_activation",
                session_id=str(session_id),
                organization_instance_id=state.get("organization_instance_id", organization_id(str(session_id))),
                agent_id=agent_id,
                result="provider_activation_no_inference",
                usage_source="claude_print_json",
                effective_model=effective_model,
                request_id=request_id or "unavailable",
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                duration_api_ms=duration_api_ms,
                num_turns=num_turns,
                notes="Claude --print returned success but no result, tokens, turns, or API duration.",
                extra={
                "provider_session_id": provider_session_id,
                "total_cost_usd": claude_result.get("total_cost_usd", claude_result.get("totalCostUsd")),
                "max_budget_usd": max_budget_usd,
                "budget_source": budget_source,
            },
        ),
    )
        return {
            "decision": "block",
            "reason": "claude provider activation produced no inference evidence",
        }

    row["activation_status"] = "response_active"
    row["response_status"] = "invoked"
    row["provider_status"] = "provider_response_ready"
    row["effective_model"] = effective_model
    row["session_id"] = provider_session_id
    row["last_request_id"] = request_id
    row["usage_source"] = "claude_print_json"
    row["last_seen_at"] = now
    row["notes"] = "Claude provider activation produced runtime response evidence."

    update_provider_response_state(state, roster)
    state["readiness_scope"] = "response_evidence"
    state["last_provider_activation_agent"] = agent_id
    state["last_provider_activation_at"] = now
    state["last_provider_activation_usage_source"] = "claude_print_json"

    write_json_yaml(roster_path, roster)
    write_json_yaml(state_path, state)
    append_jsonl_atomic(
        session_dir / "invocation-evidence.jsonl",
        invocation_evidence_entry(
            ts=now,
            runtime=runtime,
            event_type="provider_activation",
            session_id=str(session_id),
            organization_instance_id=state.get("organization_instance_id", organization_id(str(session_id))),
            agent_id=agent_id,
            result="provider_response_ready",
            usage_source="claude_print_json",
            effective_model=effective_model,
            request_id=request_id or "unavailable",
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            duration_api_ms=duration_api_ms,
            num_turns=num_turns,
            notes="Claude --print activation completed and usage evidence was recorded.",
            extra={
                "provider_session_id": provider_session_id,
                "total_cost_usd": claude_result.get("total_cost_usd", claude_result.get("totalCostUsd")),
                "stdout_result_present": bool(result_text),
                "max_budget_usd": max_budget_usd,
                "budget_source": budget_source,
            },
        ),
    )
    return {
        "hookSpecificOutput": {
            "hookEventName": "ProviderActivation",
            "additionalContext": f"Claude provider activation complete for `{agent_id}` with `{effective_model}`.",
        },
        "activation": {
            "agent_id": agent_id,
            "provider": "anthropic",
            "effective_model": effective_model,
            "session_id": provider_session_id,
            "request_id": request_id,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "usage_source": "claude_print_json",
            "max_budget_usd": max_budget_usd,
            "budget_source": budget_source,
        },
    }



























def invocation_evidence_entry(
    *,
    ts: str,
    runtime: str,
    event_type: str,
    session_id: str,
    organization_instance_id: str,
    result: str,
    usage_source: str,
    notes: str,
    agent_id: str = "__organization__",
    effective_model: str = "unavailable",
    request_id: str = "unavailable",
    input_tokens: int | None = None,
    output_tokens: int | None = None,
    duration_api_ms: int | None = None,
    num_turns: int | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    entry = {
        "ts": ts,
        "runtime": runtime,
        "event_type": event_type,
        "agent_id": agent_id,
        "organization_instance_id": organization_instance_id,
        "session_id": session_id,
        "request_id": request_id,
        "result": result,
        "effective_model": effective_model,
        "usage_source": usage_source,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "duration_api_ms": duration_api_ms,
        "num_turns": num_turns,
        "notes": notes,
    }
    if extra:
        entry.update(extra)
    return entry


def markdown_table_cells(line: str) -> list[str]:
    return [cell.strip().strip("`") for cell in line.strip().strip("|").split("|")]


def extract_invocation_evidence_rows(task_detail_path: Path) -> list[dict[str, str]]:
    lines = task_detail_path.read_text(encoding="utf-8").splitlines()
    in_section = False
    table_lines: list[str] = []
    for line in lines:
        if line.startswith("## "):
            if in_section:
                break
            in_section = line.strip() == "## Invocation Evidence"
            continue
        if in_section and line.startswith("|"):
            table_lines.append(line)

    headers: list[str] | None = None
    rows: list[dict[str, str]] = []
    for line in table_lines:
        cells = markdown_table_cells(line)
        if not cells:
            continue
        if set("".join(cells)) <= {"-"}:
            continue
        if headers is None:
            headers = cells
            continue
        if len(cells) != len(headers):
            continue
        rows.append(dict(zip(headers, cells)))
    return rows


def validate_required_task_invocations(
    task_detail_path: Path,
    *,
    required_agents: tuple[str, ...],
) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []
    try:
        rows = extract_invocation_evidence_rows(task_detail_path)
    except (OSError, UnicodeDecodeError) as exc:
        return [f"task detail unreadable for Invocation Evidence: {exc}"], warnings

    if not rows:
        return ["Invocation Evidence table missing required role evidence"], warnings

    skipped_results = {"", "not_invoked", "not_started", "pending", "blocked", "unavailable"}
    rows_by_agent = {row.get("Agent", "").strip(): row for row in rows if row.get("Agent", "").strip()}
    for agent_id in required_agents:
        row = rows_by_agent.get(agent_id)
        if row is None:
            errors.append(f"Invocation Evidence missing required agent: {agent_id}")
            continue
        result = evidence_result_status(row.get("Result", ""))
        if result in skipped_results:
            errors.append(f"Invocation Evidence required agent not invoked: {agent_id} result={result or '<empty>'}")
        if not row.get("Effective Model", "").strip():
            errors.append(f"Invocation Evidence required agent missing effective model: {agent_id}")
        if not row.get("Usage Source", "").strip():
            errors.append(f"Invocation Evidence required agent missing usage source: {agent_id}")
    return errors, warnings


def load_provider_activation_log(evidence_path: Path) -> tuple[list[dict[str, Any]], list[str]]:
    if not evidence_path.exists():
        return [], [f"provider evidence log missing: {evidence_path}"]

    entries: list[dict[str, Any]] = []
    errors: list[str] = []
    for lineno, line in enumerate(evidence_path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError as exc:
            errors.append(f"{evidence_path}:{lineno}: provider evidence JSON unreadable: {exc}")
            continue
        if isinstance(parsed, dict):
            entries.append(parsed)
        else:
            errors.append(f"{evidence_path}:{lineno}: provider evidence entry is not an object")
    return entries, errors


def matching_provider_activation(row: dict[str, str], provider_entries: list[dict[str, Any]]) -> dict[str, Any] | None:
    agent_id = row.get("Agent", "").strip()
    effective_model = row.get("Effective Model", "").strip()
    usage_source = row.get("Usage Source", "").strip()
    session_id = row.get("Session ID", "").strip()
    request_id = row.get("Request ID", "").strip()
    request_id_required = request_id.lower() not in {"", "n/a", "na", "none", "unavailable"}
    for entry in provider_entries:
        if entry.get("event_type") not in {"provider_activation", "agent_dispatch"}:
            continue
        if entry.get("result") != "provider_response_ready":
            continue
        if str(entry.get("agent_id", "")).strip() != agent_id:
            continue
        if request_id_required and str(entry.get("request_id", "")).strip() != request_id:
            continue
        if effective_model and str(entry.get("effective_model", "")).strip() != effective_model:
            continue
        if usage_source and str(entry.get("usage_source", "")).strip() != usage_source:
            continue
        provider_session_id = str(entry.get("provider_session_id") or entry.get("session_id") or "").strip()
        if session_id and provider_session_id != session_id:
            continue
        return entry
    return None


def validate_task_detail_provider_evidence(
    task_detail_value: str,
    provider_evidence_log_path: Path | None = None,
) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []
    if not task_detail_value:
        return errors, warnings

    task_detail_path = Path(task_detail_value).expanduser()
    if not task_detail_path.exists():
        return [f"task detail path not found: {task_detail_value}"], warnings

    try:
        rows = extract_invocation_evidence_rows(task_detail_path)
    except (OSError, UnicodeDecodeError) as exc:
        return [f"task detail unreadable: {exc}"], warnings

    if not rows:
        warnings.append(f"task detail has no Invocation Evidence table: {task_detail_path}")
        return errors, warnings

    micro_flow_local_allowed, micro_flow_policy_errors = controlled_micro_flow_policy_allows_local_evidence(
        task_detail_path
    )
    registry_by_agent = {row["agent_id"]: row for row in parse_registry()}
    skipped_results = {"", "not_invoked", "not_started", "pending", "blocked", "unavailable"}
    provider_entries: list[dict[str, Any]] = []
    if provider_evidence_log_path is not None:
        provider_entries, provider_log_errors = load_provider_activation_log(provider_evidence_log_path)
        errors.extend(provider_log_errors)
    for row in rows:
        agent_id = row.get("Agent", "").strip()
        result = evidence_result_status(row.get("Result", ""))
        if not agent_id or result in skipped_results:
            continue

        if agent_id == "gate-task-creator" and normalize_cell(row.get("Usage Source")) == "builder_command":
            continue

        if is_local_controlled_micro_flow_evidence(row):
            if micro_flow_local_allowed:
                continue
            errors.extend(
                f"{task_detail_path}: local controlled micro-flow evidence is not allowed: {error}"
                for error in micro_flow_policy_errors
            )
            continue

        registry_row = registry_by_agent.get(agent_id, {})
        provider = registry_row.get("provider", "")
        intended_model = row.get("Intended Model", "").strip()
        if not intended_model or intended_model == "registry":
            intended_model = registry_row.get("primary_model", intended_model)

        effective_model = row.get("Effective Model", "")
        evidence_errors = validate_provider_evidence(
            agent_id=agent_id,
            provider=provider,
            intended_model=intended_model,
            effective_model=effective_model,
            usage_source=row.get("Usage Source", ""),
        )
        errors.extend(f"{task_detail_path}: {error}" for error in evidence_errors)
        tier_warning = model_tier_mismatch_warning(agent_id, intended_model, effective_model)
        if tier_warning:
            warnings.append(f"{task_detail_path}: {tier_warning}")

        if provider_evidence_log_path is not None and matching_provider_activation(row, provider_entries) is None:
            session_id = row.get("Session ID", "").strip() or "<empty>"
            errors.append(
                f"{task_detail_path}: Invocation Evidence has no matching provider transcript: "
                f"{agent_id} session_id={session_id}"
            )

    return errors, warnings


def validate_gate_role_transport_evidence(
    task_detail_value: str,
    provider_evidence_log_path: Path | None = None,
) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []
    gate_agents = {"gate-prompt-formatter", "gate-task-creator", "teams-project-manager"}
    if not task_detail_value:
        return errors, warnings

    task_detail_path = Path(task_detail_value).expanduser()
    if not task_detail_path.exists():
        return [f"task detail path not found: {task_detail_value}"], warnings

    try:
        rows = extract_invocation_evidence_rows(task_detail_path)
    except (OSError, UnicodeDecodeError) as exc:
        return [f"task detail unreadable: {exc}"], warnings

    micro_flow_local_allowed, micro_flow_policy_errors = controlled_micro_flow_policy_allows_local_evidence(
        task_detail_path
    )
    registry_by_agent = {row["agent_id"]: row for row in parse_registry()}
    skipped_results = {"", "not_invoked", "not_started", "pending", "blocked", "unavailable"}
    failed_results = {
        "provider_response_timeout",
        "provider_activation_failed",
        "provider_process_failed",
        "provider_activation_no_inference",
        "provider_request_sent",
        "launch_failed",
        "failed",
        "failure",
        "error",
        "rejected",
        "timeout",
    }
    provider_entries: list[dict[str, Any]] = []
    if provider_evidence_log_path is not None:
        provider_entries, provider_log_errors = load_provider_activation_log(provider_evidence_log_path)
        errors.extend(provider_log_errors)
    for row in rows:
        agent_id = row.get("Agent", "").strip()
        if agent_id not in gate_agents:
            continue
        if agent_id == "gate-task-creator" and normalize_cell(row.get("Usage Source")) == "builder_command":
            continue
        result = evidence_result_status(row.get("Result", ""))
        if result in skipped_results:
            continue
        if is_local_controlled_micro_flow_evidence(row):
            if micro_flow_local_allowed:
                continue
            errors.extend(
                f"{task_detail_path}: local controlled micro-flow evidence is not allowed: {error}"
                for error in micro_flow_policy_errors
            )
            continue
        registry_row = registry_by_agent.get(agent_id, {})
        provider = registry_row.get("provider", "")
        intended_model = row.get("Intended Model", "").strip()
        if not intended_model or intended_model == "registry":
            intended_model = registry_row.get("primary_model", intended_model)
        evidence_errors = validate_provider_evidence(
            agent_id=agent_id,
            provider=provider,
            intended_model=intended_model,
            effective_model=row.get("Effective Model", ""),
            usage_source=row.get("Usage Source", ""),
        )
        errors.extend(f"{task_detail_path}: {error}" for error in evidence_errors)
        if (
            provider_evidence_log_path is not None
            and result not in failed_results
            and matching_provider_activation(row, provider_entries) is None
        ):
            session_id = row.get("Session ID", "").strip() or "<empty>"
            errors.append(
                f"{task_detail_path}: Gate role evidence has no matching provider transcript: "
                f"{agent_id} session_id={session_id}"
            )

    if provider_evidence_log_path is not None:
        rows_by_gate_agent: dict[str, list[dict[str, str]]] = {}
        for row in rows:
            agent_id = row.get("Agent", "").strip()
            result = evidence_result_status(row.get("Result", ""))
            if agent_id in gate_agents and result not in skipped_results:
                rows_by_gate_agent.setdefault(agent_id, []).append(row)

        for agent_id, agent_rows in rows_by_gate_agent.items():
            latest_row = agent_rows[-1]
            latest_result = evidence_result_status(latest_row.get("Result", ""))
            if latest_result not in failed_results:
                continue
            errors.append(
                f"{task_detail_path}: Gate role latest evidence is failed or incomplete without later "
                f"successful provider transcript: {agent_id} result={latest_result or '<empty>'}"
            )

    return errors, warnings


def validate_preflight_state(session_dir: Path, state: dict[str, Any]) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []
    roster_path = session_dir / "roster.json"
    evidence_path = session_dir / "invocation-evidence.jsonl"

    if not roster_path.exists():
        errors.append("roster.json missing")
        roster: list[dict[str, Any]] = []
    else:
        try:
            roster = read_json(roster_path)
            if not isinstance(roster, list):
                errors.append("roster.json is not a list")
                roster = []
        except (OSError, json.JSONDecodeError) as exc:
            errors.append(f"roster.json unreadable: {exc}")
            roster = []

    registry_sha1 = state.get("model_registry_sha1")
    if registry_sha1 and MODEL_REGISTRY.exists() and registry_sha1 != file_sha1(MODEL_REGISTRY):
        warnings.append("model registry hash changed since bootstrap")

    if not evidence_path.exists():
        warnings.append("invocation-evidence.jsonl missing; legacy metadata-only state")

    # Metadata-only SessionStart never proves provider response availability. Activation
    # code may mark response_active only after provider evidence is recorded.
    for row in roster:
        status = row.get("activation_status", "")
        usage_source = row.get("usage_source", "")
        has_response_evidence = bool(
            row.get("effective_model")
            and usage_source
            and usage_source != "bootstrap_metadata_only"
        )
        if status == "response_active" and not has_response_evidence:
            errors.append(f"{row.get('agent_id', '<unknown>')}: response_active without runtime evidence")
        if status == "response_active" and has_response_evidence:
            errors.extend(validate_provider_evidence(
                agent_id=row.get("agent_id", "<unknown>"),
                provider=row.get("provider", ""),
                intended_model=row.get("intended_model", ""),
                effective_model=row.get("effective_model", ""),
                usage_source=usage_source,
            ))
            tier_warning = model_tier_mismatch_warning(
                row.get("agent_id", "<unknown>"),
                row.get("intended_model", ""),
                row.get("effective_model", ""),
            )
            if tier_warning:
                warnings.append(tier_warning)
        if status == "active" and not has_response_evidence:
            warnings.append(f"{row.get('agent_id', '<unknown>')}: legacy active without runtime evidence")

    return errors, warnings


def process_is_running(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def process_command_line(pid: int) -> str:
    if pid <= 0 or shutil.which("ps") is None:
        return ""
    try:
        completed = subprocess.run(
            ["ps", "-p", str(pid), "-o", "command="],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=2,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    if completed.returncode != 0:
        return ""
    return completed.stdout.strip()


def command_line_matches_session_daemon(command_line: str, command_name: str) -> bool:
    normalized = " ".join(command_line.split())
    if not normalized:
        return False
    return "itb_bootstrap_builder.py" in normalized and f" {command_name}" in normalized







def session_start_config_digest() -> str:
    hasher = hashlib.sha256()
    for path in (
        Path(__file__).resolve(),
        HOOK_BUNDLE_DIR / "codex-hooks.example.json",
        HOOK_BUNDLE_DIR / "claude-settings-hooks.example.json",
        SAIHAI_ROOT / "organization" / "settings.json",
    ):
        hasher.update(str(path).encode("utf-8"))
        hasher.update(b"\0")
        try:
            hasher.update(path.read_bytes())
        except OSError as exc:
            hasher.update(f"{type(exc).__name__}:{exc}".encode("utf-8", errors="replace"))
        hasher.update(b"\0")
    return "sha256:" + hasher.hexdigest()


def session_start_metadata_pointer(
    *,
    runtime: str,
    state_root: Path,
    hook_input: dict[str, Any],
) -> dict[str, Any]:
    session_id = normalize_cell(
        hook_input.get("session_id")
        or hook_input.get("sessionId")
        or hook_input.get("conversation_id")
        or "unknown-session"
    )
    cwd = normalize_cell(hook_input.get("cwd") or os.getcwd())
    session_dir = state_root / safe_id(session_id)
    session_dir.mkdir(parents=True, exist_ok=True)
    state_root.mkdir(parents=True, exist_ok=True)
    pointer_path = session_dir / "active-execution-context.json"
    metadata = {
        "session_id": session_id,
        "runtime": runtime,
        "cwd": cwd,
        "started_at": current_timestamp(),
        "harness_config_digest": session_start_config_digest(),
        "active_execution_context": None,
        "active_execution_context_pointer_path": str(pointer_path),
    }
    write_json_yaml(pointer_path, metadata)
    atomic_write_text(state_root / "last-session", session_id + "\n")
    return metadata


def session_start_metadata_output(
    *,
    runtime: str,
    state_root: Path,
    hook_input: dict[str, Any],
) -> dict[str, Any]:
    metadata = session_start_metadata_pointer(runtime=runtime, state_root=state_root, hook_input=hook_input)
    return {
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
        },
        "sessionStartMetadata": metadata,
    }


def session_metadata_pointer_ready(session_dir: Path, session_id: str) -> bool:
    pointer_path = session_dir / "active-execution-context.json"
    if not pointer_path.exists():
        return False
    try:
        pointer = read_json(pointer_path)
    except (OSError, json.JSONDecodeError):
        return False
    if not isinstance(pointer, dict):
        return False
    pointer_session_id = normalize_cell(pointer.get("session_id") or pointer.get("sessionId"))
    pointer_path_value = normalize_cell(
        pointer.get("active_execution_context_pointer_path")
        or pointer.get("activeExecutionContextPointerPath")
    )
    return (not pointer_session_id or pointer_session_id == session_id) and bool(pointer_path_value)





def stable_preflight_queue_fingerprint(event: dict[str, Any]) -> dict[str, Any]:
    if not event:
        return {}
    return {
        "role_id": normalize_cell(event.get("role_id")),
        "result": normalize_cell(event.get("result")),
        "nudge_result": normalize_cell(event.get("nudge_result")),
    }


def stable_preflight_dispatch_fingerprint(event: dict[str, Any]) -> dict[str, Any]:
    if not event:
        return {}
    queue_finalize = event.get("queue_finalize") if isinstance(event.get("queue_finalize"), dict) else {}
    gtc_queue = event.get("gate_task_creator_queue") if isinstance(event.get("gate_task_creator_queue"), dict) else {}
    auto_scaffold = event.get("gate_entry_auto_scaffold") if isinstance(event.get("gate_entry_auto_scaffold"), dict) else {}
    return {
        "agent_id": normalize_cell(event.get("agent_id")),
        "result": normalize_cell(event.get("result")),
        "effective_model": normalize_cell(event.get("effective_model")),
        "usage_source": normalize_cell(event.get("usage_source")),
        "dispatch_mode": normalize_cell(event.get("dispatch_mode")),
        "response_repaired": bool(event.get("response_repaired")),
        "response_validation_errors": list(event.get("response_validation_errors") or []),
        "queue_finalize_result": normalize_cell(queue_finalize.get("result")),
        "queue_finalize_status": normalize_cell(queue_finalize.get("status")),
        "gate_task_creator_queue_result": normalize_cell(gtc_queue.get("result")),
        "gate_entry_auto_scaffold_result": normalize_cell(auto_scaffold.get("result")),
        "gate_entry_auto_scaffold_task_id": normalize_cell(auto_scaffold.get("command_task_id")),
    }


def active_task_output(*, runtime: str, state_root: Path, hook_input: dict[str, Any]) -> dict[str, Any]:
    session_id = current_session_id(state_root, hook_input)
    if not session_id:
        session_id = "unknown-session"
    session_id = str(session_id)
    session_dir = state_root / safe_id(session_id)
    session_dir.mkdir(parents=True, exist_ok=True)
    state_path = session_dir / "bootstrap.json"
    status_path = session_dir / "status"
    status = status_path.read_text(encoding="utf-8").strip() if status_path.exists() else "missing"
    now = current_timestamp()
    action = normalize_cell(hook_input.get("action") or "set").lower()
    path = active_task_path(session_dir)

    if action in {"clear", "close", "unset"}:
        existed = path.exists()
        if existed:
            path.unlink()
        event = {
            "ts": now,
            "runtime": runtime,
            "event_type": "active_task",
            "session_id": session_id,
            "action": "clear",
            "result": "active_task_cleared",
            "active_task_existed": existed,
        }
        append_jsonl_atomic(session_dir / "active-task-events.jsonl", event)
        return {"activeTask": event | {"active_task_path": str(path)}}

    if action not in {"set", "update"}:
        reason = f"unsupported active-task action: {action}"
        return {"decision": "block", "reason": reason, "activeTask": {"status": "blocked", "reason": reason}}

    errors: list[str] = []
    bootstrap_ready = status == "ready" and state_path.exists()
    metadata_pointer_ready = session_metadata_pointer_ready(session_dir, session_id)
    if not bootstrap_ready and not metadata_pointer_ready:
        errors.append(
            "ITB session metadata is not ready for active task registration: "
            f"status={status}, metadata_pointer_ready={str(metadata_pointer_ready).lower()}"
        )

    task_detail_path = hook_task_detail_path(hook_input)
    if task_detail_path is None:
        errors.append("active-task registration requires task_detail_path")
    elif not task_detail_path.exists():
        errors.append(f"task_detail_path does not exist: {task_detail_path}")
    flow_phase = hook_flow_phase(hook_input)
    flow_phase_error = flow_phase_validation_error(hook_flow_phase_raw(hook_input))
    if flow_phase_error:
        errors.append(flow_phase_error)
    final_validation_warnings: list[str] = []
    if task_detail_path is not None and task_detail_path.exists() and flow_phase == "pre_final_response":
        optional_pre_final_sections: list[str] = []
        optional_pre_final_sections.extend(normalize_string_list(hook_input.get("optional_pre_final_sections")))
        optional_pre_final_sections.extend(normalize_string_list(hook_input.get("optionalPreFinalSections")))
        if truthy_input(
            hook_input.get("allow_missing_final_transport_render_check")
            or hook_input.get("allowMissingFinalTransportRenderCheck")
        ):
            optional_pre_final_sections.append("Final Transport Render Check")
        final_task_errors, final_task_warnings = validate_task_flow_artifact(
            task_detail_path,
            flow_phase,
            optional_pre_final_sections=tuple(optional_pre_final_sections),
        )
        final_provider_errors, final_provider_warnings = validate_task_detail_provider_evidence(
            str(task_detail_path),
            session_dir / "invocation-evidence.jsonl",
        )
        errors.extend(final_task_errors)
        errors.extend(final_provider_errors)
        final_validation_warnings.extend(final_task_warnings)
        final_validation_warnings.extend(final_provider_warnings)

    if errors:
        event = {
            "ts": now,
            "runtime": runtime,
            "event_type": "active_task",
            "session_id": session_id,
            "action": action,
            "result": "active_task_blocked",
            "errors": errors,
        }
        if final_validation_warnings:
            event["warnings"] = final_validation_warnings
        append_jsonl_atomic(session_dir / "active-task-events.jsonl", event)
        reason = "; ".join(errors)
        return {"decision": "block", "reason": reason, "activeTask": event | {"active_task_path": str(path)}}

    assert task_detail_path is not None
    task_id = normalize_cell(hook_input.get("task_id") or hook_input.get("taskId"))
    if not task_id:
        task_id = task_detail_path.parent.name.split("-", 1)[0]
    record = {
        "status": "active",
        "ts": now,
        "runtime": runtime,
        "session_id": session_id,
        "task_id": task_id,
        "task_detail_path": str(task_detail_path),
        "flow_phase": flow_phase,
        "owner_role": normalize_cell(hook_input.get("owner_role") or hook_input.get("ownerRole")),
        "last_gate": normalize_cell(hook_input.get("last_gate") or hook_input.get("lastGate")),
        "source": normalize_cell(hook_input.get("source") or "active-task"),
    }
    write_json_yaml(path, record)
    append_jsonl_atomic(
        session_dir / "active-task-events.jsonl",
        record
        | {
            "event_type": "active_task",
            "action": action,
            "result": "active_task_set",
            "active_task_path": str(path),
        },
    )
    return {"activeTask": record | {"active_task_path": str(path), "result": "active_task_set"}}


GATE_PRECHECK_DEFAULT_PHASES = {
    "team-completion-check": "post_routing",
    "gate-task-assessor": "post_routing",
    "finalization-check": "pre_final_response",
    "gate-task-guardian": "pre_final_response",
}


def gate_precheck_output(
    *,
    runtime: str,
    state_root: Path,
    hook_input: dict[str, Any],
    gate_role: str,
) -> dict[str, Any]:
    session_id, session_source = resolve_session_id(state_root, hook_input)
    if not session_id:
        session_id = "unknown-session"
    session_dir = state_root / safe_id(session_id)
    session_dir.mkdir(parents=True, exist_ok=True)
    now = current_timestamp()
    default_phase = GATE_PRECHECK_DEFAULT_PHASES[gate_role]
    raw_phase = hook_flow_phase_raw(hook_input)
    flow_phase = normalize_flow_phase(raw_phase, default=default_phase)
    auto_final_transport_render_check = truthy_input(
        hook_input.get("auto_final_transport_render_check")
        or hook_input.get("autoFinalTransportRenderCheck")
        or hook_input.get("run_final_transport_render_check")
        or hook_input.get("runFinalTransportRenderCheck")
    )
    errors: list[str] = []
    warnings: list[str] = []
    task_detail_text = ""
    phase_error = flow_phase_validation_error(raw_phase)
    if phase_error:
        errors.append(phase_error)

    task_detail_path = hook_task_detail_path(hook_input)
    if task_detail_path is None:
        errors.append("gate precheck requires task_detail_path")
    elif not task_detail_path.exists():
        errors.append(f"task_detail_path does not exist: {task_detail_path}")
    else:
        try:
            task_detail_text = task_detail_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            errors.append(f"task detail unreadable: {exc}")
            task_detail_text = ""
        optional_pre_final_sections = (
            ("Final Transport Render Check",)
            if gate_role == "finalization-check" and auto_final_transport_render_check
            else ()
        )
        task_errors, task_warnings = validate_task_flow_artifact(
            task_detail_path,
            flow_phase,
            optional_pre_final_sections=optional_pre_final_sections,
        )
        errors.extend(task_errors)
        warnings.extend(task_warnings)
        if gate_role == "team-completion-check":
            team_completion_section = markdown_section(task_detail_text, "Team Completion Check")
            if not team_completion_section:
                errors.append("Team Completion Check missing for team-completion-check")
            else:
                errors.extend(
                    validate_gate_output_section_schema(
                        "Team Completion Check",
                        key_value_table(team_completion_section),
                    )
                )
        if gate_role in {"finalization-check", "gate-task-guardian"} and flow_phase == "pre_final_response":
            provider_errors, provider_warnings = validate_task_detail_provider_evidence(
                str(task_detail_path),
                session_dir / "invocation-evidence.jsonl",
            )
            errors.extend(provider_errors)
            warnings.extend(provider_warnings)

    precheck_status = "block" if errors else "pass"
    event = {
        "ts": now,
        "runtime": runtime,
        "event_type": "gate_precheck",
        "session_id": session_id,
        "session_source": session_source,
        "gate_role": gate_role,
        "flow_phase": flow_phase,
        "task_detail_path": str(task_detail_path) if task_detail_path else "",
        "precheck_status": precheck_status,
        "result": "gate_precheck_blocked" if errors else "gate_precheck_passed",
        "validation_errors": errors,
        "validation_warnings": warnings,
        "machine_verdict": "blocked_by_builder_precheck" if errors else "ready_for_gate_verdict",
        "llm_scope": "blocked_until_artifacts_fixed" if errors else "verdict_reason_only",
        "notification_class": notification_class_for_event(
            event_type="gate_precheck",
            result="gate_precheck_blocked" if errors else "gate_precheck_passed",
            status=precheck_status,
            errors=errors,
        ),
    }
    task_id = task_detail_compact_task_id(
        hook_input=hook_input,
        task_detail_path=task_detail_path,
        task_detail_text=task_detail_text,
    )
    gate_command: dict[str, Any] | None = None
    if gate_role == "team-completion-check":
        gate_command = team_completion_command_payload(
            runtime=runtime,
            session_id=session_id,
            task_id=task_id,
            task_detail_path=task_detail_path,
            task_detail_text=task_detail_text,
            errors=errors,
            warnings=warnings,
            now=now,
        )
        gate_command = write_gate_command_artifact(
            session_dir=session_dir,
            task_id=task_id,
            artifact_name="tpm_completion",
            payload=gate_command,
        )
        event["gate_command_artifact_path"] = gate_command["artifact_path"]
        event["gate_command_status"] = gate_command["status"]
    elif gate_role == "finalization-check":
        gate_command = finalization_command_payload(
            runtime=runtime,
            session_id=session_id,
            task_id=task_id,
            task_detail_path=task_detail_path,
            task_detail_text=task_detail_text,
            errors=errors,
            warnings=warnings,
            now=now,
        )
        active_output = active_task_output(
            runtime=runtime,
            state_root=state_root,
            hook_input={
                "session_id": session_id,
                "task_id": task_id,
                "task_detail_path": str(task_detail_path) if task_detail_path else "",
                "flow_phase": "pre_final_response",
                "owner_role": "finalization-check",
                "last_gate": "finalization-check",
                "source": "finalization-check",
                "allow_missing_final_transport_render_check": auto_final_transport_render_check,
            },
        )
        gate_command["active_task"] = active_output.get("activeTask") if isinstance(active_output, dict) else {}
        gate_command = write_gate_command_artifact(
            session_dir=session_dir,
            task_id=task_id,
            artifact_name="finalization",
            payload=gate_command,
        )
        event["gate_command_artifact_path"] = gate_command["artifact_path"]
        event["gate_command_status"] = gate_command["status"]
        event["active_task_result"] = (gate_command.get("active_task") or {}).get("result", "")
        event["notification_class"] = gate_command.get("notification_class") or event["notification_class"]
        if auto_final_transport_render_check and gate_command.get("status") == "pass":
            final_transport = final_transport_render_check_output(
                runtime=runtime,
                state_root=state_root,
                hook_input={
                    "session_id": session_id,
                    "task_id": task_id,
                    "task_detail_path": str(task_detail_path) if task_detail_path else "",
                    "style_profile": hook_input.get("style_profile")
                    or hook_input.get("styleProfile")
                    or "main_transport_renderer_default",
                    "finalization_artifact_path": gate_command["artifact_path"],
                    "source": "finalization-check:auto_final_transport_render_check",
                },
            )
            gate_command["final_transport_render_check"] = final_transport.get("finalTransportRenderCheck", final_transport)
            event["final_transport_render_check"] = json_event_safe(gate_command["final_transport_render_check"])
            if final_transport.get("decision") == "block":
                output_reason = normalize_cell(final_transport.get("reason")) or "final transport render check blocked"
                errors.append(output_reason)
                gate_command["status"] = "block"
                gate_command["next_phase_allowed"] = False
                gate_command["next_action"] = gate_command_next_action("finalization-check", "block")
                gate_command["llm_dispatch_policy"] = gate_command_llm_dispatch_policy("finalization-check", "block")
                gate_command["reason"] = output_reason
                event["precheck_status"] = "block"
                event["result"] = "gate_precheck_blocked"
                event["gate_command_status"] = "block"
                event["machine_verdict"] = "blocked_by_builder_precheck"
                event["llm_scope"] = "blocked_until_artifacts_fixed"
                event["notification_class"] = notification_class_for_event(
                    event_type="gate_precheck",
                    result="gate_precheck_blocked",
                    status="block",
                    errors=errors,
                )
            gate_command = write_gate_command_artifact(
                session_dir=session_dir,
                task_id=task_id,
                artifact_name="finalization",
                payload=gate_command,
            )
            event["gate_command_artifact_path"] = gate_command["artifact_path"]
    if gate_command is not None:
        event["next_action"] = gate_command.get("next_action", "")
        event["llm_dispatch_policy"] = gate_command.get("llm_dispatch_policy", "")
    append_jsonl_atomic(session_dir / "gate-precheck-events.jsonl", event)

    output = {"gatePrecheck": event}
    if gate_command is not None:
        output["gateCommand"] = gate_command
    if errors:
        output["decision"] = "block"
        output["reason"] = "; ".join(errors)
    return output


def hook_repo_root(hook_input: dict[str, Any]) -> Path | None:
    value = hook_input.get("repo_root") or hook_input.get("repoRoot") or os.environ.get("ITB_REPO_ROOT")
    if not value:
        return None
    return Path(str(value)).expanduser()


def git_status_porcelain(repo_root: Path) -> tuple[str, list[str]]:
    completed = subprocess.run(
        ["git", "-C", str(repo_root), "status", "--porcelain=v1", "--untracked-files=all"],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip() or f"exit={completed.returncode}"
        return "", [f"git status failed for repo_root={repo_root}: {detail}"]
    return completed.stdout, []


def split_path_specs(value: Any) -> list[str]:
    if isinstance(value, list):
        raw_items = value
    else:
        raw_items = str(value or "").replace("\n", ",").split(",")
    return [normalize_cell(item) for item in raw_items if normalize_cell(item)]


def normalize_repo_relative_path(repo_root: Path, value: str) -> str:
    raw = normalize_cell(value).strip("/")
    if not raw:
        return ""
    if Path(raw).is_absolute():
        try:
            raw = str(Path(raw).expanduser().resolve(strict=False).relative_to(repo_root.resolve(strict=False)))
        except ValueError:
            return ""
    normalized = Path(raw).as_posix().strip("/")
    if not normalized or normalized == "." or normalized.startswith("../") or normalized == "..":
        return ""
    return normalized


def task_owned_path_specs(*, repo_root: Path, hook_input: dict[str, Any], task_detail_text: str) -> list[str]:
    raw_values: list[str] = []
    for key in ("owned_paths", "ownedPaths", "task_owned_paths", "taskOwnedPaths", "approved_paths", "approvedPaths"):
        raw_values.extend(split_path_specs(hook_input.get(key)))
    change_manifest = key_value_table(markdown_section(task_detail_text, "Task Change Manifest"))
    for key in ("owned_paths", "Owned Paths", "approved_paths", "Approved Paths", "changed_files", "Changed Files"):
        raw_values.extend(split_path_specs(table_value(change_manifest, key)))
    paths: list[str] = []
    for item in raw_values:
        normalized = normalize_repo_relative_path(repo_root, item)
        if normalized and normalized not in paths:
            paths.append(normalized)
    return paths


def git_status_entry_path(line: str) -> str:
    if line.startswith("?? "):
        return line[3:].strip()
    path = line[3:].strip() if len(line) > 3 else ""
    if " -> " in path:
        path = path.rsplit(" -> ", 1)[-1].strip()
    return path


def path_matches_owned_specs(path: str, owned_specs: list[str]) -> bool:
    normalized = path.strip("/")
    for owned in owned_specs:
        spec = owned.strip("/")
        if normalized == spec or normalized.startswith(f"{spec}/"):
            return True
    return False


def git_diff_for_paths(repo_root: Path, paths: list[str], *, cached: bool = False) -> tuple[str, list[str]]:
    if not paths:
        return "", []
    args = ["git", "-C", str(repo_root), "diff", "--binary"]
    if cached:
        args.append("--cached")
    args.extend(["--", *paths])
    completed = subprocess.run(
        args,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip() or f"exit={completed.returncode}"
        return "", [f"git diff failed for repo_root={repo_root}: {detail}"]
    return completed.stdout, []


def untracked_file_snapshot(repo_root: Path, path: str) -> dict[str, Any]:
    file_path = repo_root / path
    if not file_path.is_file():
        return {"path": path, "status": "untracked_unreadable"}
    data = file_path.read_bytes()
    text_preview = ""
    try:
        text_preview = data[:4000].decode("utf-8")
    except UnicodeDecodeError:
        text_preview = ""
    return {
        "path": path,
        "status": "untracked",
        "sha256": hashlib.sha256(data).hexdigest(),
        "byte_count": len(data),
        "line_count": data.count(b"\n") + (1 if data and not data.endswith(b"\n") else 0),
        "preview": text_preview,
    }


def suggested_dirty_repo_manifests(
    *,
    repo_root: Path,
    hook_input: dict[str, Any],
    task_detail_text: str,
    git_status_lines: list[str],
) -> tuple[dict[str, Any], dict[str, Any], str, str, list[str]]:
    owned_specs = task_owned_path_specs(repo_root=repo_root, hook_input=hook_input, task_detail_text=task_detail_text)
    if not owned_specs:
        return {}, {}, "", "diff_scope_and_manifest_required", []
    task_id = normalize_cell(hook_input.get("task_id") or hook_input.get("taskId"))
    if not task_id:
        manifest = key_value_table(markdown_section(task_detail_text, "Task Change Manifest"))
        task_id = table_value(manifest, "task_id", "Task ID")
    entries = [{"status": line[:2], "path": git_status_entry_path(line), "raw": line} for line in git_status_lines]
    owned_entries = [entry for entry in entries if entry["path"] and path_matches_owned_specs(entry["path"], owned_specs)]
    unrelated_entries = [entry for entry in entries if entry["path"] and not path_matches_owned_specs(entry["path"], owned_specs)]
    unrelated_paths = [entry["path"] for entry in unrelated_entries]
    if not owned_entries:
        return (
            {
                "repo_root": str(repo_root),
                "task_id": task_id,
                "owned_paths": [],
                "excluded_paths": unrelated_paths,
                "approved_scope": "dirty_repo_without_task_owned_diff",
                "approved_diff_snapshot": "not_applicable",
                "reviewed_artifacts": "evaluator-precheck:dirty_repo_no_task_owned_diff",
                "commit_required": False,
                "commit_not_required_reason": "repo_dirty_but_no_task_owned_git_diff",
                "commit_hashes": [],
                "unrelated_dirty_paths": unrelated_paths,
            },
            {
                "commit_required": False,
                "push_required": False,
                "pr_required": False,
                "publication_required": False,
                "publication_not_required_reason": "repo_dirty_but_no_task_owned_git_diff",
                "publication_flow": "not_required",
                "handoff_to": "vault_final_update",
            },
            "dirty_repo_no_task_owned_diff",
            "quality_verdict_only",
            [],
        )
    owned_paths = [entry["path"] for entry in owned_entries]
    unstaged_diff, unstaged_errors = git_diff_for_paths(repo_root, owned_paths)
    staged_diff, staged_errors = git_diff_for_paths(repo_root, owned_paths, cached=True)
    untracked_snapshots = [
        untracked_file_snapshot(repo_root, entry["path"])
        for entry in owned_entries
        if entry["status"] == "??"
    ]
    snapshot = {
        "git_status": [entry["raw"] for entry in owned_entries],
        "unstaged_diff": unstaged_diff,
        "staged_diff": staged_diff,
        "untracked_files": untracked_snapshots,
    }
    return (
        {
            "repo_root": str(repo_root),
            "task_id": task_id,
            "owned_paths": owned_paths,
            "excluded_paths": unrelated_paths,
            "approved_scope": "task_owned_git_diff",
            "approved_diff_snapshot": snapshot,
            "reviewed_artifacts": "evaluator-precheck:task_owned_dirty_diff",
            "commit_required": True,
            "commit_hashes": [],
            "unrelated_dirty_paths": unrelated_paths,
        },
        {
            "task_id": task_id,
            "repo_root": str(repo_root),
            "branch_plan": "evaluator-precheck_unverified",
            "commit_required": True,
            "push_required": False,
            "pr_required": False,
            "publication_required": True,
            "publication_policy": "commit_required_for_task_owned_git_diff",
            "publication_flow": "commit_only",
            "handoff_to": "git-publisher",
        },
        "task_owned_dirty_manifest_suggested",
        "quality_verdict_and_manifest_review",
        unstaged_errors + staged_errors,
    )


def evaluator_precheck_output(
    *,
    runtime: str,
    state_root: Path,
    hook_input: dict[str, Any],
) -> dict[str, Any]:
    session_id, session_source = resolve_session_id(state_root, hook_input)
    if not session_id:
        session_id = "unknown-session"
    session_dir = state_root / safe_id(session_id)
    session_dir.mkdir(parents=True, exist_ok=True)
    now = current_timestamp()
    errors: list[str] = []
    warnings: list[str] = []
    task_detail_text = ""

    task_detail_path = hook_task_detail_path(hook_input)
    if task_detail_path is None:
        errors.append("evaluator precheck requires task_detail_path")
    elif not task_detail_path.exists():
        errors.append(f"task_detail_path does not exist: {task_detail_path}")
    else:
        task_errors, task_warnings = validate_task_flow_artifact(task_detail_path, "post_routing")
        errors.extend(task_errors)
        warnings.extend(task_warnings)
        task_detail_text = task_detail_path.read_text(encoding="utf-8")
        team_completion = key_value_table(markdown_section(task_detail_text, "Team Completion Check"))
        if team_completion:
            completion_status = table_value(
                team_completion,
                "Completion Status",
                "completion_status",
                "Assessment Status",
                "assessment_status",
            )
            if normalized_publication_value(completion_status) != "ready_for_evaluation":
                errors.append("Team Completion Check is not ready_for_evaluation")
        else:
            assessment = key_value_table(markdown_section(task_detail_text, "Completion Assessment"))
            if normalize_cell(assessment.get("Assessment Status")) != "ready_for_evaluation":
                errors.append("Team Completion Check is missing or not ready_for_evaluation")

    repo_root = hook_repo_root(hook_input)
    git_diff_status = "not_checked"
    git_status_lines: list[str] = []
    shortcut = ""
    llm_scope = ""
    suggested_task_change_manifest: dict[str, Any] = {}
    suggested_git_publication_manifest: dict[str, Any] = {}
    if repo_root is None:
        warnings.append("repo_root missing; evaluator must decide publication scope")
    elif not repo_root.exists():
        errors.append(f"repo_root does not exist: {repo_root}")
    else:
        porcelain, git_errors = git_status_porcelain(repo_root)
        errors.extend(git_errors)
        git_status_lines = [line for line in porcelain.splitlines() if line.strip()]
        if not git_errors:
            if git_status_lines:
                git_diff_status = "dirty"
                (
                    suggested_task_change_manifest,
                    suggested_git_publication_manifest,
                    shortcut,
                    llm_scope,
                    manifest_errors,
                ) = suggested_dirty_repo_manifests(
                    repo_root=repo_root,
                    hook_input=hook_input,
                    task_detail_text=task_detail_text,
                    git_status_lines=git_status_lines,
                )
                errors.extend(manifest_errors)
            else:
                git_diff_status = "no_diff"
                shortcut = "no_diff_publication_not_required"
                task_id = normalize_cell(hook_input.get("task_id") or hook_input.get("taskId"))
                if not task_id and task_detail_path is not None:
                    task_id = task_detail_path.parent.name.split("-", 1)[0]
                suggested_task_change_manifest = {
                    "repo_root": str(repo_root),
                    "task_id": task_id,
                    "owned_paths": [],
                    "excluded_paths": [],
                    "approved_scope": "no_git_diff",
                    "approved_diff_snapshot": "not_applicable",
                    "reviewed_artifacts": "evaluator-precheck:no_diff",
                    "commit_required": False,
                    "commit_not_required_reason": "no_task_owned_git_diff",
                    "commit_hashes": [],
                    "unrelated_dirty_paths": [],
                }
                suggested_git_publication_manifest = {
                    "commit_required": False,
                    "push_required": False,
                    "pr_required": False,
                    "publication_required": False,
                    "publication_not_required_reason": "no_task_owned_git_diff",
                    "publication_flow": "not_required",
                    "handoff_to": "vault_final_update",
                }

    precheck_status = "block" if errors else "pass"
    effective_llm_scope = (
        "blocked_until_artifacts_fixed"
        if errors
        else llm_scope
        if git_diff_status == "dirty" and shortcut
        else "quality_verdict_only"
        if shortcut
        else "diff_scope_and_manifest_required"
    )
    event = {
        "ts": now,
        "runtime": runtime,
        "event_type": "evaluator_precheck",
        "session_id": session_id,
        "session_source": session_source,
        "gate_role": "gate-task-evaluator",
        "flow_phase": "post_routing",
        "task_detail_path": str(task_detail_path) if task_detail_path else "",
        "repo_root": str(repo_root) if repo_root else "",
        "git_diff_status": git_diff_status,
        "git_status_lines": git_status_lines,
        "shortcut": shortcut,
        "precheck_status": precheck_status,
        "result": "evaluator_precheck_blocked" if errors else "evaluator_precheck_passed",
        "validation_errors": errors,
        "validation_warnings": warnings,
        "suggested_task_change_manifest": suggested_task_change_manifest,
        "suggested_git_publication_manifest": suggested_git_publication_manifest,
        "llm_scope": effective_llm_scope,
    }
    task_id = task_detail_compact_task_id(
        hook_input=hook_input,
        task_detail_path=task_detail_path,
        task_detail_text=task_detail_text,
    )
    gate_command = evaluation_precheck_command_payload(
        runtime=runtime,
        session_id=session_id,
        task_id=task_id,
        task_detail_path=task_detail_path,
        repo_root=repo_root,
        git_diff_status=git_diff_status,
        git_status_lines=git_status_lines,
        shortcut=shortcut,
        llm_scope=effective_llm_scope,
        suggested_task_change_manifest=suggested_task_change_manifest,
        suggested_git_publication_manifest=suggested_git_publication_manifest,
        errors=errors,
        warnings=warnings,
        now=now,
    )
    gate_command = write_gate_command_artifact(
        session_dir=session_dir,
        task_id=task_id,
        artifact_name="evaluation",
        payload=gate_command,
    )
    event["gate_command_artifact_path"] = gate_command["artifact_path"]
    event["gate_command_status"] = gate_command["status"]
    append_jsonl_atomic(session_dir / "gate-precheck-events.jsonl", event)

    output = {"gatePrecheck": event, "gateCommand": gate_command}
    if errors:
        output["decision"] = "block"
        output["reason"] = "; ".join(errors)
    return output




def hook_install_path_value(hook_input: dict[str, Any], *keys: str) -> Path | None:
    for key in keys:
        raw = normalize_cell(hook_input.get(key))
        if raw:
            return Path(os.path.expandvars(raw)).expanduser()
    return None


def hook_install_target_paths(runtime: str, hook_input: dict[str, Any]) -> dict[str, Path | None]:
    home_dir = hook_install_path_value(hook_input, "home_dir", "homeDir", "home") or Path.home()
    if runtime == "codex":
        settings_path = home_dir / ".codex" / "hooks.json"
        config_path: Path | None = home_dir / ".codex" / "config.toml"
        hooks_dir = home_dir / ".codex" / "hooks"
        settings_path = (
            hook_install_path_value(hook_input, "codex_hooks_path", "codexHooksPath", "hooks_json_path", "hooksJsonPath", "settings_path", "settingsPath")
            or settings_path
        )
        config_path = hook_install_path_value(hook_input, "codex_config_path", "codexConfigPath", "config_path", "configPath") or config_path
    else:
        settings_path = home_dir / ".claude" / "settings.json"
        config_path = None
        hooks_dir = home_dir / ".claude" / "hooks"
        settings_path = (
            hook_install_path_value(hook_input, "claude_settings_path", "claudeSettingsPath", "settings_path", "settingsPath")
            or settings_path
        )
    hooks_dir = hook_install_path_value(hook_input, "hooks_dir", "hooksDir") or hooks_dir
    return {"settings_path": settings_path, "config_path": config_path, "hooks_dir": hooks_dir}


def hook_install_builder_path(hook_input: dict[str, Any]) -> Path:
    explicit = hook_install_path_value(hook_input, "builder_path", "builderPath", "itb_builder", "itbBuilder")
    if explicit is not None:
        return explicit
    env_value = os.environ.get("ITB_BUILDER") or os.environ.get("ITB_BOOTSTRAP_BUILDER")
    if env_value:
        return Path(os.path.expandvars(env_value)).expanduser()
    return ITB_ROOT / "scripts" / "itb_bootstrap_builder.py"


def hook_atomic_write_path(path: Path) -> Path:
    try:
        if path.is_symlink():
            return path.resolve(strict=False)
    except OSError:
        return path
    return path


def shell_double_quote(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def hook_command_for(runtime: str, hooks_dir: Path, script_name: str) -> str:
    return hook_command_for_builder(runtime, hooks_dir, script_name, ITB_ROOT / "scripts" / "itb_bootstrap_builder.py")


def hook_command_for_builder(runtime: str, hooks_dir: Path, script_name: str, builder_path: Path) -> str:
    state_root = f"$HOME/.{runtime}/state/itb"
    return (
        f"ITB_RUNTIME={runtime} "
        f"ITB_STATE_ROOT={shell_double_quote(state_root)} "
        f"ITB_BUILDER={shell_double_quote(str(builder_path))} "
        f"{shell_double_quote(str(hooks_dir / script_name))}"
    )


def hook_event_specs(runtime: str, hooks_dir: Path, builder_path: Path | None = None) -> list[dict[str, Any]]:
    builder_path = builder_path or ITB_ROOT / "scripts" / "itb_bootstrap_builder.py"
    specs = [
        {
            "event": "SessionStart",
            "script": "itb-session-start.sh",
            "matcher": "startup|resume|clear|compact",
            "timeout": 10,
        },
        {
            "event": "Stop",
            "script": "itb-final-response-guard.sh",
            "matcher": None,
            "timeout": 10,
        },
    ]
    return [spec | {"command": hook_command_for_builder(runtime, hooks_dir, str(spec["script"]), builder_path)} for spec in specs]


def hook_existing_only_event_specs(runtime: str, hooks_dir: Path, builder_path: Path | None = None) -> list[dict[str, Any]]:
    return []


def hook_retired_event_specs(runtime: str, hooks_dir: Path, builder_path: Path | None = None) -> list[dict[str, Any]]:
    builder_path = builder_path or ITB_ROOT / "scripts" / "itb_bootstrap_builder.py"
    specs = [
        {
            "event": "UserPromptSubmit",
            "script": "itb-prompt-preflight.sh",
            "matcher": None,
            "timeout": 10,
        },
        {
            "event": "PreToolUse",
            "script": "itb-pretooluse-guard.sh",
            "matcher": None,
            "timeout": 10,
        },
        {
            "event": "SessionEnd",
            "script": "itb-session-end.sh",
            "matcher": None,
            "timeout": 10,
        },
    ]
    return [spec | {"command": hook_command_for_builder(runtime, hooks_dir, str(spec["script"]), builder_path)} for spec in specs]


def simple_pipe_matcher_tokens(value: str) -> list[str]:
    if not value:
        return []
    tokens = [item.strip() for item in value.split("|") if item.strip()]
    if not tokens:
        return []
    if any(not re.fullmatch(r"[A-Za-z0-9_.-]+", item) for item in tokens):
        return []
    return tokens


def merged_hook_matcher(existing: Any, desired: str | None) -> str | None:
    existing_text = normalize_cell(existing)
    if desired is None:
        return existing_text or None
    if not existing_text or existing_text == desired:
        return desired
    existing_tokens = simple_pipe_matcher_tokens(existing_text)
    desired_tokens = simple_pipe_matcher_tokens(desired)
    if existing_tokens and desired_tokens:
        merged: list[str] = []
        for token in desired_tokens + existing_tokens:
            if token not in merged:
                merged.append(token)
        return "|".join(merged)
    return desired


def hook_timeout_value(existing: Any, desired: int) -> int:
    if isinstance(existing, (int, float)) and int(existing) > desired:
        return int(existing)
    return desired


def build_hook_event_entry(spec: dict[str, Any]) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "hooks": [
            {
                "type": "command",
                "command": spec["command"],
                "timeout": spec["timeout"],
            }
        ]
    }
    matcher = spec.get("matcher")
    if matcher is not None:
        entry["matcher"] = matcher
    return entry


def canonicalize_matching_hook_entries(
    *,
    entries: list[Any],
    spec: dict[str, Any],
    changes: list[dict[str, Any]],
) -> bool:
    event = str(spec["event"])
    script = str(spec["script"])
    matched = False
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        entry_hooks = entry.get("hooks")
        if not isinstance(entry_hooks, list):
            continue
        for hook in entry_hooks:
            if not isinstance(hook, dict):
                continue
            command = normalize_cell(hook.get("command"))
            if script not in command:
                continue
            matched = True
            desired_matcher = merged_hook_matcher(entry.get("matcher"), spec.get("matcher"))
            if desired_matcher is not None and entry.get("matcher") != desired_matcher:
                entry["matcher"] = desired_matcher
                changes.append({"action": "update_matcher", "event": event, "script": script, "matcher": desired_matcher})
            if hook.get("type") != "command":
                hook["type"] = "command"
                changes.append({"action": "update_hook_type", "event": event, "script": script})
            if command != spec["command"]:
                hook["command"] = spec["command"]
                changes.append({"action": "update_command", "event": event, "script": script})
            desired_timeout = hook_timeout_value(hook.get("timeout"), int(spec["timeout"]))
            if hook.get("timeout") != desired_timeout:
                hook["timeout"] = desired_timeout
                changes.append({"action": "update_timeout", "event": event, "script": script, "timeout": desired_timeout})
            break
    return matched


def prune_retired_hook_entries(
    *,
    hooks: dict[str, Any],
    retired_specs: list[dict[str, Any]],
    changes: list[dict[str, Any]],
) -> None:
    for spec in retired_specs:
        event = str(spec["event"])
        script = str(spec["script"])
        entries = hooks.get(event)
        if not isinstance(entries, list):
            continue
        pruned_entries: list[Any] = []
        removed_count = 0
        for entry in entries:
            if not isinstance(entry, dict):
                pruned_entries.append(entry)
                continue
            entry_hooks = entry.get("hooks")
            if not isinstance(entry_hooks, list):
                pruned_entries.append(entry)
                continue
            kept_hooks = []
            for hook in entry_hooks:
                if isinstance(hook, dict) and script in normalize_cell(hook.get("command")):
                    removed_count += 1
                    continue
                kept_hooks.append(hook)
            if kept_hooks:
                updated_entry = dict(entry)
                updated_entry["hooks"] = kept_hooks
                pruned_entries.append(updated_entry)
        if not removed_count:
            continue
        if pruned_entries:
            hooks[event] = pruned_entries
        else:
            hooks.pop(event, None)
        changes.append(
            {
                "action": "remove_retired_itb_hook",
                "event": event,
                "script": script,
                "removed_count": removed_count,
            }
        )


def merge_hook_settings(data: Any, runtime: str, hooks_dir: Path, builder_path: Path | None = None) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    merged: dict[str, Any]
    if isinstance(data, dict):
        merged = json.loads(json.dumps(data))
    else:
        merged = {}
    changes: list[dict[str, Any]] = []
    hooks = merged.get("hooks")
    if not isinstance(hooks, dict):
        hooks = {}
        merged["hooks"] = hooks
        changes.append({"action": "initialize_hooks_object"})

    for spec in hook_event_specs(runtime, hooks_dir, builder_path):
        event = str(spec["event"])
        script = str(spec["script"])
        entries = hooks.get(event)
        if not isinstance(entries, list):
            entries = []
            hooks[event] = entries
            changes.append({"action": "initialize_event", "event": event})

        matched = canonicalize_matching_hook_entries(entries=entries, spec=spec, changes=changes)
        if not matched:
            entries.append(build_hook_event_entry(spec))
            changes.append({"action": "add_hook_event", "event": event, "script": script})
    for spec in hook_existing_only_event_specs(runtime, hooks_dir, builder_path):
        entries = hooks.get(str(spec["event"]))
        if isinstance(entries, list):
            canonicalize_matching_hook_entries(entries=entries, spec=spec, changes=changes)
    prune_retired_hook_entries(
        hooks=hooks,
        retired_specs=hook_retired_event_specs(runtime, hooks_dir, builder_path),
        changes=changes,
    )
    return merged, changes


def ensure_codex_hooks_feature(text: str) -> tuple[str, list[dict[str, Any]]]:
    changes: list[dict[str, Any]] = []
    lines = text.splitlines()
    features_start = -1
    for index, line in enumerate(lines):
        if re.fullmatch(r"\s*\[features\]\s*", line):
            features_start = index
            break
    if features_start == -1:
        prefix = text.rstrip("\n")
        suffix = "\n\n" if prefix else ""
        changes.append({"action": "add_features_section", "key": "codex_hooks"})
        return f"{prefix}{suffix}[features]\ncodex_hooks = true\n", changes

    section_end = len(lines)
    for index in range(features_start + 1, len(lines)):
        if re.match(r"\s*\[.*\]\s*$", lines[index]):
            section_end = index
            break
    for index in range(features_start + 1, section_end):
        if re.match(r"\s*codex_hooks\s*=", lines[index]):
            if re.fullmatch(r"\s*codex_hooks\s*=\s*true\s*", lines[index]):
                return text if text.endswith("\n") else text + "\n", changes
            lines[index] = "codex_hooks = true"
            changes.append({"action": "enable_codex_hooks_feature", "key": "codex_hooks"})
            return "\n".join(lines) + "\n", changes
    lines.insert(features_start + 1, "codex_hooks = true")
    changes.append({"action": "insert_codex_hooks_feature", "key": "codex_hooks"})
    return "\n".join(lines) + "\n", changes


def backup_target_file(path: Path, backup_paths: list[str]) -> None:
    if not path.exists():
        return
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d%H%M%S")
    backup = path.with_name(f"{path.name}.itb-backup-{stamp}")
    counter = 1
    while backup.exists():
        backup = path.with_name(f"{path.name}.itb-backup-{stamp}-{counter}")
        counter += 1
    shutil.copy2(path, backup)
    backup_paths.append(str(backup))


def hook_file_change(path: Path, before_text: str, after_text: str, action: str, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    before_sha = hashlib.sha256(before_text.encode("utf-8")).hexdigest() if before_text else ""
    after_sha = hashlib.sha256(after_text.encode("utf-8")).hexdigest() if after_text else ""
    change = {
        "path": str(path),
        "action": action,
        "before_sha256": before_sha,
        "after_sha256": after_sha,
        "changed": before_text != after_text,
    }
    if extra:
        change.update(extra)
    return change


def hook_settings_command_entries(settings: Any, event: str) -> list[dict[str, Any]]:
    if not isinstance(settings, dict):
        return []
    hooks = settings.get("hooks")
    if not isinstance(hooks, dict):
        return []
    entries = hooks.get(event)
    if not isinstance(entries, list):
        return []
    command_entries: list[dict[str, Any]] = []
    for entry_index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            continue
        entry_hooks = entry.get("hooks")
        if not isinstance(entry_hooks, list):
            continue
        for hook_index, hook in enumerate(entry_hooks):
            if not isinstance(hook, dict):
                continue
            command = normalize_cell(hook.get("command"))
            if not command:
                continue
            command_entries.append(
                {
                    "entry_index": entry_index,
                    "hook_index": hook_index,
                    "matcher": normalize_cell(entry.get("matcher")),
                    "type": normalize_cell(hook.get("type")),
                    "command": command,
                    "timeout": hook.get("timeout"),
                }
            )
    return command_entries


CODEX_HOOK_STATE_EVENT_NAMES = {
    "SessionStart": "session_start",
    "Stop": "stopped",
}


def read_codex_hook_state_entries(config_path: Path | None) -> tuple[dict[str, dict[str, Any]], list[str]]:
    if config_path is None:
        return {}, ["codex_config_path_missing"]
    if not config_path.exists():
        return {}, ["codex_config_missing"]
    try:
        lines = config_path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        return {}, [f"codex_config_unreadable:{type(exc).__name__}:{exc}"]

    entries: dict[str, dict[str, Any]] = {}
    current_key = ""
    for line in lines:
        header = re.fullmatch(r'\s*\[hooks\.state\."([^"]+)"\]\s*', line)
        if header:
            current_key = header.group(1)
            entries.setdefault(current_key, {})
            continue
        if re.fullmatch(r"\s*\[.*\]\s*", line):
            current_key = ""
            continue
        if not current_key:
            continue
        trusted = re.fullmatch(r'\s*trusted_hash\s*=\s*"([^"]*)"\s*', line)
        if trusted:
            entries[current_key]["trusted_hash"] = trusted.group(1)
            continue
        enabled = re.fullmatch(r"\s*enabled\s*=\s*(true|false)\s*", line)
        if enabled:
            entries[current_key]["enabled"] = enabled.group(1) == "true"
    return entries, []


def codex_hook_state_candidates(settings_path: Path | None, event: str, entry_index: Any, hook_index: Any) -> list[str]:
    event_name = CODEX_HOOK_STATE_EVENT_NAMES.get(event)
    if not event_name or settings_path is None or entry_index is None or hook_index is None:
        return []
    suffix = f":{event_name}:{entry_index}:{hook_index}"
    candidates = [f"{settings_path}{suffix}"]
    resolved = hook_atomic_write_path(settings_path)
    if resolved != settings_path:
        candidates.append(f"{resolved}{suffix}")
    fully_resolved = settings_path.resolve(strict=False)
    if fully_resolved not in (settings_path, resolved):
        candidates.append(f"{fully_resolved}{suffix}")
    return candidates


def codex_hook_trust_state_result(entry: dict[str, Any] | None, expected_hash: str = "") -> str:
    if entry is None:
        return "missing"
    if entry.get("enabled") is False:
        return "disabled"
    actual_hash = normalize_cell(entry.get("trusted_hash"))
    if not actual_hash:
        return "missing_trusted_hash"
    if expected_hash:
        return "verified" if actual_hash == expected_hash else "hash_mismatch"
    return "present_unverified"


def apply_codex_hook_trust_state(
    *,
    config_path: Path | None,
    settings_path: Path | None,
    checks: list[dict[str, Any]],
    require_hook_trust_state: bool,
) -> dict[str, Any]:
    entries, read_issues = read_codex_hook_state_entries(config_path)
    summary = {
        "config_path": str(config_path) if config_path else "",
        "config_exists": config_path.exists() if config_path else False,
        "state_entry_count": len(entries),
        "checked_count": 0,
        "verified_events": [],
        "present_unverified_events": [],
        "missing_events": [],
        "disabled_events": [],
        "missing_trusted_hash_events": [],
        "hash_mismatch_events": [],
        "issues": read_issues,
        "result": "not_checked" if read_issues else "pass",
        "required": require_hook_trust_state,
    }
    for check in checks:
        event = normalize_cell(check.get("event"))
        candidates = codex_hook_state_candidates(
            settings_path,
            event,
            check.get("entry_index"),
            check.get("hook_index"),
        )
        if not candidates:
            continue
        summary["checked_count"] += 1
        matched_key = next((candidate for candidate in candidates if candidate in entries), "")
        entry = entries.get(matched_key) if matched_key else None
        state_result = codex_hook_trust_state_result(entry)
        check["hook_trust_state_result"] = state_result
        check["hook_trust_state_key"] = matched_key or candidates[0]
        check["hook_trust_state_config_path"] = str(config_path) if config_path else ""
        if state_result in {"verified", "present_unverified"}:
            check["hook_trust_state_hash"] = normalize_cell(entry.get("trusted_hash")) if entry else ""
            if state_result == "verified":
                summary["verified_events"].append(event)
            else:
                summary["present_unverified_events"].append(event)
        elif state_result == "disabled":
            summary["disabled_events"].append(event)
        elif state_result == "missing_trusted_hash":
            summary["missing_trusted_hash_events"].append(event)
        elif state_result == "hash_mismatch":
            summary["hash_mismatch_events"].append(event)
        else:
            summary["missing_events"].append(event)
        if require_hook_trust_state and state_result != "verified":
            issue = f"hook_trust_state_{state_result}"
            check.setdefault("issues", []).append(issue)
            check["result"] = "block"
    problem_events = (
        summary["missing_events"]
        + summary["disabled_events"]
        + summary["missing_trusted_hash_events"]
        + summary["hash_mismatch_events"]
    )
    unverified_events = summary["present_unverified_events"]
    if read_issues:
        summary["result"] = "block" if require_hook_trust_state else "not_checked"
    elif problem_events:
        summary["result"] = "block" if require_hook_trust_state else "missing"
    elif unverified_events:
        summary["result"] = "block" if require_hook_trust_state else "present_unverified"
    else:
        summary["result"] = "pass"
    return summary


def expand_hook_command_value(value: str, home_dir: Path) -> str:
    expanded = value.replace("${HOME}", str(home_dir)).replace("$HOME", str(home_dir))
    return os.path.expandvars(expanded)


def parse_hook_command(command: str, home_dir: Path) -> tuple[dict[str, str], Path | None, list[str], list[str]]:
    try:
        parts = shlex.split(command)
    except ValueError as exc:
        return {}, None, [], [f"parse_error:{type(exc).__name__}:{exc}"]
    env: dict[str, str] = {}
    script_path: Path | None = None
    trailing_args: list[str] = []
    for index, part in enumerate(parts):
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", part):
            key, value = part.split("=", 1)
            env[key] = expand_hook_command_value(value, home_dir)
            continue
        script_path = Path(expand_hook_command_value(part, home_dir)).expanduser()
        trailing_args = [expand_hook_command_value(item, home_dir) for item in parts[index + 1 :]]
        break
    if script_path is None:
        return env, None, trailing_args, ["missing_script_path"]
    return env, script_path, trailing_args, []


def path_is_under(path: Path, root: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(root.resolve(strict=False))
        return True
    except ValueError:
        return False
    except OSError:
        return False


def validate_hook_command_entry(
    *,
    runtime: str,
    home_dir: Path,
    hooks_dir: Path,
    builder_path: Path,
    spec: dict[str, Any],
    entry: dict[str, Any],
    require_script_path_match: bool = False,
) -> dict[str, Any]:
    command = normalize_cell(entry.get("command"))
    env, script_path, trailing_args, parse_issues = parse_hook_command(command, home_dir)
    issues = list(parse_issues)
    script_name = str(spec["script"])
    expected_script_path = hooks_dir / script_name
    expected_matcher = spec.get("matcher")
    matcher = normalize_cell(entry.get("matcher"))

    if normalize_cell(entry.get("type")) != "command":
        issues.append("hook_type_not_command")
    if expected_matcher is not None and matcher != normalize_cell(expected_matcher):
        issues.append("matcher_mismatch")
    if trailing_args:
        issues.append("unexpected_command_args")
    if env.get("ITB_RUNTIME") != runtime:
        issues.append("runtime_env_mismatch")
    expected_state_root = f"{home_dir}/.{runtime}/state/itb"
    if env.get("ITB_STATE_ROOT") != expected_state_root:
        issues.append("state_root_env_mismatch")
    if script_path is None:
        script_path_text = ""
    else:
        script_path_text = str(script_path)
        if script_path.name != script_name:
            issues.append("script_name_mismatch")
        if require_script_path_match and script_path != expected_script_path:
            issues.append("script_path_mismatch")
        if not script_path.exists():
            issues.append("script_missing")
        elif not os.access(script_path, os.X_OK):
            issues.append("script_not_executable")

    builder_env = env.get("ITB_BUILDER") or env.get("ITB_BOOTSTRAP_BUILDER")
    copied_outside_repo = script_path is not None and not path_is_under(script_path, HOOK_BUNDLE_DIR)
    if not builder_env and copied_outside_repo:
        issues.append("missing_itb_builder")
    elif builder_env:
        parsed_builder = Path(builder_env).expanduser()
        if parsed_builder != builder_path:
            issues.append("builder_path_mismatch")
        if not parsed_builder.exists():
            issues.append("builder_missing")

    return {
        "event": spec["event"],
        "script": script_name,
        "command": command,
        "entry_index": entry.get("entry_index"),
        "hook_index": entry.get("hook_index"),
        "matcher": matcher,
        "script_path": script_path_text,
        "expected_script_path": str(expected_script_path),
        "runtime_env": env.get("ITB_RUNTIME", ""),
        "state_root_env": env.get("ITB_STATE_ROOT", ""),
        "builder_path": builder_env or "",
        "issues": issues,
        "result": "pass" if not issues else "block",
    }


HOOK_HEALTH_DEFAULT_SMOKE_SCRIPTS = [
    "itb-final-response-guard.sh",
]
HOOK_HEALTH_INITIAL_HOOK_SMOKE_SCRIPTS = [
    "itb-session-start.sh",
    "itb-final-response-guard.sh",
]
HOOK_HEALTH_ALL_SMOKE_SCRIPTS = [
    "itb-session-start.sh",
    "itb-final-response-guard.sh",
]
HOOK_HEALTH_SMOKE_ALIASES = {
    "sessionstart": ["itb-session-start.sh"],
    "session_start": ["itb-session-start.sh"],
    "itb-session-start.sh": ["itb-session-start.sh"],
    "stop": ["itb-final-response-guard.sh"],
    "final-response-guard": ["itb-final-response-guard.sh"],
    "final_response_guard": ["itb-final-response-guard.sh"],
    "itb-final-response-guard.sh": ["itb-final-response-guard.sh"],
    "default": HOOK_HEALTH_DEFAULT_SMOKE_SCRIPTS,
    "safe": HOOK_HEALTH_DEFAULT_SMOKE_SCRIPTS,
    "initial": HOOK_HEALTH_INITIAL_HOOK_SMOKE_SCRIPTS,
    "initial_hook_set": HOOK_HEALTH_INITIAL_HOOK_SMOKE_SCRIPTS,
    "all": HOOK_HEALTH_ALL_SMOKE_SCRIPTS,
}
HOOK_HEALTH_DEFAULT_LIVE_EVENTS = ["SessionStart", "Stop"]
HOOK_HEALTH_DEFAULT_REQUIRED_LIVE_EVENTS = ["SessionStart", "Stop"]
HOOK_HEALTH_LIVE_EVENT_ALIASES = {
    "sessionstart": "SessionStart",
    "session_start": "SessionStart",
    "session-start": "SessionStart",
    "stop": "Stop",
    "final_response_guard": "Stop",
    "final-response-guard": "Stop",
}


def hook_health_smoke_scripts(hook_input: dict[str, Any]) -> tuple[list[str], list[str]]:
    requested = normalize_string_list(hook_input.get("smoke_scripts") or hook_input.get("smokeScripts"))
    requested.extend(normalize_string_list(hook_input.get("smoke_events") or hook_input.get("smokeEvents")))
    if not requested:
        return list(HOOK_HEALTH_DEFAULT_SMOKE_SCRIPTS), []
    scripts: list[str] = []
    issues: list[str] = []
    for raw_item in requested:
        normalized = normalize_cell(raw_item).strip()
        alias = normalized.lower().replace(" ", "_")
        alias = alias.replace("-", "_") if not normalized.endswith(".sh") else normalized.lower()
        mapped = HOOK_HEALTH_SMOKE_ALIASES.get(alias) or HOOK_HEALTH_SMOKE_ALIASES.get(normalized)
        if not mapped:
            issues.append(f"unsupported_smoke_target:{normalized}")
            continue
        for script_name in mapped:
            if script_name not in scripts:
                scripts.append(script_name)
    ordered = [script_name for script_name in HOOK_HEALTH_ALL_SMOKE_SCRIPTS if script_name in scripts]
    return ordered, issues


def hook_health_smoke_payload(script_name: str, smoke_state_root: Path | None = None) -> dict[str, Any]:
    smoke_session_id = "hook-health-check-startup-preflight"
    if script_name == "itb-final-response-guard.sh":
        return {"session_id": "hook-health-check-final-guard"}
    if script_name == "itb-session-start.sh":
        smoke_cwd = (smoke_state_root / "_cwd") if smoke_state_root else Path("/tmp")
        return {
            "session_id": smoke_session_id,
            "cwd": str(smoke_cwd),
            "source": "SessionStart",
            "force_session_start_rebuild": True,
        }
    return {}


def run_hook_health_smoke(
    check: dict[str, Any],
    home_dir: Path,
    timeout_seconds: float,
    smoke_state_root: Path | None = None,
) -> dict[str, Any]:
    script_name = normalize_cell(check.get("script"))
    if script_name == "itb-session-start.sh" and smoke_state_root is None:
        return {
            "script": script_name,
            "result": "block",
            "issues": ["smoke_state_root_required"],
        }
    if smoke_state_root is not None:
        try:
            smoke_state_root.mkdir(parents=True, exist_ok=True)
            (smoke_state_root / "_cwd").mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            return {
                "script": script_name,
                "result": "block",
                "issues": [f"smoke_state_root_create_failed:{type(exc).__name__}:{exc}"],
            }
    payload = hook_health_smoke_payload(script_name, smoke_state_root=smoke_state_root)
    if not payload:
        return {"script": script_name, "result": "skipped_unsupported_script"}
    script_path = Path(normalize_cell(check.get("script_path")))
    command = normalize_cell(check.get("command"))
    env_values, _, _, parse_issues = parse_hook_command(command, home_dir)
    if parse_issues:
        return {"script": script_name, "result": "block", "issues": parse_issues}
    env = os.environ.copy()
    env["HOME"] = str(home_dir)
    env.update(env_values)
    if smoke_state_root is not None:
        env["ITB_STATE_ROOT"] = str(smoke_state_root)
    try:
        completed = subprocess.run(
            ["bash", str(script_path)],
            input=json.dumps(payload),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            timeout=timeout_seconds,
            check=False,
        )
    except Exception as exc:
        return {"script": script_name, "result": "block", "issues": [f"smoke_exception:{type(exc).__name__}:{exc}"]}
    output: Any = {}
    issues: list[str] = []
    if completed.returncode != 0:
        issues.append(f"exit_code:{completed.returncode}")
    try:
        output = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        issues.append(f"stdout_not_json:{exc.msg}")
    session_dir = Path(env.get("ITB_STATE_ROOT", "")) / safe_id(normalize_cell(payload.get("session_id")))
    if script_name == "itb-session-start.sh":
        hook_output = output.get("hookSpecificOutput") if isinstance(output, dict) else {}
        if not isinstance(hook_output, dict) or hook_output.get("hookEventName") != "SessionStart":
            issues.append("session_start_hook_output_missing")
        try:
            pointer = read_json(session_dir / "active-execution-context.json")
        except Exception as exc:
            issues.append(f"active_execution_context_pointer_read_failed:{type(exc).__name__}:{exc}")
            pointer = {}
        if isinstance(pointer, dict):
            allowed_keys = {
                "session_id",
                "runtime",
                "cwd",
                "started_at",
                "harness_config_digest",
                "active_execution_context",
                "active_execution_context_pointer_path",
            }
            extra_keys = sorted(set(pointer) - allowed_keys)
            if extra_keys:
                issues.append("session_start_pointer_extra_keys:" + ",".join(extra_keys))
            if pointer.get("active_execution_context") is not None:
                issues.append("session_start_active_context_not_null")
            if normalize_cell(pointer.get("active_execution_context_pointer_path")) != str(session_dir / "active-execution-context.json"):
                issues.append("session_start_pointer_path_mismatch")
        if (session_dir / "bootstrap.json").exists():
            issues.append("session_start_wrote_legacy_bootstrap")
        if (session_dir / "roster.json").exists():
            issues.append("session_start_wrote_legacy_roster")
        if (session_dir / "queue").exists():
            issues.append("session_start_created_queue_state")
    if script_name == "itb-final-response-guard.sh":
        if not isinstance(output, dict) or output.get("permissionDecision") != "allow":
            issues.append("final_guard_not_allowed")
    return {
        "script": script_name,
        "result": "pass" if not issues else "block",
        "issues": issues,
        "returncode": completed.returncode,
        "state_root": env.get("ITB_STATE_ROOT", ""),
        "session_id": payload.get("session_id", ""),
        "stdout": output if isinstance(output, dict) else {},
        "stderr": completed.stderr[-1000:],
    }


def hook_health_live_event_name(raw_item: Any) -> str:
    raw = normalize_cell(raw_item).strip()
    if raw in HOOK_HEALTH_DEFAULT_LIVE_EVENTS:
        return raw
    alias = raw.lower().replace(" ", "_")
    return HOOK_HEALTH_LIVE_EVENT_ALIASES.get(alias) or HOOK_HEALTH_LIVE_EVENT_ALIASES.get(raw.lower()) or ""


def hook_health_live_event_list(raw_items: list[Any]) -> tuple[list[str], list[str]]:
    events: list[str] = []
    issues: list[str] = []
    for raw_item in raw_items:
        raw = normalize_cell(raw_item).strip()
        event_name = hook_health_live_event_name(raw)
        if not event_name:
            issues.append(f"unsupported_live_event:{raw}")
            continue
        if event_name not in events:
            events.append(event_name)
    return events, issues


def hook_health_live_events(hook_input: dict[str, Any], require_live_evidence: bool) -> tuple[list[str], list[str], list[str]]:
    checked_raw = normalize_string_list(
        hook_input.get("live_events")
        or hook_input.get("liveEvents")
        or hook_input.get("checked_live_events")
        or hook_input.get("checkedLiveEvents")
    )
    checked_events, checked_issues = hook_health_live_event_list(checked_raw)
    if not checked_events and not checked_raw:
        checked_events = list(HOOK_HEALTH_DEFAULT_LIVE_EVENTS)

    required_raw = normalize_string_list(hook_input.get("required_live_events") or hook_input.get("requiredLiveEvents"))
    required_events, required_issues = hook_health_live_event_list(required_raw)
    if require_live_evidence and not required_events and not required_raw:
        required_events = list(HOOK_HEALTH_DEFAULT_REQUIRED_LIVE_EVENTS)
    for event_name in required_events:
        if event_name not in checked_events:
            checked_events.append(event_name)
    return checked_events, required_events, checked_issues + required_issues


def hook_health_configured_live_state_root(
    *,
    runtime: str,
    home_dir: Path,
    checks: list[dict[str, Any]],
    hook_input: dict[str, Any],
) -> tuple[Path, str]:
    explicit = hook_install_path_value(hook_input, "live_state_root", "liveStateRoot", "live_state_dir", "liveStateDir")
    if explicit is not None:
        return explicit, "hook_input"
    for check in checks:
        state_root_env = normalize_cell(check.get("state_root_env"))
        if state_root_env:
            return Path(os.path.expandvars(state_root_env)).expanduser(), "hook_settings:ITB_STATE_ROOT"
    return home_dir / f".{runtime}" / "state" / "itb", "default_home"


def hook_health_live_session_id(live_state_root: Path, hook_input: dict[str, Any]) -> tuple[str, str]:
    explicit = normalize_cell(hook_input.get("live_session_id") or hook_input.get("liveSessionId"))
    if explicit:
        return explicit, "hook_input"
    last_session = live_state_root / "last-session"
    if not last_session.exists():
        return "", "missing_last_session"
    try:
        value = last_session.read_text(encoding="utf-8").strip()
    except OSError:
        return "", "last_session_unreadable"
    return value, "last-session" if value else "last_session_empty"


def hook_health_latest_jsonl_event(path: Path, event_types: set[str]) -> tuple[dict[str, Any], str]:
    try:
        records = read_jsonl(path)
    except Exception as exc:
        return {}, f"read_failed:{type(exc).__name__}:{exc}"
    for record in reversed(records):
        event_type = normalize_cell(record.get("event_type"))
        if not event_types or event_type in event_types:
            return record, ""
    return {}, ""


def hook_health_read_text_if_exists(path: Path) -> tuple[str, str]:
    if not path.exists():
        return "", ""
    try:
        return path.read_text(encoding="utf-8").strip(), ""
    except OSError as exc:
        return "", f"read_failed:{type(exc).__name__}:{exc}"


def hook_health_read_json_object_if_exists(path: Path) -> tuple[dict[str, Any], str]:
    if not path.exists():
        return {}, ""
    try:
        data = read_json(path)
    except Exception as exc:
        return {}, f"read_failed:{type(exc).__name__}:{exc}"
    return data if isinstance(data, dict) else {}, ""


def hook_health_live_event_evidence(event_name: str, session_dir: Path, session_id: str) -> dict[str, Any]:
    evidence: dict[str, Any] = {
        "event": event_name,
        "session_id": session_id,
        "session_dir": str(session_dir) if session_id else "",
        "observed": False,
        "result": "missing",
        "source": "",
        "evidence_path": "",
        "ts": "",
        "issues": [],
    }
    if not session_id:
        evidence["issues"] = ["live_session_missing"]
        return evidence

    if event_name == "SessionStart":
        pointer_path = session_dir / "active-execution-context.json"
        pointer, pointer_issue = hook_health_read_json_object_if_exists(pointer_path)
        issues = [pointer_issue] if pointer_issue else []
        pointer_ready = bool(pointer) and normalize_cell(pointer.get("session_id")) == session_id
        if not pointer_ready:
            issues.append("active_execution_context_pointer_missing")
        if pointer.get("active_execution_context") is not None:
            issues.append("active_execution_context_not_null")
        evidence.update(
            {
                "observed": pointer_ready,
                "result": "observed" if pointer_ready else "missing",
                "source": "active-execution-context",
                "evidence_path": str(pointer_path),
                "ts": normalize_cell(pointer.get("started_at")),
                "active_execution_context": pointer.get("active_execution_context"),
                "harness_config_digest": normalize_cell(pointer.get("harness_config_digest")),
                "issues": issues,
            }
        )
        return evidence

    event_specs = {
        "Stop": {
            "path": session_dir / "final-response-guard-events.jsonl",
            "event_types": {"final_response_guard"},
            "source": "final-response-guard-events",
            "missing_issue": "final_response_guard_event_missing",
        },
    }
    spec = event_specs.get(event_name)
    if not spec:
        evidence["issues"] = [f"unsupported_live_event:{event_name}"]
        return evidence
    path = spec["path"]
    record, read_issue = hook_health_latest_jsonl_event(path, set(spec["event_types"]))
    issues = [read_issue] if read_issue else []
    if not record:
        issues.append(str(spec["missing_issue"]))
    evidence.update(
        {
            "observed": bool(record),
            "result": "observed" if record else "missing",
            "source": spec["source"] if record else "",
            "evidence_path": str(path),
            "ts": normalize_cell(record.get("ts")),
            "event_result": normalize_cell(record.get("result")),
            "issues": issues,
        }
    )
    return evidence


def hook_health_apply_live_evidence_freshness(
    events: list[dict[str, Any]],
    *,
    now: str,
    max_age_seconds: float,
) -> tuple[list[str], list[str]]:
    stale_events: list[str] = []
    unknown_age_events: list[str] = []
    for event in events:
        event["max_age_seconds"] = max_age_seconds
        if max_age_seconds <= 0:
            event["age_seconds"] = None
            event["freshness_result"] = "not_checked"
            continue
        if not event.get("observed"):
            event["age_seconds"] = None
            event["freshness_result"] = "not_observed"
            continue
        age_seconds = iso_age_seconds(now, normalize_cell(event.get("ts")))
        event["age_seconds"] = round(age_seconds, 3) if age_seconds is not None else None
        if age_seconds is None:
            event["freshness_result"] = "unknown"
            event.setdefault("issues", []).append("live_evidence_timestamp_missing_or_invalid")
            unknown_age_events.append(normalize_cell(event.get("event")))
        elif age_seconds > max_age_seconds:
            event["freshness_result"] = "stale"
            event.setdefault("issues", []).append("live_evidence_stale")
            stale_events.append(normalize_cell(event.get("event")))
        else:
            event["freshness_result"] = "fresh"
    return stale_events, unknown_age_events


def hook_health_live_evidence_report(
    *,
    runtime: str,
    home_dir: Path,
    checks: list[dict[str, Any]],
    hook_input: dict[str, Any],
    require_live_evidence: bool,
) -> dict[str, Any]:
    live_state_root, state_root_source = hook_health_configured_live_state_root(
        runtime=runtime,
        home_dir=home_dir,
        checks=checks,
        hook_input=hook_input,
    )
    checked_events, required_events, input_issues = hook_health_live_events(hook_input, require_live_evidence)
    live_session_id, session_source = hook_health_live_session_id(live_state_root, hook_input)
    session_dir = live_state_root / safe_id(live_session_id) if live_session_id else live_state_root
    events = [hook_health_live_event_evidence(event_name, session_dir, live_session_id) for event_name in checked_events]
    max_age_seconds = bounded_float_input(
        hook_input.get("max_live_evidence_age_seconds")
        or hook_input.get("maxLiveEvidenceAgeSeconds")
        or hook_input.get("live_evidence_max_age_seconds")
        or hook_input.get("liveEvidenceMaxAgeSeconds"),
        default=0.0,
        minimum=0.0,
        maximum=31_536_000.0,
    )
    freshness_now = normalize_cell(hook_input.get("live_evidence_now") or hook_input.get("liveEvidenceNow")) or current_timestamp()
    stale_events, unknown_age_events = hook_health_apply_live_evidence_freshness(
        events,
        now=freshness_now,
        max_age_seconds=max_age_seconds,
    )
    missing_events = [item["event"] for item in events if not item.get("observed")]
    required_missing_events = [item["event"] for item in events if item["event"] in required_events and not item.get("observed")]
    required_stale_events = [event_name for event_name in stale_events if event_name in required_events]
    required_unknown_age_events = [event_name for event_name in unknown_age_events if event_name in required_events]
    if require_live_evidence:
        result = "block" if (required_missing_events or required_stale_events or required_unknown_age_events or input_issues) else "pass"
    else:
        if stale_events or unknown_age_events:
            result = "stale"
        elif missing_events:
            result = "missing"
        else:
            result = "pass"
    return {
        "runtime": runtime,
        "state_root": str(live_state_root),
        "state_root_source": state_root_source,
        "state_root_exists": live_state_root.exists(),
        "session_id": live_session_id,
        "session_source": session_source,
        "session_dir": str(session_dir) if live_session_id else "",
        "session_dir_exists": session_dir.exists() if live_session_id else False,
        "checked_events": checked_events,
        "required_events": required_events,
        "freshness_now": freshness_now,
        "max_age_seconds": max_age_seconds,
        "observed_count": len([item for item in events if item.get("observed")]),
        "missing_events": missing_events,
        "required_missing_events": required_missing_events,
        "stale_events": stale_events,
        "required_stale_events": required_stale_events,
        "unknown_age_events": unknown_age_events,
        "required_unknown_age_events": required_unknown_age_events,
        "input_issues": input_issues,
        "events": events,
        "result": result,
    }


def hook_health_check_remediation(
    *,
    issues: list[str],
    hook_trust_state: dict[str, Any],
    live_evidence: dict[str, Any],
    checks: list[dict[str, Any]],
    smoke_results: list[dict[str, Any]],
    require_hook_trust_state: bool,
    require_live_evidence: bool,
) -> dict[str, Any]:
    categories: list[str] = []

    def is_hook_trust_issue(issue: str) -> bool:
        return issue.startswith("hook_trust_state:") or ":hook_trust_state_" in issue

    def is_check_hook_trust_issue(issue: str) -> bool:
        return issue.startswith("hook_trust_state_")

    blocked_smoke = [smoke for smoke in smoke_results if smoke.get("result") == "block"]
    smoke_global_issues: set[str] = set()
    for smoke in smoke_results:
        script_name = normalize_cell(smoke.get("script"))
        for issue in normalize_string_list(smoke.get("issues")):
            smoke_global_issues.add(issue)
            if script_name:
                smoke_global_issues.add(f"{script_name}:{issue}")

    def is_hook_smoke_issue(issue: str) -> bool:
        return (
            issue in smoke_global_issues
            or issue.startswith("unsupported_smoke_target:")
            or issue.startswith("smoke_")
            or ":smoke_" in issue
            or "_smoke_" in issue
        )

    hook_installation_issues = [
        issue
        for issue in issues
        if not issue.startswith("live_evidence:")
        and not is_hook_trust_issue(issue)
        and not is_hook_smoke_issue(issue)
    ]
    blocked_checks = [check for check in checks if check.get("result") != "pass"]
    blocked_installation_checks = [
        check
        for check in blocked_checks
        if any(not is_check_hook_trust_issue(issue) for issue in normalize_string_list(check.get("issues")))
    ]
    smoke_issues = [issue for issue in issues if is_hook_smoke_issue(issue)]

    trust_result = normalize_cell(hook_trust_state.get("result")) if hook_trust_state else ""
    trust_problem_events = []
    if hook_trust_state:
        for key in (
            "present_unverified_events",
            "missing_events",
            "disabled_events",
            "missing_trusted_hash_events",
            "hash_mismatch_events",
        ):
            trust_problem_events.extend(normalize_string_list(hook_trust_state.get(key)))
    trust_read_issues = normalize_string_list(hook_trust_state.get("issues")) if hook_trust_state else []
    trust_requires_operator = bool(
        require_hook_trust_state and hook_trust_state and (trust_result != "pass" or trust_problem_events or trust_read_issues)
    )
    if hook_trust_state and (trust_requires_operator or trust_result in {"missing", "present_unverified", "block", "not_checked"}):
        categories.append("codex_hook_trust_state")

    live_result = normalize_cell(live_evidence.get("result")) if live_evidence else ""
    live_blocking_events = []
    if live_evidence:
        for key in ("required_missing_events", "required_stale_events", "required_unknown_age_events"):
            live_blocking_events.extend(normalize_string_list(live_evidence.get(key)))
    live_requires_runtime = bool(require_live_evidence and live_evidence and (live_result == "block" or live_blocking_events))
    if live_evidence and (live_requires_runtime or live_result in {"missing", "stale"}):
        categories.append("live_evidence")

    installation_requires_operator = bool(hook_installation_issues or blocked_installation_checks)
    smoke_requires_runtime = bool(blocked_smoke or smoke_issues)

    if installation_requires_operator:
        categories.append("hook_installation")
    if smoke_requires_runtime:
        categories.append("hook_smoke")

    deduped_categories: list[str] = []
    for category in categories:
        if category not in deduped_categories:
            deduped_categories.append(category)

    result_is_blocked = bool(issues)
    if not result_is_blocked:
        next_action = "hook_health_ready"
        llm_dispatch_policy = "allow_runtime_dispatch"
    elif trust_requires_operator:
        next_action = "resolve_codex_hook_trust_state_and_rerun_hook_health_check"
        llm_dispatch_policy = "skip_llm_dispatch_until_hook_trust_state_verified"
    elif installation_requires_operator:
        next_action = "repair_hook_installation_and_rerun_hook_health_check"
        llm_dispatch_policy = "skip_llm_dispatch_until_hook_health_passes"
    elif live_requires_runtime:
        next_action = "trigger_required_hook_events_and_rerun_hook_health_check"
        llm_dispatch_policy = "skip_llm_dispatch_until_live_hook_evidence_observed"
    elif smoke_requires_runtime:
        next_action = "repair_hook_smoke_failures_and_rerun_hook_health_check"
        llm_dispatch_policy = "skip_llm_dispatch_until_hook_health_passes"
    else:
        next_action = "repair_hook_installation_and_rerun_hook_health_check"
        llm_dispatch_policy = "skip_llm_dispatch_until_hook_health_passes"

    trust_remediation = {
        "required": require_hook_trust_state,
        "result": trust_result,
        "operator_action_required": trust_requires_operator,
        "verified_events": normalize_string_list(hook_trust_state.get("verified_events")) if hook_trust_state else [],
        "present_unverified_events": normalize_string_list(hook_trust_state.get("present_unverified_events")) if hook_trust_state else [],
        "missing_events": normalize_string_list(hook_trust_state.get("missing_events")) if hook_trust_state else [],
        "disabled_events": normalize_string_list(hook_trust_state.get("disabled_events")) if hook_trust_state else [],
        "missing_trusted_hash_events": normalize_string_list(hook_trust_state.get("missing_trusted_hash_events")) if hook_trust_state else [],
        "hash_mismatch_events": normalize_string_list(hook_trust_state.get("hash_mismatch_events")) if hook_trust_state else [],
        "issues": trust_read_issues,
        "action": (
            "accept_or_refresh_codex_hook_trust_entries_then_rerun_hook_health_check"
            if trust_requires_operator
            else "no_action_required"
        ),
    }
    live_remediation = {
        "required": require_live_evidence,
        "result": live_result,
        "runtime_action_required": live_requires_runtime,
        "session_id": normalize_cell(live_evidence.get("session_id")) if live_evidence else "",
        "session_source": normalize_cell(live_evidence.get("session_source")) if live_evidence else "",
        "missing_events": normalize_string_list(live_evidence.get("missing_events")) if live_evidence else [],
        "required_missing_events": normalize_string_list(live_evidence.get("required_missing_events")) if live_evidence else [],
        "required_stale_events": normalize_string_list(live_evidence.get("required_stale_events")) if live_evidence else [],
        "required_unknown_age_events": normalize_string_list(live_evidence.get("required_unknown_age_events")) if live_evidence else [],
        "input_issues": normalize_string_list(live_evidence.get("input_issues")) if live_evidence else [],
        "action": (
            "trigger_or_restart_runtime_until_required_hook_events_are_observed_then_rerun_hook_health_check"
            if live_requires_runtime
            else "no_action_required"
        ),
    }
    installation_remediation = {
        "issues": hook_installation_issues,
        "blocked_checks": [
            {
                "event": normalize_cell(check.get("event")),
                "script": normalize_cell(check.get("script")),
                "issues": normalize_string_list(check.get("issues")),
            }
            for check in blocked_installation_checks
        ],
        "action": (
            "repair_hook_installation_and_rerun_hook_health_check"
            if hook_installation_issues or blocked_installation_checks
            else "no_action_required"
        ),
    }
    smoke_remediation = {
        "issues": smoke_issues,
        "blocked_smoke_scripts": [
            {
                "script": normalize_cell(smoke.get("script")),
                "issues": normalize_string_list(smoke.get("issues")),
            }
            for smoke in blocked_smoke
        ],
        "action": "repair_hook_smoke_failures_and_rerun_hook_health_check" if smoke_requires_runtime else "no_action_required",
    }

    return {
        "required": result_is_blocked,
        "operator_action_required": trust_requires_operator or installation_requires_operator,
        "runtime_action_required": live_requires_runtime or smoke_requires_runtime,
        "categories": deduped_categories,
        "next_action": next_action,
        "llm_dispatch_policy": llm_dispatch_policy,
        "codex_hook_trust_state": trust_remediation,
        "live_evidence": live_remediation,
        "hook_installation": installation_remediation,
        "hook_smoke": smoke_remediation,
    }


def hook_health_check_output(*, runtime: str, state_root: Path, hook_input: dict[str, Any]) -> dict[str, Any]:
    session_id, session_source = resolve_session_id(state_root, hook_input)
    if not session_id:
        session_id = "hook-health-check"
    session_dir = state_root / safe_id(session_id)
    session_dir.mkdir(parents=True, exist_ok=True)
    paths = hook_install_target_paths(runtime, hook_input)
    settings_path = paths["settings_path"]
    hooks_dir = paths["hooks_dir"]
    home_dir = hook_install_path_value(hook_input, "home_dir", "homeDir", "home") or Path.home()
    hooks_dir_explicit = hook_install_path_value(hook_input, "hooks_dir", "hooksDir") is not None
    builder_path = hook_install_builder_path(hook_input)
    run_smoke = truthy_input(hook_input.get("run_smoke") or hook_input.get("runSmoke") or hook_input.get("smoke"), default=False)
    smoke_timeout = bounded_float_input(
        hook_input.get("smoke_timeout_seconds") or hook_input.get("smokeTimeoutSeconds"),
        default=10.0,
        minimum=0.5,
        maximum=60.0,
    )
    smoke_state_root = hook_install_path_value(
        hook_input,
        "smoke_state_root",
        "smokeStateRoot",
        "smoke_state_dir",
        "smokeStateDir",
    )
    requested_smoke_scripts, smoke_target_issues = hook_health_smoke_scripts(hook_input)
    check_live_evidence = truthy_input(
        hook_input.get("check_live_evidence")
        or hook_input.get("checkLiveEvidence")
        or hook_input.get("live_evidence")
        or hook_input.get("liveEvidence"),
        default=False,
    )
    require_live_evidence = truthy_input(
        hook_input.get("require_live_evidence")
        or hook_input.get("requireLiveEvidence")
        or hook_input.get("strict_live_evidence")
        or hook_input.get("strictLiveEvidence"),
        default=False,
    )
    require_hook_trust_state = truthy_input(
        hook_input.get("require_hook_trust_state")
        or hook_input.get("requireHookTrustState")
        or hook_input.get("strict_hook_trust_state")
        or hook_input.get("strictHookTrustState"),
        default=False,
    )

    checks: list[dict[str, Any]] = []
    smoke_results: list[dict[str, Any]] = []
    live_evidence: dict[str, Any] = {}
    hook_trust_state: dict[str, Any] = {}
    issues: list[str] = []
    settings: Any = {}
    if settings_path is None or hooks_dir is None:
        issues.append("target_paths_unresolved")
    elif not settings_path.exists():
        issues.append("settings_missing")
    else:
        try:
            settings = read_json(settings_path)
        except Exception as exc:
            issues.append(f"settings_read_error:{type(exc).__name__}:{exc}")

    if not issues:
        required_specs = hook_event_specs(runtime, hooks_dir, builder_path)
        optional_specs = hook_existing_only_event_specs(runtime, hooks_dir, builder_path)
        for spec in required_specs + optional_specs:
            event = str(spec["event"])
            script_name = str(spec["script"])
            entries = [entry for entry in hook_settings_command_entries(settings, event) if script_name in normalize_cell(entry.get("command"))]
            if not entries:
                if spec in required_specs:
                    checks.append(
                        {
                            "event": event,
                            "script": script_name,
                            "result": "missing",
                            "issues": ["missing_hook_command"],
                        }
                    )
                continue
            for entry in entries:
                checks.append(
                    validate_hook_command_entry(
                        runtime=runtime,
                        home_dir=home_dir,
                        hooks_dir=hooks_dir,
                        builder_path=builder_path,
                        spec=spec,
                        entry=entry,
                        require_script_path_match=hooks_dir_explicit,
                    )
                )
        for spec in hook_retired_event_specs(runtime, hooks_dir, builder_path):
            event = str(spec["event"])
            script_name = str(spec["script"])
            entries = [entry for entry in hook_settings_command_entries(settings, event) if script_name in normalize_cell(entry.get("command"))]
            for entry in entries:
                checks.append(
                    {
                        "event": event,
                        "script": script_name,
                        "command": normalize_cell(entry.get("command")),
                        "entry_index": entry.get("entry_index"),
                        "hook_index": entry.get("hook_index"),
                        "matcher": normalize_cell(entry.get("matcher")),
                        "issues": ["retired_itb_hook_registered"],
                        "result": "block",
                    }
                )
        if runtime == "codex":
            hook_trust_state = apply_codex_hook_trust_state(
                config_path=paths.get("config_path"),
                settings_path=settings_path,
                checks=checks,
                require_hook_trust_state=require_hook_trust_state,
            )
            if require_hook_trust_state:
                issues.extend([f"hook_trust_state:{issue}" for issue in hook_trust_state.get("issues", [])])
        for check in checks:
            if check.get("result") != "pass":
                issues.extend([f"{check.get('event')}:{issue}" for issue in check.get("issues", [])])
        if run_smoke and not issues:
            issues.extend(smoke_target_issues)
        if run_smoke and not issues:
            checks_by_script: dict[str, dict[str, Any]] = {}
            for check in checks:
                script_name = normalize_cell(check.get("script"))
                if script_name and script_name not in checks_by_script:
                    checks_by_script[script_name] = check
            for script_name in requested_smoke_scripts:
                check = checks_by_script.get(script_name)
                if not check:
                    issues.append(f"{script_name}:smoke_check_missing")
                    continue
                smoke = run_hook_health_smoke(check, home_dir, smoke_timeout, smoke_state_root=smoke_state_root)
                smoke_results.append(smoke)
                if smoke.get("result") != "pass":
                    issues.extend([f"{script_name}:{issue}" for issue in smoke.get("issues", [])])

    if check_live_evidence:
        live_evidence = hook_health_live_evidence_report(
            runtime=runtime,
            home_dir=home_dir,
            checks=checks,
            hook_input=hook_input,
            require_live_evidence=require_live_evidence,
        )
        issues.extend([f"live_evidence:{issue}" for issue in live_evidence.get("input_issues", [])])
        if require_live_evidence:
            issues.extend([f"live_evidence:{event_name}:missing" for event_name in live_evidence.get("required_missing_events", [])])
            issues.extend([f"live_evidence:{event_name}:stale" for event_name in live_evidence.get("required_stale_events", [])])
            issues.extend([f"live_evidence:{event_name}:timestamp_unknown" for event_name in live_evidence.get("required_unknown_age_events", [])])

    remediation = hook_health_check_remediation(
        issues=issues,
        hook_trust_state=hook_trust_state,
        live_evidence=live_evidence,
        checks=checks,
        smoke_results=smoke_results,
        require_hook_trust_state=require_hook_trust_state,
        require_live_evidence=require_live_evidence,
    )
    event = {
        "ts": current_timestamp(),
        "runtime": runtime,
        "event_type": "hook_health_check",
        "session_id": session_id,
        "session_source": session_source,
        "settings_path": str(settings_path) if settings_path else "",
        "hooks_dir": str(hooks_dir) if hooks_dir else "",
        "home_dir": str(home_dir),
        "builder_path": str(builder_path),
        "run_smoke": run_smoke,
        "smoke_scripts": requested_smoke_scripts if run_smoke else [],
        "smoke_state_root": str(smoke_state_root) if smoke_state_root else "",
        "check_live_evidence": check_live_evidence,
        "require_live_evidence": require_live_evidence,
        "require_hook_trust_state": require_hook_trust_state,
        "hook_trust_state_result": hook_trust_state.get("result", "") if hook_trust_state else "",
        "live_evidence_result": live_evidence.get("result", "") if live_evidence else "",
        "checked_count": len(checks),
        "smoke_count": len(smoke_results),
        "issues": issues,
        "result": "pass" if not issues else "block",
        "next_action": remediation["next_action"],
        "llm_dispatch_policy": remediation["llm_dispatch_policy"],
        "remediation_required": remediation["required"],
        "operator_action_required": remediation["operator_action_required"],
        "runtime_action_required": remediation["runtime_action_required"],
        "remediation_categories": remediation["categories"],
    }
    append_jsonl_atomic(session_dir / "hook-health-check-events.jsonl", event)
    output = {"hookHealthCheck": event | {"checks": checks, "smoke_results": smoke_results, "remediation": remediation}}
    if hook_trust_state:
        output["hookHealthCheck"]["hook_trust_state"] = hook_trust_state
    if live_evidence:
        output["hookHealthCheck"]["live_evidence"] = live_evidence
    if issues:
        output["decision"] = "block"
        output["reason"] = "; ".join(issues)
    return output


def hook_install_output(*, runtime: str, state_root: Path, hook_input: dict[str, Any]) -> dict[str, Any]:
    apply_requested = truthy_input(
        hook_input.get("apply")
        or hook_input.get("write")
        or hook_input.get("install"),
        default=False,
    )
    dry_run = truthy_input(hook_input.get("dry_run") or hook_input.get("dryRun"), default=not apply_requested)
    backup_enabled = truthy_input(hook_input.get("backup"), default=True)
    session_id, session_source = resolve_session_id(state_root, hook_input)
    if not session_id:
        session_id = "hook-install"
    session_dir = state_root / safe_id(session_id)
    session_dir.mkdir(parents=True, exist_ok=True)
    paths = hook_install_target_paths(runtime, hook_input)
    settings_path = paths["settings_path"]
    config_path = paths["config_path"]
    hooks_dir = paths["hooks_dir"]
    if settings_path is None or hooks_dir is None:
        return {"decision": "block", "reason": "hook-install target paths could not be resolved"}
    builder_path = hook_install_builder_path(hook_input)

    planned_changes: list[dict[str, Any]] = []
    updated_paths: list[str] = []
    backup_paths: list[str] = []
    errors: list[str] = []

    current_settings: Any = {}
    if settings_path.exists():
        try:
            current_settings = read_json(settings_path)
        except Exception as exc:
            errors.append(f"failed to read settings JSON {settings_path}: {type(exc).__name__}: {exc}")
    merged_settings, settings_changes = merge_hook_settings(current_settings, runtime, hooks_dir, builder_path)
    before_settings_text = settings_path.read_text(encoding="utf-8") if settings_path.exists() and not errors else ""
    after_settings_text = json.dumps(merged_settings, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    settings_changed = before_settings_text != after_settings_text
    planned_changes.append(
        hook_file_change(
            settings_path,
            before_settings_text,
            after_settings_text,
            "update_hook_settings",
            {"logical_changes": settings_changes},
        )
    )
    if settings_changed and not dry_run and not errors:
        write_path = hook_atomic_write_path(settings_path)
        if backup_enabled:
            backup_target_file(write_path, backup_paths)
        atomic_write_text(write_path, after_settings_text)
        updated_paths.append(str(write_path))

    if runtime == "codex" and config_path is not None:
        before_config_text = config_path.read_text(encoding="utf-8") if config_path.exists() else ""
        after_config_text, config_changes = ensure_codex_hooks_feature(before_config_text)
        config_changed = before_config_text != after_config_text
        planned_changes.append(
            hook_file_change(
                config_path,
                before_config_text,
                after_config_text,
                "enable_codex_hooks_feature",
                {"logical_changes": config_changes},
            )
        )
        if config_changed and not dry_run and not errors:
            write_path = hook_atomic_write_path(config_path)
            if backup_enabled:
                backup_target_file(write_path, backup_paths)
            atomic_write_text(write_path, after_config_text)
            updated_paths.append(str(write_path))

    for file_name in HOOK_WRAPPER_INSTALL_FILES:
        source_path = HOOK_BUNDLE_DIR / file_name
        target_path = hooks_dir / file_name
        if not source_path.exists():
            errors.append(f"missing hook wrapper source: {source_path}")
            continue
        before_sha = file_sha256_if_exists(target_path)
        after_sha = file_sha256_if_exists(source_path)
        changed = before_sha != after_sha
        planned_changes.append(
            {
                "path": str(target_path),
                "action": "copy_hook_wrapper",
                "source_path": str(source_path),
                "before_sha256": before_sha,
                "after_sha256": after_sha,
                "changed": changed,
            }
        )
        if changed and not dry_run and not errors:
            target_path.parent.mkdir(parents=True, exist_ok=True)
            if backup_enabled:
                backup_target_file(target_path, backup_paths)
            shutil.copy2(source_path, target_path)
            target_path.chmod(source_path.stat().st_mode & 0o777)
            updated_paths.append(str(target_path))

    changed_count = sum(1 for change in planned_changes if change.get("changed"))
    event = {
        "ts": current_timestamp(),
        "runtime": runtime,
        "event_type": "hook_install",
        "session_id": session_id,
        "session_source": session_source,
        "dry_run": dry_run,
        "apply_requested": apply_requested,
        "settings_path": str(settings_path),
        "config_path": str(config_path) if config_path else "",
        "hooks_dir": str(hooks_dir),
        "builder_path": str(builder_path),
        "planned_change_count": changed_count,
        "updated_paths": updated_paths,
        "backup_paths": backup_paths,
        "errors": errors,
        "result": "blocked" if errors else ("dry_run" if dry_run else "updated"),
    }
    append_jsonl_atomic(session_dir / "hook-install-events.jsonl", event)
    output = {
        "hookInstall": event | {
            "planned_changes": planned_changes,
        }
    }
    if errors:
        output["decision"] = "block"
        output["reason"] = "; ".join(errors)
    return output



def final_gate_loop_policy() -> dict[str, Any]:
    return {
        "total_recovery_cycle_budget": FINAL_GATE_DEFAULT_RECOVERY_CYCLE_BUDGET,
        "tuning_range": list(FINAL_GATE_RECOVERY_CYCLE_TUNING_RANGE),
        "same_blocker_consecutive_cap": FINAL_GATE_SAME_BLOCKER_CONSECUTIVE_CAP,
        "budget_unit": FINAL_GATE_BUDGET_UNIT,
    }


def final_gate_pointer_path(session_dir: Path, hook_input: dict[str, Any]) -> Path:
    raw = normalize_cell(
        hook_input.get("active_execution_context_pointer_path")
        or hook_input.get("activeExecutionContextPointerPath")
        or hook_input.get("pointer_path")
        or hook_input.get("pointerPath")
        or os.environ.get("ITB_ACTIVE_EXECUTION_CONTEXT_POINTER")
    )
    return Path(raw).expanduser() if raw else session_dir / "active-execution-context.json"


def final_gate_legacy_gate_command_path(hook_input: dict[str, Any]) -> Path | None:
    raw = normalize_cell(
        hook_input.get("legacy_gate_command_artifact_path")
        or hook_input.get("legacyGateCommandArtifactPath")
        or hook_input.get("gate_command_artifact_path")
        or hook_input.get("gateCommandArtifactPath")
    )
    return Path(raw).expanduser() if raw else None


def final_gate_read_json_object(path: Path) -> tuple[dict[str, Any], str]:
    try:
        data = read_json(path)
    except FileNotFoundError:
        return {}, "missing"
    except Exception as exc:
        return {}, f"read_error:{type(exc).__name__}:{exc}"
    if not isinstance(data, dict):
        return {}, "not_object"
    return data, ""


def final_gate_blocker(
    blocker_id: str,
    detail: str,
    *,
    owner: str = "coordinator",
    next_action: str = "fix",
) -> dict[str, Any]:
    if next_action not in FINAL_GATE_NEXT_ACTIONS:
        next_action = "mark_blocked"
    return {
        "id": blocker_id,
        "severity": "blocking",
        "owner": owner,
        "next_action": next_action,
        "detail": detail,
    }


def final_gate_schema(
    *,
    verdict: str,
    context_type: str,
    context_id: str,
    reason_code: str,
    blockers: list[dict[str, Any]] | None = None,
    allowed_next_actions: list[str] | None = None,
) -> dict[str, Any]:
    blockers = blockers or []
    allowed = [item for item in (allowed_next_actions or []) if item in FINAL_GATE_NEXT_ACTIONS]
    if verdict == "allow":
        blockers = []
        allowed = []
    if context_type not in EXECUTION_CONTEXT_TYPES:
        context_type = "execution"
    if reason_code not in FINAL_GATE_REASON_CODES:
        reason_code = "blocked" if verdict == "block" else "complete"
    return {
        "verdict": "block" if verdict == "block" else "allow",
        "context_type": context_type,
        "context_id": context_id or "null",
        "reason_code": reason_code,
        "blockers": blockers,
        "allowed_next_actions": allowed,
    }


def final_gate_complete_status(value: Any) -> bool:
    normalized = normalized_publication_value(value)
    return normalized in {"pass", "passed", "complete", "completed", "done", "closed", "approved", "satisfied", "ok", "true"}


def final_gate_required_item(item: dict[str, Any]) -> bool:
    for key in ("required", "is_required", "isRequired"):
        if key in item:
            return truthy_input(item.get(key), default=True)
    return True


def final_gate_artifact_evidence_blocker(artifact: dict[str, Any], artifact_id: str) -> dict[str, Any] | None:
    for key in ("evidence_marker", "evidenceMarker", "evidence_id", "evidenceId"):
        if normalize_cell(artifact.get(key)):
            return None
    evidence = artifact.get("evidence")
    if isinstance(evidence, dict) and evidence:
        return None
    if isinstance(evidence, list) and evidence:
        return None
    if isinstance(evidence, str) and normalize_cell(evidence):
        return None
    evidence_path_value = normalize_cell(
        artifact.get("evidence_path")
        or artifact.get("evidencePath")
        or artifact.get("evidence_file")
        or artifact.get("evidenceFile")
    )
    if not evidence_path_value:
        return final_gate_blocker("missing_required_artifact_evidence", f"{artifact_id} evidence marker/path is missing.", next_action="fix")
    evidence_path = Path(evidence_path_value).expanduser()
    if not evidence_path.exists():
        return final_gate_blocker("missing_required_artifact_evidence", f"{artifact_id} evidence path does not exist: {evidence_path}", next_action="fix")
    return None


def final_gate_context_path_from_pointer(pointer: dict[str, Any]) -> str:
    active = pointer.get("active_execution_context")
    if isinstance(active, dict):
        for key in ("path", "context_path", "contextPath", "execution_context_path", "executionContextPath"):
            value = normalize_cell(active.get(key))
            if value:
                return value
    for key in (
        "active_execution_context_path",
        "activeExecutionContextPath",
        "execution_context_path",
        "executionContextPath",
        "context_path",
        "contextPath",
    ):
        value = normalize_cell(pointer.get(key))
        if value:
            return value
    if isinstance(active, str) and ("/" in active or active.endswith(".json")):
        return active
    return ""


def final_gate_context_id_from_context(context: dict[str, Any], pointer: dict[str, Any]) -> str:
    for source in (context, pointer):
        for key in ("context_id", "contextId", "task_id", "taskId", "id"):
            value = normalize_cell(source.get(key))
            if value:
                return value
    active = pointer.get("active_execution_context")
    if isinstance(active, dict):
        for key in ("context_id", "contextId", "task_id", "taskId", "id"):
            value = normalize_cell(active.get(key))
            if value:
                return value
    elif isinstance(active, str) and active and "/" not in active and not active.endswith(".json"):
        return active
    return ""


def final_gate_blocking_level(context: dict[str, Any]) -> tuple[str, list[dict[str, Any]]]:
    raw = normalize_cell(context.get("blocking_level") or context.get("blockingLevel"))
    if raw == "none":
        return "none", []
    if raw == "non_blocking":
        return "blocking", [
            final_gate_blocker(
                "unsupported_blocking_level",
                "`non_blocking` is not a valid blocking level; use `none` or `blocking`.",
                next_action="mark_blocked",
            )
        ]
    return "blocking", []


def final_gate_artifact_blockers(context: dict[str, Any]) -> list[dict[str, Any]]:
    blockers: list[dict[str, Any]] = []
    artifacts = context.get("required_artifacts") or context.get("requiredArtifacts") or []
    if not isinstance(artifacts, list):
        return [final_gate_blocker("invalid_required_artifacts", "required_artifacts must be a list.", next_action="mark_blocked")]
    for index, artifact in enumerate(artifacts):
        if not isinstance(artifact, dict):
            blockers.append(final_gate_blocker("invalid_required_artifact", f"required_artifacts[{index}] must be an object.", next_action="mark_blocked"))
            continue
        if not final_gate_required_item(artifact):
            continue
        artifact_id = normalize_cell(artifact.get("id") or artifact.get("name") or f"artifact_{index}")
        status = artifact.get("status")
        if not final_gate_complete_status(status):
            blockers.append(final_gate_blocker("missing_required_artifact", f"{artifact_id} status is not complete.", next_action="fix"))
            continue
        path_value = normalize_cell(artifact.get("path") or artifact.get("artifact_path") or artifact.get("artifactPath"))
        if not path_value:
            blockers.append(final_gate_blocker("missing_required_artifact", f"{artifact_id} path is missing.", next_action="fix"))
            continue
        path = Path(path_value).expanduser()
        if not path.exists():
            blockers.append(final_gate_blocker("missing_required_artifact", f"{artifact_id} does not exist: {path}", next_action="fix"))
            continue
        evidence_blocker = final_gate_artifact_evidence_blocker(artifact, artifact_id)
        if evidence_blocker is not None:
            blockers.append(evidence_blocker)
            continue
        expected_sha = normalize_cell(artifact.get("sha256") or artifact.get("expected_sha256") or artifact.get("expectedSha256"))
        if expected_sha:
            actual_sha = file_sha256_if_exists(path)
            if actual_sha != expected_sha:
                blockers.append(final_gate_blocker("artifact_hash_mismatch", f"{artifact_id} sha256 does not match: {path}", next_action="fix"))
    return blockers


def final_gate_required_check_blockers(context: dict[str, Any]) -> list[dict[str, Any]]:
    checks = context.get("required_checks") or context.get("requiredChecks") or []
    if not isinstance(checks, list):
        return [final_gate_blocker("invalid_required_checks", "required_checks must be a list.", next_action="mark_blocked")]
    blockers: list[dict[str, Any]] = []
    for index, check in enumerate(checks):
        if not isinstance(check, dict):
            blockers.append(final_gate_blocker("invalid_required_check", f"required_checks[{index}] must be an object.", next_action="mark_blocked"))
            continue
        if not final_gate_required_item(check):
            continue
        if not final_gate_complete_status(check.get("status")):
            check_id = normalize_cell(check.get("id") or check.get("name") or f"check_{index}")
            blockers.append(final_gate_blocker("missing_required_check", f"{check_id} is not complete.", next_action="fix"))
    return blockers


def final_gate_open_finding_blockers(context: dict[str, Any]) -> list[dict[str, Any]]:
    findings = context.get("open_blocking_findings") or context.get("openBlockingFindings") or []
    if isinstance(findings, int):
        return [final_gate_blocker("open_blocking_finding", f"{findings} blocking finding(s) remain open.", next_action="fix")] if findings > 0 else []
    if not isinstance(findings, list):
        return [final_gate_blocker("invalid_open_blocking_findings", "open_blocking_findings must be a list or count.", next_action="mark_blocked")]
    blockers: list[dict[str, Any]] = []
    for index, finding in enumerate(findings):
        if not isinstance(finding, dict):
            blockers.append(final_gate_blocker("invalid_open_blocking_finding", f"open_blocking_findings[{index}] must be an object.", next_action="mark_blocked"))
            continue
        status = normalized_publication_value(finding.get("status"))
        if status not in {"closed", "resolved", "done", "complete"}:
            finding_id = normalize_cell(finding.get("id") or f"finding_{index}")
            blockers.append(final_gate_blocker("open_blocking_finding", f"{finding_id} remains open.", next_action="fix"))
    return blockers


def final_gate_pending_work_blockers(context: dict[str, Any]) -> list[dict[str, Any]]:
    pending = context.get("pending_work_units") or context.get("pendingWorkUnits") or []
    if isinstance(pending, int):
        return [final_gate_blocker("pending_work_unit", f"{pending} required work unit(s) remain pending.", next_action="fix")] if pending > 0 else []
    if not isinstance(pending, list):
        return [final_gate_blocker("invalid_pending_work_units", "pending_work_units must be a list or count.", next_action="mark_blocked")]
    blockers: list[dict[str, Any]] = []
    for index, work in enumerate(pending):
        if not isinstance(work, dict):
            blockers.append(final_gate_blocker("invalid_pending_work_unit", f"pending_work_units[{index}] must be an object.", next_action="mark_blocked"))
            continue
        if not final_gate_complete_status(work.get("status")):
            work_id = normalize_cell(work.get("id") or work.get("name") or f"work_{index}")
            blockers.append(final_gate_blocker("pending_work_unit", f"{work_id} is not complete.", next_action="fix"))
    return blockers


def final_gate_from_execution_context(context: dict[str, Any], pointer: dict[str, Any]) -> dict[str, Any]:
    context_type = normalize_cell(context.get("context_type") or context.get("contextType") or pointer.get("context_type") or pointer.get("contextType") or "execution")
    context_id = final_gate_context_id_from_context(context, pointer)
    if context_type not in EXECUTION_CONTEXT_TYPES:
        return final_gate_schema(
            verdict="block",
            context_type="execution",
            context_id=context_id,
            reason_code="blocked",
            blockers=[final_gate_blocker("invalid_context_type", f"context_type is not supported: {context_type}", next_action="mark_blocked")],
            allowed_next_actions=["mark_blocked"],
        )
    blocking_level, blockers = final_gate_blocking_level(context)
    if blocking_level == "none":
        return final_gate_schema(verdict="allow", context_type=context_type, context_id=context_id, reason_code="no_active_context")
    goal_status = normalized_publication_value(context.get("goal_status") or context.get("goalStatus"))
    human_approval_required = truthy_input(context.get("human_approval_required") or context.get("humanApprovalRequired"))
    if human_approval_required:
        blockers.append(final_gate_blocker("required_human_approval", "Human approval is required before final response.", next_action="ask_human"))
    if goal_status == "blocked":
        blockers.append(final_gate_blocker("execution_context_blocked", "Execution context is marked blocked.", next_action="mark_blocked"))
    blockers.extend(final_gate_required_check_blockers(context))
    blockers.extend(final_gate_artifact_blockers(context))
    blockers.extend(final_gate_open_finding_blockers(context))
    blockers.extend(final_gate_pending_work_blockers(context))
    final_response_allowed = context.get("final_response_allowed")
    if final_response_allowed is False:
        blockers.append(final_gate_blocker("final_response_not_allowed", "execution_context.final_response_allowed is false.", next_action="fix"))
    if blockers:
        next_actions: list[str] = []
        for blocker in blockers:
            action = normalize_cell(blocker.get("next_action"))
            if action in FINAL_GATE_NEXT_ACTIONS and action not in next_actions:
                next_actions.append(action)
        reason_code = "required_approval" if any(item.get("next_action") == "ask_human" for item in blockers) else ("blocked" if any(item.get("next_action") == "mark_blocked" for item in blockers) else "incomplete")
        return final_gate_schema(
            verdict="block",
            context_type=context_type,
            context_id=context_id,
            reason_code=reason_code,
            blockers=blockers,
            allowed_next_actions=next_actions or ["fix"],
        )
    if goal_status in {"complete", "completed", "done"} or final_response_allowed is True:
        return final_gate_schema(verdict="allow", context_type=context_type, context_id=context_id, reason_code="complete")
    return final_gate_schema(
        verdict="block",
        context_type=context_type,
        context_id=context_id,
        reason_code="incomplete",
        blockers=[final_gate_blocker("goal_not_complete", f"goal_status is not complete: {goal_status or 'missing'}", next_action="fix")],
        allowed_next_actions=["fix"],
    )


def final_gate_from_legacy_gate_command(path: Path) -> dict[str, Any]:
    payload, issue = final_gate_read_json_object(path)
    if issue:
        return final_gate_schema(
            verdict="block",
            context_type="execution",
            context_id="null",
            reason_code="blocked",
            blockers=[final_gate_blocker("legacy_gate_command_unreadable", f"{path}: {issue}", next_action="mark_blocked")],
            allowed_next_actions=["mark_blocked"],
        )
    status = normalize_cell(payload.get("status"))
    context_id = normalize_cell(payload.get("task_id") or payload.get("context_id"))
    if status in {"pass", "complete"} and truthy_input(payload.get("next_phase_allowed"), default=True):
        return final_gate_schema(verdict="allow", context_type="execution", context_id=context_id, reason_code="complete")
    blockers = []
    for index, detail in enumerate(normalize_string_list(payload.get("missing_evidence") or payload.get("validation_errors") or payload.get("blockers"))):
        blockers.append(final_gate_blocker("legacy_gate_command_blocker", detail or f"legacy blocker {index}", next_action="fix"))
    if not blockers:
        blockers.append(final_gate_blocker("legacy_gate_command_blocker", "Legacy gateCommand status is not pass.", next_action="fix"))
    return final_gate_schema(
        verdict="block",
        context_type="execution",
        context_id=context_id,
        reason_code="incomplete",
        blockers=blockers,
        allowed_next_actions=["fix"],
    )


def final_gate_from_active_task_state(
    active_task: dict[str, Any] | None,
    active_errors: list[str],
) -> dict[str, Any] | None:
    if active_errors:
        return final_gate_schema(
            verdict="block",
            context_type="execution",
            context_id="active_task",
            reason_code="blocked",
            blockers=[
                final_gate_blocker(
                    "invalid_active_task_state",
                    "; ".join(active_errors),
                    next_action="mark_blocked",
                )
            ],
            allowed_next_actions=["mark_blocked"],
        )
    if not active_task:
        return None
    task_id = normalize_cell(active_task.get("task_id") or active_task.get("taskId")) or "active_task"
    flow_phase = active_task_flow_phase(active_task)
    return final_gate_schema(
        verdict="block",
        context_type="execution",
        context_id=task_id,
        reason_code="incomplete",
        blockers=[
            final_gate_blocker(
                "active_task_without_execution_context",
                f"Active task {task_id} is {flow_phase} but the session pointer has no execution context.",
                next_action="fix",
            )
        ],
        allowed_next_actions=["fix"],
    )


def execution_context_final_response_guard_output(*, runtime: str, state_root: Path, hook_input: dict[str, Any]) -> dict[str, Any]:
    session_id, session_source = resolve_session_id(state_root, hook_input)
    if not session_id:
        session_id = "final-response-guard"
    session_dir = state_root / safe_id(session_id)
    session_dir.mkdir(parents=True, exist_ok=True)
    now = current_timestamp()
    pointer_path = final_gate_pointer_path(session_dir, hook_input)
    legacy_path = final_gate_legacy_gate_command_path(hook_input)
    pointer, pointer_issue = final_gate_read_json_object(pointer_path)
    context_path = final_gate_context_path_from_pointer(pointer) if pointer else ""
    active_task, active_errors, active_warnings = load_active_task(session_dir)
    active_task_gate = final_gate_from_active_task_state(active_task, active_errors)
    context: dict[str, Any] = {}
    context_issue = ""
    if pointer_issue == "missing" and legacy_path is not None:
        gate = final_gate_from_legacy_gate_command(legacy_path)
        source = "legacy_gate_command_adapter"
    elif pointer_issue == "missing":
        gate = active_task_gate or final_gate_schema(verdict="allow", context_type="none", context_id="null", reason_code="no_active_context")
        source = "active_task" if active_task_gate else "no_active_pointer"
    elif pointer_issue:
        gate = final_gate_schema(
            verdict="block",
            context_type="execution",
            context_id="null",
            reason_code="blocked",
            blockers=[final_gate_blocker("invalid_session_pointer", f"{pointer_path}: {pointer_issue}", next_action="mark_blocked")],
            allowed_next_actions=["mark_blocked"],
        )
        source = "session_pointer"
    elif not pointer.get("active_execution_context") and not context_path:
        gate = active_task_gate or final_gate_schema(verdict="allow", context_type="none", context_id="null", reason_code="no_active_context")
        source = "active_task" if active_task_gate else "session_pointer"
    elif not context_path:
        gate = final_gate_schema(
            verdict="block",
            context_type="execution",
            context_id=final_gate_context_id_from_context({}, pointer),
            reason_code="blocked",
            blockers=[final_gate_blocker("execution_context_path_missing", "Session pointer references an active context without a context path.", next_action="mark_blocked")],
            allowed_next_actions=["mark_blocked"],
        )
        source = "session_pointer"
    else:
        context_file = Path(context_path).expanduser()
        if not context_file.is_absolute():
            context_file = session_dir / context_file
        context_path = str(context_file)
        context, context_issue = final_gate_read_json_object(context_file)
        if context_issue:
            gate = final_gate_schema(
                verdict="block",
                context_type="execution",
                context_id=final_gate_context_id_from_context({}, pointer),
                reason_code="blocked",
                blockers=[final_gate_blocker("execution_context_unreadable", f"{context_path}: {context_issue}", next_action="mark_blocked")],
                allowed_next_actions=["mark_blocked"],
            )
        else:
            gate = final_gate_from_execution_context(context, pointer)
        source = "execution_context"

    enforce = truthy_input(
        hook_input.get("enforce_final_gate")
        or hook_input.get("enforceFinalGate")
        or hook_input.get("hard_block")
        or hook_input.get("hardBlock")
        or os.environ.get("ITB_FINAL_GATE_HARD_BLOCK"),
        default=False,
    )
    event = {
        "ts": now,
        "runtime": runtime,
        "event_type": "final_response_guard",
        "session_id": session_id,
        "session_source": session_source,
        "result": "final_gate_allowed" if gate["verdict"] == "allow" else ("blocked_execution_context" if enforce else "advisory_block_execution_context"),
        "notification_class": "silent" if gate["verdict"] == "allow" else "flow_alert",
        "source": source,
        "active_execution_context_pointer_path": str(pointer_path),
        "execution_context_path": context_path,
        "execution_context_read_issue": context_issue,
        "active_task_result": normalize_cell(active_task.get("status")) if active_task else "",
        "active_task_warnings": active_warnings,
        "legacy_gate_command_artifact_path": str(legacy_path) if legacy_path else "",
        "hard_block_enforced": enforce,
        "loop_policy": final_gate_loop_policy(),
        "final_gate": gate,
    }
    append_jsonl_atomic(session_dir / "final-response-guard-events.jsonl", event)
    output = {
        "permissionDecision": "allow",
        "finalResponseGuard": event,
        "finalGate": gate,
    }
    if gate["verdict"] == "block" and enforce:
        output["decision"] = "block"
        output["permissionDecision"] = "deny"
        output["reason"] = "; ".join(normalize_cell(item.get("detail")) for item in gate["blockers"]) or "final response blocked by execution context"
    return output


def final_response_guard_output(*, runtime: str, state_root: Path, hook_input: dict[str, Any]) -> dict[str, Any]:
    if not truthy_input(hook_input.get("legacy_final_response_guard") or hook_input.get("legacyFinalResponseGuard")):
        return execution_context_final_response_guard_output(runtime=runtime, state_root=state_root, hook_input=hook_input)
    session_id, session_source = resolve_session_id(state_root, hook_input)
    if not session_id:
        session_id = "final-response-guard"
    session_dir = state_root / safe_id(session_id)
    session_dir.mkdir(parents=True, exist_ok=True)
    now = current_timestamp()
    active_task, active_errors, active_warnings = load_active_task(session_dir)
    explicit_task_detail_path = hook_task_detail_path(hook_input)
    task_detail_path = explicit_task_detail_path
    task_detail_source = "hook_input" if explicit_task_detail_path is not None else ""
    if task_detail_path is None and active_task is not None:
        task_detail_path = active_task_detail_path(active_task)
        task_detail_source = "active_task"
    raw_phase = hook_flow_phase_raw(hook_input)
    if raw_phase is not None:
        flow_phase = normalize_flow_phase(raw_phase, default="pre_final_response")
    elif active_task is not None:
        flow_phase = active_task_flow_phase(active_task)
    else:
        flow_phase = "pre_execution"
    errors = list(active_errors)
    warnings = list(active_warnings)
    auto_final_transport_render_check = truthy_input(
        hook_input.get("auto_final_transport_render_check")
        or hook_input.get("autoFinalTransportRenderCheck")
        or hook_input.get("run_final_transport_render_check")
        or hook_input.get("runFinalTransportRenderCheck"),
        default=True,
    )
    event = {
        "ts": now,
        "runtime": runtime,
        "event_type": "final_response_guard",
        "session_id": session_id,
        "session_source": session_source,
        "flow_phase": flow_phase,
        "task_detail_path": str(task_detail_path) if task_detail_path else "",
        "task_detail_source": task_detail_source,
        "auto_final_transport_render_check": auto_final_transport_render_check,
        "validation_errors": errors,
        "validation_warnings": warnings,
    }
    if errors:
        event["result"] = "blocked_active_task_state"
        event["notification_class"] = "flow_alert"
        append_jsonl_atomic(session_dir / "final-response-guard-events.jsonl", event)
        reason = "; ".join(errors)
        return {
            "decision": "block",
            "permissionDecision": "deny",
            "reason": reason,
            "finalResponseGuard": event,
        }
    if task_detail_path is None:
        event["result"] = "skipped_no_active_pre_final_task"
        event["notification_class"] = "silent"
        append_jsonl_atomic(session_dir / "final-response-guard-events.jsonl", event)
        return {"permissionDecision": "allow", "finalResponseGuard": event}
    if flow_phase != "pre_final_response":
        event["result"] = "skipped_non_pre_final_phase"
        event["notification_class"] = "silent"
        append_jsonl_atomic(session_dir / "final-response-guard-events.jsonl", event)
        return {"permissionDecision": "allow", "finalResponseGuard": event}

    guard_input = hook_input | {
        "session_id": session_id,
        "task_detail_path": str(task_detail_path),
        "flow_phase": "pre_final_response",
        "auto_final_transport_render_check": auto_final_transport_render_check,
        "source": "final-response-guard",
    }
    finalization_output = gate_precheck_output(
        runtime=runtime,
        state_root=state_root,
        hook_input=guard_input,
        gate_role="finalization-check",
    )
    gate_command = finalization_output.get("gateCommand") if isinstance(finalization_output, dict) else {}
    if not isinstance(gate_command, dict):
        gate_command = {}
    final_transport = gate_command.get("final_transport_render_check")
    if not isinstance(final_transport, dict):
        final_transport = {}
    gate_status = normalize_cell(gate_command.get("status"))
    transport_status = normalize_cell(final_transport.get("status"))
    transport_result = normalize_cell(final_transport.get("result"))
    passed = (
        gate_status == "pass"
        and bool(gate_command.get("next_phase_allowed"))
        and normalize_cell(gate_command.get("handoff_to")) == "main_transport_renderer"
        and (
            not auto_final_transport_render_check
            or transport_status == "complete"
            or transport_result in {"updated", "dry_run"}
        )
    )
    event = event | {
        "gate_command_status": gate_status,
        "gate_command_artifact_path": normalize_cell(gate_command.get("artifact_path")),
        "final_transport_render_status": transport_status,
        "final_transport_render_result": transport_result,
        "notification_class": normalize_cell(gate_command.get("notification_class")) or "flow_alert",
        "validation_errors": list(errors) + list(gate_command.get("validation_errors") or []),
        "validation_warnings": list(warnings) + list(gate_command.get("validation_warnings") or []),
        "result": "final_response_allowed" if passed else "blocked_finalization_gate",
    }
    append_jsonl_atomic(session_dir / "final-response-guard-events.jsonl", event)
    if passed:
        return {
            "permissionDecision": "allow",
            "finalResponseGuard": event,
            "gateCommand": gate_command,
        }
    reason = normalize_cell(finalization_output.get("reason") if isinstance(finalization_output, dict) else "")
    if not reason:
        reason = normalize_cell(gate_command.get("reason")) or "final response blocked by finalization gate"
    return {
        "decision": "block",
        "permissionDecision": "deny",
        "reason": reason,
        "finalResponseGuard": event,
        "gateCommand": gate_command,
    }


def notification_input_bool(hook_input: dict[str, Any], *keys: str, env_name: str = "", default: bool = False) -> bool:
    for key in keys:
        if key in hook_input:
            return truthy_input(hook_input.get(key), default=default)
    if env_name:
        return env_flag(env_name, default=default)
    return default


def notification_class_list(value: Any) -> list[str]:
    items: list[str] = []
    for item in normalize_string_list(value):
        items.extend(part.strip() for part in str(item).split(",") if part.strip())
    return [normalize_cell(item) for item in items if normalize_cell(item)]


def applescript_string(value: Any, *, max_length: int = 220) -> str:
    text = normalize_cell(value)
    if len(text) > max_length:
        text = text[: max_length - 1] + "..."
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def notification_dispatch_output(*, runtime: str, state_root: Path, hook_input: dict[str, Any]) -> dict[str, Any]:
    session_id, session_source = resolve_session_id(state_root, hook_input)
    if not session_id:
        session_id = "notification-dispatch"
    session_dir = state_root / safe_id(session_id)
    session_dir.mkdir(parents=True, exist_ok=True)
    now = current_timestamp()
    errors = hook_input.get("errors")
    if isinstance(errors, str):
        error_list = [errors] if errors else []
    elif isinstance(errors, list):
        error_list = [normalize_cell(item) for item in errors if normalize_cell(item)]
    else:
        error_list = []
    notification_class = normalize_cell(
        hook_input.get("notification_class")
        or hook_input.get("notificationClass")
        or notification_class_for_event(
            event_type=normalize_cell(hook_input.get("event_type") or hook_input.get("eventType")),
            result=normalize_cell(hook_input.get("result")),
            status=normalize_cell(hook_input.get("status")),
            decision=normalize_cell(hook_input.get("decision")),
            approval_required=truthy_input(hook_input.get("approval_required") or hook_input.get("approvalRequired")),
            errors=error_list,
        )
    )
    allowed_classes = notification_class_list(
        hook_input.get("notification_classes")
        or hook_input.get("notificationClasses")
        or os.environ.get("ITB_OS_NOTIFICATION_CLASSES")
        or "flow_alert,approval_wait"
    )
    dry_run = notification_input_bool(hook_input, "dry_run", "dryRun", default=False)
    enabled = notification_input_bool(
        hook_input,
        "enable_os_notification",
        "enableOsNotification",
        "send_os_notification",
        "sendOsNotification",
        env_name="ITB_OS_NOTIFICATIONS",
        default=False,
    )
    force = notification_input_bool(hook_input, "force_notification", "forceNotification", default=False)
    task_id = normalize_cell(hook_input.get("task_id") or hook_input.get("taskId"))
    event_type = normalize_cell(hook_input.get("event_type") or hook_input.get("eventType") or "itb_notification")
    default_titles = {
        "flow_alert": "ITB Flow Alert",
        "approval_wait": "ITB Approval Required",
        "done": "ITB Task Complete",
        "silent": "ITB Notification",
    }
    title = normalize_cell(hook_input.get("title")) or default_titles.get(notification_class, "ITB Notification")
    subtitle = normalize_cell(hook_input.get("subtitle") or hook_input.get("subTitle") or task_id or event_type)
    body = normalize_cell(
        hook_input.get("body")
        or hook_input.get("message")
        or hook_input.get("reason")
        or hook_input.get("summary")
        or f"{event_type}: {notification_class}"
    )
    sound_name = normalize_cell(hook_input.get("sound_name") or hook_input.get("soundName"))
    script_parts = [f"display notification {applescript_string(body)}"]
    if title:
        script_parts.append(f"with title {applescript_string(title, max_length=80)}")
    if subtitle:
        script_parts.append(f"subtitle {applescript_string(subtitle, max_length=120)}")
    if sound_name:
        script_parts.append(f"sound name {applescript_string(sound_name, max_length=80)}")
    script = " ".join(script_parts)
    event = {
        "ts": now,
        "runtime": runtime,
        "event_type": "notification_dispatch",
        "session_id": session_id,
        "session_source": session_source,
        "source_event_type": event_type,
        "task_id": task_id,
        "notification_class": notification_class,
        "allowed_notification_classes": allowed_classes,
        "enabled": enabled,
        "dry_run": dry_run,
        "force": force,
        "title": title,
        "subtitle": subtitle,
        "body": body,
        "sound_name": sound_name,
        "osascript": script,
    }
    if notification_class in {"", "silent"} and not force:
        event["result"] = "skipped_silent"
        append_jsonl_atomic(session_dir / "notification-events.jsonl", event)
        return {"notificationDispatch": event}
    if notification_class not in allowed_classes and not force:
        event["result"] = "skipped_class_not_allowed"
        append_jsonl_atomic(session_dir / "notification-events.jsonl", event)
        return {"notificationDispatch": event}
    if dry_run:
        event["result"] = "dry_run"
        append_jsonl_atomic(session_dir / "notification-events.jsonl", event)
        return {"notificationDispatch": event}
    if not enabled and not force:
        event["result"] = "skipped_disabled"
        append_jsonl_atomic(session_dir / "notification-events.jsonl", event)
        return {"notificationDispatch": event}
    osascript = shutil.which("osascript")
    if not osascript:
        event["result"] = "skipped_osascript_unavailable"
        append_jsonl_atomic(session_dir / "notification-events.jsonl", event)
        return {"notificationDispatch": event}
    completed = subprocess.run(
        [osascript, "-e", script],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=5,
        check=False,
    )
    event.update(
        {
            "result": "sent" if completed.returncode == 0 else "failed",
            "returncode": completed.returncode,
            "stdout": completed.stdout.strip(),
            "stderr": completed.stderr.strip(),
        }
    )
    append_jsonl_atomic(session_dir / "notification-events.jsonl", event)
    output = {"notificationDispatch": event}
    if completed.returncode != 0:
        output["decision"] = "block"
        output["reason"] = completed.stderr.strip() or completed.stdout.strip() or "osascript notification failed"
    return output











































def role_queue(*, runtime: str, state_root: Path, hook_input: dict[str, Any]) -> dict[str, Any]:
    session_id = str(
        current_session_id(state_root, hook_input)
        or "unknown-session"
    )
    session_dir = state_root / safe_id(session_id)
    session_dir.mkdir(parents=True, exist_ok=True)
    state_path = session_dir / "bootstrap.json"
    state = read_json(state_path) if state_path.exists() else {}
    organization_instance_id = str(
        state.get("organization_instance_id")
        or hook_input.get("organization_instance_id")
        or hook_input.get("organizationInstanceId")
        or organization_id(session_id)
    )
    role_id = normalize_cell(hook_input.get("role_id") or hook_input.get("roleId") or hook_input.get("agent_id") or hook_input.get("agentId"))
    if not role_id:
        return {"decision": "block", "reason": "role-queue requires role_id or agent_id"}
    role_row = role_agent_row_for(role_id, organization_instance_id=organization_instance_id)
    if not role_row:
        return {"decision": "block", "reason": f"role-agent registry has no active role: {role_id}"}

    queue_root = queue_root_for(session_dir, hook_input)
    queue_root.mkdir(parents=True, exist_ok=True)
    inbox_path = queue_root / str(role_row["inbox_path"])
    action = normalize_cell(hook_input.get("action") or "enqueue").lower()
    if action == "inspect":
        inbox = load_inbox(inbox_path, role_id)
        return {
            "roleQueue": {
                "action": "inspect",
                "result": "inbox_loaded",
                "role_id": role_id,
                "queue_root": str(queue_root),
                "inbox_path": str(inbox_path),
                "message_count": len(inbox.get("messages", [])),
                "inbox": inbox,
            }
        }
    if action != "enqueue":
        return {"decision": "block", "reason": f"unsupported role-queue action: {action}"}

    task_id = normalize_cell(hook_input.get("task_id") or hook_input.get("taskId"))
    if not task_id:
        return {"decision": "block", "reason": "role-queue enqueue requires task_id"}
    now = current_timestamp()
    message_id = normalize_cell(hook_input.get("message_id") or hook_input.get("messageId") or f"msg-{uuid.uuid4().hex}")
    unit_id = normalize_cell(hook_input.get("unit_id") or hook_input.get("unitId"))
    from_role = normalize_cell(hook_input.get("from_role") or hook_input.get("fromRole") or hook_input.get("source_agent") or "entrypoint")
    priority = normalize_cell(hook_input.get("priority") or "normal")
    report_id = normalize_cell(hook_input.get("report_id") or hook_input.get("reportId") or f"rep-{uuid.uuid4().hex}")
    component_errors = (
        queue_component_errors(task_id, "task_id")
        + queue_component_errors(message_id, "message_id")
        + queue_component_errors(report_id, "report_id")
    )
    if component_errors:
        return {"decision": "block", "reason": "; ".join(component_errors)}
    report_path = Path(str(role_row["report_dir"])) / task_id / f"{report_id}.yaml"
    try:
        pending_before_enqueue = pending_inbox_messages(inbox_path, role_id)
    except ValueError as exc:
        return {"decision": "block", "reason": str(exc)}
    instruction = str(hook_input.get("instruction") or hook_input.get("prompt") or "")
    payload = hook_input.get("payload") if isinstance(hook_input.get("payload"), dict) else {}
    payload = dict(payload)
    payload.setdefault("type", normalize_cell(hook_input.get("payload_type") or hook_input.get("payloadType") or "task_delegation"))
    if hook_input.get("context_ref") or hook_input.get("contextRef"):
        payload["context_ref"] = str(hook_input.get("context_ref") or hook_input.get("contextRef"))
    payload.setdefault("expected_output", normalize_cell(hook_input.get("expected_output") or hook_input.get("expectedOutput") or "role_report"))
    payload.setdefault("report_path", str(report_path))
    if truthy_input(hook_input.get("skip_auto_queue_handoff") or hook_input.get("skipAutoQueueHandoff")):
        payload["skip_auto_queue_handoff"] = True
    copy_auto_handoff_context_to_payload(payload, hook_input)
    prompt_submit_chain_id = prompt_submit_chain_id_from_mapping(hook_input) or prompt_submit_chain_id_from_mapping(payload)
    if not prompt_submit_chain_id and role_id == "gate-prompt-formatter":
        prompt_submit_chain_id = task_id or message_id
    stamp_payload_prompt_submit_chain_id(payload, prompt_submit_chain_id)
    task_payload_path: Path | None = None
    task_payload_data: dict[str, Any] | None = None
    if instruction.strip():
        task_payload_path = queue_root / "tasks" / task_id / f"{message_id}.yaml"
        task_payload_data = {
            "task_payload_version": "1",
            "message_id": message_id,
            "task_id": task_id,
            "unit_id": unit_id,
            "to_role": role_id,
            "instruction": instruction,
            "created_at": now,
        }
        payload.setdefault("instruction_ref", str(task_payload_path.relative_to(queue_root)))
    message = {
        "message_id": message_id,
        "from_role": from_role,
        "to_role": role_id,
        "task_id": task_id,
        "unit_id": unit_id,
        "created_at": now,
        "priority": priority,
        "status": "pending",
        "payload": payload,
    }
    validation_errors = validate_queue_message(message)
    if validation_errors:
        return {"decision": "block", "reason": "; ".join(validation_errors)}

    try:
        inbox = append_inbox_message(
            inbox_path,
            role_id,
            message,
            queue_root,
            task_payload_path=task_payload_path,
            task_payload_data=task_payload_data,
        )
    except (TimeoutError, ValueError) as exc:
        return {"decision": "block", "reason": str(exc)}
    dry_run = truthy_input(hook_input.get("dry_run") or hook_input.get("dryRun") or os.environ.get("ITB_ROLE_QUEUE_DRY_RUN"))
    pending_recovery_events: list[dict[str, Any]] = []
    unrecovered_pending_before_enqueue = list(pending_before_enqueue)
    if pending_before_enqueue:
        recovered_message_ids: set[str] = set()
        for pending_message in pending_before_enqueue:
            recovered_event = recover_pending_message_from_existing_report(
                runtime=runtime,
                session_dir=session_dir,
                session_id=session_id,
                organization_instance_id=organization_instance_id,
                queue_root=queue_root,
                inbox_path=inbox_path,
                role_id=role_id,
                role_row=role_row,
                message=pending_message,
                now=now,
            )
            if recovered_event:
                recovery_handoff_input = merge_auto_handoff_context_from_payload(
                    hook_input,
                    pending_message.get("payload") if isinstance(pending_message.get("payload"), dict) else {},
                )
                recovered_event["auto_handoff"] = maybe_enqueue_auto_queue_handoff(
                    runtime=runtime,
                    state_root=state_root,
                    session_id=session_id,
                    organization_instance_id=organization_instance_id,
                    from_role=role_id,
                    finalized=recovered_event,
                    hook_input=recovery_handoff_input,
                )
                pending_recovery_events.append(recovered_event)
                recovered_message_ids.add(normalize_cell(pending_message.get("message_id")))
        if recovered_message_ids:
            unrecovered_pending_before_enqueue = [
                item
                for item in pending_before_enqueue
                if normalize_cell(item.get("message_id")) not in recovered_message_ids
            ]

    if unrecovered_pending_before_enqueue:
        oldest_pending = unrecovered_pending_before_enqueue[0]
        oldest_sla = gate_sla_status_for_message(role_id, oldest_pending, now)
        nudge = {
            "result": "enqueue_only_pending_message_present",
            "sent": False,
            "transport": "headless_cli",
            "pending_message_count": len(unrecovered_pending_before_enqueue),
            "oldest_pending_message_id": normalize_cell(unrecovered_pending_before_enqueue[0].get("message_id")),
            "oldest_pending_sla": oldest_sla,
            "recovered_pending_count": len(pending_recovery_events),
            "recovered_pending_messages": pending_recovery_events,
        }
    else:
        nudge = {
            "result": "enqueue_only",
            "sent": False,
            "transport": "headless_cli",
            "reason": "role-queue records durable queue state only; provider execution is performed by the independent CLI orchestration layer.",
            "pending_message_count": len(pending_before_enqueue) + 1,
            "recovered_pending_count": len(pending_recovery_events),
            "recovered_pending_messages": pending_recovery_events,
        }
    result = "queued"
    sla = gate_sla_status_for_message(role_id, message, now)
    event = {
        "ts": now,
        "runtime": runtime,
        "event_type": "role_queue",
        "session_id": session_id,
        "organization_instance_id": organization_instance_id,
        "role_id": role_id,
        "task_id": task_id,
        "message_id": message_id,
        "result": result,
        "queue_root": str(queue_root),
        "inbox_path": str(inbox_path),
        "task_payload_path": str(task_payload_path) if task_payload_path else "",
        "nudge": nudge,
        "recovered_pending_count": len(pending_recovery_events),
        **sla,
        "notification_class": notification_class_for_event(
            event_type="role_queue",
            result=result,
            sla_breached=bool(sla.get("sla_breached")),
        ),
    }
    append_jsonl_atomic(session_dir / "queue-events.jsonl", event)
    append_queue_metric(
        session_dir=session_dir,
        queue_root=queue_root,
        runtime=runtime,
        session_id=session_id,
        organization_instance_id=organization_instance_id,
        role_id=role_id,
        message=message,
        event_type="queued",
        result=result,
        now=now,
        extra={"queue_transport": "headless_cli", "nudge_result": normalize_cell(nudge.get("result"))},
    )
    completion_wait = wait_for_role_queue_completion_if_requested(
        runtime=runtime,
        state_root=state_root,
        session_dir=session_dir,
        session_id=session_id,
        organization_instance_id=organization_instance_id,
        queue_root=queue_root,
        inbox_path=inbox_path,
        role_id=role_id,
        role_row=role_row,
        message=message,
        dry_run=dry_run,
        hook_input=hook_input,
    )
    return {
        "roleQueue": {
            "action": "enqueue",
            "result": result,
            "role_id": role_id,
            "task_id": task_id,
            "message_id": message_id,
            "queue_root": str(queue_root),
            "inbox_path": str(inbox_path),
            "task_payload_path": str(task_payload_path) if task_payload_path else "",
            "report_path": str(queue_root / report_path),
            "message_count": len(inbox.get("messages", [])),
            "nudge": nudge,
            "completion_wait": completion_wait,
            "notification_class": event["notification_class"],
        }
    }


def merged_manifest_input(hook_input: dict[str, Any], nested_keys: tuple[str, ...]) -> dict[str, Any]:
    merged: dict[str, Any] = {}
    for key in nested_keys:
        value = hook_input.get(key)
        if isinstance(value, dict):
            merged.update(value)
    for key, value in hook_input.items():
        if key not in nested_keys:
            merged[key] = value
    return merged


def normalize_context_refs(value: Any) -> list[dict[str, str]]:
    raw_items: list[Any]
    if isinstance(value, list):
        raw_items = value
    elif isinstance(value, str) and value.strip():
        raw_items = [value]
    else:
        raw_items = []
    refs: list[dict[str, str]] = []
    for item in raw_items:
        if isinstance(item, dict):
            ref_type = normalize_cell(item.get("type") or "context")
            path = normalize_cell(item.get("path") or item.get("ref") or item.get("value"))
            label = normalize_cell(item.get("label") or item.get("name"))
        else:
            ref_type = "context"
            path = normalize_cell(item)
            label = ""
        if not path:
            continue
        ref = {"type": ref_type, "path": path}
        if label:
            ref["label"] = label
        refs.append(ref)
    return refs


def dedupe_context_refs(refs: list[dict[str, str]]) -> list[dict[str, str]]:
    deduped: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for ref in refs:
        key = (normalize_cell(ref.get("type")), normalize_cell(ref.get("path")))
        if not key[1] or key in seen:
            continue
        seen.add(key)
        deduped.append(ref)
    return deduped


def agent_call_context_refs(manifest: dict[str, Any], *, role_layer: str, assignment_role: str) -> list[dict[str, str]]:
    refs = normalize_context_refs(manifest.get("context_refs") or manifest.get("contextRefs"))
    if manifest.get("context_ref") or manifest.get("contextRef"):
        refs.extend(normalize_context_refs(manifest.get("context_ref") or manifest.get("contextRef")))
    refs.extend([dict(ref) for ref in ROLE_LAYER_CONTEXT_PRESET_REFS.get(role_layer, [])])
    if assignment_role:
        refs.extend([dict(ref) for ref in ASSIGNMENT_ROLE_CONTEXT_PRESET_REFS.get(assignment_role, [])])
    return dedupe_context_refs(refs)


def nested_mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def agent_call_target_role(manifest: dict[str, Any]) -> str:
    to_mapping = nested_mapping(manifest.get("to") or manifest.get("target"))
    return normalize_cell(
        manifest.get("to_role")
        or manifest.get("toRole")
        or manifest.get("to_agent")
        or manifest.get("toAgent")
        or manifest.get("role_id")
        or manifest.get("roleId")
        or manifest.get("agent_id")
        or manifest.get("agentId")
        or to_mapping.get("role")
        or to_mapping.get("role_id")
        or to_mapping.get("roleId")
        or to_mapping.get("agent")
        or to_mapping.get("agent_id")
        or to_mapping.get("agentId")
    )


def agent_call_provider_override_errors(manifest: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    forbidden_top_level = (
        "provider",
        "model",
        "intended_model",
        "intendedModel",
        "effective_model",
        "effectiveModel",
        "execution_mode",
        "executionMode",
        "to_provider",
        "toProvider",
        "to_model",
        "toModel",
        "provider_override",
        "providerOverride",
        "model_override",
        "modelOverride",
    )
    for key in forbidden_top_level:
        if key in manifest and normalize_cell(manifest.get(key)):
            errors.append(f"agent-call does not accept provider/model override field: {key}")
    for nested_key in ("to", "target"):
        nested = nested_mapping(manifest.get(nested_key))
        for key in ("provider", "model", "intended_model", "intendedModel", "execution_mode", "executionMode"):
            if key in nested and normalize_cell(nested.get(key)):
                errors.append(f"agent-call does not accept provider/model override field: {nested_key}.{key}")
    return errors


def role_is_director(role_id: str, *, organization_instance_id: str) -> bool:
    row = role_agent_row_for(role_id, organization_instance_id=organization_instance_id)
    if normalize_cell(row.get("role_layer")) == "director":
        return True
    return role_id.endswith("-director")


def validate_agent_call_manifest(manifest: dict[str, Any], *, organization_instance_id: str) -> tuple[dict[str, Any], list[str]]:
    errors: list[str] = []
    version = normalize_cell(
        manifest.get("agent_call_manifest_version")
        or manifest.get("agentCallManifestVersion")
        or AGENT_CALL_MANIFEST_VERSION
    )
    if version != AGENT_CALL_MANIFEST_VERSION:
        errors.append(f"unsupported agent_call_manifest_version: {version or '<missing>'}")
    task_id = normalize_cell(manifest.get("task_id") or manifest.get("taskId"))
    from_role = normalize_cell(manifest.get("from_role") or manifest.get("fromRole") or manifest.get("source_agent") or manifest.get("sourceAgent"))
    to_role = agent_call_target_role(manifest)
    instruction = str(manifest.get("instruction") or manifest.get("prompt") or "")
    expected_output = normalize_cell(manifest.get("expected_output") or manifest.get("expectedOutput"))
    if not task_id:
        errors.append("agent-call requires task_id")
    if not from_role:
        errors.append("agent-call requires from_role")
    if not to_role:
        errors.append("agent-call requires to_role or to_agent")
    if not instruction.strip():
        errors.append("agent-call requires instruction")
    if not expected_output:
        errors.append("agent-call requires expected_output")
    errors.extend(agent_call_provider_override_errors(manifest))

    to_row = role_agent_row_for(to_role, organization_instance_id=organization_instance_id) if to_role else {}
    if to_role and not to_row:
        errors.append(f"role-agent registry has no active role: {to_role}")
    role_layer = normalize_cell(to_row.get("role_layer")) if to_row else ""
    if to_row and role_layer not in STATIC_ROLE_LAYERS:
        errors.append(f"role-agent registry role_layer missing/invalid for {to_role}: {role_layer or '<missing>'}")

    assignment_role = normalize_cell(manifest.get("assignment_role") or manifest.get("assignmentRole"))
    if assignment_role and assignment_role not in ASSIGNMENT_ROLE_VALUES:
        allowed = ", ".join(sorted(ASSIGNMENT_ROLE_VALUES))
        errors.append(f"assignment_role invalid: {assignment_role}; allowed={allowed}")
    caller_is_director = role_is_director(from_role, organization_instance_id=organization_instance_id) if from_role else False
    if assignment_role and not caller_is_director:
        errors.append("assignment_role may be assigned only by a Director")
    if caller_is_director and role_layer == "worker" and not assignment_role:
        errors.append("Director -> worker agent-call requires assignment_role, including explicit none")

    normalized = {
        "agent_call_manifest_version": AGENT_CALL_MANIFEST_VERSION,
        "task_id": task_id,
        "from_role": from_role,
        "to_role": to_role,
        "instruction": instruction,
        "expected_output": expected_output,
        "wait": truthy_input(manifest.get("wait") or manifest.get("completion_wait") or manifest.get("completionWait")),
        "assignment_role": assignment_role,
        "role_layer": role_layer,
        "context_refs": agent_call_context_refs(manifest, role_layer=role_layer, assignment_role=assignment_role),
    }
    return normalized, errors


def agent_call(*, runtime: str, state_root: Path, hook_input: dict[str, Any]) -> dict[str, Any]:
    session_id = str(current_session_id(state_root, hook_input) or "unknown-session")
    session_dir = state_root / safe_id(session_id)
    state_path = session_dir / "bootstrap.json"
    state = read_json(state_path) if state_path.exists() else {}
    organization_instance_id = str(
        state.get("organization_instance_id")
        or hook_input.get("organization_instance_id")
        or hook_input.get("organizationInstanceId")
        or organization_id(session_id)
    )
    manifest = merged_manifest_input(
        hook_input,
        ("manifest", "agent_call_manifest", "agentCallManifest", "agent_call", "agentCall"),
    )
    normalized, errors = validate_agent_call_manifest(manifest, organization_instance_id=organization_instance_id)
    if errors:
        return {"decision": "block", "reason": "; ".join(errors), "agentCall": {"result": "blocked", "errors": errors}}

    payload = dict(manifest.get("payload")) if isinstance(manifest.get("payload"), dict) else {}
    payload.update(
        {
            "type": "agent_call",
            "agent_call_manifest_version": AGENT_CALL_MANIFEST_VERSION,
            "from_role": normalized["from_role"],
            "to_role": normalized["to_role"],
            "role_layer": normalized["role_layer"],
            "context_refs": normalized["context_refs"],
        }
    )
    if normalized["assignment_role"]:
        payload["assignment_role"] = normalized["assignment_role"]
    queue_input = dict(hook_input)
    queue_input.update(
        {
            "session_id": session_id,
            "role_id": normalized["to_role"],
            "task_id": normalized["task_id"],
            "from_role": normalized["from_role"],
            "instruction": normalized["instruction"],
            "expected_output": normalized["expected_output"],
            "payload": payload,
            "queue_consumer_override": True,
        }
    )
    if normalized["wait"] and not (
        queue_input.get("completion_wait_seconds")
        or queue_input.get("completionWaitSeconds")
        or queue_input.get("completion_wait_profile")
        or queue_input.get("completionWaitProfile")
    ):
        queue_input["completion_wait_profile"] = "live_validation"
    queue_output = role_queue(runtime=runtime, state_root=state_root, hook_input=queue_input)
    role_queue_summary = queue_output.get("roleQueue") if isinstance(queue_output.get("roleQueue"), dict) else {}
    if queue_output.get("decision") == "block":
        return {
            "decision": "block",
            "reason": normalize_cell(queue_output.get("reason")) or "role-queue blocked",
            "agentCall": {
                "agent_call_receipt_version": "1",
                "result": "blocked_role_queue",
                "task_id": normalized["task_id"],
                "from_role": normalized["from_role"],
                "to_role": normalized["to_role"],
                "role_layer": normalized["role_layer"],
                "assignment_role": normalized["assignment_role"],
                "roleQueue": role_queue_summary,
            },
        }
    nudge = role_queue_summary.get("nudge") if isinstance(role_queue_summary.get("nudge"), dict) else {}
    return {
        "decision": "ok",
        "agentCall": {
            "agent_call_receipt_version": "1",
            "result": role_queue_summary.get("result", "queued"),
            "task_id": normalized["task_id"],
            "from_role": normalized["from_role"],
            "to_role": normalized["to_role"],
            "role_layer": normalized["role_layer"],
            "assignment_role": normalized["assignment_role"],
            "message_id": normalize_cell(role_queue_summary.get("message_id")),
            "queue_root": normalize_cell(role_queue_summary.get("queue_root")),
            "inbox_path": normalize_cell(role_queue_summary.get("inbox_path")),
            "payload_path": normalize_cell(role_queue_summary.get("task_payload_path")),
            "report_path": normalize_cell(role_queue_summary.get("report_path")),
            "queue_status": "pending",
            "nudge_status": normalize_cell(nudge.get("result")),
            "context_refs": normalized["context_refs"],
            "completion_wait": role_queue_summary.get("completion_wait", {}),
            "roleQueue": role_queue_summary,
        },
    }


def active_inbox_messages(path: Path, role_id: str) -> list[dict[str, Any]]:
    inbox = load_inbox(path, role_id)
    return [
        dict(item)
        for item in inbox.get("messages", [])
        if isinstance(item, dict) and normalize_cell(item.get("status") or "pending") in {"pending", "processing"}
    ]


def agent_surfaces(*, runtime: str, state_root: Path, hook_input: dict[str, Any]) -> dict[str, Any]:
    session_id = str(current_session_id(state_root, hook_input) or hook_input.get("session_id") or "unknown-session")
    organization_instance_id = str(hook_input.get("organization_instance_id") or hook_input.get("organizationInstanceId") or organization_id(session_id))
    rows = role_agent_rows(organization_instance_id=organization_instance_id)
    roles: list[dict[str, Any]] = []
    for row in rows:
        roles.append(
            {
                "role_id": row["role_id"],
                "agent_id": row["agent_id"],
                "team": row["team"],
                "role_layer": row["role_layer"],
                "provider": row["provider"],
                "intended_model": row["intended_model"],
                "execution_mode": row["execution_mode"],
                "queue_consumer": row["queue_consumer"],
                "agent_call_supported": True,
                "inbox_path": row["inbox_path"],
                "report_dir": row["report_dir"],
                "assignment_roles": sorted(ASSIGNMENT_ROLE_VALUES) if row["role_layer"] == "worker" else [],
            }
        )
    return {
        "decision": "ok",
        "agentSurfaces": {
            "schema_version": 1,
            "runtime": runtime,
            "state_root": str(state_root),
            "session_id": session_id,
            "organization_instance_id": organization_instance_id,
            "role_count": len(roles),
            "roles": roles,
        },
    }


def transport_status(*, runtime: str, state_root: Path, hook_input: dict[str, Any]) -> dict[str, Any]:
    session_id = str(current_session_id(state_root, hook_input) or hook_input.get("session_id") or "unknown-session")
    session_dir = state_root / safe_id(session_id)
    queue_root = queue_root_for(session_dir, hook_input)
    return {
        "decision": "ok",
        "transportStatus": {
            "schema_version": 1,
            "runtime": runtime,
            "state_root": str(state_root),
            "session_id": session_id,
            "queue_root": str(queue_root),
            "providers": {
                "claude_cli": {"cli": "claude", "available": bool(shutil.which("claude")), "path": shutil.which("claude") or ""},
                "codex_exec": {"cli": "codex", "available": bool(shutil.which("codex")), "path": shutil.which("codex") or ""},
            },
        },
    }


def agent_switch_manifest(hook_input: dict[str, Any]) -> dict[str, Any]:
    return merged_manifest_input(
        hook_input,
        ("manifest", "agent_switch_manifest", "agentSwitchManifest", "agent_switch", "agentSwitch", "provider_failover", "providerFailover"),
    )


def agent_switch(*, runtime: str, state_root: Path, hook_input: dict[str, Any], command_name: str = "agent-switch") -> dict[str, Any]:
    session_id = str(current_session_id(state_root, hook_input) or "unknown-session")
    session_dir = state_root / safe_id(session_id)
    state_path = session_dir / "bootstrap.json"
    roster_path = session_dir / "roster.json"
    if not state_path.exists() or not roster_path.exists():
        return {"decision": "block", "reason": "bootstrap.json or roster.json missing"}
    state = read_json(state_path)
    roster = read_json(roster_path)
    if not isinstance(roster, list):
        return {"decision": "block", "reason": "roster.json is invalid"}
    organization_instance_id = str(state.get("organization_instance_id") or organization_id(session_id))
    manifest = agent_switch_manifest(hook_input)
    target_role = normalize_cell(
        manifest.get("target_role")
        or manifest.get("targetRole")
        or manifest.get("role_id")
        or manifest.get("roleId")
        or manifest.get("agent_id")
        or manifest.get("agentId")
    )
    to_mapping = nested_mapping(manifest.get("to"))
    to_provider = normalize_cell(to_mapping.get("provider") or manifest.get("to_provider") or manifest.get("toProvider"))
    to_model = normalize_cell(to_mapping.get("model") or manifest.get("to_model") or manifest.get("toModel") or manifest.get("intended_model") or manifest.get("intendedModel"))
    to_execution_mode = normalize_cell(
        to_mapping.get("execution_mode")
        or to_mapping.get("executionMode")
        or manifest.get("to_execution_mode")
        or manifest.get("toExecutionMode")
        or PROVIDER_SWITCH_DEFAULT_EXECUTION_MODES.get(to_provider)
    )
    reason = normalize_cell(manifest.get("reason") or hook_input.get("reason"))
    persist = truthy_input(manifest.get("persist"))
    dry_run = truthy_input(manifest.get("dry_run") or manifest.get("dryRun"))
    version = normalize_cell(
        manifest.get("agent_switch_manifest_version")
        or manifest.get("agentSwitchManifestVersion")
        or AGENT_SWITCH_MANIFEST_VERSION
    )
    errors: list[str] = []
    if version != AGENT_SWITCH_MANIFEST_VERSION:
        errors.append(f"unsupported agent_switch_manifest_version: {version or '<missing>'}")
    if not target_role:
        errors.append(f"{command_name} requires target_role or role_id")
    if not reason:
        errors.append(f"{command_name} requires reason")
    if to_provider not in PROVIDER_SWITCH_DEFAULT_EXECUTION_MODES:
        errors.append("to.provider must be anthropic or openai")
    if not to_model:
        errors.append("to.model is required")
    if not to_execution_mode:
        errors.append("to.execution_mode could not be resolved")
    registry_row = role_agent_row_for(target_role, organization_instance_id=organization_instance_id) if target_role else {}
    if target_role and not registry_row:
        errors.append(f"role-agent registry has no active role: {target_role}")
    roster_row = next((item for item in roster if isinstance(item, dict) and item.get("agent_id") == target_role), None)
    if target_role and roster_row is None:
        errors.append(f"agent not found in roster: {target_role}")
    if persist:
        errors.append("persistent provider switch requires a separate human-approved registry change and Vault evidence")
    if errors:
        return {"decision": "block", "reason": "; ".join(errors), "agentSwitch": {"result": "blocked", "errors": errors}}

    assert roster_row is not None
    queue_root = queue_root_for(session_dir, manifest)
    inbox_path = queue_root / str(registry_row["inbox_path"])
    try:
        active_messages = active_inbox_messages(inbox_path, target_role)
    except ValueError as exc:
        return {"decision": "block", "reason": str(exc), "agentSwitch": {"result": "blocked"}}
    if active_messages:
        return {
            "decision": "block",
            "reason": f"{command_name} blocked because target has pending/processing messages",
            "agentSwitch": {
                "result": "blocked_pending_or_processing",
                "target_role": target_role,
                "active_message_count": len(active_messages),
                "active_message_ids": [normalize_cell(item.get("message_id")) for item in active_messages],
            },
        }

    switched_at = current_timestamp()
    updated_row = dict(roster_row)
    previous = {
        "provider": normalize_cell(roster_row.get("provider")),
        "model": normalize_cell(roster_row.get("intended_model")),
        "execution_mode": normalize_cell(roster_row.get("execution_mode")),
    }
    updated_row.update(
        {
            "provider": to_provider,
            "intended_model": to_model,
            "execution_mode": to_execution_mode,
            "provider_switch_status": "session_local",
            "provider_switch_reason": reason,
            "provider_switched_at": switched_at,
            "provider_switch_previous": previous,
        }
    )
    event = {
        "ts": switched_at,
        "runtime": runtime,
        "event_type": command_name,
        "session_id": session_id,
        "organization_instance_id": organization_instance_id,
        "target_role": target_role,
        "result": "dry_run" if dry_run else "session_roster_updated",
        "reason": reason,
        "previous": previous,
        "to": {"provider": to_provider, "model": to_model, "execution_mode": to_execution_mode},
    }
    if dry_run:
        return {"decision": "ok", "agentSwitch": event}
    merge_roster_agent_row_locked(roster_path, roster, target_role, updated_row)
    append_jsonl_atomic(session_dir / "provider-switch-events.jsonl", event)
    return {"decision": "ok", "agentSwitch": event}





def role_queue_replay_failed(*, runtime: str, state_root: Path, hook_input: dict[str, Any]) -> dict[str, Any]:
    session_id = str(current_session_id(state_root, hook_input) or "unknown-session")
    session_dir = state_root / safe_id(session_id)
    state_path = session_dir / "bootstrap.json"
    state = read_json(state_path) if state_path.exists() else {}
    organization_instance_id = str(
        state.get("organization_instance_id")
        or hook_input.get("organization_instance_id")
        or hook_input.get("organizationInstanceId")
        or organization_id(session_id)
    )
    role_id = normalize_cell(
        hook_input.get("role_id") or hook_input.get("roleId") or hook_input.get("agent_id") or hook_input.get("agentId")
    )
    if not role_id:
        return {"decision": "block", "reason": "queue-replay-failed requires role_id or agent_id"}
    role_row = role_agent_row_for(role_id, organization_instance_id=organization_instance_id)
    if not role_row:
        return {"decision": "block", "reason": f"role-agent registry has no active role: {role_id}"}
    queue_root = queue_root_for(session_dir, hook_input)
    inbox_path = queue_root / str(role_row["inbox_path"])
    inbox = load_inbox(inbox_path, role_id)
    message_filter = normalize_cell(hook_input.get("message_id") or hook_input.get("messageId"))
    max_messages = bounded_int_input(
        hook_input.get("max_messages") or hook_input.get("maxMessages") or 1,
        default=1,
        minimum=1,
        maximum=50,
    )
    dry_run = truthy_input(hook_input.get("dry_run") or hook_input.get("dryRun"))
    reason = normalize_cell(hook_input.get("reason")) or "manual failed queue replay"
    now = current_timestamp()
    candidates = [
        item
        for item in inbox.get("messages", [])
        if isinstance(item, dict)
        and normalize_cell(item.get("status")) == "failed"
        and (not message_filter or normalize_cell(item.get("message_id")) == message_filter)
    ]
    replayed: list[dict[str, Any]] = []
    for message in candidates[:max_messages]:
        message_id = normalize_cell(message.get("message_id"))
        if not message_id:
            continue
        old_report_ref = normalize_cell(message.get("report_path"))
        payload = dict(message.get("payload") if isinstance(message.get("payload"), dict) else {})
        if not old_report_ref:
            old_report_ref = normalize_cell(payload.get("report_path"))
        task_id = normalize_cell(message.get("task_id")) or "unknown-task"
        replay_report_ref = str(
            Path(str(role_row["report_dir"]))
            / task_id
            / f"rep-{message_id}-replay-{uuid.uuid4().hex[:8]}.yaml"
        )
        event = {
            "ts": now,
            "runtime": runtime,
            "event_type": "queue_failed_replay",
            "session_id": session_id,
            "organization_instance_id": organization_instance_id,
            "role_id": role_id,
            "task_id": task_id,
            "message_id": message_id,
            "result": "dry_run" if dry_run else "replayed",
            "reason": reason,
            "previous_report_path": old_report_ref,
            "new_report_path": replay_report_ref,
        }
        if not dry_run:
            payload["report_path"] = replay_report_ref
            if old_report_ref:
                payload["previous_report_path"] = old_report_ref
            replay_count = int(message.get("replay_count") or 0) + 1
            update_inbox_message(
                inbox_path,
                role_id,
                message_id,
                queue_root,
                {
                    "status": "pending",
                    "retry_count": 0,
                    "replay_count": replay_count,
                    "replayed_at": now,
                    "replay_reason": reason,
                    "failed_at": "",
                    "dead_letter_at": "",
                    "dead_letter_reason": "",
                    "error": "",
                    "report_path": replay_report_ref,
                    "previous_report_path": old_report_ref,
                    "payload": payload,
                },
            )
            append_jsonl_atomic(session_dir / "queue-events.jsonl", event)
            append_queue_metric(
                session_dir=session_dir,
                queue_root=queue_root,
                runtime=runtime,
                session_id=session_id,
                organization_instance_id=organization_instance_id,
                role_id=role_id,
                message=message,
                event_type="failed_replay",
                result="replayed",
                now=now,
                retry_count=0,
                extra={"previous_report_path": old_report_ref, "new_report_path": replay_report_ref},
            )
        replayed.append(event)
    return {
        "queueReplayFailed": {
            "result": "replayed" if replayed and not dry_run else "dry_run" if replayed else "idle",
            "role_id": role_id,
            "queue_root": str(queue_root),
            "inbox_path": str(inbox_path),
            "failed_replay_policy": "manual_explicit_replay_only",
            "candidate_count": len(candidates),
            "replayed_count": len(replayed),
            "dry_run": dry_run,
            "messages": replayed,
        }
    }


def role_queue_close_message(*, runtime: str, state_root: Path, hook_input: dict[str, Any]) -> dict[str, Any]:
    session_id = str(current_session_id(state_root, hook_input) or "unknown-session")
    session_dir = state_root / safe_id(session_id)
    state_path = session_dir / "bootstrap.json"
    state = read_json(state_path) if state_path.exists() else {}
    organization_instance_id = str(
        state.get("organization_instance_id")
        or hook_input.get("organization_instance_id")
        or hook_input.get("organizationInstanceId")
        or organization_id(session_id)
    )
    role_id = normalize_cell(
        hook_input.get("role_id") or hook_input.get("roleId") or hook_input.get("agent_id") or hook_input.get("agentId")
    )
    if not role_id:
        return {"decision": "block", "reason": "queue-close-message requires role_id or agent_id"}
    message_id = normalize_cell(hook_input.get("message_id") or hook_input.get("messageId"))
    if not message_id:
        return {"decision": "block", "reason": "queue-close-message requires explicit message_id"}
    role_row = role_agent_row_for(role_id, organization_instance_id=organization_instance_id)
    if not role_row:
        return {"decision": "block", "reason": f"role-agent registry has no active role: {role_id}"}
    queue_root = queue_root_for(session_dir, hook_input)
    inbox_path = queue_root / str(role_row["inbox_path"])
    try:
        message = queue_message_by_id(inbox_path, role_id, message_id)
    except ValueError as exc:
        return {"decision": "block", "reason": str(exc)}
    message_status = normalize_cell(message.get("status") or "pending")
    if message_status != "pending":
        return {
            "decision": "block",
            "reason": f"queue-close-message only closes pending messages: {message_id} is {message_status or 'unknown'}",
        }
    report_path, report_ref = role_agent_report_path(queue_root, role_row, message)
    if report_path.exists():
        return {
            "decision": "block",
            "reason": f"queue-close-message found existing terminal report; run queue recovery before manual close: {report_ref}",
            "role_id": role_id,
            "message_id": message_id,
            "report_path": str(report_path),
            "report_ref": report_ref,
        }
    dry_run = truthy_input(hook_input.get("dry_run") or hook_input.get("dryRun"))
    reason = normalize_cell(hook_input.get("reason")) or "manual queue close"
    result = normalize_cell(hook_input.get("result") or hook_input.get("close_result") or hook_input.get("closeResult")) or "superseded"
    now = current_timestamp()
    event = manual_close_pending_message(
        runtime=runtime,
        session_dir=session_dir,
        session_id=session_id,
        organization_instance_id=organization_instance_id,
        queue_root=queue_root,
        inbox_path=inbox_path,
        role_id=role_id,
        role_row=role_row,
        message=message,
        now=now,
        reason=reason,
        result=result,
        dry_run=dry_run,
    )
    return {
        "queueCloseMessage": {
            "result": "dry_run" if dry_run else result,
            "role_id": role_id,
            "message_id": message_id,
            "queue_root": str(queue_root),
            "inbox_path": str(inbox_path),
            "manual_close_policy": "explicit_message_id_only",
            "closed_count": 0 if dry_run else 1,
            "candidate_count": 1,
            "dry_run": dry_run,
            "message": event,
        }
    }


def role_agent_provider_evidence(role_row: dict[str, Any], hook_input: dict[str, Any]) -> dict[str, Any]:
    supplied = hook_input.get("provider_evidence") if isinstance(hook_input.get("provider_evidence"), dict) else {}
    supplied = supplied or {}
    usage_source = normalize_cell(supplied.get("usage_source") or hook_input.get("usage_source") or "")
    return {
        "provider": role_row.get("provider", ""),
        "intended_model": role_row.get("intended_model", ""),
        "effective_model": normalize_cell(supplied.get("effective_model") or hook_input.get("effective_model") or ""),
        "provider_session_id": normalize_cell(
            supplied.get("provider_session_id")
            or supplied.get("session_id")
            or hook_input.get("provider_session_id")
            or hook_input.get("providerSessionId")
            or ""
        ),
        "request_id": normalize_cell(supplied.get("request_id") or hook_input.get("request_id") or ""),
        "usage_source": usage_source,
        "transcript_path": normalize_cell(supplied.get("transcript_path") or hook_input.get("transcript_path") or ""),
        "input_tokens": supplied.get("input_tokens", hook_input.get("input_tokens", "")),
        "output_tokens": supplied.get("output_tokens", hook_input.get("output_tokens", "")),
        "duration_api_ms": supplied.get("duration_api_ms", hook_input.get("duration_api_ms", "")),
        "num_turns": supplied.get("num_turns", hook_input.get("num_turns", "")),
    }


def validate_role_agent_provider_evidence(evidence: dict[str, Any]) -> list[str]:
    usage_source = normalize_cell(evidence.get("usage_source"))
    if not usage_source:
        return ["provider evidence usage_source is required; local stub completion is disabled"]
    if usage_source == LOCAL_STUB_USAGE_SOURCE:
        return ["role_agent_worker_local_stub is not valid completion evidence"]
    errors = validate_provider_evidence(
        agent_id=normalize_cell(evidence.get("agent_id") or "role-agent-worker"),
        provider=normalize_cell(evidence.get("provider")),
        intended_model=normalize_cell(evidence.get("intended_model")),
        effective_model=normalize_cell(evidence.get("effective_model")),
        usage_source=usage_source,
    )
    for key in ("provider_session_id", "request_id", "transcript_path"):
        if not normalize_cell(evidence.get(key)):
            errors.append(f"provider evidence missing {key}")
    if normalize_cell(evidence.get("provider_session_id")) == "not_invoked":
        errors.append("provider evidence session is not_invoked")
    return errors


def hook_input_has_provider_evidence(hook_input: dict[str, Any]) -> bool:
    supplied = hook_input.get("provider_evidence")
    if isinstance(supplied, dict) and normalize_cell(supplied.get("usage_source")):
        return True
    return bool(normalize_cell(hook_input.get("usage_source")))


def role_agent_load_instruction(queue_root: Path, message: dict[str, Any]) -> tuple[str, str]:
    payload = message.get("payload") if isinstance(message.get("payload"), dict) else {}
    instruction_ref = normalize_cell(payload.get("instruction_ref"))
    if not instruction_ref:
        return "", ""
    instruction_path = safe_queue_relative_path(queue_root, instruction_ref, "instruction_ref")
    data = read_json_yaml(instruction_path)
    if not isinstance(data, dict):
        raise ValueError(f"instruction payload must contain an object: {instruction_ref}")
    return str(data.get("instruction") or ""), str(instruction_path)


def role_agent_report_path(queue_root: Path, role_row: dict[str, Any], message: dict[str, Any]) -> tuple[Path, str]:
    payload = message.get("payload") if isinstance(message.get("payload"), dict) else {}
    report_ref = normalize_cell(payload.get("report_path"))
    if not report_ref:
        report_ref = str(
            Path(str(role_row["report_dir"]))
            / normalize_cell(message.get("task_id"))
            / f"rep-{normalize_cell(message.get('message_id'))}.yaml"
        )
    report_path = safe_queue_relative_path(queue_root, report_ref, "report_path")
    return report_path, report_ref


def validate_terminal_queue_report(report: dict[str, Any], *, role_id: str, message_id: str) -> list[str]:
    errors: list[str] = []
    for key in ("report_version", "report_type", "from_role", "message_id", "created_at", "result", "status", "summary"):
        if not normalize_cell(report.get(key)):
            errors.append(f"report missing {key}")
    report_version = normalize_cell(report.get("report_version"))
    if report_version and report_version != "1":
        errors.append(f"report_version must be 1: {report_version}")
    report_role = normalize_cell(report.get("from_role"))
    if report_role and report_role != role_id:
        errors.append(f"report from_role mismatch: expected {role_id}, got {report_role}")
    report_message_id = normalize_cell(report.get("message_id"))
    if report_message_id and report_message_id != message_id:
        errors.append(f"report message_id mismatch: expected {message_id}, got {report_message_id}")
    report_status = normalize_cell(report.get("status")).lower()
    if report_status and report_status not in {"done", "failed"}:
        errors.append(f"report status must be done or failed: {report_status}")
    if report_status == "done":
        evidence = report.get("provider_evidence") if isinstance(report.get("provider_evidence"), dict) else {}
        if not evidence and isinstance(report.get("evidence"), dict):
            evidence = report.get("evidence")
        usage_source = normalized_publication_value(evidence.get("usage_source") if isinstance(evidence, dict) else "")
        if usage_source in INVALID_PUBLICATION_USAGE_SOURCES:
            errors.append("done report provider evidence usage_source is missing or not provider-backed")
    return errors


def stamp_terminal_report_schema_validation(report: dict[str, Any], *, role_id: str, message_id: str) -> None:
    errors = validate_terminal_queue_report(report, role_id=role_id, message_id=message_id)
    report["schema_validation"] = {
        "validator": "validate_terminal_queue_report",
        "status": "invalid" if errors else "valid",
        "errors": errors,
    }
    if errors:
        raise ValueError("terminal queue report schema invalid: " + "; ".join(errors))


def record_invalid_queue_report(
    *,
    runtime: str,
    session_dir: Path,
    session_id: str,
    organization_instance_id: str,
    queue_root: Path,
    role_id: str,
    message: dict[str, Any],
    report_path: Path,
    report_ref: str,
    errors: list[str],
    now: str,
) -> None:
    event = {
        "ts": now,
        "runtime": runtime,
        "event_type": "queue_report_invalid",
        "session_id": session_id,
        "organization_instance_id": organization_instance_id,
        "role_id": role_id,
        "task_id": normalize_cell(message.get("task_id")),
        "message_id": normalize_cell(message.get("message_id")),
        "result": "invalid_report",
        "report_path": str(report_path),
        "report_ref": report_ref,
        "errors": errors,
    }
    append_jsonl_atomic(session_dir / "queue-events.jsonl", event)
    append_queue_metric(
        session_dir=session_dir,
        queue_root=queue_root,
        runtime=runtime,
        session_id=session_id,
        organization_instance_id=organization_instance_id,
        role_id=role_id,
        message=message,
        event_type="report_invalid",
        result="invalid_report",
        now=now,
        extra={"report_path": report_ref, "errors": errors},
    )


def recover_pending_message_from_existing_report(
    *,
    runtime: str,
    session_dir: Path,
    session_id: str,
    organization_instance_id: str,
    queue_root: Path,
    inbox_path: Path,
    role_id: str,
    role_row: dict[str, Any],
    message: dict[str, Any],
    now: str,
    dry_run: bool = False,
) -> dict[str, Any] | None:
    message_id = normalize_cell(message.get("message_id"))
    if not message_id:
        return None
    report_path, report_ref = role_agent_report_path(queue_root, role_row, message)
    if not report_path.exists():
        return None
    report = read_json_yaml(report_path)
    if not isinstance(report, dict):
        return None
    schema_errors = validate_terminal_queue_report(report, role_id=role_id, message_id=message_id)
    if schema_errors:
        if dry_run:
            return None
        record_invalid_queue_report(
            runtime=runtime,
            session_dir=session_dir,
            session_id=session_id,
            organization_instance_id=organization_instance_id,
            queue_root=queue_root,
            role_id=role_id,
            message=message,
            report_path=report_path,
            report_ref=report_ref,
            errors=schema_errors,
            now=now,
        )
        return None
    report_status = normalize_cell(report.get("status")).lower()
    if report_status not in {"done", "failed"}:
        return None
    integrity = report_file_integrity(report_path)

    inbox_updates: dict[str, Any] = {
        "status": report_status,
        "report_path": report_ref,
        "recovered_from_report_at": now,
        "report_sha256": integrity["sha256"],
        "report_line_count": integrity["line_count"],
        "report_byte_count": integrity["byte_count"],
    }
    if report_status == "done":
        inbox_updates["done_at"] = normalize_cell(report.get("created_at")) or now
    else:
        inbox_updates["failed_at"] = normalize_cell(report.get("created_at")) or now
        inbox_updates["error"] = normalize_cell(report.get("error")) or normalize_cell(report.get("summary")) or "report status failed"

    if dry_run:
        return {
            "ts": now,
            "runtime": runtime,
            "event_type": "queue_report_recovered",
            "session_id": session_id,
            "organization_instance_id": organization_instance_id,
            "role_id": role_id,
            "task_id": normalize_cell(message.get("task_id")),
            "message_id": message_id,
            "result": report_status,
            "report_path": str(report_path),
            "report_ref": report_ref,
            "report_integrity": integrity,
            "roster_recovered": False,
            "dry_run": True,
            "would_update_inbox": inbox_updates,
            "notification_class": notification_class_for_event(
                event_type="queue_report_recovered",
                result=report_status,
            ),
        }
    update_inbox_message(inbox_path, role_id, message_id, queue_root, inbox_updates)

    evidence = report.get("provider_evidence") if isinstance(report.get("provider_evidence"), dict) else {}
    if not evidence and isinstance(report.get("evidence"), dict):
        evidence = report.get("evidence")
    roster_path = session_dir / "roster.json"
    fallback_roster = read_json(roster_path) if roster_path.exists() else []
    row_update = {
        "agent_id": role_id,
        "organization_instance_id": organization_instance_id,
        "parent_session_id": session_id,
        "response_status": f"recovered_{report_status}",
        "provider_status": "provider_report_recovered",
        "last_seen_at": now,
        "last_recovered_at": now,
        "last_recovered_message_id": message_id,
        "last_recovered_report_path": report_ref,
        "usage_source": normalize_cell(evidence.get("usage_source")) or "recovered_report",
        "session_id": normalize_cell(evidence.get("provider_session_id") or evidence.get("session_id")),
        "last_request_id": normalize_cell(evidence.get("request_id")),
        "effective_model": normalize_cell(evidence.get("effective_model")),
        "transcript_path": normalize_cell(evidence.get("transcript_path")),
        "notes": f"queue recovery found terminal {report_status} report from {report_ref}",
    }
    merge_roster_agent_row_locked(roster_path, fallback_roster, role_id, row_update)

    event = {
        "ts": now,
        "runtime": runtime,
        "event_type": "queue_report_recovered",
        "session_id": session_id,
        "organization_instance_id": organization_instance_id,
        "role_id": role_id,
        "task_id": normalize_cell(message.get("task_id")),
        "message_id": message_id,
        "result": report_status,
        "report_path": str(report_path),
        "report_ref": report_ref,
        "report_integrity": integrity,
        "roster_recovered": True,
        "roster_response_status": row_update["response_status"],
        "notification_class": notification_class_for_event(
            event_type="queue_report_recovered",
            result=report_status,
        ),
    }
    team_completion_update = maybe_update_tpm_team_completion_check(
        runtime=runtime,
        session_dir=session_dir,
        queue_root=queue_root,
        role_id=role_id,
        message=message,
        report=report,
        finalized=event,
        hook_input={},
        now=now,
    )
    if team_completion_update.get("result") != "skipped_not_tpm":
        event["team_completion_update"] = team_completion_update
    append_jsonl_atomic(session_dir / "queue-events.jsonl", event)
    append_queue_metric(
        session_dir=session_dir,
        queue_root=queue_root,
        runtime=runtime,
        session_id=session_id,
        organization_instance_id=organization_instance_id,
        role_id=role_id,
        message=message,
        event_type="report_recovered",
        result=report_status,
        now=now,
        extra=provider_usage_metric_fields(evidence)
        | {
            "report_path": report_ref,
            "report_integrity": integrity,
            "usage_source": normalize_cell(evidence.get("usage_source")),
            "effective_model": normalize_cell(evidence.get("effective_model")),
            "transcript_path": normalize_cell(evidence.get("transcript_path")),
        },
    )
    return event


ROLE_QUEUE_COMPLETION_WAIT_PROFILES: dict[str, dict[str, Any]] = {
    "off": {"timeout_seconds": 0.0, "poll_interval_seconds": 0.25, "event_driven": True},
    "none": {"timeout_seconds": 0.0, "poll_interval_seconds": 0.25, "event_driven": True},
    "hook_light": {"timeout_seconds": 0.75, "poll_interval_seconds": 0.1, "event_driven": True},
    "daemon_assisted": {"timeout_seconds": 2.0, "poll_interval_seconds": 0.25, "event_driven": True},
    "live_validation": {"timeout_seconds": 10.0, "poll_interval_seconds": 0.25, "event_driven": True},
}


def role_queue_completion_wait_profile(hook_input: dict[str, Any]) -> dict[str, Any]:
    profile_name = normalize_cell(
        hook_input.get("completion_wait_profile")
        or hook_input.get("completionWaitProfile")
        or os.environ.get("ITB_ROLE_QUEUE_COMPLETION_WAIT_PROFILE")
        or "off"
    ).lower()
    return dict(ROLE_QUEUE_COMPLETION_WAIT_PROFILES.get(profile_name) or ROLE_QUEUE_COMPLETION_WAIT_PROFILES["off"])


def role_queue_completion_wait_config(hook_input: dict[str, Any]) -> tuple[float, float, bool]:
    profile = role_queue_completion_wait_profile(hook_input)
    timeout_seconds = bounded_float_input(
        hook_input.get("completion_wait_seconds")
        or hook_input.get("completionWaitSeconds")
        or os.environ.get("ITB_ROLE_QUEUE_COMPLETION_WAIT_SECONDS")
        or profile.get("timeout_seconds")
        or 0,
        default=0.0,
        minimum=0.0,
        maximum=300.0,
    )
    poll_interval_seconds = bounded_float_input(
        hook_input.get("completion_wait_poll_seconds")
        or hook_input.get("completionWaitPollSeconds")
        or os.environ.get("ITB_ROLE_QUEUE_COMPLETION_WAIT_POLL_SECONDS")
        or profile.get("poll_interval_seconds")
        or 0.25,
        default=0.25,
        minimum=0.0,
        maximum=30.0,
    )
    explicit_event_driven = None
    if "completion_wait_event_driven" in hook_input:
        explicit_event_driven = hook_input.get("completion_wait_event_driven")
    elif "completionWaitEventDriven" in hook_input:
        explicit_event_driven = hook_input.get("completionWaitEventDriven")
    else:
        explicit_event_driven = os.environ.get("ITB_ROLE_QUEUE_COMPLETION_WAIT_EVENT_DRIVEN")
    event_driven = truthy_input(
        explicit_event_driven,
        default=bool(profile.get("event_driven", True)),
    )
    return timeout_seconds, poll_interval_seconds, event_driven


def record_role_queue_completion_wait_event(
    *,
    runtime: str,
    session_dir: Path,
    session_id: str,
    organization_instance_id: str,
    queue_root: Path,
    role_id: str,
    message: dict[str, Any],
    result: str,
    wait_result: str,
    completion_source: str,
    duration_seconds: float,
    timeout_seconds: float,
    poll_interval_seconds: float,
    report_ref: str = "",
    event_wait: dict[str, Any] | None = None,
    auto_handoff: dict[str, Any] | None = None,
) -> dict[str, Any]:
    now = current_timestamp()
    event = {
        "ts": now,
        "runtime": runtime,
        "event_type": "role_queue_completion_wait",
        "session_id": session_id,
        "organization_instance_id": organization_instance_id,
        "role_id": role_id,
        "task_id": normalize_cell(message.get("task_id")),
        "message_id": normalize_cell(message.get("message_id")),
        "result": result,
        "wait_result": wait_result,
        "completion_source": completion_source,
        "duration_sec": round(max(0.0, duration_seconds), 3),
        "timeout_seconds": timeout_seconds,
        "poll_interval_seconds": poll_interval_seconds,
        "report_ref": report_ref,
        "event_wait": event_wait or {},
        "auto_handoff": auto_handoff or {},
    }
    append_jsonl_atomic(session_dir / "queue-events.jsonl", event)
    append_queue_metric(
        session_dir=session_dir,
        queue_root=queue_root,
        runtime=runtime,
        session_id=session_id,
        organization_instance_id=organization_instance_id,
        role_id=role_id,
        message=message,
        event_type="completion_wait",
        result=wait_result,
        now=now,
        duration_seconds=duration_seconds,
        retry_count=int(message.get("retry_count") or 0),
        extra={
            "completion_source": completion_source,
            "report_ref": report_ref,
            "wait_result": wait_result,
        },
    )
    return event


def wait_for_role_queue_completion(
    *,
    runtime: str,
    state_root: Path,
    session_dir: Path,
    session_id: str,
    organization_instance_id: str,
    queue_root: Path,
    inbox_path: Path,
    role_id: str,
    role_row: dict[str, Any],
    message: dict[str, Any],
    timeout_seconds: float,
    poll_interval_seconds: float,
    event_driven: bool,
    hook_input: dict[str, Any],
) -> dict[str, Any]:
    start = time.monotonic()
    deadline = start + timeout_seconds
    message_id = normalize_cell(message.get("message_id"))
    last_event_wait: dict[str, Any] = {}
    while True:
        try:
            current_message = queue_message_by_id(inbox_path, role_id, message_id)
        except ValueError:
            current_message = message
        current_status = normalize_cell(current_message.get("status") or "pending").lower()
        report_ref = normalize_cell(current_message.get("report_path"))
        if current_status in {"done", "failed"}:
            duration = time.monotonic() - start
            event = record_role_queue_completion_wait_event(
                runtime=runtime,
                session_dir=session_dir,
                session_id=session_id,
                organization_instance_id=organization_instance_id,
                queue_root=queue_root,
                role_id=role_id,
                message=current_message,
                result=current_status,
                wait_result="completed",
                completion_source="inbox_status",
                duration_seconds=duration,
                timeout_seconds=timeout_seconds,
                poll_interval_seconds=poll_interval_seconds,
                report_ref=report_ref,
                event_wait=last_event_wait,
            )
            return event
        recovered = recover_pending_message_from_existing_report(
            runtime=runtime,
            session_dir=session_dir,
            session_id=session_id,
            organization_instance_id=organization_instance_id,
            queue_root=queue_root,
            inbox_path=inbox_path,
            role_id=role_id,
            role_row=role_row,
            message=current_message,
            now=current_timestamp(),
        )
        if recovered:
            recovery_handoff_input = merge_auto_handoff_context_from_payload(
                hook_input,
                current_message.get("payload") if isinstance(current_message.get("payload"), dict) else {},
            )
            auto_handoff = maybe_enqueue_auto_queue_handoff(
                runtime=runtime,
                state_root=state_root,
                session_id=session_id,
                organization_instance_id=organization_instance_id,
                from_role=role_id,
                finalized=recovered,
                hook_input=recovery_handoff_input,
            )
            duration = time.monotonic() - start
            event = record_role_queue_completion_wait_event(
                runtime=runtime,
                session_dir=session_dir,
                session_id=session_id,
                organization_instance_id=organization_instance_id,
                queue_root=queue_root,
                role_id=role_id,
                message=current_message,
                result=normalize_cell(recovered.get("result")) or "recovered",
                wait_result="completed",
                completion_source="report_file_recovery",
                duration_seconds=duration,
                timeout_seconds=timeout_seconds,
                poll_interval_seconds=poll_interval_seconds,
                report_ref=normalize_cell(recovered.get("report_ref")),
                event_wait=last_event_wait,
                auto_handoff=auto_handoff,
            )
            return event
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            duration = time.monotonic() - start
            return record_role_queue_completion_wait_event(
                runtime=runtime,
                session_dir=session_dir,
                session_id=session_id,
                organization_instance_id=organization_instance_id,
                queue_root=queue_root,
                role_id=role_id,
                message=current_message,
                result="timeout",
                wait_result="timeout",
                completion_source="bounded_wait",
                duration_seconds=duration,
                timeout_seconds=timeout_seconds,
                poll_interval_seconds=poll_interval_seconds,
                report_ref=report_ref,
                event_wait=last_event_wait,
            )
        wait_seconds = min(max(0.01, poll_interval_seconds), remaining)
        last_event_wait = queue_watch_wait_for_event(
            queue_root=queue_root,
            timeout_seconds=wait_seconds,
            event_driven=event_driven,
        )


def wait_for_role_queue_completion_if_requested(
    *,
    runtime: str,
    state_root: Path,
    session_dir: Path,
    session_id: str,
    organization_instance_id: str,
    queue_root: Path,
    inbox_path: Path,
    role_id: str,
    role_row: dict[str, Any],
    message: dict[str, Any],
    dry_run: bool,
    hook_input: dict[str, Any],
) -> dict[str, Any]:
    timeout_seconds, poll_interval_seconds, event_driven = role_queue_completion_wait_config(hook_input)
    if timeout_seconds <= 0:
        return {"result": "not_requested", "wait_result": "not_requested", "timeout_seconds": 0.0}
    if dry_run and not truthy_input(
        hook_input.get("completion_wait_in_dry_run")
        or hook_input.get("completionWaitInDryRun")
        or os.environ.get("ITB_ROLE_QUEUE_COMPLETION_WAIT_IN_DRY_RUN")
    ):
        return {
            "result": "skipped_dry_run",
            "wait_result": "skipped_dry_run",
            "timeout_seconds": timeout_seconds,
        }
    return wait_for_role_queue_completion(
        runtime=runtime,
        state_root=state_root,
        session_dir=session_dir,
        session_id=session_id,
        organization_instance_id=organization_instance_id,
        queue_root=queue_root,
        inbox_path=inbox_path,
        role_id=role_id,
        role_row=role_row,
        message=message,
        timeout_seconds=timeout_seconds,
        poll_interval_seconds=poll_interval_seconds,
        event_driven=event_driven,
        hook_input=hook_input,
    )


def update_roster_role_runtime_state(
    *,
    session_dir: Path,
    role_id: str,
    organization_instance_id: str,
    session_id: str,
    now: str,
    response_status: str,
    provider_status: str,
    notes: str,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    roster_path = session_dir / "roster.json"
    try:
        fallback_roster = read_json(roster_path) if roster_path.exists() else []
    except (OSError, json.JSONDecodeError):
        fallback_roster = []
    row_update = {
        "agent_id": role_id,
        "organization_instance_id": organization_instance_id,
        "parent_session_id": session_id,
        "response_status": response_status,
        "provider_status": provider_status,
        "last_seen_at": now,
        "last_probe_at": now,
        "notes": notes,
    }
    if extra:
        row_update.update(extra)
    merge_roster_agent_row_locked(roster_path, fallback_roster, role_id, row_update)
    return row_update


def preview_dead_letter_pending_message(
    *,
    runtime: str,
    session_id: str,
    organization_instance_id: str,
    queue_root: Path,
    role_id: str,
    role_row: dict[str, Any],
    message: dict[str, Any],
    now: str,
    reason: str,
    response_status: str = "failed",
    provider_status: str = "dead_letter",
) -> dict[str, Any]:
    message_id = normalize_cell(message.get("message_id"))
    report_path, report_ref = role_agent_report_path(queue_root, role_row, message)
    retry_count = int(message.get("retry_count") or 0)
    return {
        "ts": now,
        "runtime": runtime,
        "event_type": "queue_dead_letter",
        "session_id": session_id,
        "organization_instance_id": organization_instance_id,
        "role_id": role_id,
        "task_id": normalize_cell(message.get("task_id")),
        "message_id": message_id,
        "result": "failed",
        "reason": reason,
        "retry_count": retry_count,
        "report_path": str(report_path),
        "report_ref": report_ref,
        "dry_run": True,
        "roster_updated": False,
        "roster_response_status": response_status,
        "roster_provider_status": provider_status,
        "notification_class": notification_class_for_event(
            event_type="queue_dead_letter",
            result="dead_lettered",
        ),
    }


def dead_letter_pending_message(
    *,
    runtime: str,
    session_dir: Path,
    session_id: str,
    organization_instance_id: str,
    queue_root: Path,
    inbox_path: Path,
    role_id: str,
    role_row: dict[str, Any],
    message: dict[str, Any],
    now: str,
    reason: str,
    response_status: str = "failed",
    provider_status: str = "queue_dead_letter",
) -> dict[str, Any]:
    message_id = normalize_cell(message.get("message_id"))
    report_path, report_ref = role_agent_report_path(queue_root, role_row, message)
    retry_count = int(message.get("retry_count") or 0)
    report = {
        "report_version": "1",
        "report_id": report_path.stem,
        "report_type": "queue_dead_letter",
        "from_role": role_id,
        "task_id": normalize_cell(message.get("task_id")),
        "unit_id": normalize_cell(message.get("unit_id")),
        "message_id": message_id,
        "created_at": now,
        "result": "dead_letter",
        "status": "failed",
        "summary": reason,
        "error": reason,
        "retry_count": retry_count,
        "queue": {
            "queue_root": str(queue_root),
            "inbox_path": str(inbox_path),
            "report_path": report_ref,
        },
    }
    stamp_terminal_report_schema_validation(report, role_id=role_id, message_id=message_id)
    write_json_yaml(report_path, report)
    integrity = report_file_integrity(report_path)
    update_inbox_message(
        inbox_path,
        role_id,
        message_id,
        queue_root,
        {
            "status": "failed",
            "failed_at": now,
            "dead_letter_at": now,
            "dead_letter_reason": reason,
            "report_path": report_ref,
            "report_sha256": integrity["sha256"],
            "report_line_count": integrity["line_count"],
            "report_byte_count": integrity["byte_count"],
            "error": reason,
        },
    )
    event = {
        "ts": now,
        "runtime": runtime,
        "event_type": "queue_dead_letter",
        "session_id": session_id,
        "organization_instance_id": organization_instance_id,
        "role_id": role_id,
        "task_id": normalize_cell(message.get("task_id")),
        "message_id": message_id,
        "result": "failed",
        "reason": reason,
        "retry_count": retry_count,
        "report_path": str(report_path),
        "report_ref": report_ref,
        "report_integrity": integrity,
        "notification_class": notification_class_for_event(
            event_type="queue_dead_letter",
            result="dead_lettered",
        ),
    }
    row_update = update_roster_role_runtime_state(
        session_dir=session_dir,
        role_id=role_id,
        organization_instance_id=organization_instance_id,
        session_id=session_id,
        now=now,
        response_status=response_status,
        provider_status=provider_status,
        notes=f"queue recovery closed pending message: {reason}",
        extra={
            "last_failed_message_id": message_id,
            "last_failed_report_path": report_ref,
            "last_failure_reason": reason,
        },
    )
    event["roster_updated"] = True
    event["roster_response_status"] = row_update["response_status"]
    event["roster_provider_status"] = row_update["provider_status"]
    append_jsonl_atomic(session_dir / "queue-events.jsonl", event)
    append_queue_metric(
        session_dir=session_dir,
        queue_root=queue_root,
        runtime=runtime,
        session_id=session_id,
        organization_instance_id=organization_instance_id,
        role_id=role_id,
        message=message,
        event_type="dead_letter",
        result="failed",
        now=now,
        retry_count=retry_count,
        extra={"report_path": report_ref, "reason": reason, "report_integrity": integrity},
    )
    return event


def manual_close_pending_message(
    *,
    runtime: str,
    session_dir: Path,
    session_id: str,
    organization_instance_id: str,
    queue_root: Path,
    inbox_path: Path,
    role_id: str,
    role_row: dict[str, Any],
    message: dict[str, Any],
    now: str,
    reason: str,
    result: str = "superseded",
    dry_run: bool = False,
) -> dict[str, Any]:
    message_id = normalize_cell(message.get("message_id"))
    report_path, report_ref = role_agent_report_path(queue_root, role_row, message)
    retry_count = int(message.get("retry_count") or 0)
    result = normalize_cell(result) or "superseded"
    reason = normalize_cell(reason) or "manual queue close"
    report = {
        "report_version": "1",
        "report_id": report_path.stem,
        "report_type": "queue_manual_close",
        "from_role": role_id,
        "task_id": normalize_cell(message.get("task_id")),
        "unit_id": normalize_cell(message.get("unit_id")),
        "message_id": message_id,
        "created_at": now,
        "result": result,
        "status": "failed",
        "summary": reason,
        "error": reason,
        "retry_count": retry_count,
        "manual_close": {
            "reason": reason,
            "result": result,
            "previous_status": normalize_cell(message.get("status")),
        },
        "queue": {
            "queue_root": str(queue_root),
            "inbox_path": str(inbox_path),
            "report_path": report_ref,
        },
    }
    stamp_terminal_report_schema_validation(report, role_id=role_id, message_id=message_id)
    inbox_updates = {
        "status": "failed",
        "failed_at": now,
        "manual_close_at": now,
        "manual_close_reason": reason,
        "manual_close_result": result,
        "report_path": report_ref,
        "error": reason,
    }
    event = {
        "ts": now,
        "runtime": runtime,
        "event_type": "queue_manual_close",
        "session_id": session_id,
        "organization_instance_id": organization_instance_id,
        "role_id": role_id,
        "task_id": normalize_cell(message.get("task_id")),
        "message_id": message_id,
        "result": result,
        "status": "failed",
        "reason": reason,
        "retry_count": retry_count,
        "report_path": str(report_path),
        "report_ref": report_ref,
        "dry_run": dry_run,
        "roster_updated": False,
        "notification_class": "silent",
    }
    if dry_run:
        event["would_write_report"] = report
        event["would_update_inbox"] = inbox_updates
        return event

    write_json_yaml(report_path, report)
    integrity = report_file_integrity(report_path)
    inbox_updates = inbox_updates | {
        "report_sha256": integrity["sha256"],
        "report_line_count": integrity["line_count"],
        "report_byte_count": integrity["byte_count"],
    }
    update_inbox_message(inbox_path, role_id, message_id, queue_root, inbox_updates)
    event["report_integrity"] = integrity
    append_jsonl_atomic(session_dir / "queue-events.jsonl", event)
    append_queue_metric(
        session_dir=session_dir,
        queue_root=queue_root,
        runtime=runtime,
        session_id=session_id,
        organization_instance_id=organization_instance_id,
        role_id=role_id,
        message=message,
        event_type="manual_close",
        result=result,
        now=now,
        retry_count=retry_count,
        extra={"report_path": report_ref, "reason": reason, "report_integrity": integrity},
    )
    return event


def record_queue_sla_breach(
    *,
    runtime: str,
    session_dir: Path,
    session_id: str,
    organization_instance_id: str,
    queue_root: Path,
    inbox_path: Path,
    role_id: str,
    message: dict[str, Any],
    now: str,
    dry_run: bool = False,
    notification_result: str = "sla_breached",
) -> dict[str, Any] | None:
    sla = gate_sla_status_for_message(role_id, message, now)
    if not sla.get("sla_breached"):
        return None
    message_id = normalize_cell(message.get("message_id"))
    retry_count = int(message.get("retry_count") or 0)
    event = {
        "ts": now,
        "runtime": runtime,
        "event_type": "queue_sla_breach",
        "session_id": session_id,
        "organization_instance_id": organization_instance_id,
        "role_id": role_id,
        "from_role": normalize_cell(message.get("from_role")),
        "to_role": normalize_cell(message.get("to_role")) or role_id,
        "hop_key": f"{normalize_cell(message.get('from_role')) or 'unknown'}->{normalize_cell(message.get('to_role')) or role_id}",
        "task_id": normalize_cell(message.get("task_id")),
        "message_id": message_id,
        "result": "sla_breached",
        "retry_count": retry_count,
        **sla,
        "notification_class": notification_class_for_event(
            event_type="queue_sla_breach",
            result=notification_result or "sla_breached",
            sla_breached=True,
        ),
    }
    if notification_result and notification_result != "sla_breached":
        event["notification_result"] = notification_result
    if dry_run:
        event["dry_run"] = True
        event["would_update_inbox"] = {
            "last_sla_breach_at": now,
            "sla_breach_count": int(message.get("sla_breach_count") or 0) + 1,
            "sla_threshold_seconds": sla["sla_threshold_seconds"],
            "sla_pending_seconds": sla["sla_pending_seconds"],
            "sla_threshold_source": sla["sla_threshold_source"],
        }
        return event
    update_inbox_message(
        inbox_path,
        role_id,
        message_id,
        queue_root,
        {
            "last_sla_breach_at": now,
            "sla_breach_count": int(message.get("sla_breach_count") or 0) + 1,
            "sla_threshold_seconds": sla["sla_threshold_seconds"],
            "sla_pending_seconds": sla["sla_pending_seconds"],
            "sla_threshold_source": sla["sla_threshold_source"],
        },
    )
    append_jsonl_atomic(session_dir / "queue-events.jsonl", event)
    append_queue_metric(
        session_dir=session_dir,
        queue_root=queue_root,
        runtime=runtime,
        session_id=session_id,
        organization_instance_id=organization_instance_id,
        role_id=role_id,
        message=message,
        event_type="sla_breach",
        result="sla_breached",
        now=now,
        retry_count=retry_count,
        extra={"notification_class": event["notification_class"]},
    )
    return event


def write_role_agent_report(
    *,
    queue_root: Path,
    role_row: dict[str, Any],
    message: dict[str, Any],
    instruction: str,
    report_path: Path,
    report_ref: str,
    evidence: dict[str, Any],
    result: str,
    status: str,
    error: str = "",
) -> dict[str, Any]:
    now = current_timestamp()
    report = {
        "report_version": "1",
        "report_id": report_path.stem,
        "report_type": "role_agent_worker_report",
        "from_role": role_row["role_id"],
        "task_id": normalize_cell(message.get("task_id")),
        "unit_id": normalize_cell(message.get("unit_id")),
        "message_id": normalize_cell(message.get("message_id")),
        "created_at": now,
        "result": result,
        "status": status,
        "error": error,
        "summary": (
            "role-agent worker consumed queue message; provider execution deferred"
            if evidence.get("usage_source") == "role_agent_worker_local_stub"
            else "role-agent worker recorded provider evidence"
        ),
        "instruction_preview": instruction[:500],
        "evidence": evidence,
        "queue": {
            "report_path": report_ref,
            "queue_root": str(queue_root),
        },
    }
    stamp_terminal_report_schema_validation(
        report,
        role_id=normalize_cell(role_row["role_id"]),
        message_id=normalize_cell(message.get("message_id")),
    )
    write_json_yaml(report_path, report)
    return report


def role_agent_step_once(*, runtime: str, state_root: Path, hook_input: dict[str, Any]) -> dict[str, Any]:
    session_id = str(
        current_session_id(state_root, hook_input)
        or "unknown-session"
    )
    session_dir = state_root / safe_id(session_id)
    state_path = session_dir / "bootstrap.json"
    state = read_json(state_path) if state_path.exists() else {}
    organization_instance_id = str(
        state.get("organization_instance_id")
        or hook_input.get("organization_instance_id")
        or hook_input.get("organizationInstanceId")
        or os.environ.get("ITB_ORGANIZATION_INSTANCE_ID")
        or organization_id(session_id)
    )
    role_id = normalize_cell(
        hook_input.get("role_id")
        or hook_input.get("roleId")
        or hook_input.get("agent_id")
        or hook_input.get("agentId")
        or os.environ.get("ITB_AGENT_ID")
    )
    if not role_id:
        return {"decision": "block", "reason": "role-agent-worker requires role_id or ITB_AGENT_ID"}
    role_row = role_agent_row_for(role_id, organization_instance_id=organization_instance_id)
    if not role_row:
        return {"decision": "block", "reason": f"role-agent registry has no active role: {role_id}"}
    queue_root = queue_root_for(session_dir, hook_input)
    inbox_path = queue_root / str(role_row["inbox_path"])
    now = current_timestamp()
    if not hook_input_has_provider_evidence(hook_input):
        inbox = load_inbox(inbox_path, role_id)
        pending = [
            message
            for message in inbox.get("messages", [])
            if isinstance(message, dict) and normalize_cell(message.get("status") or "pending") == "pending"
        ]
        if pending:
            return {
                "roleAgentWorker": {
                    "result": "message_blocked",
                    "role_id": role_id,
                    "queue_root": str(queue_root),
                    "inbox_path": str(inbox_path),
                    "messages_processed": 0,
                    "messages_blocked": len(pending),
                    "error": "role-agent-worker no longer claims pending messages without provider evidence; use role-queue nudge and provider-authored report",
                }
            }
        return {
            "roleAgentWorker": {
                "result": "idle",
                "role_id": role_id,
                "queue_root": str(queue_root),
                "inbox_path": str(inbox_path),
                "messages_processed": 0,
            }
        }
    message, _inbox = claim_pending_message(inbox_path, role_id, queue_root, now=now)
    if message is None:
        return {
            "roleAgentWorker": {
                "result": "idle",
                "role_id": role_id,
                "queue_root": str(queue_root),
                "inbox_path": str(inbox_path),
                "messages_processed": 0,
            }
        }

    message_id = normalize_cell(message.get("message_id"))
    try:
        instruction, instruction_path = role_agent_load_instruction(queue_root, message)
        report_path, report_ref = role_agent_report_path(queue_root, role_row, message)
        evidence = role_agent_provider_evidence(role_row, hook_input)
        evidence["agent_id"] = role_id
        evidence_errors = validate_role_agent_provider_evidence(evidence)
        if evidence_errors:
            raise ValueError("; ".join(evidence_errors))
        report = write_role_agent_report(
            queue_root=queue_root,
            role_row=role_row,
            message=message,
            instruction=instruction,
            report_path=report_path,
            report_ref=report_ref,
            evidence=evidence,
            result="completed",
            status="done",
        )
        integrity = report_file_integrity(report_path)
        update_inbox_message(
            inbox_path,
            role_id,
            message_id,
            queue_root,
            {
                "status": "done",
                "done_at": current_timestamp(),
                "report_path": report_ref,
                "report_sha256": integrity["sha256"],
                "report_line_count": integrity["line_count"],
                "report_byte_count": integrity["byte_count"],
            },
        )
        event = {
            "ts": current_timestamp(),
            "runtime": runtime,
            "event_type": "role_agent_worker",
            "session_id": session_id,
            "organization_instance_id": organization_instance_id,
            "role_id": role_id,
            "message_id": message_id,
            "task_id": message.get("task_id", ""),
            "result": "message_processed",
            "report_path": str(report_path),
            "report_integrity": integrity,
            "instruction_path": instruction_path,
            "usage_source": evidence.get("usage_source", ""),
        }
        append_jsonl_atomic(session_dir / "queue-events.jsonl", event)
        append_queue_metric(
            session_dir=session_dir,
            queue_root=queue_root,
            runtime=runtime,
            session_id=session_id,
            organization_instance_id=organization_instance_id,
            role_id=role_id,
            message=message,
            event_type="finalized",
            result="done",
            now=current_timestamp(),
            extra=provider_usage_metric_fields(evidence)
            | {
                "usage_source": normalize_cell(evidence.get("usage_source")),
                "effective_model": normalize_cell(evidence.get("effective_model")),
                "transcript_path": normalize_cell(evidence.get("transcript_path")),
                "report_integrity": integrity,
            },
        )
        return {
            "roleAgentWorker": event
            | {
                "queue_root": str(queue_root),
                "inbox_path": str(inbox_path),
                "messages_processed": 1,
                "report": report,
            }
        }
    except Exception as exc:
        error = str(exc)
        fallback_ref = str(Path(str(role_row["report_dir"])) / normalize_cell(message.get("task_id")) / f"failed-{message_id}.yaml")
        fallback_path = safe_queue_relative_path(queue_root, fallback_ref, "fallback_report_path")
        evidence = role_agent_provider_evidence(role_row, hook_input)
        report = write_role_agent_report(
            queue_root=queue_root,
            role_row=role_row,
            message=message,
            instruction="",
            report_path=fallback_path,
            report_ref=fallback_ref,
            evidence=evidence,
            result="failed",
            status="failed",
            error=error,
        )
        integrity = report_file_integrity(fallback_path)
        update_inbox_message(
            inbox_path,
            role_id,
            message_id,
            queue_root,
            {
                "status": "failed",
                "failed_at": current_timestamp(),
                "report_path": fallback_ref,
                "report_sha256": integrity["sha256"],
                "report_line_count": integrity["line_count"],
                "report_byte_count": integrity["byte_count"],
                "error": error,
            },
        )
        append_queue_metric(
            session_dir=session_dir,
            queue_root=queue_root,
            runtime=runtime,
            session_id=session_id,
            organization_instance_id=organization_instance_id,
            role_id=role_id,
            message=message,
            event_type="finalized",
            result="failed",
            now=current_timestamp(),
            extra=provider_usage_metric_fields(evidence)
            | {
                "error": error,
                "usage_source": normalize_cell(evidence.get("usage_source")),
                "effective_model": normalize_cell(evidence.get("effective_model")),
                "transcript_path": normalize_cell(evidence.get("transcript_path")),
                "report_integrity": integrity,
            },
        )
        return {
            "roleAgentWorker": {
                "result": "message_failed",
                "role_id": role_id,
                "message_id": message_id,
                "queue_root": str(queue_root),
                "inbox_path": str(inbox_path),
                "report_path": str(fallback_path),
                "messages_processed": 0,
                "error": error,
                "report": report,
            }
        }


def role_agent_worker(*, runtime: str, state_root: Path, hook_input: dict[str, Any]) -> dict[str, Any]:
    max_messages_raw = normalize_cell(
        hook_input.get("max_messages")
        or hook_input.get("maxMessages")
        or os.environ.get("ITB_ROLE_AGENT_MAX_MESSAGES")
        or "1"
    )
    max_messages = 0 if max_messages_raw in {"0", "infinite", "forever"} else int(max_messages_raw)
    poll_interval = bounded_float_input(
        hook_input.get("poll_interval_seconds")
        or hook_input.get("pollIntervalSeconds")
        or os.environ.get("ITB_ROLE_AGENT_POLL_INTERVAL_SECONDS")
        or 2,
        default=2,
        minimum=0.1,
        maximum=60,
    )
    idle_timeout = bounded_float_input(
        hook_input.get("idle_timeout_seconds")
        or hook_input.get("idleTimeoutSeconds")
        or os.environ.get("ITB_ROLE_AGENT_IDLE_TIMEOUT_SECONDS")
        or 0,
        default=0,
        minimum=0,
        maximum=86400,
    )
    started = time.monotonic()
    results: list[dict[str, Any]] = []
    while True:
        step = role_agent_step_once(runtime=runtime, state_root=state_root, hook_input=hook_input)
        worker = step.get("roleAgentWorker", {})
        if worker.get("result") in {"message_processed", "message_failed"}:
            results.append(worker)
            if max_messages and len(results) >= max_messages:
                break
            continue
        if worker.get("result") == "message_blocked":
            results.append(worker)
            break
        if max_messages and results:
            break
        if max_messages == 0 and idle_timeout <= 0:
            time.sleep(poll_interval)
            continue
        if idle_timeout <= 0 or time.monotonic() - started >= idle_timeout:
            results.append(worker)
            break
        time.sleep(poll_interval)
    processed = sum(1 for item in results if item.get("result") == "message_processed")
    failed = sum(1 for item in results if item.get("result") == "message_failed")
    blocked = sum(1 for item in results if item.get("result") == "message_blocked")
    return {
        "roleAgentWorker": {
            "result": "worker_complete",
            "messages_processed": processed,
            "messages_failed": failed,
            "messages_blocked": blocked,
            "steps": results,
        }
    }


def block_reason(state: dict[str, Any]) -> str:
    return f"""ITB bootstrap state is incomplete.

| Field | Value |
|---|---|
| runtime | `{state.get('runtime', '')}` |
| session_id | `{state.get('session_id', '')}` |
| bootstrap_status | `{state.get('bootstrap_status', '')}` |
| readiness_scope | `{state.get('readiness_scope', '')}` |
| state_dir | `{state.get('outputs', {}).get('state_dir', '')}` |
| validation_errors | `{'; '.join(state.get('validation_errors', []))}` |

Run metadata-only `infra-team-bootstrap session-start` or repair task flow evidence first.
"""



def read_bootstrap_state(state_path: Path) -> dict[str, Any]:
    try:
        state = read_json(state_path) if state_path.exists() else {}
        return state if isinstance(state, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def write_shutdown_evidence(
    *,
    runtime: str,
    session_dir: Path,
    session_id: str,
    hook_input: dict[str, Any],
    shutdown_result: dict[str, Any],
    now: str,
    event_label: str,
    event_type: str,
    input_filename: str,
    evidence_prefix: str,
    notes: str,
) -> None:
    atomic_write_text(session_dir / "status", "archived\n")
    atomic_write_text(session_dir / "last_event", f"{event_label} {now}\n")
    write_json_yaml(session_dir / input_filename, hook_input)
    write_json_yaml(session_dir / "shutdown.json", shutdown_result)
    append_jsonl_atomic(
        session_dir / "invocation-evidence.jsonl",
        invocation_evidence_entry(
            ts=now,
            runtime=runtime,
            event_type=event_type,
            session_id=session_id,
            organization_instance_id=shutdown_result["organization_instance_id"],
            result=f"{evidence_prefix}_{shutdown_result.get('archive_result', 'archived')}",
            usage_source="bootstrap_metadata_only",
            notes=notes,
            extra=shutdown_result,
        ),
    )



def archive_shutdown(
    *,
    runtime: str,
    state_root: Path,
    hook_input: dict[str, Any],
    session_id: str,
    dry_run: bool = False,
) -> dict[str, Any]:
    resolved_session_id = normalize_cell(session_id)
    if not resolved_session_id:
        resolved_session_id, _ = resolve_session_id(state_root, hook_input)
    if not resolved_session_id:
        resolved_session_id = "unknown-session"
    session_dir = state_root / safe_id(resolved_session_id)
    session_dir.mkdir(parents=True, exist_ok=True)
    now = current_timestamp()
    state = read_bootstrap_state(session_dir / "bootstrap.json")
    organization_instance_id = normalize_cell(
        state.get("organization_instance_id")
        or hook_input.get("organization_instance_id")
        or hook_input.get("organizationInstanceId")
        or organization_id(resolved_session_id)
    )
    result = {
        "runtime": runtime,
        "session_id": resolved_session_id,
        "organization_instance_id": organization_instance_id,
        "archive_result": "dry_run" if dry_run else "archived",
        "dry_run": dry_run,
        "archived_at": now,
        "shutdown_scope": "state_only_headless",
        "note": "No provider process shutdown is performed by Sahai hooks; CLI orchestration owns worker lifecycle.",
    }
    if not dry_run:
        write_shutdown_evidence(
            runtime=runtime,
            session_dir=session_dir,
            session_id=resolved_session_id,
            hook_input=hook_input,
            shutdown_result=result,
            now=now,
            event_label="ArchiveShutdown",
            event_type="archive_shutdown",
            input_filename="archive-shutdown-input.json",
            evidence_prefix="archive_shutdown",
            notes="State-only archive completed for headless Sahai runtime.",
        )
    return {"archiveShutdown": result}


def resolve_session_id(state_root: Path, hook_input: dict[str, Any] | None = None) -> tuple[str, str]:
    if hook_input:
        hook_session_id = normalize_cell(hook_input.get("session_id") or hook_input.get("sessionId"))
        if hook_session_id:
            return hook_session_id, "hook_input"
    parent_session_id = normalize_cell(os.environ.get("ITB_PARENT_SESSION_ID"))
    if parent_session_id:
        return parent_session_id, "env:ITB_PARENT_SESSION_ID"
    last_session = state_root / "last-session"
    if not last_session.exists():
        return "", "missing"
    return last_session.read_text(encoding="utf-8").strip(), "last-session"


def current_session_id(state_root: Path, hook_input: dict[str, Any] | None = None) -> str:
    session_id, _source = resolve_session_id(state_root, hook_input)
    return session_id



def gate_skill_contract_lint_root(hook_input: dict[str, Any]) -> Path:
    raw_root = normalize_cell(hook_input.get("skills_root") or hook_input.get("skillsRoot"))
    return Path(raw_root).expanduser() if raw_root else SKILLS_ROOT


def gate_skill_contract_lint_findings(skills_root: Path) -> tuple[list[dict[str, Any]], list[str]]:
    findings: list[dict[str, Any]] = []
    checked_files: list[str] = []
    file_paths: dict[str, Path] = {}
    default_skills_root = same_filesystem_path(skills_root, SKILLS_ROOT)

    def default_contract_path(relative: str) -> Path | None:
        if not default_skills_root:
            return None
        if relative.endswith("/SKILL.md"):
            role_id = relative.split("/", 1)[0]
            if role_id in SAIHAI_MIGRATED_ROLE_IDS:
                path = sahai_role_skill_path(role_id)
                if path.exists():
                    return path
                legacy_path = legacy_sahai_role_skill_path(role_id)
                return legacy_path if legacy_path.exists() else None
        if relative.startswith("infra-team-bootstrap/config/"):
            path = ITB_ROOT / "config" / relative.removeprefix("infra-team-bootstrap/config/")
            return path if path.exists() else None
        if relative.startswith("infra-team-bootstrap/references/"):
            path = ITB_ROOT / "references" / relative.removeprefix("infra-team-bootstrap/references/")
            return path if path.exists() else None
        return None

    def finding_file(relative: str) -> str:
        return str(file_paths.get(relative, skills_root / relative))

    def read_required(relative: str) -> str:
        path = default_contract_path(relative) or skills_root / relative
        file_paths[relative] = path
        checked_files.append(str(path))
        if not path.exists():
            findings.append(
                {
                    "rule_id": "required_file_missing",
                    "severity": "error",
                    "file": str(path),
                    "message": f"required gate contract file missing: {relative}",
                }
            )
            return ""
        return path.read_text(encoding="utf-8")

    def require_contains(relative: str, text: str, pattern: str, rule_id: str, message: str) -> None:
        if text and pattern not in text:
            findings.append(
                {
                    "rule_id": rule_id,
                    "severity": "error",
                    "file": finding_file(relative),
                    "message": message,
                    "expected": pattern,
                }
            )

    def forbid_contains(relative: str, text: str, pattern: str, rule_id: str, message: str) -> None:
        if text and pattern in text:
            findings.append(
                {
                    "rule_id": rule_id,
                    "severity": "error",
                    "file": finding_file(relative),
                    "message": message,
                    "forbidden": pattern,
                }
            )

    def forbid_regex(relative: str, text: str, pattern: str, rule_id: str, message: str) -> None:
        match = re.search(pattern, text or "", flags=re.IGNORECASE)
        if match:
            findings.append(
                {
                    "rule_id": rule_id,
                    "severity": "error",
                    "file": finding_file(relative),
                    "message": message,
                    "forbidden_pattern": pattern,
                    "match": match.group(0),
                }
            )

    assessor_rel = "gate-task-assessor/SKILL.md"
    guardian_rel = "gate-task-guardian/SKILL.md"
    gtc_rel = "gate-task-creator/SKILL.md"
    gte_rel = "gate-task-evaluator/SKILL.md"
    humanizer_rel = "gate-response-humanizer/SKILL.md"
    tester_rel = "tech-tester/SKILL.md"
    publisher_rel = "git-publisher/SKILL.md"
    infra_qa_rel = "infra-local-qa/SKILL.md"
    tpm_rel = "teams-project-manager/SKILL.md"
    registry_rel = "infra-team-bootstrap/config/role-agent-registry.yaml"
    model_registry_rel = "infra-team-bootstrap/references/model-registry.md"
    team_config_rel = "infra-team-bootstrap/references/team-config.md"
    comprehensive_plan_rel = "infra-team-bootstrap/references/headless-organization-comprehensive-plan.md"

    assessor_text = read_required(assessor_rel)
    guardian_text = read_required(guardian_rel)
    gtc_text = read_required(gtc_rel)
    gte_text = read_required(gte_rel)
    humanizer_text = read_required(humanizer_rel)
    tester_text = read_required(tester_rel)
    publisher_text = read_required(publisher_rel)
    infra_qa_text = read_required(infra_qa_rel)
    tpm_text = read_required(tpm_rel)
    registry_text = read_required(registry_rel)
    model_registry_text = read_required(model_registry_rel)
    team_config_text = read_required(team_config_rel)
    comprehensive_plan_text = read_required(comprehensive_plan_rel)

    for relative, text, role_label in (
        (assessor_rel, assessor_text, "assessor"),
        (guardian_rel, guardian_text, "guardian"),
    ):
        require_contains(relative, text, "status: reference", f"{role_label}_must_be_reference", f"{role_label} role must be reference-only")
        require_contains(
            relative,
            text,
            "runtime execution agent としては使わず",
            f"{role_label}_must_forbid_runtime",
            f"{role_label} role must explicitly forbid runtime execution use",
        )
        require_contains(
            relative,
            text,
            "allowed-tools: Read, Grep, Glob",
            f"{role_label}_allowed_tools_thin",
            f"{role_label} role must keep thin read-only tool surface",
        )
        forbid_contains(relative, text, "allowed-tools: Read, Grep, Glob, Bash", f"{role_label}_forbid_bash", f"{role_label} role must not include Bash")
        forbid_contains(relative, text, "## 実行手順", f"{role_label}_forbid_runtime_steps", f"{role_label} role must not define runtime procedure steps")
        forbid_contains(relative, text, "## Validation Checklist", f"{role_label}_forbid_checklist", f"{role_label} role must not carry old validation checklist")

    forbid_contains(assessor_rel, assessor_text, "## Completion Assessment", "assessor_forbid_completion_assessment_section", "assessor must not render old Completion Assessment section")
    forbid_contains(assessor_rel, assessor_text, "Handoff To: gate-task-assessor", "assessor_forbid_self_handoff", "assessor must not be a new-task handoff target")
    forbid_regex(assessor_rel, assessor_text, r"\|\s*(?:Handoff To|Completion Handoff)\s*\|\s*`?gate-task-assessor`?\s*\|", "assessor_forbid_self_handoff_table", "assessor must not define markdown handoff table targeting itself")
    forbid_regex(assessor_rel, assessor_text, r"Completion Report\s*を\s*`?gate-task-assessor`?\s*へ\s*渡す", "assessor_forbid_completion_report_handoff", "assessor must not instruct handoff to gate-task-assessor")
    forbid_contains(guardian_rel, guardian_text, "## Guardian Verdict", "guardian_forbid_verdict_section", "guardian must not render old Guardian Verdict section")
    forbid_contains(guardian_rel, guardian_text, "| Guardian | gate-task-guardian |", "guardian_forbid_markdown_verdict_table", "guardian must not define old markdown verdict table")
    forbid_regex(guardian_rel, guardian_text, r"\|\s*(?:Handoff To|Completion Handoff|Guardian)\s*\|\s*`?gate-task-guardian`?\s*\|", "guardian_forbid_self_handoff_table", "guardian must not define markdown handoff/verdict table targeting itself")

    require_contains(gtc_rel, gtc_text, "gtc-scaffold", "gtc_scaffold_contract_required", "GTC must point mechanical artifact creation to gtc-scaffold")
    require_contains(gtc_rel, gtc_text, "LLM が手書きしない", "gtc_no_handwritten_artifacts_required", "GTC must forbid LLM handwritten task artifacts")
    require_contains(gtc_rel, gtc_text, "builder command", "gtc_builder_command_owner_required", "GTC must identify builder command as artifact owner")
    forbid_contains(gtc_rel, gtc_text, "## Validation Checklist", "gtc_forbid_old_checklist", "GTC must not carry old checklist prose")
    forbid_contains(gtc_rel, gtc_text, "18-step", "gtc_forbid_old_step_count", "GTC must not mention old 18-step procedure")

    require_contains(gte_rel, gte_text, "allowed-tools: Read, Grep, Glob", "gte_allowed_tools_thin", "GTE must keep thin read-only tool surface")
    require_contains(gte_rel, gte_text, "## Thin Verdict Scope", "gte_thin_verdict_scope_required", "GTE must declare thin verdict scope")
    require_contains(gte_rel, gte_text, "role-report の compact fields", "gte_compact_fields_required", "GTE must use compact role-report fields")
    require_contains(gte_rel, gte_text, "provider が手書きしない", "gte_no_handwritten_artifact_required", "GTE must not hand-write mechanical artifacts")
    forbid_contains(gte_rel, gte_text, "allowed-tools: Read, Grep, Glob, Bash", "gte_forbid_bash", "GTE must not include Bash")
    forbid_contains(gte_rel, gte_text, "## 実行手順", "gte_forbid_runtime_steps", "GTE must not define old runtime procedure")
    forbid_contains(gte_rel, gte_text, "## Validation Checklist", "gte_forbid_checklist", "GTE must not carry old validation checklist")
    forbid_contains(gte_rel, gte_text, "## Quality Evaluation", "gte_forbid_quality_evaluation_section", "GTE must not render full Quality Evaluation section")

    require_contains(humanizer_rel, humanizer_text, "`finalization-check` と `final-transport-render-check`", "humanizer_finalization_contract_required", "humanizer must point to finalization commands")
    forbid_contains(humanizer_rel, humanizer_text, "guardian_status: complete", "humanizer_forbid_guardian_status", "humanizer must not wait on guardian status")
    forbid_contains(humanizer_rel, humanizer_text, "Guardian Verdict が complete", "humanizer_forbid_guardian_verdict", "humanizer must not require Guardian Verdict")
    forbid_contains(humanizer_rel, humanizer_text, "`gate-task-guardian` complete", "humanizer_forbid_guardian_complete", "humanizer must not require gate-task-guardian complete")

    require_contains(tester_rel, tester_text, "`finalization-check` complete", "tester_finalization_required", "tester must refer to finalization-check completion")
    forbid_contains(tester_rel, tester_text, "`gate-task-guardian` complete", "tester_forbid_guardian_complete", "tester must not refer to guardian completion")
    require_contains(publisher_rel, publisher_text, "`finalization-check` / `Completion Envelope`", "publisher_finalization_required", "publisher must require finalization-check / Completion Envelope")
    forbid_contains(publisher_rel, publisher_text, "`vault_final_update` / `gate-task-guardian`", "publisher_forbid_guardian_path", "publisher must not route to gate-task-guardian")
    forbid_contains(publisher_rel, publisher_text, "GTG", "publisher_forbid_gtg", "publisher must not use GTG flow")
    require_contains(infra_qa_rel, infra_qa_text, "`vault-final-update` / `finalization-check`", "infra_qa_finalization_required", "infra QA must use finalization command path")
    forbid_contains(infra_qa_rel, infra_qa_text, "`gate-task-guardian` に Vault 更新証跡を渡す", "infra_qa_forbid_guardian_handoff", "infra QA must not hand off Vault evidence to guardian")

    for director_name in ("tech-director", "contents-director", "business-director", "infra-director"):
        relative = f"{director_name}/SKILL.md"
        text = read_required(relative)
        require_contains(relative, text, "`teams-project-manager` via structured Completion Report", "director_tpm_handoff_required", "Director must report completion to TPM")
        require_contains(relative, text, "`team-completion-check` command evidence", "director_team_completion_command_required", "Director must rely on team-completion-check evidence")
        for pattern in (
            "then `gate-task-assessor` after Completion Report",
            "Completion Report を `gate-task-assessor` へ渡す",
            "| Handoff To | gate-task-assessor |",
            "| Completion Handoff | `gate-task-assessor` |",
            "handoff 先を `gate-task-assessor`",
        ):
            forbid_contains(relative, text, pattern, "director_forbid_assessor_handoff", "Director must not hand off to gate-task-assessor")

    try:
        registry_path = file_paths.get(registry_rel, skills_root / registry_rel)
        registry = load_role_agent_registry(registry_path)
    except Exception as exc:
        findings.append(
            {
                "rule_id": "role_agent_registry_unreadable",
                "severity": "error",
                "file": str(file_paths.get(registry_rel, skills_root / registry_rel)),
                "message": f"role-agent registry unreadable: {type(exc).__name__}: {exc}",
            }
        )
        registry = {}
    agents = registry.get("agents") if isinstance(registry, dict) else {}
    for role_id in ("gate-task-assessor", "gate-task-guardian"):
        row = agents.get(role_id) if isinstance(agents, dict) else {}
        if not isinstance(row, dict):
            findings.append(
                {
                    "rule_id": "reference_gate_registry_missing",
                    "severity": "error",
                    "file": str(file_paths.get(registry_rel, skills_root / registry_rel)),
                    "message": f"{role_id} missing from role-agent registry",
                }
            )
            continue
        if truthy_input(row.get("queue_consumer")):
            findings.append(
                {
                    "rule_id": "reference_gate_queue_consumer_enabled",
                    "severity": "error",
                    "file": str(file_paths.get(registry_rel, skills_root / registry_rel)),
                    "message": f"{role_id} must not be queue_consumer",
                }
            )
        if normalize_string_list(row.get("allowed_tools")) != ["Read", "Grep", "Glob"]:
            findings.append(
                {
                    "rule_id": "reference_gate_allowed_tools_not_thin",
                    "severity": "error",
                    "file": str(file_paths.get(registry_rel, skills_root / registry_rel)),
                    "message": f"{role_id} allowed_tools must be Read/Grep/Glob only",
                    "actual": row.get("allowed_tools"),
                }
            )

    model_rows: dict[str, dict[str, str]] = {}
    if model_registry_text:
        try:
            model_registry_path = file_paths.get(model_registry_rel, skills_root / model_registry_rel)
            model_rows = {row["agent_id"]: row for row in parse_model_registry_file(model_registry_path)}
        except Exception as exc:
            findings.append(
                {
                    "rule_id": "model_registry_unreadable",
                    "severity": "error",
                    "file": str(file_paths.get(model_registry_rel, skills_root / model_registry_rel)),
                    "message": f"model registry unreadable: {type(exc).__name__}: {exc}",
                }
            )
    for role_id in ("gate-task-assessor", "gate-task-guardian"):
        row = model_rows.get(role_id, {})
        if not row:
            findings.append(
                {
                    "rule_id": "reference_gate_model_registry_missing",
                    "severity": "error",
                    "file": str(file_paths.get(model_registry_rel, skills_root / model_registry_rel)),
                    "message": f"{role_id} missing from model registry",
                }
            )
            continue
        expected_model_values = {
            "status": "reference",
            "always_active": "false",
        }
        for key, expected in expected_model_values.items():
            actual = normalize_cell(row.get(key))
            if actual != expected:
                findings.append(
                    {
                        "rule_id": "reference_gate_model_registry_runtime_enabled",
                        "severity": "error",
                        "file": str(file_paths.get(model_registry_rel, skills_root / model_registry_rel)),
                        "message": f"{role_id} model registry {key} must be {expected}",
                        "agent_id": role_id,
                        "field": key,
                        "expected": expected,
                        "actual": actual,
                    }
                )

    require_contains(team_config_rel, team_config_text, "`status: reference`", "team_config_reference_profile_required", "team config must document reference roles")
    forbid_contains(team_config_rel, team_config_text, "Gate 系 role と `teams-project-manager` は必ず", "team_config_forbid_all_gate_runtime", "team config must not require all gate roles to be runtime-active")
    require_contains(comprehensive_plan_rel, comprehensive_plan_text, "-> team-completion-check", "plan_team_completion_check_required", "comprehensive plan must include team-completion-check")
    require_contains(comprehensive_plan_rel, comprehensive_plan_text, "-> finalization-check", "plan_finalization_check_required", "comprehensive plan must include finalization-check")
    require_contains(comprehensive_plan_rel, comprehensive_plan_text, "-> final-transport-render-check", "plan_final_transport_check_required", "comprehensive plan must include final-transport-render-check")
    for pattern in ("-> assessor", "-> guardian", "guardian complete", "Guardian 必須化", "assessor / evaluator / guardian"):
        forbid_contains(comprehensive_plan_rel, comprehensive_plan_text, pattern, "plan_forbid_retired_gate_runtime_chain", "comprehensive plan must not describe retired assessor/guardian chain")

    retired_eval_patterns = [
        "gate-task-assessor へ渡す",
        "gate-task-assessor への Completion Report handoff",
        "handoff 先を常に gate-task-assessor",
        "Completion Report を作成して gate-task-assessor",
        "gate-task-assessor ->",
        "assessor / evaluator / guardian",
        "assessor は ready_for_evaluation",
        "GTG",
        "Guardian が complete",
        "Guardian Status Checked",
        "Guardian Verdict complete",
        "Guardian Verdict が missing",
        "Guardian Verdict を作って",
        "guardian_status",
        "guardian -> main transport renderer",
        "guardian complete",
        "guardian OK",
        "next_role: gate-task-guardian",
        "gate-task-guardian へ戻す",
    ]
    for eval_path in sorted(skills_root.glob("*/evals/evals.json")):
        checked_files.append(str(eval_path))
        try:
            eval_payload = json.loads(eval_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            findings.append(
                {
                    "rule_id": "eval_json_unreadable",
                    "severity": "error",
                    "file": str(eval_path),
                    "message": f"eval json unreadable: {type(exc).__name__}: {exc}",
                }
            )
            continue
        for eval_case in eval_payload.get("evals", []) if isinstance(eval_payload, dict) else []:
            eval_text = json.dumps(eval_case, ensure_ascii=False, sort_keys=True)
            eval_id = normalize_cell(eval_case.get("id")) if isinstance(eval_case, dict) else ""
            for pattern in retired_eval_patterns:
                if pattern in eval_text:
                    findings.append(
                        {
                            "rule_id": "eval_forbid_retired_gate_runtime_flow",
                            "severity": "error",
                            "file": str(eval_path),
                            "eval_id": eval_id,
                            "message": "eval must not expect retired assessor/guardian runtime flow",
                            "forbidden": pattern,
                        }
                    )
    return findings, sorted(dict.fromkeys(checked_files))


def gate_skill_contract_lint_output(*, runtime: str, state_root: Path, hook_input: dict[str, Any]) -> dict[str, Any]:
    skills_root = gate_skill_contract_lint_root(hook_input)
    findings, checked_files = gate_skill_contract_lint_findings(skills_root)
    status = "block" if findings else "pass"
    summary = {
        "status": status,
        "runtime": runtime,
        "skills_root": str(skills_root),
        "checked_file_count": len(checked_files),
        "checked_files": checked_files,
        "finding_count": len(findings),
        "findings": findings,
    }
    output: dict[str, Any] = {"gateSkillContractLint": summary}
    if findings:
        output["decision"] = "block"
        output["reason"] = "; ".join(
            f"{finding.get('rule_id')}: {finding.get('message')}" for finding in findings[:5]
        )
    return output


def gate_skill_contract_lint_preflight_enabled(hook_input: dict[str, Any]) -> bool:
    if truthy_input(hook_input.get("skip_gate_skill_contract_lint") or hook_input.get("skipGateSkillContractLint")):
        return False
    for key in ("gate_skill_contract_lint", "gateSkillContractLint"):
        if key in hook_input:
            return truthy_input(hook_input.get(key), default=True)
    return env_flag("ITB_PREFLIGHT_GATE_SKILL_CONTRACT_LINT", default=True)


def compact_gate_skill_contract_lint_summary(output: dict[str, Any]) -> dict[str, Any]:
    lint = output.get("gateSkillContractLint") if isinstance(output.get("gateSkillContractLint"), dict) else {}
    findings = lint.get("findings") if isinstance(lint.get("findings"), list) else []
    return {
        "status": normalize_cell(lint.get("status") or "unknown"),
        "finding_count": lint.get("finding_count", len(findings)),
        "checked_file_count": lint.get("checked_file_count", 0),
        "skills_root": normalize_cell(lint.get("skills_root")),
        "findings": findings[:10],
    }


def record_hook_error(
    *,
    runtime: str,
    state_root: Path,
    command: str,
    hook_input: dict[str, Any],
    exc: Exception,
) -> dict[str, Any]:
    session_id = normalize_cell(
        hook_input.get("session_id")
        or hook_input.get("sessionId")
        or hook_input.get("conversation_id")
        or os.environ.get("ITB_PARENT_SESSION_ID")
        or "unknown-session"
    )
    session_dir = state_root / safe_id(session_id)
    event = {
        "ts": current_timestamp(),
        "runtime": runtime,
        "command": command,
        "session_id": session_id,
        "error_type": type(exc).__name__,
        "error": str(exc),
        "traceback": traceback.format_exc(),
    }
    append_jsonl_atomic(session_dir / "hook-errors.jsonl", event)
    return event


def merge_cli_hook_input(
    args: argparse.Namespace,
    hook_input: dict[str, Any],
    *,
    role_field: str = "",
    include_dry_run: bool = False,
    include_max_messages: bool = False,
    include_max_cycles: bool = False,
    include_poll_interval: bool = False,
    include_idle_timeout: bool = False,
    include_message_id: bool = False,
    include_reason: bool = False,
    include_report_json: bool = False,
    include_wait: bool = False,
) -> dict[str, Any]:
    merged = hook_input
    if include_report_json and getattr(args, "report_json", ""):
        try:
            report_data = json.loads(args.report_json)
        except json.JSONDecodeError as exc:
            merged = merged | {"_cli_report_json_error": f"{type(exc).__name__}: {exc}"}
        else:
            if isinstance(report_data, dict):
                merged = merged | report_data
            else:
                merged = merged | {"_cli_report_json_error": "report-json must be a JSON object"}
    if args.session_id:
        merged = merged | {"session_id": args.session_id}
    if role_field and args.role_id:
        merged = merged | {role_field: args.role_id}
    if include_dry_run and args.dry_run:
        merged = merged | {"dry_run": True}
    if include_max_messages and args.max_messages:
        merged = merged | {"max_messages": args.max_messages}
    if include_max_cycles and args.max_cycles:
        merged = merged | {"max_cycles": args.max_cycles}
    if include_poll_interval and args.poll_interval_seconds:
        merged = merged | {"poll_interval_seconds": args.poll_interval_seconds}
    if include_idle_timeout and args.idle_timeout_seconds:
        merged = merged | {"idle_timeout_seconds": args.idle_timeout_seconds}
    if include_message_id and getattr(args, "message_id", ""):
        merged = merged | {"message_id": args.message_id}
    if include_reason and getattr(args, "reason", ""):
        merged = merged | {"reason": args.reason}
    if include_wait and getattr(args, "wait", False):
        merged = merged | {"wait": True}
    return merged


def duplicate_cli_option_error(argv: list[str], options: tuple[str, ...]) -> str:
    duplicates: list[str] = []
    for option in options:
        count = sum(1 for item in argv if item == option or item.startswith(f"{option}="))
        if count > 1:
            duplicates.append(option)
    if not duplicates:
        return ""
    return "duplicate protected CLI option(s): " + ", ".join(duplicates)


def run_main_command(
    *,
    args: argparse.Namespace,
    parser: argparse.ArgumentParser,
    hook_input: dict[str, Any],
    state_root: Path,
) -> dict[str, Any]:
    if args.command == "session-start":
        start_input = merge_cli_hook_input(args, hook_input)
        return session_start_metadata_output(
            runtime=args.runtime,
            state_root=state_root,
            hook_input=start_input,
        )
    if args.command == "final-response-guard":
        return final_response_guard_output(runtime=args.runtime, state_root=state_root, hook_input=merge_cli_hook_input(args, hook_input))
    if args.command == "notification-dispatch":
        notify_input = merge_cli_hook_input(args, hook_input, include_dry_run=True)
        return notification_dispatch_output(runtime=args.runtime, state_root=state_root, hook_input=notify_input)
    if args.command == "hook-install":
        install_input = merge_cli_hook_input(args, hook_input, include_dry_run=True)
        return hook_install_output(runtime=args.runtime, state_root=state_root, hook_input=install_input)
    if args.command == "hook-health-check":
        return hook_health_check_output(runtime=args.runtime, state_root=state_root, hook_input=merge_cli_hook_input(args, hook_input))
    if args.command == "active-task":
        return active_task_output(runtime=args.runtime, state_root=state_root, hook_input=merge_cli_hook_input(args, hook_input))
    if args.command == "gtc-scaffold":
        return gtc_scaffold_output(
            runtime=args.runtime,
            state_root=state_root,
            hook_input=merge_cli_hook_input(args, hook_input, include_dry_run=True),
        )
    if args.command == "task-detail-append":
        return task_detail_append_output(
            runtime=args.runtime,
            state_root=state_root,
            hook_input=merge_cli_hook_input(args, hook_input, include_dry_run=True),
        )
    if args.command == "vault-final-update":
        return vault_final_update_output(
            runtime=args.runtime,
            state_root=state_root,
            hook_input=merge_cli_hook_input(args, hook_input, include_dry_run=True),
        )
    if args.command == "final-transport-render-check":
        return final_transport_render_check_output(
            runtime=args.runtime,
            state_root=state_root,
            hook_input=merge_cli_hook_input(args, hook_input, include_dry_run=True),
        )
    if args.command in {"assessor-precheck", "team-completion-check"}:
        return gate_precheck_output(
            runtime=args.runtime,
            state_root=state_root,
            hook_input=merge_cli_hook_input(args, hook_input),
            gate_role="team-completion-check",
        )
    if args.command in {"guardian-precheck", "finalization-check"}:
        return gate_precheck_output(
            runtime=args.runtime,
            state_root=state_root,
            hook_input=merge_cli_hook_input(args, hook_input),
            gate_role="finalization-check",
        )
    if args.command == "evaluator-precheck":
        return evaluator_precheck_output(runtime=args.runtime, state_root=state_root, hook_input=merge_cli_hook_input(args, hook_input))
    if args.command == "provider-activate":
        return provider_activate(runtime=args.runtime, state_root=state_root, hook_input=merge_cli_hook_input(args, hook_input, role_field="agent_id"))
    if args.command == "agent-call":
        call_input = merge_cli_hook_input(args, hook_input, role_field="to_role", include_dry_run=True, include_wait=True)
        return agent_call(runtime=args.runtime, state_root=state_root, hook_input=call_input)
    if args.command == "agent-switch":
        switch_input = merge_cli_hook_input(args, hook_input, role_field="target_role", include_dry_run=True, include_reason=True)
        return agent_switch(runtime=args.runtime, state_root=state_root, hook_input=switch_input, command_name="agent-switch")
    if args.command == "provider-failover":
        switch_input = merge_cli_hook_input(args, hook_input, role_field="target_role", include_dry_run=True, include_reason=True)
        return agent_switch(runtime=args.runtime, state_root=state_root, hook_input=switch_input, command_name="provider-failover")
    if args.command == "agent-surfaces":
        return agent_surfaces(runtime=args.runtime, state_root=state_root, hook_input=merge_cli_hook_input(args, hook_input))
    if args.command == "transport-status":
        return transport_status(runtime=args.runtime, state_root=state_root, hook_input=merge_cli_hook_input(args, hook_input))
    if args.command == "agent-dispatch":
        return agent_dispatch(runtime=args.runtime, state_root=state_root, hook_input=merge_cli_hook_input(args, hook_input, role_field="agent_id"))
    if args.command == "role-queue":
        queue_input = merge_cli_hook_input(args, hook_input, role_field="role_id", include_dry_run=True)
        return role_queue(runtime=args.runtime, state_root=state_root, hook_input=queue_input)
    if args.command == "role-report":
        duplicate_error = duplicate_cli_option_error(
            sys.argv[1:],
            ("--runtime", "--state-root", "--session-id", "--role-id", "--message-id", "--report-json"),
        )
        if duplicate_error:
            return {"decision": "block", "reason": duplicate_error}
        report_input = merge_cli_hook_input(
            args,
            hook_input,
            role_field="role_id",
            include_message_id=True,
            include_report_json=True,
        )
        return role_report(runtime=args.runtime, state_root=state_root, hook_input=report_input)
    if args.command == "queue-replay-failed":
        replay_input = merge_cli_hook_input(
            args,
            hook_input,
            role_field="role_id",
            include_dry_run=True,
            include_max_messages=True,
            include_message_id=True,
            include_reason=True,
        )
        return role_queue_replay_failed(runtime=args.runtime, state_root=state_root, hook_input=replay_input)
    if args.command == "queue-close-message":
        close_input = merge_cli_hook_input(
            args,
            hook_input,
            role_field="role_id",
            include_dry_run=True,
            include_message_id=True,
            include_reason=True,
        )
        return role_queue_close_message(runtime=args.runtime, state_root=state_root, hook_input=close_input)
    if args.command == "sync-policy-digest-skills":
        sync_input = merge_cli_hook_input(args, hook_input, role_field="role_id", include_dry_run=True)
        return sync_policy_digest_skills(runtime=args.runtime, state_root=state_root, hook_input=sync_input)
    if args.command == "shared-resource-lock":
        lock_input = merge_cli_hook_input(args, hook_input)
        return shared_resource_lock_output(runtime=args.runtime, state_root=state_root, hook_input=lock_input)
    if args.command == "shared-file-update":
        file_input = merge_cli_hook_input(args, hook_input)
        return shared_file_update_output(runtime=args.runtime, state_root=state_root, hook_input=file_input)
    if args.command == "gate-latency-report":
        report_input = merge_cli_hook_input(args, hook_input)
        return gate_latency_report_output(runtime=args.runtime, state_root=state_root, hook_input=report_input)
    if args.command == "context-surface-report":
        report_input = merge_cli_hook_input(args, hook_input, role_field="role_id")
        return context_surface_report_output(runtime=args.runtime, state_root=state_root, hook_input=report_input)
    if args.command == "gate-skill-contract-lint":
        lint_input = merge_cli_hook_input(args, hook_input)
        return gate_skill_contract_lint_output(runtime=args.runtime, state_root=state_root, hook_input=lint_input)
    if args.command == "role-agent-worker":
        worker_input = merge_cli_hook_input(
            args,
            hook_input,
            role_field="role_id",
            include_max_messages=True,
            include_poll_interval=True,
            include_idle_timeout=True,
        )
        return role_agent_worker(runtime=args.runtime, state_root=state_root, hook_input=worker_input)
    if args.command == "archive-shutdown":
        session_id = args.session_id
        if args.current and args.session_id:
            parser.error("archive-shutdown accepts either --current or --session-id, not both")
        if args.current:
            session_id = current_session_id(state_root, hook_input)
        return archive_shutdown(
            runtime=args.runtime,
            state_root=state_root,
            hook_input=hook_input,
            session_id=session_id,
            dry_run=args.dry_run,
        )
    raise ValueError(f"unsupported command: {args.command}")


def main() -> int:
    validate_vault(os.environ)
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "command",
        choices=[
            "session-start",
            "final-response-guard",
            "notification-dispatch",
            "hook-install",
            "hook-health-check",
            "active-task",
            "gtc-scaffold",
            "task-detail-append",
            "vault-final-update",
            "final-transport-render-check",
            "team-completion-check",
            "assessor-precheck",
            "finalization-check",
            "guardian-precheck",
            "evaluator-precheck",
            "provider-activate",
            "agent-call",
            "agent-switch",
            "provider-failover",
            "agent-surfaces",
            "transport-status",
            "agent-dispatch",
            "role-queue",
            "role-report",
            "queue-replay-failed",
            "queue-close-message",
            "sync-policy-digest-skills",
            "shared-resource-lock",
            "shared-file-update",
            "gate-latency-report",
            "context-surface-report",
            "gate-skill-contract-lint",
            "role-agent-worker",
            "archive-shutdown",
        ],
    )
    parser.add_argument("--runtime", required=True, choices=["codex", "claude"])
    parser.add_argument("--state-root", required=True)
    parser.add_argument("--session-id", default="")
    parser.add_argument("--role-id", default="")
    parser.add_argument("--message-id", default="")
    parser.add_argument("--reason", default="")
    parser.add_argument("--report-json", default="")
    parser.add_argument("--input-json-file", default="")
    parser.add_argument("--max-messages", default="")
    parser.add_argument("--poll-interval-seconds", default="")
    parser.add_argument("--idle-timeout-seconds", default="")
    parser.add_argument("--current", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--wait", action="store_true")
    args = parser.parse_args()

    state_root = Path(args.state_root).expanduser()
    hook_input: dict[str, Any] = {}
    try:
        hook_input = load_json_file_input(args.input_json_file) if args.input_json_file else load_hook_input()
        output = run_main_command(args=args, parser=parser, hook_input=hook_input, state_root=state_root)
        print(json.dumps(output, ensure_ascii=False))
        if args.command == "gate-skill-contract-lint" and output.get("decision") == "block":
            return 2
    except Exception as exc:
        error_input = hook_input if isinstance(hook_input, dict) else {}
        if args.session_id:
            error_input = error_input | {"session_id": args.session_id}
        if args.role_id:
            error_input = error_input | {"role_id": args.role_id}
        event = record_hook_error(
            runtime=args.runtime,
            state_root=state_root,
            command=args.command,
            hook_input=error_input,
            exc=exc,
        )
        print(
            json.dumps(
                {
                    "decision": "block",
                    "reason": f"ITB hook command failed: {type(exc).__name__}: {exc}",
                    "hookError": {
                        "session_id": event["session_id"],
                        "command": event["command"],
                        "error_type": event["error_type"],
                        "error": event["error"],
                        "hook_errors_path": str(state_root / safe_id(event["session_id"]) / "hook-errors.jsonl"),
                    },
                },
                ensure_ascii=False,
            )
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
