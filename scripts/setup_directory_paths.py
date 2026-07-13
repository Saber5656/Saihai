#!/usr/bin/env python3
"""Create or validate Saihai's local directory-path.env catalog."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from directory_paths import (  # noqa: E402
    ALIASES,
    SCHEMA,
    EnvError,
    default_catalog_path,
    load_environment,
    parse_env,
)


def args_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--agents-vault", type=Path)
    parser.add_argument("--user-vault", type=Path)
    parser.add_argument("--sahai-root", type=Path)
    parser.add_argument("--skills-repo-root", type=Path)
    parser.add_argument("--skills-root", type=Path)
    parser.add_argument("--dotfiles-root", type=Path)
    parser.add_argument("--dev-root", type=Path)
    parser.add_argument("--dev-worktrees-root", type=Path)
    parser.add_argument("--task-worktree-root", type=Path)
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--non-interactive", action="store_true")
    return parser


def require_directory(
    label: str,
    path: Path | None,
    *,
    required: bool,
    writable: bool = False,
) -> Path | None:
    if path is None:
        if required:
            raise EnvError(f"missing_required_option:key={label}")
        return None
    resolved = path.expanduser().resolve()
    if not resolved.is_dir():
        raise EnvError(f"configured_path_not_directory:key={label}")
    if not os.access(resolved, os.R_OK):
        raise EnvError(f"configured_path_not_readable:key={label}")
    if writable and not os.access(resolved, os.W_OK):
        raise EnvError(f"configured_path_not_read_write:key={label}")
    return resolved


def quote_value(value: Path) -> str:
    return '"' + str(value).replace("\\", "\\\\").replace('"', '\\"') + '"'


def main() -> int:
    args = args_parser().parse_args()
    default_env_file = default_catalog_path(REPO_ROOT)
    catalog_root = default_env_file.parent
    env_file = (args.env_file or default_env_file).expanduser().resolve()
    if args.check:
        check_env = os.environ.copy()
        for key in set(SCHEMA) | set(ALIASES) | {"SAHAI_DIRECTORY_PATH_ENV"}:
            check_env.pop(key, None)
        check_env["SAHAI_DIRECTORY_PATH_ENV"] = str(env_file)
        load_environment(environ=check_env, require_catalog=True)
        print("Saihai directory path check: ok")
        return 0
    if env_file.exists():
        raise EnvError("directory_path_file_already_exists")
    vault = args.agents_vault
    if vault is None and not args.non_interactive:
        entered = input("AGENTS_VAULT_ROOT: ").strip()
        vault = Path(entered) if entered else None
    vault = require_directory("AGENTS_VAULT_ROOT", vault, required=True, writable=True)
    configured_paths = {
        "SAHAI_ROOT": require_directory("SAHAI_ROOT", args.sahai_root or catalog_root, required=True),
        "USER_VAULT_ROOT": require_directory("USER_VAULT_ROOT", args.user_vault, required=True),
        "SKILLS_REPO_ROOT": require_directory("SKILLS_REPO_ROOT", args.skills_repo_root, required=True),
        "SKILLS_ROOT": require_directory("SKILLS_ROOT", args.skills_root, required=True),
        "DOTFILES_ROOT": require_directory("DOTFILES_ROOT", args.dotfiles_root, required=True),
        "DEV_ROOT": require_directory("DEV_ROOT", args.dev_root, required=True),
        "DEV_WORKTREES_ROOT": require_directory("DEV_WORKTREES_ROOT", args.dev_worktrees_root, required=True),
        "TASK_WORKTREE_ROOT": require_directory("TASK_WORKTREE_ROOT", args.task_worktree_root, required=True),
    }
    example = REPO_ROOT / "directory-path.env.example"
    if not example.is_file():
        raise EnvError("directory_path_example_not_found")
    parse_env(example.read_text(encoding="utf-8"))
    configured = {"AGENTS_VAULT_ROOT": vault, **configured_paths}
    lines = ["# Generated from directory-path.env.example by scripts/setup_directory_paths.py"]
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
    print("Saihai directory path file created")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except EnvError as exc:
        print(f"Saihai directory path error: {exc}", file=sys.stderr)
        raise SystemExit(2)
