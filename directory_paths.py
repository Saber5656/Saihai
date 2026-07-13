"""Safe, stdlib-only loading for Saihai's directory path catalog.

Diagnostics deliberately contain key names and status codes only.  They never
contain values read from an environment file.
"""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, MutableMapping


class EnvError(RuntimeError):
    """Raised when Saihai directory path configuration is unsafe or invalid."""


@dataclass(frozen=True)
class EnvField:
    kind: str = "string"
    required: bool = False
    choices: tuple[str, ...] = ()
    minimum: float | None = None
    maximum: float | None = None


PATH = EnvField("path")

SCHEMA: dict[str, EnvField] = {
    "SAIHAI_ROOT": EnvField("path", required=True),
    "AGENTS_VAULT_ROOT": EnvField("path", required=True),
    "USER_VAULT_ROOT": EnvField("path", required=True),
    "SKILLS_REPO_ROOT": EnvField("path", required=True),
    "SKILLS_ROOT": EnvField("path", required=True),
    "DOTFILES_ROOT": EnvField("path", required=True),
    "DEV_ROOT": EnvField("path", required=True),
    "DEV_WORKTREES_ROOT": EnvField("path", required=True),
    "TASK_WORKTREE_ROOT": EnvField("path", required=True),
    "SAIHAI_ORCH_STATE_ROOT": PATH,
    "SAIHAI_ITB_STATE_ROOTS": EnvField("path_list"),
    "SENSITIVE_ACCESS_GUARD_STATE_ROOT": PATH,
    # Compatibility aliases: accepted for reads, never advertised in directory-path.env.example.
    "SAHAI_ROOT": PATH,
    "AGENT_TEAMS_VIEWER_ROOT": PATH,
    "YASU_VAULT_ROOT": PATH,
    "SKILLS_REPO_SKILLS_ROOT": PATH,
    "DEV_REPO_ROOT": PATH,
    "SAHAI_ORCH_STATE_ROOT": PATH,
    "SAHAI_ITB_STATE_ROOTS": EnvField("path_list"),
}

INTERNAL_KEYS: frozenset[str] = frozenset()
ALIASES = {
    "SAHAI_ROOT": "SAIHAI_ROOT",
    "AGENT_TEAMS_VIEWER_ROOT": "SAIHAI_ROOT",
    "YASU_VAULT_ROOT": "USER_VAULT_ROOT",
    "SKILLS_REPO_SKILLS_ROOT": "SKILLS_ROOT",
    "DEV_REPO_ROOT": "DEV_ROOT",
    "SAHAI_ORCH_STATE_ROOT": "SAIHAI_ORCH_STATE_ROOT",
    "SAHAI_ITB_STATE_ROOTS": "SAIHAI_ITB_STATE_ROOTS",
}
CATALOG_ENV_KEY = "SAIHAI_DIRECTORY_PATH_ENV"
LEGACY_CATALOG_ENV_KEY = "SAHAI_DIRECTORY_PATH_ENV"
KEY_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")


def _primary_checkout(checkout: Path) -> Path | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(checkout), "rev-parse", "--path-format=absolute", "--git-common-dir"],
            capture_output=True, text=True, check=False, timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode:
        return None
    common = Path(result.stdout.strip())
    return common.parent if common.name == ".git" else None


def default_catalog_path(checkout_root: Path | None = None) -> Path:
    """Return the primary checkout catalog path, whether or not it exists."""
    checkout = (checkout_root or Path(__file__).resolve().parent).resolve()
    primary = _primary_checkout(checkout)
    return (primary or checkout) / "directory-path.env"


def resolve_env_file(checkout_root: Path | None = None, environ: Mapping[str, str] | None = None) -> Path | None:
    env = os.environ if environ is None else environ
    checkout = (checkout_root or Path(__file__).resolve().parent).resolve()
    selector_key = CATALOG_ENV_KEY if CATALOG_ENV_KEY in env else LEGACY_CATALOG_ENV_KEY
    if selector_key in env:
        raw = env[selector_key]
        return Path(raw).expanduser().resolve() if raw else None
    root_value = env.get("SAIHAI_ROOT") or env.get("SAHAI_ROOT")
    if root_value:
        candidate = Path(root_value).expanduser() / "directory-path.env"
        if candidate.is_file():
            return candidate.resolve()
    candidate = checkout / "directory-path.env"
    if candidate.is_file():
        return candidate.resolve()
    primary_candidate = default_catalog_path(checkout)
    if primary_candidate != candidate and primary_candidate.is_file():
        return primary_candidate.resolve()
    return None


