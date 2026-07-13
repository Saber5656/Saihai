"""Load non-path Saihai runtime options from the repository-local .env.

Directory locations are intentionally rejected here and belong to
directory-path.env, which is loaded by directory_paths.py.
"""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, MutableMapping


class EnvError(RuntimeError):
    """Raised when runtime configuration is unsafe or invalid."""


@dataclass(frozen=True)
class EnvField:
    kind: str = "string"
    choices: tuple[str, ...] = ()
    minimum: float | None = None
    maximum: float | None = None


BOOL = EnvField("bool")
POSITIVE_INT = EnvField("int", minimum=1)
NONNEGATIVE_INT = EnvField("int", minimum=0)
NONNEGATIVE_NUMBER = EnvField("number", minimum=0)

SCHEMA: dict[str, EnvField] = {
    "AGENT_ORG_STATE": EnvField("enum", choices=("enabled", "maintenance", "disabled")),
    "ITB_INBOX_TERMINAL_KEEP": EnvField("int", minimum=0, maximum=10000),
    "ITB_FINAL_GATE_HARD_BLOCK": BOOL,
    "ITB_GATE_ENTRY_AUTO_GTC": BOOL,
    "ITB_GATE_ENTRY_CODEX_EXEC": BOOL,
    "ITB_GATE_ENTRY_DISPATCH": BOOL,
    "ITB_GATE_ENTRY_QUEUE": BOOL,
    "ITB_MICRO_FAST_PATH": BOOL,
    "ITB_OS_NOTIFICATIONS": BOOL,
    "ITB_PREFLIGHT_GATE_SKILL_CONTRACT_LINT": BOOL,
    "ITB_ROLE_QUEUE_COMPLETION_WAIT_EVENT_DRIVEN": BOOL,
    "ITB_ROLE_QUEUE_COMPLETION_WAIT_IN_DRY_RUN": BOOL,
    "ITB_ROLE_QUEUE_DRY_RUN": BOOL,
    "ITB_CLAUDE_CLI_DISPATCH_TIMEOUT_SECONDS": POSITIVE_INT,
    "ITB_CLAUDE_TRANSCRIPT_DISCOVERY_MAX_FILES": POSITIVE_INT,
    "ITB_CLAUDE_TRANSCRIPT_STALE_TOLERANCE_SECONDS": NONNEGATIVE_INT,
    "ITB_CODEX_EXEC_DISPATCH_TIMEOUT_SECONDS": POSITIVE_INT,
    "ITB_GATE_ENTRY_TASK_LIKE_MIN_CHARS": POSITIVE_INT,
    "ITB_GATE_LATENCY_REPORT_ENRICHMENT_MAX_FILES": POSITIVE_INT,
    "ITB_PRE_GPF_MICRO_MAX_CHARS": POSITIVE_INT,
    "ITB_PROVIDER_ACTIVATION_TIMEOUT_SECONDS": POSITIVE_INT,
    "ITB_PROVIDER_USAGE_TRANSCRIPT_MAX_BYTES": POSITIVE_INT,
    "ITB_QUEUE_WATCH_NUDGE_COOLDOWN_SECONDS": EnvField("number", minimum=0, maximum=3600),
    "ITB_ROLE_AGENT_IDLE_TIMEOUT_SECONDS": EnvField("number", minimum=0, maximum=86400),
    "ITB_ROLE_AGENT_MAX_MESSAGES": NONNEGATIVE_INT,
    "ITB_ROLE_AGENT_POLL_INTERVAL_SECONDS": EnvField("number", minimum=0.1, maximum=60),
    "ITB_ROLE_QUEUE_COMPLETION_WAIT_POLL_SECONDS": EnvField("number", minimum=0, maximum=30),
    "ITB_ROLE_QUEUE_COMPLETION_WAIT_SECONDS": EnvField("number", minimum=0, maximum=300),
    "ITB_TASK_DETAIL_LINE_CAP": EnvField("int", minimum=80, maximum=2000),
    "ITB_PROVIDER_ACTIVATION_MAX_BUDGET_USD": NONNEGATIVE_NUMBER,
    "ITB_PROVIDER_PERMISSION_MODE": EnvField("enum", choices=("acceptEdits", "auto", "default", "plan")),
    "ITB_CODEX_APPROVAL_POLICY": EnvField("enum", choices=("untrusted", "on-failure", "on-request", "never")),
    "ITB_CODEX_REASONING_EFFORT": EnvField("enum", choices=("minimal", "low", "medium", "high", "xhigh")),
    "ITB_CODEX_SERVICE_TIER": EnvField("enum", choices=("auto", "default", "flex", "fast")),
    "ITB_ROLE_QUEUE_COMPLETION_WAIT_PROFILE": EnvField("enum", choices=("off", "none", "hook_light", "daemon_assisted", "live_validation")),
    "ITB_CLAUDE_EFFORT": EnvField("enum", choices=("low", "medium", "high", "max")),
    "ITB_CLAUDE_DEFAULT_EFFORT": EnvField("enum", choices=("low", "medium", "high", "max")),
    "ITB_CLAUDE_HAIKU_SONNET_EFFORT": EnvField("enum", choices=("low", "medium", "high", "max")),
    "ITB_CLAUDE_SONNET_HAIKU_EFFORT": EnvField("enum", choices=("low", "medium", "high", "max")),
    "ITB_CLAUDE_OPUS_EFFORT": EnvField("enum", choices=("low", "medium", "high", "max")),
    "ITB_CODEX_MODEL": EnvField(),
    "ITB_OS_NOTIFICATION_CLASSES": EnvField(),
}

