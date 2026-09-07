#!/usr/bin/env python3
"""Bounded, informational effective-artifact observations. Never grants authority."""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import selectors
import stat
import subprocess
import sys
import time
from typing import Any

REPO = Path(__file__).resolve().parents[4]
SCHEMA = Path(__file__).resolve().parents[1] / "schemas/effective-bundle-observation.schema.json"
MAX_BYTES = 1024 * 1024
MAX_MEMBERS = 128
CATEGORIES = ("common", "role", "policy", "workflow", "skill", "runtime_config")
POLICIES = ("AI-Organization", "Gate-IO-Contract", "Dispatcher-IO-Contract", "Task-File-Conventions")
ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,95}\Z")


class ObservationError(ValueError):
    """Only constant, public-safe reason codes cross the CLI boundary."""


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()).hexdigest()


def decode_json(raw: bytes) -> Any:
    if len(raw) > MAX_BYTES:
        raise ObservationError("input_too_large")
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ObservationError("duplicate_json_key")
            result[key] = value
        return result
    try:
        return json.loads(raw, object_pairs_hook=pairs, parse_constant=lambda _: (_ for _ in ()).throw(ObservationError("invalid_number")))
    except (ValueError, UnicodeError, RecursionError) as exc:
        if isinstance(exc, ObservationError):
            raise
        raise ObservationError("invalid_json") from None


def _keys(value, keys):
    if not isinstance(value, dict) or set(value) != set(keys):
        raise ObservationError("invalid_fields")


def _identifier(value):
    if not isinstance(value, str) or not ID.fullmatch(value):
        raise ObservationError("invalid_identifier")


def _location(value, roots):
    _keys(value, ("root", "path"))
    if value["root"] not in roots or not isinstance(value["path"], str):
        raise ObservationError("unknown_root")
    p = Path(value["path"])
    if p.is_absolute() or not p.parts or any(x in (".", "..") for x in value["path"].split("/")) or "\x00" in value["path"]:
        raise ObservationError("invalid_relative_path")
    return roots[value["root"]] / p


def _validate_spec(spec, roots):
    _keys(spec, ("members", "policy_snapshots"))
    if not isinstance(spec["members"], list) or not 1 <= len(spec["members"]) <= MAX_MEMBERS:
        raise ObservationError("invalid_member_count")
    members = {}
    for member in spec["members"]:
        _keys(member, ("id", "category", "source", "installed"))
        _identifier(member["id"])
        if member["id"] in members:
            raise ObservationError("duplicate_member")
        if member["category"] not in CATEGORIES:
            raise ObservationError("invalid_category")
        _location(member["source"], roots)
        if member["installed"] is not None:
            _location(member["installed"], roots)
        members[member["id"]] = member
    if not isinstance(spec["policy_snapshots"], list) or len(spec["policy_snapshots"]) > MAX_MEMBERS:
        raise ObservationError("invalid_snapshot_count")
    seen = set()
    for pair in spec["policy_snapshots"]:
        _keys(pair, ("role", "policy"))
        for key in ("role", "policy"):
            _identifier(pair[key])
            if pair[key] not in members or members[pair[key]]["category"] != key:
                raise ObservationError("invalid_snapshot_member")
        identity = (pair["role"], pair["policy"])
        if identity in seen:
            raise ObservationError("duplicate_snapshot")
        seen.add(identity)


def _stat_identity(info):
    return [info.st_dev, info.st_ino, info.st_mode, info.st_size, info.st_mtime_ns, info.st_ctime_ns]


def _components(path):
    result = []
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        info = current.lstat()
        result.append([str(current), _stat_identity(info), os.readlink(current) if stat.S_ISLNK(info.st_mode) else None])
    return result


