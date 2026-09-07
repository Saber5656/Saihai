#!/usr/bin/env python3
"""Read actual workflow contracts; report parity without granting authority.

This is a bounded data parser, not an Actions runner. Expressions are retained
or reported unknown, never executed. Expected inventories and policy inputs
are assertions to compare, not authenticated adoption or successful CI runs.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import itertools
import json
import math
import os
import re
import stat
from pathlib import Path
from typing import Any

import yaml

MAX_BYTES = 1024 * 1024
MAX_NODES = 10000
MAX_DEPTH = 64
MAX_CELLS = 256
WORKFLOW_PATH = re.compile(r"\.github/workflows/[A-Za-z0-9_.-]+\.ya?ml")
SHA256 = re.compile(r"[a-f0-9]{64}")
SHA1 = re.compile(r"[a-f0-9]{40}")
NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_-]{0,95}")
WORKFLOW_FIELDS = {"name", "run-name", "on", "permissions", "env", "defaults", "concurrency", "jobs"}
JOB_FIELDS = {"name", "runs-on", "container", "services", "env", "defaults", "permissions",
              "timeout-minutes", "concurrency", "needs", "if", "strategy", "steps",
              "environment", "continue-on-error", "outputs"}
STEP_FIELDS = {"name", "id", "uses", "run", "with", "env", "shell", "working-directory",
               "if", "continue-on-error", "timeout-minutes"}
EVENTS = {"push", "pull_request", "merge_group", "schedule", "workflow_dispatch"}
ROW_FIELDS = {"cell_id", "workflow_path", "job_id", "matrix", "check_name", "contract"}


class InventoryError(ValueError):
    """Invalid or unsupported inventory input; no empty-success fallback."""


class _WorkflowLoader(yaml.SafeLoader):
    pass


# Copy resolver lists: mutating SafeLoader would change the existing ITB parser.
_WorkflowLoader.yaml_implicit_resolvers = {
    key: [(tag, pattern) for tag, pattern in resolvers if tag != "tag:yaml.org,2002:bool"]
    for key, resolvers in yaml.SafeLoader.yaml_implicit_resolvers.items()
}
_WorkflowLoader.add_implicit_resolver("tag:yaml.org,2002:bool", re.compile(r"^(true|false|True|False|TRUE|FALSE)$"), list("tTfF"))


def _mapping(loader: _WorkflowLoader, node: yaml.MappingNode) -> dict:
    result = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=True)
        if type(key) is not str or not key or len(key) > 256:
            raise InventoryError("mapping_key_must_be_bounded_string")
        if key in result:
            raise InventoryError("duplicate_mapping_key:" + key)
        result[key] = loader.construct_object(value_node, deep=True)
    return result


_WorkflowLoader.add_constructor("tag:yaml.org,2002:map", _mapping)


def _bounded_json(value: Any, depth: int = 0, budget: list[int] | None = None) -> None:
    if budget is None:
        budget = [MAX_NODES]
    budget[0] -= 1
    if budget[0] < 0 or depth > MAX_DEPTH:
        raise InventoryError("input_node_or_depth_budget")
    if type(value) is dict:
        for key, item in value.items():
            if type(key) is not str or not 1 <= len(key) <= 256:
                raise InventoryError("invalid_object_key")
            _bounded_json(item, depth + 1, budget)
    elif type(value) is list:
        for item in value:
            _bounded_json(item, depth + 1, budget)
    elif type(value) is str:
        if len(value) > MAX_BYTES or "\x00" in value:
            raise InventoryError("invalid_text")
    elif type(value) is float:
        if not math.isfinite(value):
            raise InventoryError("non_finite_number")
    elif value is not None and type(value) not in (bool, int):
        raise InventoryError("non_json_value")


def parse_workflow(raw: str) -> dict:
    """Decode bounded YAML/JSON with no aliases, explicit tags or duplicate keys."""
    if type(raw) is not str or len(raw.encode("utf-8")) > MAX_BYTES:
        raise InventoryError("workflow_byte_budget")
    try:
        depth = nodes = 0
        for event in yaml.parse(raw, Loader=_WorkflowLoader):
            nodes += 1
            if nodes > MAX_NODES:
                raise InventoryError("workflow_node_budget")
            if getattr(event, "anchor", None) is not None or isinstance(event, yaml.AliasEvent):
                raise InventoryError("aliases_and_anchors_unsupported")
            if getattr(event, "tag", None) is not None:
                raise InventoryError("explicit_tags_unsupported")
            if isinstance(event, (yaml.MappingStartEvent, yaml.SequenceStartEvent)):
                depth += 1
                if depth > MAX_DEPTH:
                    raise InventoryError("workflow_depth_budget")
            elif isinstance(event, (yaml.MappingEndEvent, yaml.SequenceEndEvent)):
                depth -= 1
        value = yaml.load(raw, Loader=_WorkflowLoader)
        _bounded_json(value)
        if type(value) is not dict:
            raise InventoryError("workflow_object_required")
        return value
    except (yaml.YAMLError, RecursionError, UnicodeError) as exc:
        raise InventoryError("workflow_parse_error:" + type(exc).__name__) from exc


def digest(value: Any) -> str:
    _bounded_json(value)
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                    ensure_ascii=True, allow_nan=False).encode()).hexdigest()


def _object(value: Any, fields: set[str] | None, path: str, issues: list[str]) -> dict:
    if type(value) is not dict:
        issues.append(path + ":object_required")
        return {}
    if fields is not None:
        issues.extend(path + ":unknown_field:" + key for key in sorted(set(value) - fields))
    return value


def _events(value: Any) -> dict:
    if type(value) is str:
        return {value: None}
    if type(value) is list and all(type(x) is str for x in value) and len(set(value)) == len(value):
        return dict.fromkeys(value)
    if type(value) is dict:
        return value
    raise InventoryError("workflow.on:invalid_events")


def _matrix(value: Any) -> list[dict]:
    if value is None:
        return [{}]
    if type(value) is not dict or len(value) > 18:
        raise InventoryError("unsupported_matrix")
    axes = {key: vals for key, vals in value.items() if key not in {"include", "exclude"}}
    size = 1
    for key, vals in axes.items():
        if (NAME.fullmatch(key) is None or type(vals) is not list or not 1 <= len(vals) <= 32
                or any(type(v) not in (str, int, bool) for v in vals)
                or any(type(v) is str and "${{" in v for v in vals)
                or len({json.dumps(v) for v in vals}) != len(vals)):
            raise InventoryError("unsupported_matrix_axis")
        size *= len(vals)
        if size > MAX_CELLS:
            raise InventoryError("matrix_cell_budget")
    originals = [dict(zip(axes, values)) for values in itertools.product(*axes.values())] if axes else []
    includes, excludes = value.get("include", []), value.get("exclude", [])
    for rows in (includes, excludes):
        if (type(rows) is not list or len(rows) > MAX_CELLS
                or any(type(row) is not dict or not row or any(type(v) not in (str, int, bool) for v in row.values()) for row in rows)):
            raise InventoryError("unsupported_matrix_include_exclude")
    def equal(a, b):
        return type(a) is type(b) and a == b
    originals = [cell for cell in originals if not any(all(k in cell and equal(cell[k], v) for k, v in row.items()) for row in excludes)]
    cells = copy.deepcopy(originals)
    for addition in includes:
        matched = False
        for original, cell in zip(originals, cells):
            if all(key not in original or equal(original[key], val) for key, val in addition.items()):
                cell.update(addition)
                matched = True
        if not matched:
            cells.append(copy.deepcopy(addition))
    if not cells and not value:
        cells = [{}]
    if not cells or len(cells) > MAX_CELLS:
        raise InventoryError("matrix_empty_or_over_budget")
    if len({json.dumps(cell, sort_keys=True) for cell in cells}) != len(cells):
        raise InventoryError("duplicate_matrix_cell")
    return cells


def _resolve_name(name: Any, matrix: dict) -> str | None:
    if type(name) is not str or not 1 <= len(name) <= 256:
        return None
    result = re.sub(r"\$\{\{\s*matrix\.([A-Za-z_][A-Za-z0-9_-]*)\s*\}\}",
                    lambda m: str(matrix[m[1]]) if m[1] in matrix else m[0], name)
    return None if "${{" in result else result


def _text(value: Any, path: str, errors: list[str], maximum: int = 4096) -> None:
    if type(value) is not str or not 1 <= len(value) <= maximum or not value.strip():
        errors.append(path + ":nonempty_bounded_string_required")


def _strings(value: Any, path: str, errors: list[str], maximum: int = 64) -> None:
    if type(value) is not list or not 1 <= len(value) <= maximum:
        errors.append(path + ":nonempty_bounded_string_array_required")
    else:
        for item in value:
            _text(item, path, errors, 256)


def _scalar_map(value: Any, path: str, errors: list[str]) -> None:
    value = _object(value, None, path, errors)
    if len(value) > 128:
        errors.append(path + ":map_budget")
    for name, item in value.items():
        if type(item) not in (str, int, float, bool):
            errors.append(path + "." + name + ":scalar_required")


def _permissions(value: Any, path: str, errors: list[str]) -> None:
    # Deliberately supported mapping form; shorthand or future permission
    # names need an explicit schema revision, never permissive acceptance.
    names = {"actions", "attestations", "checks", "contents", "deployments", "discussions",
             "id-token", "issues", "models", "packages", "pages", "pull-requests", "security-events", "statuses"}
    value = _object(value, names, path, errors)
    for name, level in value.items():
        levels = {"write", "none"} if name == "id-token" else {"read", "none"} if name == "models" else {"read", "write", "none"}
        if type(level) is not str or level not in levels:
            errors.append(path + "." + name + ":invalid_permission_enum")


def _defaults(value: Any, path: str, errors: list[str]) -> None:
    value = _object(value, {"run"}, path, errors)
    if "run" not in value:
        errors.append(path + ":run_required")
    run = _object(value.get("run"), {"shell", "working-directory"}, path + ".run", errors)
    for name, item in run.items():
        _text(item, path + ".run." + name, errors)


def _concurrency(value: Any, path: str, errors: list[str]) -> None:
    if type(value) is str:
        _text(value, path, errors, 256)
        return
    value = _object(value, {"group", "cancel-in-progress"}, path, errors)
    _text(value.get("group"), path + ".group", errors, 256)
    if "cancel-in-progress" in value and type(value["cancel-in-progress"]) is not bool:
        errors.append(path + ".cancel-in-progress:boolean_required")


def _runner(value: Any, path: str, errors: list[str]) -> None:
    if type(value) is str:
        _text(value, path, errors, 256)
    elif type(value) is list:
        _strings(value, path, errors, 16)
    else:
        value = _object(value, {"group", "labels"}, path, errors)
        if not value:
            errors.append(path + ":runner_group_or_labels_required")
        if "group" in value:
            _text(value["group"], path + ".group", errors, 256)
        if "labels" in value:
            if type(value["labels"]) is str:
                _text(value["labels"], path + ".labels", errors, 256)
            else:
                _strings(value["labels"], path + ".labels", errors, 16)


def _container(value: Any, path: str, errors: list[str], *, service: bool = False) -> None:
    if type(value) is str and not service:
        _text(value, path, errors)
        return
    value = _object(value, {"image", "credentials", "env", "ports", "volumes", "options"}, path, errors)
    _text(value.get("image"), path + ".image", errors)
    if "credentials" in value:
        credentials = _object(value["credentials"], {"username", "password"}, path + ".credentials", errors)
        for field in ("username", "password"):
            _text(credentials.get(field), path + ".credentials." + field, errors)
    if "env" in value:
        _scalar_map(value["env"], path + ".env", errors)
    for field in ("ports", "volumes"):
        if field in value:
            _strings(value[field], path + "." + field, errors)
    if "options" in value:
        _text(value["options"], path + ".options", errors)


def _common_fields(value: dict, path: str, errors: list[str]) -> None:
    for field, validator in (("permissions", _permissions), ("defaults", _defaults),
                             ("concurrency", _concurrency), ("env", _scalar_map)):
        if field in value:
            validator(value[field], path + "." + field, errors)
    for field in ("if", "continue-on-error"):
        if field in value and type(value[field]) is not bool:
            _text(value[field], path + "." + field, errors)
    if "timeout-minutes" in value:
        number = value["timeout-minutes"]
        if type(number) is not int or not 1 <= number <= 1440:
            errors.append(path + ".timeout-minutes:bounded_integer_required")


def _event_configs(events: dict, path: str, errors: list[str]) -> None:
    for name, config in events.items():
        event_path = path + ".on." + name
        if name not in EVENTS:
            errors.append(event_path + ":unsupported_event")
            continue
        if name == "schedule":
            if type(config) is not list or not 1 <= len(config) <= 16:
                errors.append(event_path + ":schedule_array_required")
            else:
                for entry in config:
                    entry = _object(entry, {"cron"}, event_path, errors)
                    _text(entry.get("cron"), event_path + ".cron", errors, 256)
            continue
        if config is None:
            continue
        fields = {"types"} if name == "merge_group" else set() if name == "workflow_dispatch" else {
            "branches", "branches-ignore", "paths", "paths-ignore"}
        if name == "push":
            fields |= {"tags", "tags-ignore"}
        elif name == "pull_request":
            fields |= {"types"}
        # Parameterized workflow_dispatch is deliberately unsupported. Do not
        # silently accept input contracts this bounded producer cannot model.
        config = _object(config, fields, event_path, errors)
        for field, filters in config.items():
            _strings(filters, event_path + "." + field, errors)
        for field in ("branches", "tags", "paths"):
            if field in config and field + "-ignore" in config:
                errors.append(event_path + ":conflicting_include_exclude_filters")


def _shape(workflow: dict, path: str, errors: list[str]) -> dict:
    """Structural failures are errors, separate from operational quality gaps."""
    _object(workflow, WORKFLOW_FIELDS, path, errors)
    events = _events(workflow.get("on"))
    if not events:
        raise InventoryError(path + ":events_empty")
    _event_configs(events, path, errors)
    _common_fields(workflow, path, errors)
    for field in ("name", "run-name"):
        if field in workflow:
            _text(workflow[field], path + "." + field, errors, 256)
    jobs = _object(workflow.get("jobs"), None, path + ".jobs", errors)
    if not 1 <= len(jobs) <= 64:
        raise InventoryError(path + ":jobs_length")
    for job_id, raw in jobs.items():
        job_path = path + "/" + job_id
        if NAME.fullmatch(job_id) is None:
            errors.append(job_path + ":invalid_job_id")
        job = _object(raw, JOB_FIELDS, job_path, errors)
        _runner(job.get("runs-on"), job_path + ".runs-on", errors)
        _common_fields(job, job_path, errors)
        if "name" in job:
            _text(job["name"], job_path + ".name", errors, 256)
        if "needs" in job:
            if type(job["needs"]) is str:
                _text(job["needs"], job_path + ".needs", errors, 96)
            else:
                _strings(job["needs"], job_path + ".needs", errors)
        if "container" in job:
            _container(job["container"], job_path + ".container", errors)
        if "services" in job:
            services = _object(job["services"], None, job_path + ".services", errors)
            if len(services) > 16:
                errors.append(job_path + ":services_budget")
            for name, service in services.items():
                _container(service, job_path + ".services." + name, errors, service=True)
        if "outputs" in job:
            outputs = _object(job["outputs"], None, job_path + ".outputs", errors)
            for name, value in outputs.items():
                _text(value, job_path + ".outputs." + name, errors)
        if "environment" in job:
            environment = job["environment"]
            if type(environment) is str:
                _text(environment, job_path + ".environment", errors)
            else:
                environment = _object(environment, {"name", "url"}, job_path + ".environment", errors)
                _text(environment.get("name"), job_path + ".environment.name", errors)
                if "url" in environment:
                    _text(environment["url"], job_path + ".environment.url", errors)
        if "strategy" in job:
            strategy = _object(job["strategy"], {"matrix", "fail-fast", "max-parallel"}, job_path + ".strategy", errors)
            if "fail-fast" in strategy and type(strategy["fail-fast"]) is not bool:
                errors.append(job_path + ".strategy.fail-fast:boolean_required")
            if "max-parallel" in strategy and (type(strategy["max-parallel"]) is not int or not 1 <= strategy["max-parallel"] <= MAX_CELLS):
                errors.append(job_path + ".strategy.max-parallel:bounded_integer_required")
        steps = job.get("steps")
        if type(steps) is not list or not 1 <= len(steps) <= 128:
            errors.append(job_path + ":steps_required")
            continue
        for i, step in enumerate(steps):
            step_path = f"{job_path}.steps[{i}]"
            step = _object(step, STEP_FIELDS, step_path, errors)
            _common_fields(step, step_path, errors)
            if ("uses" in step) == ("run" in step):
                errors.append(step_path + ":exactly_one_run_or_uses_required")
            for field in ("name", "id", "run", "uses", "working-directory", "shell"):
                if field in step:
                    _text(step[field], step_path + "." + field, errors, MAX_BYTES if field == "run" else 4096)
            if "with" in step:
                _scalar_map(step["with"], step_path + ".with", errors)
    return jobs


def _expression_gaps(value: Any, path: str, matrix_keys: set[str]) -> list[str]:
    if type(value) is dict:
        return [gap for key, item in value.items() for gap in _expression_gaps(item, path + "." + key, matrix_keys)]
    if type(value) is list:
        return [gap for index, item in enumerate(value) for gap in _expression_gaps(item, f"{path}[{index}]", matrix_keys)]
    if type(value) is not str:
        return []
    remaining = value
    for match in re.finditer(r"\$\{\{(.*?)\}\}", value, re.DOTALL):
        expression = match[1].strip()
        if expression not in {"github.workspace", "runner.temp", "github.repository", "github.workflow",
                              "github.event_name", "github.ref", "github.run_id", "github.run_attempt", "always()", "steps.codeql-init.outputs.codeql-path",
                              "steps.codeql-init.outputs.codeql-version", "steps.codeql-init.outcome",
                              "steps.codeql-analysis.outcome", "steps.codeql-analysis.outputs.sarif-id",
                              "needs.validation_shards.result"} | {"matrix." + key for key in matrix_keys}:
            return [path + ":unsupported_expression"]
        remaining = remaining.replace(match[0], "")
    return [path + ":unsupported_expression"] if "${{" in remaining else []


def _quality_gaps(workflow: dict, job: dict, path: str) -> list[str]:
    gaps = []
    container = job.get("container")
    image = container.get("image") if type(container) is dict else container
    if type(image) is not str or re.search(r"@sha256:[a-f0-9]{64}$", image) is None:
        gaps.append(path + ":floating_runtime:runner_not_immutable_build_runtime")
    concurrency_errors: list[str] = []
    _concurrency(job.get("concurrency", workflow.get("concurrency")), path + ".concurrency", concurrency_errors)
    if concurrency_errors:
        gaps.append(path + ":missing_concurrency")
    if "merge_group" not in _events(workflow.get("on")):
        gaps.append(path + ":merge_group_missing")
    if type(job.get("timeout-minutes")) is not int or not 1 <= job["timeout-minutes"] <= 1440:
        gaps.append(path + ":timeout_missing_or_invalid")
    retained = False
    for step in job.get("steps", []) if type(job.get("steps")) is list else []:
        if type(step) is not dict:
            continue
        uses, settings = step.get("uses", ""), step.get("with", {})
        settings = settings if type(settings) is dict else {}
        if type(uses) is not str:
            continue
        if uses and re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_./-]+@[a-f0-9]{40}", uses) is None:
            gaps.append(path + ":action_ref_not_immutable")
        if uses.startswith("actions/setup-python@"):
            gaps.append(path + ":runtime_artifact_unpinned")
            if re.fullmatch(r"\d+\.\d+\.\d+", str(settings.get("python-version", ""))) is None:
                gaps.append(path + ":floating_runtime:python_version")
        if uses.startswith("actions/upload-artifact@"):
            days = settings.get("retention-days")
            retained = type(days) is int and 1 <= days <= 90
        if uses.startswith("github/codeql-action/init@"):
            gaps.extend([path + ":codeql_bundle_integrity_unverified", path + ":codeql_implicit_cache_unverified"])
    if not retained:
        gaps.append(path + ":evidence_retention_unset")
    return gaps


def observe_workflows(sources: Any) -> dict:
    """Project every supplied source and matrix cell, retaining full job context."""
    result = {"parsing": "valid", "source_digests": {}, "jobs": [], "errors": [], "gaps": [], "unknowns": []}
    if type(sources) is not dict or not 1 <= len(sources) <= 32:
        result.update(parsing="invalid", errors=["sources:bounded_object_required"])
        return result
    for path, raw in sorted(sources.items(), key=lambda x: str(x[0])):
        try:
            if type(path) is not str or WORKFLOW_PATH.fullmatch(path) is None:
                raise InventoryError("invalid_workflow_path")
            workflow = parse_workflow(raw)
            result["source_digests"][path] = hashlib.sha256(raw.encode()).hexdigest()
            jobs = _shape(workflow, path, result["errors"])
            for job_id, job in jobs.items():
                if type(job) is not dict:
                    continue
                strategy = job.get("strategy", {})
                if type(strategy) is not dict:
                    raise InventoryError("unsupported_strategy")
                matrix = _matrix(strategy.get("matrix"))
                if len(result["jobs"]) + len(matrix) > MAX_CELLS:
                    raise InventoryError("total_cell_budget")
                for cell in matrix:
                    name = _resolve_name(job.get("name", job_id if not cell else None), cell)
                    if name is None:
                        result["unknowns"].append(path + ":unsupported_check_name_expression")
                    row = {"cell_id": path + "::" + job_id + "::" + json.dumps(cell, sort_keys=True, separators=(",", ":")),
                           "workflow_path": path, "job_id": job_id, "matrix": cell, "check_name": name,
                           "contract": {"workflow": {k: copy.deepcopy(v) for k, v in workflow.items() if k != "jobs"},
                                        "job": copy.deepcopy(job)}}
                    result["jobs"].append(row)
                    result["unknowns"].extend(_expression_gaps(row["contract"], path + "/" + job_id, set(cell)))
                result["gaps"].extend(_quality_gaps(workflow, job, path + "/" + job_id))
        except (InventoryError, ValueError, TypeError) as exc:
            result["errors"].append(str(path) + ":" + str(exc))
    if result["errors"]:
        result["parsing"] = "invalid"
    elif result["unknowns"]:
        result["parsing"] = "unknown"
    result["gaps"].extend(result["unknowns"])
    return result


def applicability(row: dict, context: dict) -> dict:
    """Bounded event/branch evaluation; no expression engine or success inference."""
    evidence = row["workflow_path"] + ":on/job.if/needs"
    def verdict(state, reason):
        return {"state": state, "reason": reason, "evidence": evidence}
    workflow, job = row["contract"]["workflow"], row["contract"]["job"]
    structural_errors: list[str] = []
    try:
        _shape({**workflow, "jobs": {row["job_id"]: job}}, row["workflow_path"], structural_errors)
    except InventoryError:
        structural_errors.append("unsupported_workflow_structure")
    if structural_errors:
        return verdict("unknown", "invalid_workflow_structure")
    event = context.get("event")
    events = _events(workflow.get("on"))
    if event not in events:
        return verdict("not_applicable", "event_not_triggered")
    config = events[event]
    if config is not None:
        if type(config) is not dict:
            return verdict("unknown", "event_configuration_requires_runtime_evidence")
        if set(config) - {"branches", "types"}:
            return verdict("unknown", "unsupported_event_filter")
        if "types" in config:
            if type(config["types"]) is not list or context.get("action") is None:
                return verdict("unknown", "event_action_unavailable")
            if context["action"] not in config["types"]:
                return verdict("not_applicable", "event_action_filtered")
        if "branches" in config:
            patterns = config["branches"]
            if type(patterns) is not list or any(type(p) is not str or re.search(r"[*!?\[\]]", p) for p in patterns):
                return verdict("unknown", "unsupported_branch_pattern")
            ref = context.get("base_ref") if event in {"pull_request", "merge_group"} else context.get("ref")
            if type(ref) is not str or not ref.startswith("refs/heads/"):
                return verdict("unknown", "branch_context_unavailable")
            if ref.removeprefix("refs/heads/") not in patterns:
                return verdict("not_applicable", "branch_filtered")
    if job.get("if") is False:
        return verdict("not_applicable", "job_condition_false")
    if job.get("if") is not None or job.get("needs"):
        return verdict("unknown", "job_condition_or_needs_requires_run_evidence")
    return verdict("applicable", "event_and_branch_match")


def _execution(row: dict) -> dict:
    steps = row["contract"]["job"].get("steps", [])
    uses = [s.get("uses", "") for s in steps if type(s) is dict and type(s.get("uses", "")) is str] if type(steps) is list else []
    if any(u.startswith("github/codeql-action/analyze@") for u in uses):
        return {"classification": "hybrid", "local_state": "local_unavailable", "remote_state": "remote_pending",
                "reason": "Local CodeQL analysis needs a verified bundle/executor; GitHub code-scanning upload needs the remote service. Neither ran here.",
                "evidence": row["workflow_path"] + ":" + row["job_id"] + ":steps"}
    known_actions = ("actions/checkout@", "actions/setup-python@")
    if any(u and not u.startswith(known_actions) for u in uses):
        return {"classification": "unknown", "local_state": "unknown", "remote_state": "unknown",
                "reason": "Action execution prerequisites are not established.", "evidence": row["workflow_path"]}
    return {"classification": "local_reproducible", "local_state": "not_run", "remote_state": None,
            "reason": "Declared run commands need matching runtime, locks, services and environment; no execution result is implied.",
            "evidence": row["workflow_path"] + ":" + row["job_id"] + ":steps"}


def _differences(expected: Any, observed: Any, path: str) -> list[str]:
    if type(expected) is not type(observed):
        return [path + ":type_changed"]
    if type(expected) is dict:
        changes = [path + "." + key + ":missing" for key in sorted(set(expected) - set(observed))]
        changes += [path + "." + key + ":unexpected" for key in sorted(set(observed) - set(expected))]
        for key in sorted(set(expected) & set(observed)):
            changes.extend(_differences(expected[key], observed[key], path + "." + key))
        return changes
    if type(expected) is list:
        if len(expected) != len(observed):
            return [path + ":length_changed"]
        return [change for i, (a, b) in enumerate(zip(expected, observed)) for change in _differences(a, b, f"{path}[{i}]")]
    return [] if expected == observed else [path + ":changed"]


def _exact(value: Any, fields: set[str], path: str) -> dict:
    if type(value) is not dict or set(value) != fields:
        raise InventoryError(path + ":exact_fields_required")
    _bounded_json(value)
    return value


def _hashes(value: Any, path: str) -> dict:
    if type(value) is not dict or len(value) > 64:
        raise InventoryError(path + ":bounded_hash_map_required")
    for name, sha in value.items():
        if (type(name) is not str or not 1 <= len(name) <= 256 or name.startswith("/")
                or any(p in {"", ".", ".."} for p in name.split("/")) or "\\" in name
                or type(sha) is not str or SHA256.fullmatch(sha) is None):
            raise InventoryError(path + ":invalid_path_or_sha256")
    return value


def audit_inventory(*, sources: Any, expected: Any, target: Any, event_context: Any,
                    lock_digests: Any, policy_snapshot: Any = None) -> dict:
    """Compare actual workflow bytes and locks with assertions; no policy adoption."""
    observed = observe_workflows(sources)
    result = {**copy.deepcopy(observed), "assessment_version": "1", "parity": "unknown", "drift": [],
              "readiness": "blocked", "authorizes_execution": False, "inventory_digest": None,
              "policy_digest": None, "policy_status": "missing", "binding_digest": None}
    try:
        _exact(expected, {"inventory_version", "repository", "source_digests", "jobs", "lock_digests"}, "expected")
        if expected["inventory_version"] != "1":
            raise InventoryError("expected:unsupported_version")
        _exact(target, {"repository", "head_sha", "base_sha"}, "target")
        if (type(target["repository"]) is not str or re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", target["repository"]) is None
                or expected["repository"] != target["repository"]
                or any(type(target[k]) is not str or SHA1.fullmatch(target[k]) is None for k in ("head_sha", "base_sha"))):
            raise InventoryError("target:invalid_identity")
        _exact(event_context, {"event", "ref", "base_ref", "fork", "action"}, "context")
        if (type(event_context["event"]) is not str or event_context["event"] not in EVENTS
                or type(event_context["fork"]) is not bool
                or any(type(event_context[k]) is not str or not 1 <= len(event_context[k]) <= 256 for k in ("ref", "base_ref"))
                or event_context["action"] is not None and (type(event_context["action"]) is not str or len(event_context["action"]) > 96)):
            raise InventoryError("context:invalid_value")
        _hashes(expected["source_digests"], "expected.source_digests")
        _hashes(expected["lock_digests"], "expected.lock_digests")
        _hashes(lock_digests, "actual.lock_digests")
        if type(expected["jobs"]) is not list or not 1 <= len(expected["jobs"]) <= MAX_CELLS:
            raise InventoryError("expected.jobs:bounded_nonempty_array_required")
        expected_jobs = {}
        for row in expected["jobs"]:
            _exact(row, ROW_FIELDS, "expected.job")
            if type(row["cell_id"]) is not str or not 1 <= len(row["cell_id"]) <= 1024 or row["cell_id"] in expected_jobs:
                raise InventoryError("expected.job:invalid_or_duplicate_cell_id")
            expected_jobs[row["cell_id"]] = row
        actual_jobs = {row["cell_id"]: row for row in observed["jobs"]}
        result["drift"].extend(_differences(expected["source_digests"], observed["source_digests"], "source_digests"))
        result["drift"].extend(_differences(expected["lock_digests"], lock_digests, "lock_digests"))
        result["drift"].extend("missing_job:" + key for key in sorted(set(expected_jobs) - set(actual_jobs)))
        result["drift"].extend("unexpected_job:" + key for key in sorted(set(actual_jobs) - set(expected_jobs)))
        for key in sorted(set(actual_jobs) & set(expected_jobs)):
            result["drift"].extend(_differences(expected_jobs[key], actual_jobs[key], "jobs." + key))
        result["parity"] = "drift" if result["drift"] else "match"
        required = []
        if policy_snapshot is None:
            result["gaps"].append("authoritative_policy_missing")
        else:
            _exact(policy_snapshot, {"policy_version", "repository", "required_checks"}, "policy")
            required = policy_snapshot["required_checks"]
            if (policy_snapshot["policy_version"] != "1" or policy_snapshot["repository"] != target["repository"]
                    or type(required) is not list or len(required) > MAX_CELLS
                    or any(type(check) is not str or not 1 <= len(check) <= 256 for check in required)
                    or len(set(required)) != len(required)):
                raise InventoryError("policy:invalid_assertion")
            result.update(policy_digest=digest(policy_snapshot), policy_status="unverified")
            result["gaps"].append("policy_assertion_not_authenticated_adoption")
        for row in result["jobs"]:
            if observed["parsing"] == "valid":
                row["applicability"] = applicability(row, event_context)
                row["execution"] = _execution(row)
            else:
                row["applicability"] = {"state": "unknown", "reason": "invalid_or_unknown_workflow", "evidence": row["workflow_path"]}
                row["execution"] = {"classification": "unknown", "local_state": "unknown", "remote_state": "unknown",
                                    "reason": "Workflow structure or expressions are not established.", "evidence": row["workflow_path"]}
        for check in required:
            matches = [row for row in result["jobs"] if row["check_name"] == check]
            if len(matches) != 1:
                result["gaps"].append("required_check_missing_or_ambiguous:" + check)
            elif matches[0]["applicability"]["state"] != "applicable":
                result["gaps"].append("required_check_not_applicable:" + check)
        result["inventory_digest"] = digest({"sources": observed["source_digests"], "jobs": observed["jobs"], "locks": lock_digests})
        result["binding_digest"] = digest({"target": target, "event_context": event_context,
                                           "inventory_digest": result["inventory_digest"], "expected_digest": digest(expected),
                                           "policy_digest": result["policy_digest"]})
    except (InventoryError, ValueError, TypeError, KeyError) as exc:
        result["errors"].append(str(exc))
    return result


def _read_file(root: Path, relative: str) -> str:
    # Traverse with dirfd and O_NOFOLLOW so a concurrent symlink swap cannot
    # redirect an otherwise valid repository-relative read outside the root.
    parts = relative.split("/")
    if relative.startswith("/") or any(part in {"", ".", ".."} for part in parts):
        raise InventoryError("unsafe_relative_path")
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in parts[:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        child = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
        try:
            info = os.fstat(child)
            if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_BYTES:
                raise InventoryError("file_missing_or_over_budget:" + relative)
            with os.fdopen(child, "rb", closefd=False) as handle:
                content = handle.read(MAX_BYTES + 1)
            if len(content) > MAX_BYTES:
                raise InventoryError("file_byte_budget:" + relative)
            return content.decode("utf-8")
        finally:
            os.close(child)
    except OSError as exc:
        raise InventoryError("file_or_symlink_read_rejected:" + relative) from exc
    finally:
        os.close(fd)


def audit_repository(root: Path, expected: Any, event_context: Any, target: Any,
                     policy_snapshot: Any = None) -> dict:
    """Discover both actual workflow extensions, including unexpected files."""
    try:
        root = root.resolve(strict=True)
        directory = root / ".github/workflows"
        if directory.is_symlink() or directory.parent.is_symlink():
            raise InventoryError("symlink_workflow_directory")
        paths = sorted([*directory.glob("*.yml"), *directory.glob("*.yaml")])
        if len(paths) > 32:
            raise InventoryError("workflow_count_budget")
        sources = {p.relative_to(root).as_posix(): _read_file(root, p.relative_to(root).as_posix()) for p in paths}
        _hashes(expected.get("lock_digests"), "expected.lock_digests")
        locks = {name: hashlib.sha256(_read_file(root, name).encode()).hexdigest() for name in expected["lock_digests"]}
        return audit_inventory(sources=sources, expected=expected, target=target, event_context=event_context,
                               lock_digests=locks, policy_snapshot=policy_snapshot)
    except (InventoryError, OSError, ValueError, AttributeError, TypeError) as exc:
        return {"assessment_version": "1", "parsing": "invalid", "parity": "unknown", "readiness": "blocked",
                "authorizes_execution": False, "errors": [str(exc)], "gaps": [], "drift": [], "jobs": []}


def main() -> None:
    parser = argparse.ArgumentParser(description="Report actual workflow parity; never authorize execution")
    parser.add_argument("--repository-root", type=Path, required=True)
    parser.add_argument("--expected", type=Path, required=True)
    parser.add_argument("--head", required=True)
    parser.add_argument("--base", required=True)
    parser.add_argument("--event", choices=sorted(EVENTS), default="pull_request")
    parser.add_argument("--ref", default="refs/heads/unknown")
    parser.add_argument("--base-ref", default="refs/heads/main")
    parser.add_argument("--check", action="store_true", help="Fail for missing adoption or any unresolved delivery gap")
    args = parser.parse_args()
    try:
        expected = parse_workflow(args.expected.read_text())
        result = audit_repository(args.repository_root, expected,
                                  {"event": args.event, "ref": args.ref, "base_ref": args.base_ref, "fork": True, "action": None},
                                  {"repository": expected.get("repository"), "head_sha": args.head, "base_sha": args.base})
    except (InventoryError, OSError) as exc:
        result = {"readiness": "blocked", "authorizes_execution": False, "errors": [str(exc)]}
    print(json.dumps(result, sort_keys=True))
    # Report-mode exit zero means the diagnostic ran, not that delivery is ready.
    raise SystemExit(1 if result.get("errors") or result.get("parity") != "match" or args.check else 0)


if __name__ == "__main__":
    main()
