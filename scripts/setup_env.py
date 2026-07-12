#!/usr/bin/env python3
"""Create or validate a local Saihai .env without printing configured values."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from saihai_env import EnvError, load_environment, parse_env  # noqa: E402


def args_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--agents-vault", type=Path)
    parser.add_argument("--user-vault", type=Path)
    parser.add_argument("--skills-root", type=Path)
    parser.add_argument("--dotfiles-root", type=Path)
    parser.add_argument("--env-file", type=Path, default=REPO_ROOT / ".env")
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--non-interactive", action="store_true")
    return parser


def require_directory(label: str, path: Path | None, *, required: bool) -> Path | None:
    if path is None:
        if required:
            raise EnvError(f"missing_required_option:key={label}")
        return None
    resolved = path.expanduser().resolve()
    if not resolved.is_dir():
        raise EnvError(f"configured_path_not_directory:key={label}")
    if required and not os.access(resolved, os.R_OK | os.W_OK):
        raise EnvError(f"configured_path_not_read_write:key={label}")
    return resolved


def quote_value(value: Path) -> str:
    return '"' + str(value).replace("\\", "\\\\").replace('"', '\\"') + '"'


def main() -> int:
    args = args_parser().parse_args()
    env_file = args.env_file.expanduser().resolve()
    if args.check:
        check_env = os.environ.copy()
        check_env["SAIHAI_ENV_FILE"] = str(env_file)
        load_environment(environ=check_env, require_vault=True)
        print("Saihai environment check: ok")
        return 0
    if env_file.exists():
        raise EnvError("env_file_already_exists")
    vault = args.agents_vault
    if vault is None and not args.non_interactive:
        entered = input("AGENTS_VAULT_ROOT: ").strip()
        vault = Path(entered) if entered else None
    vault = require_directory("AGENTS_VAULT_ROOT", vault, required=True)
    optional = {
        "USER_VAULT_ROOT": require_directory("USER_VAULT_ROOT", args.user_vault, required=False),
        "SKILLS_ROOT": require_directory("SKILLS_ROOT", args.skills_root, required=False),
        "DOTFILES_ROOT": require_directory("DOTFILES_ROOT", args.dotfiles_root, required=False),
    }
    example = REPO_ROOT / ".env.example"
    if not example.is_file():
        raise EnvError("env_example_not_found")
    parse_env(example.read_text(encoding="utf-8"))
    configured = {"AGENTS_VAULT_ROOT": vault, **{key: value for key, value in optional.items() if value is not None}}
    lines = ["# Generated from .env.example by scripts/setup_env.py"]
    for line in example.read_text(encoding="utf-8").splitlines():
        candidate = line.lstrip("# ")
        key = candidate.split("=", 1)[0] if "=" in candidate else ""
        if key in configured:
            lines.append(f"{key}={quote_value(configured[key])}")
        else:
            lines.append(line)
    fd = os.open(env_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")
    os.chmod(env_file, 0o600)
    print("Saihai environment file created")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except EnvError as exc:
        print(f"Saihai environment error: {exc}", file=sys.stderr)
        raise SystemExit(2)
