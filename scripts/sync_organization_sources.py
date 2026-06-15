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
import re
import shutil
from pathlib import Path

DEFAULT_AGENT_VAULT = Path(
    "/Users/takagiyasushi/Library/Mobile Documents/iCloud~md~obsidian/Documents/Agents-Vault"
)
DEFAULT_SKILLS_ROOT = Path("/Users/takagiyasushi/skills-repo/skills")

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
        "source": str(src),
        "path": str(dst),
        "bytes": dst.stat().st_size,
        "sha1": sha1(dst),
    }


def sync_policies(agent_vault: Path, org_root: Path) -> list[dict[str, str | int]]:
    source_root = agent_vault / "03-Contexts" / "Policies"
    records = []
    for name in POLICY_REFS:
        src = source_root / name
        if src.is_file():
            records.append(copy_with_record(src, org_root / "policies" / name))
    return records


def sync_runtime(org_root: Path) -> list[dict[str, str | int]]:
    records = []
    for dst_name, rel in RUNTIME_REFS.items():
        src = org_root / rel
        if src.is_file():
            records.append(copy_with_record(src, org_root / "runtime" / dst_name))
    return records


def sync_roles(skills_root: Path, org_root: Path) -> list[dict[str, str | int | bool]]:
    records = []
    for skill_md in sorted(skills_root.glob("*/SKILL.md")):
        text = skill_md.read_text(encoding="utf-8")
        meta = parse_frontmatter(text)
        if not is_team_role(skill_md.parent, meta):
            continue
        name = meta.get("name") or skill_md.parent.name
        safe_name = re.sub(r"[^a-zA-Z0-9_.-]+", "-", name).strip("-")
        dst = org_root / "roles" / f"{safe_name}.md"
        base = copy_with_record(skill_md, dst)
        records.append(
            {
                **base,
                "role_id": name,
                "team": meta.get("team", name.split("-", 1)[0]),
                "category": meta.get("category", ""),
                "status": meta.get("status", ""),
                "purpose": meta.get("purpose", ""),
                "user_invocable": meta.get("user-invocable", ""),
                "compatibility_source": "skills-repo",
                "migration_stage": "mirrored_not_deleted",
            }
        )
    return records


def main() -> None:
    parser = argparse.ArgumentParser(description="Sync organization sources")
    parser.add_argument("--agent-vault", type=Path, default=DEFAULT_AGENT_VAULT)
    parser.add_argument("--skills-root", type=Path, default=DEFAULT_SKILLS_ROOT)
    parser.add_argument("--repo-root", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args()

    org_root = args.repo_root / "organization"
    org_root.mkdir(parents=True, exist_ok=True)

    policies = sync_policies(args.agent_vault, org_root)
    runtime = sync_runtime(org_root)
    roles = sync_roles(args.skills_root, org_root)

    (org_root / "policy-index.json").write_text(
        json.dumps({"schema_version": 1, "policies": policies}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    (org_root / "role-index.json").write_text(
        json.dumps({"schema_version": 1, "roles": roles}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    (org_root / "sync-summary.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "policy_count": len(policies),
                "runtime_ref_count": len(runtime),
                "role_count": len(roles),
                "agent_vault": str(args.agent_vault),
                "skills_root": str(args.skills_root),
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    print(json.dumps({"policies": len(policies), "runtime": len(runtime), "roles": len(roles)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