def _unquote(raw: str, line_no: int) -> str:
    value = raw.strip()
    if not value:
        return ""
    if value[0] in "\"'":
        quote = value[0]
        escaped = False
        end = None
        for index in range(1, len(value)):
            char = value[index]
            if quote == '"' and char == "\\" and not escaped:
                escaped = True
                continue
            if char == quote and not escaped:
                end = index
                break
            escaped = False
        if end is None or value[end + 1 :].strip() not in ("",) and not value[end + 1 :].lstrip().startswith("#"):
            raise EnvError(f"invalid_quoted_value:line={line_no}")
        body = value[1:end]
        if quote == "'":
            return body
        escapes = {"n": "\n", "r": "\r", "t": "\t", '"': '"', "\\": "\\"}
        return re.sub(r"\\(.)", lambda match: escapes.get(match.group(1), match.group(1)), body)
    comment = re.search(r"\s+#", value)
    if comment:
        value = value[: comment.start()].rstrip()
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
        if key in {CATALOG_ENV_KEY, LEGACY_CATALOG_ENV_KEY}:
            raise EnvError("circular_env_file_key")
        if key not in SCHEMA:
            raise EnvError(f"unknown_key:key={key}:line={line_no}")
        if key in INTERNAL_KEYS:
            raise EnvError(f"internal_key_forbidden:key={key}:line={line_no}")
        if key in parsed:
            raise EnvError(f"duplicate_key:key={key}:line={line_no}")
        value = _unquote(raw_value, line_no)
        if "$(" in value or "`" in value or re.search(r"\$(?!\{HOME\})", value):
            raise EnvError(f"shell_expansion_forbidden:key={key}:line={line_no}")
        if re.search(r"\$\{(?!HOME\})[^}]+\}", value):
            raise EnvError(f"shell_expansion_forbidden:key={key}:line={line_no}")
        parsed[key] = value
    return parsed


def _validate(key: str, value: str, base: Path, home: Path) -> str:
    field = SCHEMA[key]
    if value == "":
        return ""
    if field.kind in {"path", "path_list"}:
        items = value.split(os.pathsep) if field.kind == "path_list" else [value]
        resolved: list[str] = []
        for item in items:
            item = item.replace("${HOME}", str(home))
            if item == "~" or item.startswith("~/"):
                item = str(home) + item[1:]
            path = Path(item)
            # Normalize without resolving symlinks. Security-sensitive consumers
            # must still be able to detect and reject symlink roots themselves.
            candidate = base / path if not path.is_absolute() else path
            resolved.append(os.path.abspath(candidate))
        return os.pathsep.join(resolved)
    if field.kind == "bool" and value.lower() not in {"1", "0", "true", "false", "yes", "no", "on", "off"}:
        raise EnvError(f"invalid_bool:key={key}")
    if field.kind == "int":
        try:
            number = int(value)
        except ValueError as exc:
            raise EnvError(f"invalid_integer:key={key}") from exc
        if field.minimum is not None and number < field.minimum:
            raise EnvError(f"integer_out_of_range:key={key}")
        if field.maximum is not None and number > field.maximum:
            raise EnvError(f"integer_out_of_range:key={key}")
    if field.kind == "number":
        try:
            number = float(value)
        except ValueError as exc:
            raise EnvError(f"invalid_number:key={key}") from exc
        if field.minimum is not None and number < field.minimum:
            raise EnvError(f"number_out_of_range:key={key}")
        if field.maximum is not None and number > field.maximum:
            raise EnvError(f"number_out_of_range:key={key}")
    if field.kind == "enum" and value not in field.choices:
        raise EnvError(f"invalid_enum:key={key}")
    return value