def _open_regular(path):
    """Walk the resolved absolute path with dirfds; never follow replacement links."""
    fd = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in path.parts[1:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        return os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
    finally:
        os.close(fd)


def _read(location, roots, approved_links):
    path = _location(location, roots)
    result = {"status": "unknown", "location_digest": digest([location, str(roots[location["root"]])]),
              "resolved_digest": None, "link_digest": None, "sha256": None, "sha1": None,
              "bytes": None, "mode": None, "file_identity": None}
    try:
        before = _components(path)
        resolved = path.resolve(strict=True)
        linked = any(row[2] is not None for row in before)
        if linked and approved_links.get(str(path)) != str(resolved):
            raise ObservationError("unapproved_symlink")
        if not any(resolved.is_relative_to(root) for root in roots.values()):
            raise ObservationError("path_escape")
        fd = _open_regular(resolved)
        try:
            initial = os.fstat(fd)
            if not stat.S_ISREG(initial.st_mode):
                raise ObservationError("not_regular_file")
            if initial.st_size > MAX_BYTES:
                raise ObservationError("file_too_large")
            chunks = []
            total = 0
            while total <= MAX_BYTES:
                chunk = os.read(fd, min(65536, MAX_BYTES + 1 - total))
                if not chunk:
                    break
                chunks.append(chunk)
                total += len(chunk)
            if total > MAX_BYTES:
                raise ObservationError("file_too_large")
            data = b"".join(chunks)
            if (_stat_identity(initial) != _stat_identity(os.fstat(fd))
                    or _stat_identity(initial) != _stat_identity(resolved.stat())
                    or before != _components(path) or resolved != path.resolve(strict=True)):
                raise ObservationError("changed_during_read")
        finally:
            os.close(fd)
        # Timestamps are used for race detection, not stable content identity.
        links = [[digest(row[0]), digest(row[2])] for row in before if row[2] is not None]
        result.update(status="read", resolved_digest=digest(str(resolved)), link_digest=digest(links),
                      sha256=hashlib.sha256(data).hexdigest(), sha1=hashlib.sha1(data).hexdigest(),
                      bytes=len(data), mode=stat.S_IMODE(initial.st_mode), file_identity=digest([initial.st_dev, initial.st_ino]))
        return result, data
    except ObservationError as exc:
        result["status"] = str(exc)
    except FileNotFoundError:
        result["status"] = "missing"
    except PermissionError:
        result["status"] = "unreadable"
    except (OSError, RuntimeError):
        result["status"] = "unsafe_or_unreadable"
    return result, None


def _git_output(command, env):
    """Bound command output as well as elapsed time; never return Git diagnostics."""
    process = subprocess.Popen(command, env=env, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    deadline = time.monotonic() + 5
    chunks, total = [], 0
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not selector.select(remaining):
                    raise ObservationError("git_timeout")
                chunk = os.read(process.stdout.fileno(), min(65536, MAX_BYTES + 1 - total))
                if not chunk:
                    break
                chunks.append(chunk)
                total += len(chunk)
                if total > MAX_BYTES:
                    raise ObservationError("git_output_too_large")
        if process.wait(timeout=max(0.001, deadline - time.monotonic())) != 0:
            raise ObservationError("git_unavailable")
        return b"".join(chunks)
    finally:
        if process.poll() is None:
            process.kill()
        process.wait()
        process.stdout.close()


def _source_identity(expected, roots):
    result = {"expected_commit": None, "observed_commit": None, "repository_digest": None, "status": "unknown"}
    if expected is None:
        return result
    _keys(expected, ("root", "commit"))
    if expected["root"] not in roots or not isinstance(expected["commit"], str) or not re.fullmatch(r"[a-f0-9]{40}", expected["commit"]):
        raise ObservationError("invalid_source_identity")
    root = roots[expected["root"]]
    result.update(expected_commit=expected["commit"], repository_digest=digest(str(root)))
    try:
        # -C alone does not constrain Git when GIT_DIR/WORK_TREE/INDEX_FILE etc.
        # are inherited. Never attach another checkout's commit to this root.
        git_env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
        git_env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull)
        command = ["git", "--no-optional-locks", "-c", "core.fsmonitor=false", "-C", str(root)]
        def read_git(arguments):
            return _git_output(command + arguments, git_env)
        toplevel = read_git(["rev-parse", "--show-toplevel"]).decode().strip()
        if Path(toplevel).resolve() != root:
            return result
        head = read_git(["rev-parse", "HEAD"]).decode().strip()
        if not re.fullmatch(r"[a-f0-9]{40}", head):
            return result
        dirty = read_git(["status", "--porcelain=v1", "--untracked-files=normal"])
        after = read_git(["rev-parse", "HEAD"]).decode().strip()
        result.update(observed_commit=head, status="match" if head == expected["commit"] == after and not dirty else "source_changed")
    except (ObservationError, OSError, subprocess.SubprocessError, UnicodeError):
        pass
    return result


def observe_bundle(*, catalog_roots, member_spec, target_surface, expected_source_identity, approved_symlinks=None):
    """Python caller supplies bounded mappings, never evidence of trusted selection.

    approved_symlinks maps exact lexical paths to exact canonical targets; both
    sides must be in the explicit roots. The CLI only supplies known COMMON links.
    """
    _identifier(target_surface)
    if not isinstance(catalog_roots, dict) or not catalog_roots:
        raise ObservationError("invalid_roots")
    roots = {}
    for key, value in catalog_roots.items():
        _identifier(key)
        root = Path(value)
        if not root.is_absolute() or root != root.resolve() or not root.is_dir():
            raise ObservationError("invalid_root")
        roots[key] = root
    _validate_spec(member_spec, roots)
    links = approved_symlinks or {}
    if not isinstance(links, dict) or any(not isinstance(k, str) or not isinstance(v, str) for k, v in links.items()):
        raise ObservationError("invalid_symlink_mapping")
    source_before = _source_identity(expected_source_identity, roots)
    members, contents = [], {}
    for spec in sorted(member_spec["members"], key=lambda x: x["id"]):
        source, data = _read(spec["source"], roots, links)
        installed = _read(spec["installed"], roots, links)[0] if spec["installed"] is not None else None
        relation = "not_observed"
        if installed is not None:
            relation = "unknown"
            if source["status"] == installed["status"] == "read":
                relation = "drift" if source["sha256"] != installed["sha256"] else "same_file" if source["file_identity"] == installed["file_identity"] else "matching_copy"
        members.append({"id": spec["id"], "category": spec["category"], "source": source, "installed": installed, "relation": relation})
        contents[spec["id"]] = data
    snapshots = []
    for pair in sorted(member_spec["policy_snapshots"], key=lambda x: (x["role"], x["policy"])):
        role, policy = contents[pair["role"]], contents[pair["policy"]]
        embedded, current, status = None, None, "unknown"
        if role is not None and policy is not None:
            pattern = rb"(?m)^\| " + re.escape(pair["policy"].encode()) + rb" \| `ready` \| `([a-f0-9]{40})` \|"
            found = re.findall(pattern, role)
            current = hashlib.sha1(policy).hexdigest()
            if len(found) == 1:
                embedded = found[0].decode()
                status = "match" if embedded == current else "stale_policy_snapshot"
            else:
                status = "missing_or_ambiguous_snapshot"
        snapshots.append({**pair, "embedded_sha1": embedded, "current_sha1": current, "status": status})
    source_after = _source_identity(expected_source_identity, roots)
    if source_before != source_after:
        source_after["status"] = "source_changed"
    observation = {"schema_version": 1, "target_surface": target_surface, "task_id": None, "run_id": None,
                   "membership": "asserted_unverified", "trusted_selection": "unknown",
                   "missing_categories": sorted(set(CATEGORIES) - {m["category"] for m in members}),
                   "source_identity": source_after, "manifest_digest": digest(member_spec), "members": members,
                   "policy_snapshots": snapshots, "deployment": "not_observed", "runtime_generation": "unknown",
                   "target_verification": "unverified", "observed_at": dt.datetime.now(dt.timezone.utc).isoformat()}
    observation["content_digest"] = digest({k: v for k, v in observation.items() if k != "observed_at"})
    return observation


def _schema_errors(value, schema, root, path="$", depth=0):
    if depth > 20:
        return ["schema_depth"]
    if "$ref" in schema:
        schema = root["$defs"][schema["$ref"].split("/")[-1]]
    if "anyOf" in schema:
        return [] if any(not _schema_errors(value, s, root, path, depth + 1) for s in schema["anyOf"]) else [path + ":type"]
    kinds = {"object": dict, "array": list, "string": str, "integer": int, "null": type(None)}
    kind = schema.get("type")
    if kind and type(value) is not kinds[kind]:
        return [path + ":type"]
    if "enum" in schema and value not in schema["enum"]:
        return [path + ":enum"]
    if isinstance(value, str) and (len(value) > schema.get("maxLength", MAX_BYTES) or "pattern" in schema and not re.fullmatch(schema["pattern"], value)):
        return [path + ":string"]
    if type(value) is int and (value < schema.get("minimum", 0) or value > schema.get("maximum", 2**63 - 1)):
        return [path + ":range"]
    errors = []
    if isinstance(value, dict):
        properties = schema.get("properties", {})
        if set(value) - set(properties) or set(schema.get("required", [])) - set(value):
            return [path + ":fields"]
        for key, item in value.items():
            errors.extend(_schema_errors(item, properties[key], root, path + "." + key, depth + 1))
    if isinstance(value, list):
        if not schema.get("minItems", 0) <= len(value) <= schema.get("maxItems", MAX_MEMBERS):
            return [path + ":count"]
        if schema.get("uniqueItems") and len({digest(x) for x in value}) != len(value):
            return [path + ":duplicates"]
        for item in value:
            errors.extend(_schema_errors(item, schema["items"], root, path + "[]", depth + 1))
    return errors


def validate_observation(value):
    schema = json.loads(SCHEMA.read_text())
    errors = _schema_errors(value, schema, schema)
    if errors:
        return errors
    if value["content_digest"] != digest({k: v for k, v in value.items() if k not in ("content_digest", "observed_at")}):
        errors.append("content_digest_mismatch")
    ids = [member["id"] for member in value["members"]]
    if len(set(ids)) != len(ids):
        errors.append("duplicate_member")
    if value["missing_categories"] != sorted(set(CATEGORIES) - {m["category"] for m in value["members"]}):
        errors.append("category_mismatch")
    by_id = {m["id"]: m for m in value["members"]}
    for member in value["members"]:
        for file in (member["source"], member["installed"]):
            if file is None:
                continue
            fields = ("resolved_digest", "link_digest", "sha256", "sha1", "bytes", "mode", "file_identity")
            if any((file[k] is not None) != (file["status"] == "read") for k in fields):
                errors.append("inconsistent_file_status")
        source, installed = member["source"], member["installed"]
        relation = "not_observed" if installed is None else "unknown"
        if installed is not None and source["status"] == installed["status"] == "read":
            relation = "drift" if source["sha256"] != installed["sha256"] else "same_file" if source["file_identity"] == installed["file_identity"] else "matching_copy"
        if relation != member["relation"]:
            errors.append("inconsistent_relation")
    seen = set()
    for snapshot in value["policy_snapshots"]:
        pair = (snapshot["role"], snapshot["policy"])
        if pair in seen or any(mid not in by_id or by_id[mid]["category"] != category for mid, category in zip(pair, ("role", "policy"))):
            errors.append("invalid_snapshot_binding")
            continue
        seen.add(pair)
        policy = by_id[snapshot["policy"]]["source"]
        role = by_id[snapshot["role"]]["source"]
        embedded, current = snapshot["embedded_sha1"], snapshot["current_sha1"]
        both_read = policy["status"] == role["status"] == "read"
        if not both_read:
            consistent = snapshot["status"] == "unknown" and embedded is None and current is None
        elif snapshot["status"] == "missing_or_ambiguous_snapshot":
            consistent = embedded is None and current == policy["sha1"]
        else:
            consistent = (snapshot["status"] in ("match", "stale_policy_snapshot") and embedded is not None
                          and current == policy["sha1"] and (embedded == current) == (snapshot["status"] == "match"))
        if not consistent:
            errors.append("inconsistent_snapshot")
    identity = value["source_identity"]
    if identity["status"] == "match" and (identity["expected_commit"] is None or identity["expected_commit"] != identity["observed_commit"] or identity["repository_digest"] is None):
        errors.append("inconsistent_source_identity")
    return errors


def compare_bundle(*, expected, observed):
    """Compare assertions only. Even identical observations remain unverified."""
    if validate_observation(expected) or validate_observation(observed):
        raise ObservationError("invalid_observation")
    reasons = []
    if expected["content_digest"] != observed["content_digest"]:
        reasons.append("observation_changed")
    if any(s["status"] == "stale_policy_snapshot" for s in observed["policy_snapshots"]):
        reasons.append("stale_policy_snapshot")
    if any(m["relation"] == "drift" for m in observed["members"]):
        reasons.append("installed_drift")
    if any(m["source"]["status"] != "read" or m["installed"] is not None and m["installed"]["status"] != "read" for m in observed["members"]):
        reasons.append("member_unavailable")
    if observed["source_identity"]["status"] == "source_changed":
        reasons.append("source_changed")
    if any(s["status"] in ("unknown", "missing_or_ambiguous_snapshot") for s in observed["policy_snapshots"]):
        reasons.append("snapshot_unavailable")
    return {"comparison": "different" if expected["content_digest"] != observed["content_digest"] else "equal_assertions", "reasons": reasons,
            "membership": "asserted_unverified", "trusted_selection": "unknown", "runtime_generation": "unknown"}


def _audit_slice(surface):
    sys.path.insert(0, str(REPO))
    from directory_paths import load_environment, validate_vault
    env = {}
    result = load_environment(checkout_root=REPO, environ=env, require_catalog=True)
    if result["status"] != "loaded":
        raise ObservationError("catalog_unavailable")
    os.environ.update(env)
    validate_vault(env)
    roots = {"repo": Path(env["SAIHAI_ROOT"]), "vault": Path(env["AGENTS_VAULT_ROOT"]), "dotfiles": Path(env["DOTFILES_ROOT"])}
    # Fixed audit slice, not a complete run manifest. No arbitrary path CLI input.
    def member(mid, category, root, path, installed=None):
        return {"id": mid, "category": category, "source": {"root": root, "path": path}, "installed": installed}
    members = [member(p, "policy", "vault", "03-Contexts/Policies/" + p + ".md") for p in POLICIES]
    roles = ("tech-architect", "tech-reviewer")
    members += [member(r, "role", "repo", "organization/roles/" + r + "/skill.md") for r in roles]
    members += [member("COMMON", "common", "dotfiles", "COMMON-AGENTS.md")]
    # Installation roots come from the known surface's documented discovery path.
    installed_root = Path.home() / (".codex" if surface == "codex-app" else ".claude")
    links = {}
    if installed_root.is_dir() and installed_root == installed_root.resolve():
        roots["surface"] = installed_root
        name = "AGENTS.md" if surface == "codex-app" else "CLAUDE.md"
        members[-1]["installed"] = {"root": "surface", "path": name}
        links[str(installed_root / name)] = str(roots["dotfiles"] / "COMMON-AGENTS.md")
    spec = {"members": members, "policy_snapshots": [{"role": r, "policy": p} for r in roles for p in POLICIES]}
    return observe_bundle(catalog_roots=roots, member_spec=spec, target_surface=surface, expected_source_identity=None, approved_symlinks=links)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("observe", "compare", "validate"))
    parser.add_argument("--surface", choices=("codex-app", "claude-cli"), default="codex-app")
    args = parser.parse_args()
    try:
        if args.command == "observe":
            result = _audit_slice(args.surface)
        else:
            value = decode_json(sys.stdin.buffer.read(MAX_BYTES + 1))
            if args.command == "validate":
                errors = validate_observation(value)
                result = {"errors": errors}
                print(json.dumps(result))
                return 2 if errors else 0
            _keys(value, ("expected", "observed"))
            result = compare_bundle(**value)
        print(json.dumps(result, sort_keys=True))
        return 0
    except (ObservationError, OSError, ValueError, TypeError, RecursionError):
        print(json.dumps({"error": "observation_input_or_environment_invalid"}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