KEY_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")


def _primary_checkout(checkout: Path) -> Path | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(checkout), "rev-parse", "--path-format=absolute", "--git-common-dir"],
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode:
        return None
    common = Path(result.stdout.strip())
    return common.parent if common.name == ".git" else None


def resolve_env_file(checkout_root: Path | None = None, environ: Mapping[str, str] | None = None) -> Path | None:
    env = os.environ if environ is None else environ
    checkout = (checkout_root or Path(__file__).resolve().parent).resolve()
    if "SAIHAI_ENV_FILE" in env:
        raw = env["SAIHAI_ENV_FILE"]
        return Path(raw).expanduser().resolve() if raw else None
    candidate = checkout / ".env"
    if candidate.is_file():
        return candidate.resolve()
    primary = _primary_checkout(checkout)
    if primary and primary != checkout and (primary / ".env").is_file():
        return (primary / ".env").resolve()
    return None


def _unquote(raw: str, line_no: int) -> str:
    value = raw.strip()
    if not value:
        return ""
    if value[0] in "\"'":
        quote = value[0]
        end = value.find(quote, 1)
        if end < 0 or (value[end + 1 :].strip() and not value[end + 1 :].lstrip().startswith("#")):
            raise EnvError(f"invalid_quoted_value:line={line_no}")
        return value[1:end]
    comment = re.search(r"\s+#", value)
    value = value[: comment.start()].rstrip() if comment else value
    if any(char.isspace() for char in value):
        raise EnvError(f"unquoted_whitespace:line={line_no}")
    return value


def parse_env(text: str) -> dict[str, str]:
    parsed: dict[str, str] = {}
    for line_no, raw_line in enumerate(text.splitlines(), 1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export ") or "=" not in line:
            raise EnvError(f"malformed_line:line={line_no}")
        key, raw_value = line.split("=", 1)
        key = key.strip()
        if not KEY_RE.fullmatch(key):
            raise EnvError(f"invalid_key:line={line_no}")
        if key == "SAIHAI_ENV_FILE":
            raise EnvError("circular_env_file_key")
        if key not in SCHEMA:
            raise EnvError(f"unknown_or_path_key:key={key}:line={line_no}")
        if key in parsed:
            raise EnvError(f"duplicate_key:key={key}:line={line_no}")
        value = _unquote(raw_value, line_no)
        if "$(" in value or "`" in value or "$" in value:
            raise EnvError(f"shell_expansion_forbidden:key={key}:line={line_no}")
        parsed[key] = value
    return parsed


def _validate(key: str, value: str) -> str:
    if value == "":
        return value
    field = SCHEMA[key]
    if field.kind == "bool" and value.lower() not in {"1", "0", "true", "false", "yes", "no", "on", "off"}:
        raise EnvError(f"invalid_bool:key={key}")
    if field.kind in {"int", "number"}:
        try:
            number = int(value) if field.kind == "int" else float(value)
        except ValueError as exc:
            raise EnvError(f"invalid_{field.kind}:key={key}") from exc
        if field.minimum is not None and number < field.minimum or field.maximum is not None and number > field.maximum:
            raise EnvError(f"value_out_of_range:key={key}")
    if field.kind == "enum" and value not in field.choices:
        raise EnvError(f"invalid_enum:key={key}")
    return value


def load_environment(
    *,
    checkout_root: Path | None = None,
    environ: MutableMapping[str, str] | None = None,
) -> dict[str, object]:
    target = os.environ if environ is None else environ
    env_file = resolve_env_file(checkout_root, target)
    loaded: list[str] = []
    skipped: list[str] = []
    if env_file is not None:
        if not env_file.is_file():
            raise EnvError("env_file_not_found")
        for key, raw_value in parse_env(env_file.read_text(encoding="utf-8")).items():
            if key in target:
                skipped.append(key)
                continue
            target[key] = _validate(key, raw_value)
            loaded.append(key)
    for key in SCHEMA:
        if key in target:
            target[key] = _validate(key, target[key])
    return {
        "status": "loaded" if env_file else "not_configured",
        "source": "explicit" if "SAIHAI_ENV_FILE" in target else ("discovered" if env_file else "none"),
        "loaded_keys": tuple(sorted(loaded)),
        "skipped_process_keys": tuple(sorted(skipped)),
    }