def expand_path_aliases(text: str, environ: Mapping[str, str] | None = None) -> str:
    """Expand known directory aliases without invoking shell expansion."""
    env = os.environ if environ is None else environ
    expanded = text
    keys = sorted(set(SCHEMA) | set(ALIASES), key=len, reverse=True)
    for key in keys:
        value = env.get(key)
        if value is None:
            canonical = ALIASES.get(key)
            value = env.get(canonical) if canonical else None
        if value is None:
            continue
        expanded = expanded.replace(f"${{{key}}}", value)
        expanded = re.sub(rf"\${re.escape(key)}(?![A-Z0-9_])", lambda _match: value, expanded)
    return expanded


def validate_vault(environ: Mapping[str, str]) -> None:
    value = environ.get("AGENTS_VAULT_ROOT")
    if not value:
        raise EnvError("agents_vault_missing_or_empty")
    path = Path(value)
    if not path.exists():
        raise EnvError("agents_vault_not_found")
    if not path.is_dir():
        raise EnvError("agents_vault_not_directory")
    if not os.access(path, os.R_OK | os.W_OK):
        raise EnvError("agents_vault_not_read_write")


def validate_required_paths(environ: Mapping[str, str]) -> None:
    """Require every canonical directory declared as mandatory by the schema."""
    for key, field in SCHEMA.items():
        if not field.required or key in ALIASES:
            continue
        value = environ.get(key)
        if not value:
            raise EnvError(f"required_path_missing_or_empty:key={key}")
        path = Path(value)
        if not path.exists():
            raise EnvError(f"required_path_not_found:key={key}")
        if not path.is_dir():
            raise EnvError(f"required_path_not_directory:key={key}")
        if not os.access(path, os.R_OK):
            raise EnvError(f"required_path_not_readable:key={key}")
    validate_vault(environ)


def load_environment(
    *,
    checkout_root: Path | None = None,
    environ: MutableMapping[str, str] | None = None,
    require_vault: bool = False,
    require_catalog: bool = False,
    load_runtime: bool = True,
) -> dict[str, object]:
    target = os.environ if environ is None else environ
    warnings: list[str] = []

    def apply_aliases() -> None:
        for alias, canonical in ALIASES.items():
            if alias in target:
                warning = f"deprecated_alias:{alias}:use={canonical}"
                if warning not in warnings:
                    warnings.append(warning)
                if canonical not in target:
                    target[canonical] = target[alias]

    # Normalize deprecated process aliases before reading .env so process
    # precedence is preserved during the migration window.
    apply_aliases()
    env_file = resolve_env_file(checkout_root, target)
    loaded: list[str] = []
    skipped: list[str] = []
    if env_file is not None:
        if not env_file.is_file():
            raise EnvError("env_file_not_found")
        parsed = parse_env(env_file.read_text(encoding="utf-8"))
        for key, raw_value in parsed.items():
            if key in target:  # Explicit empty process values also win.
                skipped.append(key)
                continue
            target[key] = _validate(key, raw_value, env_file.parent, Path.home())
            loaded.append(key)
    apply_aliases()
    # Validate supported process values too, without exposing them.
    base = env_file.parent if env_file else (checkout_root or Path(__file__).resolve().parent)
    for key in SCHEMA:
        if key in target and target[key] != "":
            target[key] = _validate(key, target[key], Path(base), Path.home())
    if require_catalog:
        validate_required_paths(target)
    elif require_vault:
        validate_vault(target)
    runtime_diagnostics: dict[str, object] | None = None
    if load_runtime:
        from saihai_env import EnvError as RuntimeEnvError
        from saihai_env import load_environment as load_runtime_environment

        try:
            runtime_diagnostics = load_runtime_environment(
                checkout_root=checkout_root,
                environ=target,
            )
        except RuntimeEnvError as exc:
            raise EnvError(f"runtime_environment_invalid:{exc}") from exc
    return {
        "status": "loaded" if env_file else "not_configured",
        "source": (
            "explicit"
            if CATALOG_ENV_KEY in target or LEGACY_CATALOG_ENV_KEY in target
            else ("discovered" if env_file else "none")
        ),
        "loaded_keys": tuple(sorted(loaded)),
        "skipped_process_keys": tuple(sorted(skipped)),
        "warnings": tuple(sorted(warnings)),
        "runtime": runtime_diagnostics,
    }
