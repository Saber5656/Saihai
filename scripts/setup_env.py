#!/usr/bin/env python3
"""Create or validate Saihai's optional non-path runtime .env."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from saihai_env import SCHEMA, EnvError, load_environment, parse_env  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--env-file", type=Path, default=REPO_ROOT / ".env")
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    env_file = args.env_file.expanduser().resolve()
    if args.check:
        env = os.environ.copy()
        for key in set(SCHEMA) | {"SAIHAI_ENV_FILE"}:
            env.pop(key, None)
        env["SAIHAI_ENV_FILE"] = str(env_file)
        load_environment(environ=env)
        print("Saihai runtime environment check: ok")
        return 0
    if env_file.exists():
        raise EnvError("env_file_already_exists")
    example = REPO_ROOT / ".env.example"
    parse_env(example.read_text(encoding="utf-8"))
    fd = os.open(env_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(example.read_text(encoding="utf-8"))
    os.chmod(env_file, 0o600)
    print("Saihai runtime environment file created")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except EnvError as exc:
        print(f"Saihai runtime environment error: {exc}", file=sys.stderr)
        raise SystemExit(2)
