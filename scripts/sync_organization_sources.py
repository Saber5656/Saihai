#!/usr/bin/env python3
"""Mirror organization policies and team role definitions into this repo.

The viewer is becoming the canonical place for organization operating
knowledge. This script keeps the migration reproducible while preserving the
existing Agents-Vault and skills-repo files as compatibility sources.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
from saihai_env import load_environment, validate_vault  # noqa: E402

ENV_DIAGNOSTICS = load_environment(checkout_root=REPO_ROOT, require_vault=True)


def env_path(name: str, default: Path) -> Path:
    return Path(os.environ.get(name, str(default))).expanduser()


DEFAULT_AGENT_VAULT = env_path(
    "AGENTS_VAULT_ROOT",
    Path("."),
)
DEFAULT_SKILLS_ROOT = Path(os.environ["SKILLS_ROOT"]).expanduser() if os.environ.get("SKILLS_ROOT") else None

POLICY_REFS = [
    "AI-Organization.md",
    "Gate-IO-Contract.md",
    "Dispatcher-IO-Contract.md",
    "Task-File-Conventions.md",
]

RUNTIME_REFS = {
    "role-agent-registry.yaml": "runtime/infra-team-bootstrap/config/role-agent-registry.yaml",
    "model-registry.md": "runtime/infra-team-bootstrap/references/model-registry.md",
    "team-config.md": "runtime/infra-team-bootstrap/references/team-config.md",
}

TEAM_PREFIXES = ("gate-", "teams-", "tech-", "contents-", "business-", "infra-")
TOOL_ROLE_ALLOWLIST = {"git-publisher"}
PATH_ALIASES = tuple(
    item
    for item in (
        ("AGENTS_VAULT_ROOT", DEFAULT_AGENT_VAULT),
        ("SKILLS_ROOT", DEFAULT_SKILLS_ROOT),
    )
    if item[1] is not None
)
ROLE_SKILL_FILENAME = "skill.md"


def public_path(path: Path) -> str:
    resolved = path.expanduser()
    for alias, root in PATH_ALIASES:
        try:
            relative = resolved.relative_to(root)
        except ValueError:
            continue
        suffix = relative.as_posix()
        return f"${{{alias}}}/{suffix}" if suffix and suffix != "." else f"${{{alias}}}"
    return str(path)


def sha1(path: Path) -> str:
    h = hashlib.sha1()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def parse_frontmatter(text: str) -> dict[str, str]:
    if not text.startswith("---\n"):
        return {}
    end = text.find("\n---", 4)
    if end < 0:
        return {}
    meta: dict[str, str] = {}
    for line in text[4:end].splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        meta[key.strip()] = value.strip().strip('"').strip("'")
    return meta


def is_team_role(skill_dir: Path, meta: dict[str, str]) -> bool:
    name = meta.get("name") or skill_dir.name
    category = meta.get("category", "")
    return (
        category == "Team Role"
        or name.startswith(TEAM_PREFIXES)
        or name in TOOL_ROLE_ALLOWLIST
    )


def copy_with_record(src: Path, dst: Path) -> dict[str, str | int]:
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)
    return {
        "source": public_path(src),
        "path": public_path(dst),
        "bytes": dst.stat().st_size,
        "sha1": sha1(dst),
    }


def relative_repo_path(repo_root: Path, path: Path) -> str:
    try:
        return path.relative_to(repo_root).as_posix()
    except ValueError:
        return public_path(path)


def role_record_path(skill_dir: Path, relative: Path) -> str:
    suffix = relative.as_posix()
    return f"${{SKILLS_ROOT}}/{skill_dir.name}/{suffix}"


def sync_policies(agent_vault: Path, org_root: Path) -> list[dict[str, str | int]]:
    source_root = agent_vault / "03-Contexts" / "Policies"
    records = []
    for name in POLICY_REFS:
        src = source_root / name
        if src.is_file():
            dst = org_root / "policies" / name
            record = copy_with_record(src, dst)
            record["path"] = relative_repo_path(org_root.parent, dst)
            records.append(record)
    return records


def sync_runtime(org_root: Path) -> list[dict[str, str | int]]:
    records = []
    for dst_name, rel in RUNTIME_REFS.items():
        src = org_root / rel
        if src.is_file():
            dst = org_root / "runtime" / dst_name
            record = copy_with_record(src, dst)
            record["source"] = relative_repo_path(org_root.parent, src)
            record["path"] = relative_repo_path(org_root.parent, dst)
            records.append(record)
    return records


def sync_roles(skills_root: Path, org_root: Path) -> list[dict[str, str | int | bool | list[dict[str, str | int]]]]:
    records = []
    for skill_md in sorted(skills_root.glob("*/SKILL.md")):
        text = skill_md.read_text(encoding="utf-8")
        meta = parse_frontmatter(text)
        if not is_team_role(skill_md.parent, meta):
            continue
        name = meta.get("name") or skill_md.parent.name
        safe_name = re.sub(r"[^a-zA-Z0-9_.-]+", "-", name).strip("-")
        role_dir = org_root / "roles" / safe_name
        legacy_flat_dst = org_root / "roles" / f"{safe_name}.md"
        if legacy_flat_dst.exists():
            legacy_flat_dst.unlink()
        if role_dir.exists():
            shutil.rmtree(role_dir)
        artifacts: list[dict[str, str | int]] = []
        for src in sorted(skill_md.parent.rglob("*")):
            if not src.is_file():
                continue
            rel = src.relative_to(skill_md.parent)
            dst_rel = Path(ROLE_SKILL_FILENAME) if rel == Path("SKILL.md") else rel
            dst = role_dir / dst_rel
            copy_with_record(src, dst)
            artifacts.append(
                {
                    "source": role_record_path(skill_md.parent, rel),
                    "path": relative_repo_path(org_root.parent, dst),
                    "bytes": dst.stat().st_size,
                    "sha1": sha1(dst),
                }
            )
        skill_dst = role_dir / ROLE_SKILL_FILENAME
        records.append(
            {
                "source": role_record_path(skill_md.parent, Path("SKILL.md")),
                "path": relative_repo_path(org_root.parent, skill_dst),
                "bytes": skill_dst.stat().st_size,
                "sha1": sha1(skill_dst),
                "role_id": name,
                "team": meta.get("team", name.split("-", 1)[0]),
                "category": meta.get("category", ""),
                "status": meta.get("status", ""),
                "purpose": meta.get("purpose", ""),
                "user_invocable": meta.get("user-invocable", ""),
                "compatibility_source": "skills-repo",
                "migration_stage": "directory_mirror",
                "artifact_count": len(artifacts),
                "artifacts": artifacts,
            }
        )
    return records


def main() -> None:
    validate_vault(os.environ)
    parser = argparse.ArgumentParser(description="Sync organization sources")
    parser.add_argument("--agent-vault", type=Path, default=DEFAULT_AGENT_VAULT)
    parser.add_argument("--skills-root", type=Path, default=DEFAULT_SKILLS_ROOT)
    parser.add_argument("--repo-root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument(
        "--scope",
        choices=("all", "policies", "runtime", "roles"),
        default="all",
        help="Limit the sync to one source group.",
    )
    args = parser.parse_args()

    org_root = args.repo_root / "organization"
    if args.scope in {"all", "roles"}:
        if args.skills_root is None:
            parser.error("roles sync requires external SKILLS_ROOT or explicit --skills-root")
        source = args.skills_root.expanduser().resolve()
        destination = (org_root / "roles").resolve()
        if not source.is_dir():
            parser.error("roles sync source must be an existing directory")
        if source == destination or source in destination.parents or destination in source.parents:
            parser.error("roles sync source and destination must not overlap")
    org_root.mkdir(parents=True, exist_ok=True)

    policies = sync_policies(args.agent_vault, org_root) if args.scope in {"all", "policies"} else []
    runtime = sync_runtime(org_root) if args.scope in {"all", "runtime"} else []
    roles = sync_roles(args.skills_root, org_root) if args.scope in {"all", "roles"} else []

    if args.scope in {"all", "policies"}:
        (org_root / "policy-index.json").write_text(
            json.dumps({"schema_version": 1, "policies": policies}, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    if args.scope in {"all", "roles"}:
        (org_root / "role-index.json").write_text(
            json.dumps({"schema_version": 1, "roles": roles}, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    summary_path = org_root / "sync-summary.json"
    previous_summary: dict[str, object] = {}
    if summary_path.exists():
        try:
            previous_summary = json.loads(summary_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            previous_summary = {}
    summary = {
        "schema_version": 1,
        "policy_count": len(policies)
        if args.scope in {"all", "policies"}
        else previous_summary.get("policy_count", 0),
        "runtime_ref_count": len(runtime)
        if args.scope in {"all", "runtime"}
        else previous_summary.get("runtime_ref_count", 0),
        "role_count": len(roles)
        if args.scope in {"all", "roles"}
        else previous_summary.get("role_count", 0),
        "last_scope": args.scope,
        "agent_vault": public_path(args.agent_vault),
        "skills_root": public_path(args.skills_root),
    }
    summary_path.write_text(
        json.dumps(
            summary,
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    print(json.dumps({"policies": len(policies), "runtime": len(runtime), "roles": len(roles)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
