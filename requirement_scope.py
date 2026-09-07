"""Lossless requirement accounting and host-owned change scope checks.

Ledger declarations are observations, never execution authority.
"""
from __future__ import annotations
import copy
import hashlib
import json
import re
import subprocess
from pathlib import Path
from typing import Any


class ScopeError(ValueError):
    pass


def digest(value: Any) -> str:
    return 'sha256:' + hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
        separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def selected_unit(ledger: dict) -> dict:
    if not isinstance(ledger, dict):
        raise ScopeError('requirement_ledger_invalid:object_required')
    errors = gtc_unit_ledger_errors(ledger)
    if errors:
        raise ScopeError('requirement_ledger_invalid:' + ','.join(errors))
    return next(unit for unit in ledger['task_units'] if unit['unit_id'] == ledger['selected_unit_id'])


def revise(ledger: dict, *, expected_digest: str, version: str,
           replacements: dict[str, str], additions: list[dict], units: list[dict],
           selected_unit_id: str, resolved_decisions: tuple[str, ...] = ()) -> dict:
    """An explicit host revision preserves all other requirements and observations.

    Unit replacement is explicit too: no previous non-goal may silently vanish.
    This does not authorize the revision or mark old worker output current.
    """
    selected_unit(ledger)
    if (not isinstance(replacements, dict) or not isinstance(additions, list)
            or any(not isinstance(row, dict) for row in additions)
            or not isinstance(units, list) or any(not isinstance(unit, dict) for unit in units)):
        raise ScopeError('requirements_revision_invalid')
    if digest(ledger) != expected_digest or not version or version == ledger['requirements_version']:
        raise ScopeError('requirements_revision_stale')
    old = {row['requirement_id']: row for row in ledger['requirements']}
    if set(replacements) - old.keys() or any(not isinstance(t, str) or not t.strip() for t in replacements.values()):
        raise ScopeError('requirements_replacement_invalid')
    if any(row.get('requirement_id') in old for row in additions):
        raise ScopeError('requirements_addition_conflict')
    if any(not isinstance(identifier, str) or identifier not in old
           or 'requires_decision' not in old[identifier] for identifier in resolved_decisions):
        raise ScopeError('requirements_decision_resolution_invalid')
    result = copy.deepcopy(ledger)
    result['requirements'] = [dict(row, text=replacements.get(row['requirement_id'], row['text']))
                              for row in result['requirements']] + copy.deepcopy(additions)
    for row in result['requirements']:
        if row['requirement_id'] in resolved_decisions:
            row.pop('requires_decision', None)
    old_units = {unit['unit_id']: unit for unit in ledger['task_units']}
    for unit in units:
        previous = old_units.get(unit.get('unit_id'))
        if previous and not set(previous['scope']['out']) <= set(unit.get('scope', {}).get('out', [])):
            raise ScopeError('requirements_non_goals_lost')
    if not old_units.keys() <= {unit.get('unit_id') for unit in units}:
        raise ScopeError('requirements_units_lost')
    merged_units = [dict(copy.deepcopy(old_units.get(unit['unit_id'], {})), **copy.deepcopy(unit)) for unit in units]
    result.update(requirements_version=version, selection_requirements_version=version,
                  task_units=merged_units, selected_unit_id=selected_unit_id)
    result['requirements_history'].append({'previous_digest': expected_digest,
        'previous_version': ledger['requirements_version'], 'version': version,
        'replacements': copy.deepcopy(replacements), 'additions': copy.deepcopy(additions),
        'resolved_decisions': list(resolved_decisions),
        'previous_requirements': copy.deepcopy(ledger['requirements'])})
    selected_unit(result)
    return result


