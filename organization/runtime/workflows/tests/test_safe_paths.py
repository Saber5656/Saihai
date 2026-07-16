#!/usr/bin/env python3
"""Tests for workflow state-root path confinement."""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import safe_paths


def expect_rejected(callback, reason: str) -> None:
    try:
        callback()
    except safe_paths.SafePathError as exc:
        assert str(exc) == reason, (str(exc), reason)
    else:
        raise AssertionError(f"expected safe path rejection: {reason}")


def test_constructor_rejects_traversal_absolute_and_scope_escape() -> None:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        expected = root.resolve() / "provider-evidence" / "run-safe" / "result.json"
        assert safe_paths.state_artifact_path(
            root, "provider-evidence", "run-safe", "result.json"
        ) == expected
        expect_rejected(
            lambda: safe_paths.state_artifact_path(
                root, "provider-evidence", "..", "result.json"
            ),
            "unsafe_path_component_0",
        )
        expect_rejected(
            lambda: safe_paths.state_artifact_path(
                root, "provider-evidence", "/tmp", "result.json"
            ),
            "unsafe_path_component_0",
        )
        expect_rejected(
            lambda: safe_paths.confined_state_path(
                root,
                root / "reports" / "run-safe" / "result.json",
                namespaces={"provider-evidence"},
            ),
            "state_artifact_namespace_mismatch",
        )
        expect_rejected(
            lambda: safe_paths.confined_state_path(
                root,
                Path("provider-evidence") / "~" / "result.json",
                namespaces={"provider-evidence"},
            ),
            "unsafe_path_component_1",
        )


def test_confined_path_rejects_symlink_chain() -> None:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        outside = root / "outside"
        outside.mkdir()
        evidence = root / "provider-evidence"
        evidence.mkdir()
        (evidence / "run-link").symlink_to(outside, target_is_directory=True)
        expect_rejected(
            lambda: safe_paths.state_artifact_path(
                root,
                "provider-evidence",
                "run-link",
                "result.json",
            ),
            "state_artifact_symlink",
        )
        expect_rejected(
            lambda: safe_paths.confined_state_path(
                root,
                evidence / "run-link" / "result.json",
                namespaces={"provider-evidence"},
            ),
            "state_artifact_symlink",
        )


def test_trusted_root_path_resolves_inside_and_rejects_escape() -> None:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        trusted = root / "trusted"
        trusted.mkdir()
        outside = root / "outside"
        outside.mkdir()
        (trusted / "nested").mkdir()
        (trusted / "nested" / "present.txt").write_text("ok", encoding="utf-8")

        assert safe_paths.confined_trusted_root_path(
            trusted,
            "nested/present.txt",
            label="candidate",
            strict=True,
        ) == (trusted / "nested" / "present.txt").resolve()
        assert safe_paths.confined_trusted_root_path(
            trusted,
            "nested/missing.txt",
            label="candidate",
        ) == (trusted / "nested" / "missing.txt").resolve()
        expect_rejected(
            lambda: safe_paths.confined_trusted_root_path(
                trusted,
                "../outside/result.txt",
                label="candidate",
            ),
            "unsafe_candidate_component_0",
        )
        (trusted / "escape").symlink_to(outside, target_is_directory=True)
        expect_rejected(
            lambda: safe_paths.confined_trusted_root_path(
                trusted,
                "escape/result.txt",
                label="candidate",
            ),
            "candidate_outside_trusted_root",
        )


def test_exact_allowlisted_path_returns_host_member_only() -> None:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        allowed = root / "registered-worktree"
        allowed.mkdir()
        other = root / "unregistered-worktree"
        other.mkdir()
        alias = root / "registered-alias"
        alias.symlink_to(allowed, target_is_directory=True)

        assert safe_paths.exact_allowlisted_path(
            allowed,
            allowed_paths={allowed},
            label="checkout",
        ) == allowed.resolve()
        expect_rejected(
            lambda: safe_paths.exact_allowlisted_path(
                alias,
                allowed_paths={allowed},
                label="checkout",
            ),
            "checkout_not_allowlisted",
        )
        expect_rejected(
            lambda: safe_paths.exact_allowlisted_path(
                other,
                allowed_paths={allowed},
                label="checkout",
            ),
            "checkout_not_allowlisted",
        )


if __name__ == "__main__":
    tests = (
        test_constructor_rejects_traversal_absolute_and_scope_escape,
        test_confined_path_rejects_symlink_chain,
        test_trusted_root_path_resolves_inside_and_rejects_escape,
        test_exact_allowlisted_path_returns_host_member_only,
    )
    for test in tests:
        test()
    print(json.dumps({"result": "pass", "cases": len(tests)}, ensure_ascii=False))