def validate_diff(root: Path, ledger: dict, *, base: str, actual_paths: list[str]) -> dict:
    """Check actual Git hunks against explicit selected-unit host contracts.

    Ranges use the base's one-based line coordinates; insertions name an exact
    boundary (0 permits the start). Broad path permission alone is insufficient.
    Binary/mode/rename/deletion edits require explicit operation contracts.
    """
    unit = selected_unit(ledger)
    if not re.fullmatch(r'[a-f0-9]{40}', base):
        raise ScopeError('scope_base_invalid')
    contracts = unit.get('change_contracts')
    if not isinstance(contracts, list) or not contracts:
        raise ScopeError('selected_unit_change_contracts_required')
    by_path = {}
    for contract in contracts:
        if not isinstance(contract, dict) or set(contract) != {'path', 'base', 'requirement_ids', 'operations', 'ranges', 'insertions'}:
            raise ScopeError('change_contract_invalid')
        path = contract['path']
        if (not isinstance(path, str) or not path or Path(path).is_absolute() or '..' in Path(path).parts
                or path in by_path or contract['base'] != base
                or not any(a == '.' or path == a or path.startswith(a + '/') for a in unit['allowed_paths'])
                or not isinstance(contract['requirement_ids'], list) or not contract['requirement_ids']
                or any(not isinstance(item, str) for item in contract['requirement_ids'])
                or not set(contract['requirement_ids']) <= set(unit['requirement_ids'])):
            raise ScopeError('change_contract_binding_invalid')
        operations = contract['operations']
        if (not isinstance(operations, list) or not operations or any(not isinstance(op, str) for op in operations)
                or not set(operations) <= {'edit', 'create', 'delete', 'mode', 'binary'}):
            raise ScopeError('change_contract_operations_invalid')
        ranges, insertions = contract['ranges'], contract['insertions']
        if (not isinstance(ranges, list) or any(not isinstance(r, list) or len(r) != 2
                or any(type(n) is not int for n in r) or not 1 <= r[0] <= r[1] for r in ranges)
                or not isinstance(insertions, list) or any(type(n) is not int or n < 0 for n in insertions)):
            raise ScopeError('change_contract_ranges_invalid')
        by_path[path] = contract
    evidence = []
    for path in actual_paths:
        if path not in by_path:
            raise ScopeError('actual_diff_outside_selected_unit')
        contract = by_path[path]
        if (root / path).is_symlink():
            raise ScopeError('actual_diff_symlink_forbidden')
        done = subprocess.run(['git', 'diff', '--no-ext-diff', '--no-textconv', '--no-renames',
            '--unified=0', base, '--', path], cwd=root, capture_output=True, check=True, timeout=30)
        patch = done.stdout.decode('utf-8', errors='strict')
        tracked = subprocess.run(['git', 'ls-files', '--error-unmatch', '--', path], cwd=root,
            capture_output=True, timeout=30).returncode == 0
        operation = 'delete' if 'deleted file mode ' in patch else 'create' if not tracked or 'new file mode ' in patch else 'edit'
        if operation not in contract['operations']:
            raise ScopeError('actual_diff_operation_outside_scope')
        if 'old mode ' in patch and 'mode' not in contract['operations']:
            raise ScopeError('actual_diff_mode_outside_scope')
        if ('Binary files ' in patch or 'GIT binary patch' in patch) and 'binary' not in contract['operations']:
            raise ScopeError('actual_diff_binary_outside_scope')
        hunks = re.findall(r'^@@ -(\d+)(?:,(\d+))? \+\d+(?:,\d+)? @@', patch, re.M)
        if operation == 'edit':
            for start, count in hunks:
                line, size = int(start), int(count or '1')
                if size == 0:
                    allowed = line in contract['insertions']
                else:
                    allowed = any(lo <= line and line + size - 1 <= hi for lo, hi in contract['ranges'])
                if not allowed:
                    raise ScopeError('actual_diff_hunk_outside_scope')
        evidence.append({'path': path, 'requirement_ids': contract['requirement_ids'],
                         'operation': operation, 'patch_digest': digest(patch)})
    return {'requirements_version': ledger['requirements_version'], 'ledger_digest': digest(ledger),
            'selected_unit_id': unit['unit_id'], 'base': base, 'changes': evidence}

def gtc_unit_ledger_errors(envelope: dict[str, Any]) -> list[str]:
    """Validate local accounting only; envelope declarations confer no authority."""
    errors: list[str] = []
    try:
        # Optional YAML parsing may yield dates, non-string keys or aliases.
        # Reject values that cannot be recorded losslessly as finite JSON.
        if json.loads(json.dumps(envelope, allow_nan=False)) != envelope:
            return ["requirement_ledger.json_value"]
    except (TypeError, ValueError, OverflowError, RecursionError):
        return ["requirement_ledger.json_value"]

    def text(value: Any) -> bool:
        return isinstance(value, str) and bool(value.strip())

    def strings(value: Any, *, empty: bool = False) -> bool:
        return (isinstance(value, list) and (empty or bool(value))
                and all(text(item) for item in value) and len(set(value)) == len(value))

    if type(envelope.get("scope_contract_version")) is not int or envelope["scope_contract_version"] != 1:
        errors.append("scope_contract_version")
    version = envelope.get("requirements_version")
    if not text(version):
        errors.append("requirements_version")
    if envelope.get("selection_requirements_version") != version or not text(version):
        errors.append("selection_requirements_version")
    if not isinstance(envelope.get("requirements_history"), list):
        errors.append("requirements_history")
    requirements = envelope.get("requirements")
    requirement_ids: set[str] = set()
    if not isinstance(requirements, list) or not requirements:
        errors.append("requirements")
    else:
        for index, requirement in enumerate(requirements):
            if not isinstance(requirement, dict):
                errors.append(f"requirements[{index}]")
                continue
            identifier = requirement.get("requirement_id")
            if not text(identifier) or identifier in requirement_ids or not text(requirement.get("text")):
                errors.append(f"requirements[{index}]")
            elif text(identifier):
                requirement_ids.add(identifier)
    units = envelope.get("task_units")
    if not isinstance(units, list) or not units:
        return errors + ["task_units"]
    unit_ids: set[str] = set()
    covered: set[str] = set()
    dependencies: dict[str, list[str]] = {}
    for index, unit in enumerate(units):
        prefix = f"task_units[{index}]"
        if not isinstance(unit, dict):
            errors.append(prefix)
            continue
        identifier = unit.get("unit_id")
        for field in ("unit_id", "title", "main_team", "assignee", "repository"):
            if not text(unit.get(field)):
                errors.append(f"{prefix}.{field}")
        if text(identifier):
            if identifier in unit_ids:
                errors.append(f"{prefix}.unit_id.duplicate")
            unit_ids.add(identifier)
        scope = unit.get("scope")
        for field in ("in", "out"):
            if not isinstance(scope, dict) or not strings(scope.get(field)):
                errors.append(f"{prefix}.scope.{field}")
        for field in ("deliverables", "done_criteria", "allowed_paths", "requirement_ids", "depends_on"):
            if not strings(unit.get(field), empty=field == "depends_on"):
                errors.append(f"{prefix}.{field}")
        refs = unit.get("requirement_ids")
        if strings(refs):
            covered.update(refs)
            if set(refs) - requirement_ids:
                errors.append(f"{prefix}.requirement_ids.unknown")
        deps = unit.get("depends_on")
        if text(identifier) and strings(deps, empty=True):
            dependencies[identifier] = deps
        binding = unit.get("binding")
        # Existing bindings are observations, not proof that their targets exist.
        if not isinstance(binding, dict) or binding.get("status") not in ("pending", "existing"):
            errors.append(f"{prefix}.binding")
        elif binding["status"] == "existing" and any(not text(binding.get(key)) for key in ("task_id", "issue")):
            errors.append(f"{prefix}.binding")
    if requirement_ids - covered:
        errors.append("requirements.uncovered")
    for identifier, deps in dependencies.items():
        if set(deps) - unit_ids:
            errors.append(f"task_units.{identifier}.depends_on.unknown")
    # Iterative topological removal avoids recursive traversal of untrusted input.
    remaining = dict(dependencies)
    while remaining:
        roots = {key for key, deps in remaining.items() if not set(deps) & remaining.keys()}
        if not roots:
            errors.append("task_units.depends_on.cycle")
            break
        remaining = {key: deps for key, deps in remaining.items() if key not in roots}
    selected = envelope.get("selected_unit_id")
    if not text(selected) or selected not in unit_ids:
        errors.append("selected_unit_id")
    elif dependencies.get(selected):
        # No trusted prerequisite-completion producer is connected in U1.
        errors.append("selected_unit_id.prerequisite_integration_pending")
    return list(dict.fromkeys(errors))
