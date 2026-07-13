#!/usr/bin/env python3
"""Static tests for Saihai main-agent enforcement profiles."""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROFILE_ROOT = ROOT / "organization/runtime/workflows/profiles"
CLAUDE_PROFILE = PROFILE_ROOT / "claude-main-agent.settings.example.json"
CODEX_PROFILE = PROFILE_ROOT / "codex-main-agent.config.example.toml"
CODEX_RULES = PROFILE_ROOT / "codex-main-agent.rules.example"
LAUNCHER = PROFILE_ROOT / "saihai-frontend-session.sh"
VERIFY_DOC = PROFILE_ROOT / "verify_enforcement.md"
RUNBOOK = ROOT / "docs/runbooks/main-agent-enforcement.md"
CODEX_CANARY_STATE_ROOT = "/tmp/saihai-frontdoor-canary"

APPROVED_CLAUDE_BASH_PREFIXES = (
    "Bash(python3 scripts/configure_organization.py workflow-frontdoor bridge-submit-request*)",
    "Bash(python3 scripts/configure_organization.py workflow-frontdoor bridge-read-projection*)",
    "Bash(python3 scripts/configure_organization.py workflow-frontdoor bridge-ack-output*)",
)
REQUIRED_CLAUDE_DENY_ENTRIES = {
    "Edit",
    "Write",
    "NotebookEdit",
    "Bash(git status)",
    "Bash(python3 scripts/saihai.py frontdoor approve *)",
    "Bash(python3 scripts/saihai.py frontdoor status *)",
    "Bash(python3 scripts/configure_organization.py workflow-frontdoor approve *)",
    "Bash(python3 scripts/configure_organization.py workflow-frontdoor status *)",
    "Read(.env)",
    "Read(**/.env)",
    "Read(*credential*)",
    "Read(**/*credential*)",
    "Read(*secret*)",
    "Read(**/*secret*)",
    "Read(*token*)",
    "Read(**/*token*)",
    "Read(*key*)",
    "Read(**/*key*)",
    "Read(id_rsa*)",
    "Read(**/id_rsa*)",
    "Read(id_ed25519*)",
    "Read(**/id_ed25519*)",
    "Read(*.pem)",
    "Read(**/*.pem)",
}


def load_claude_profile() -> dict:
    return json.loads(CLAUDE_PROFILE.read_text(encoding="utf-8"))


def parse_scalar(value: str) -> object:
    if value == "true":
        return True
    if value == "false":
        return False
    if value.startswith('"') and value.endswith('"'):
        return value[1:-1]
    raise AssertionError(f"unsupported TOML scalar in test parser: {value!r}")


def parse_simple_toml(text: str) -> dict:
    parsed: dict[str, object] = {}
    current: dict[str, object] = parsed
    for raw_line in text.splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if not line:
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1]
            current = {}
            parsed[section] = current
            continue
        if "=" not in line:
            raise AssertionError(f"unsupported TOML line in test parser: {raw_line!r}")
        key, value = (part.strip() for part in line.split("=", 1))
        current[key] = parse_scalar(value)
    return parsed


def load_codex_profile() -> dict:
    try:
        import tomllib  # type: ignore[import-not-found]
    except ModuleNotFoundError:
        return parse_simple_toml(CODEX_PROFILE.read_text(encoding="utf-8"))
    return tomllib.loads(CODEX_PROFILE.read_text(encoding="utf-8"))


def assert_equal(actual, expected, label: str) -> None:
    assert actual == expected, f"{label}: expected {expected!r}, got {actual!r}"


def assert_contains(text: str, needle: str, label: str) -> None:
    assert needle in text, f"{label}: missing {needle!r}"


def make_stub(bin_dir: Path, name: str, marker: Path) -> None:
    stub = bin_dir / name
    stub.write_text(
        f"#!/bin/sh\nprintf '%s\\n' \"$0 $*\" >> {marker}\nexit 0\n",
        encoding="utf-8",
    )
    stub.chmod(stub.stat().st_mode | stat.S_IXUSR)


def install_codex_profile(codex_home: Path) -> None:
    (codex_home / "rules").mkdir(parents=True, exist_ok=True)
    (codex_home / "saihai-main-agent.config.toml").write_text(
        CODEX_PROFILE.read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    (codex_home / "rules/saihai-main-agent.rules").write_text(
        CODEX_RULES.read_text(encoding="utf-8"),
        encoding="utf-8",
    )


def run_launcher(
    args: list[str],
    marker: Path,
    bin_dir: Path,
    *,
    codex_home: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["PATH"] = f"{bin_dir}{os.pathsep}{env.get('PATH', '')}"
    if codex_home is not None:
        env["CODEX_HOME"] = str(codex_home)
    return subprocess.run(
        [str(LAUNCHER), *args],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


def test_profiles_parse() -> None:
    assert "permissions" in load_claude_profile()
    codex = load_codex_profile()
    assert_equal(codex["approval_policy"], "on-request", "codex approval policy")


def test_claude_deny_covers_mutation_tools() -> None:
    permissions = load_claude_profile()["permissions"]
    deny = set(permissions["deny"])
    assert REQUIRED_CLAUDE_DENY_ENTRIES.issubset(deny)


def test_claude_allow_only_bridge_prefixes() -> None:
    permissions = load_claude_profile()["permissions"]
    for entry in permissions["allow"]:
        if not entry.startswith("Bash("):
            assert_equal(entry, "Read(*)", "non-Bash allow entry")
            continue
        assert entry in APPROVED_CLAUDE_BASH_PREFIXES, f"unexpected Bash allow entry: {entry}"
    assert not any(entry in {"Bash", "Bash(*)"} for entry in permissions["allow"])


def test_claude_pins_default_mode() -> None:
    permissions = load_claude_profile()["permissions"]
    assert_equal(permissions["defaultMode"], "default", "Claude defaultMode")


def test_claude_disables_bypass_mode() -> None:
    permissions = load_claude_profile()["permissions"]
    assert_equal(permissions["disableBypassPermissionsMode"], "disable", "Claude bypass pin")


def test_codex_profile_pins_readonly_and_approval() -> None:
    profile = load_codex_profile()
    assert_equal(profile["approval_policy"], "on-request", "Codex approval policy")
    assert_equal(profile["approvals_reviewer"], "user", "Codex approvals reviewer")
    assert_equal(profile["default_permissions"], ":read-only", "Codex default permissions")
    assert_equal(profile["allow_login_shell"], False, "Codex login shell")
    assert_equal(profile["web_search"], "disabled", "Codex web search")


def test_codex_rules_allow_only_bridge_prefixes() -> None:
    rules = CODEX_RULES.read_text(encoding="utf-8")
    assert_contains(
        rules,
        '["bridge-submit-request", "bridge-read-projection", "bridge-ack-output"]',
        "bridge command alternatives",
    )
    assert_contains(rules, 'decision = "allow"', "allow decision")
    assert '"--state-root", "*"' not in rules, "Codex rules must not treat '*' as a wildcard"
    assert_contains(
        rules,
        f'"--state-root", "{CODEX_CANARY_STATE_ROOT}"',
        "fixed canary state-root bridge rule",
    )
    assert "scripts/saihai.py\", \"frontdoor" not in rules
    assert_contains(
        rules,
        f"workflow-frontdoor --state-root {CODEX_CANARY_STATE_ROOT} bridge-read-projection",
        "canary state-root bridge match",
    )
    assert_contains(
        rules,
        "workflow-frontdoor --state-root /tmp/frontdoor-state bridge-read-projection",
        "arbitrary state-root negative example",
    )
    assert_contains(rules, "frontdoor approve", "negative direct frontdoor approval example")
    assert_contains(rules, "frontdoor status", "negative direct frontdoor status example")
    assert_contains(rules, "workflow-frontdoor create-run", "negative facade example")
    assert_contains(
        rules,
        f"workflow-frontdoor --state-root {CODEX_CANARY_STATE_ROOT} create-run",
        "negative canary-root facade example",
    )
    assert_contains(rules, "run-provider", "negative provider-dispatch example")
    assert_contains(rules, "child-thread-create", "negative child-thread action example")
    assert_contains(rules, "git status", "negative git example")


def run_codex_execpolicy(binary: str, command: list[str]) -> dict:
    completed = subprocess.run(
        [
            binary,
            "execpolicy",
            "check",
            "--pretty",
            "--rules",
            str(CODEX_RULES),
            *command,
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert_equal(completed.returncode, 0, f"execpolicy return for {command}")
    try:
        return json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise AssertionError(
            f"execpolicy returned invalid JSON for {command}: {completed.stdout!r}; stderr={completed.stderr!r}"
        ) from exc


def test_codex_rules_with_installed_execpolicy() -> None:
    binary = shutil.which("codex")
    if binary is None:
        return

    help_result = subprocess.run(
        [binary, "execpolicy", "check", "--help"],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    if help_result.returncode != 0 or "--rules" not in help_result.stdout:
        return

    allowed = [
        [
            "python3",
            "scripts/configure_organization.py",
            "workflow-frontdoor",
            "bridge-read-projection",
            "--request-id",
            "req-example",
        ],
        [
            "python3",
            "scripts/configure_organization.py",
            "workflow-frontdoor",
            "--state-root",
            CODEX_CANARY_STATE_ROOT,
            "bridge-read-projection",
            "--request-id",
            "req-example",
        ],
    ]
    for command in allowed:
        payload = run_codex_execpolicy(binary, command)
        assert_equal(payload.get("decision"), "allow", f"execpolicy decision for {command}")

    not_allowed = [
        [
            "python3",
            "scripts/configure_organization.py",
            "workflow-frontdoor",
            "--state-root",
            "/tmp/frontdoor-state",
            "bridge-read-projection",
            "--request-id",
            "req-example",
        ],
        [
            "python3",
            "scripts/configure_organization.py",
            "workflow-frontdoor",
            "create-run",
            "--request-id",
            "req-example",
        ],
        [
            "python3",
            "scripts/configure_organization.py",
            "workflow-frontdoor",
            "--state-root",
            CODEX_CANARY_STATE_ROOT,
            "drain",
            "--run-id",
            "run-example",
        ],
        [
            "python3",
            "scripts/configure_organization.py",
            "workflow-frontdoor",
            "--state-root",
            CODEX_CANARY_STATE_ROOT,
            "run-provider",
            "--run-id",
            "run-example",
            "--adapter-json",
            "adapter.json",
        ],
        [
            "python3",
            "scripts/configure_organization.py",
            "workflow-frontdoor",
            "--state-root",
            CODEX_CANARY_STATE_ROOT,
            "child-thread-create",
            "--plan-json",
            "{}",
            "--result-json",
            "{}",
        ],
        ["git", "status"],
    ]
    for command in not_allowed:
        payload = run_codex_execpolicy(binary, command)
        assert "decision" not in payload, f"execpolicy unexpectedly allowed {command}: {payload}"
        assert_equal(payload.get("matchedRules"), [], f"execpolicy matches for {command}")


def test_launcher_refuses_bypass_flags() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        marker = tmp_path / "invoked.log"
        make_stub(tmp_path, "claude", marker)
        make_stub(tmp_path, "codex", marker)
        cases = [
            ["--dangerously-skip-permissions"],
            ["--allow-dangerously-skip-permissions"],
            ["--permission-mode", "bypassPermissions"],
            ["--settings", "other.json"],
            ["--settings=other.json"],
            ["--allowedTools", "Bash(*)"],
            ["--allowedTools=Bash(*)"],
            ["--allowed-tools", "Bash(*)"],
            ["--allowed-tools=Bash(*)"],
            ["--codex", "--dangerously-bypass-approvals-and-sandbox"],
            ["--codex", "--yolo"],
            ["--codex", "--sandbox", "workspace-write"],
            ["--codex", "-a", "never"],
            ["--codex", "--config", "approval_policy='never'"],
            ["--codex", "--profile", "other"],
        ]
        for args in cases:
            completed = run_launcher(args, marker, tmp_path)
            assert_equal(completed.returncode, 2, f"launcher return for {args}")
            assert "refused:" in completed.stderr
        assert not marker.exists(), "forbidden launcher args invoked a stub command"


def test_launcher_passes_clean_args() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        marker = tmp_path / "invoked.log"
        codex_home = tmp_path / "codex-home"
        install_codex_profile(codex_home)
        make_stub(tmp_path, "claude", marker)
        make_stub(tmp_path, "codex", marker)

        claude = run_launcher(["--name", "saihai-test"], marker, tmp_path)
        assert_equal(claude.returncode, 0, "Claude launcher clean args")
        first = marker.read_text(encoding="utf-8").splitlines()[-1]
        assert_contains(first, "claude --settings", "Claude settings arg")
        assert_contains(first, "claude-main-agent.settings.example.json", "Claude settings path")

        codex = run_launcher(["--codex", "hello"], marker, tmp_path, codex_home=codex_home)
        assert_equal(codex.returncode, 0, "Codex launcher clean args")
        second = marker.read_text(encoding="utf-8").splitlines()[-1]
        assert_contains(second, "codex --profile saihai-main-agent", "Codex profile arg")
        assert_contains(second, "--sandbox read-only", "Codex sandbox arg")
        assert_contains(second, "--ask-for-approval on-request", "Codex approval arg")
        assert_contains(second, 'default_permissions=":read-only"', "Codex permissions override")


def test_launcher_requires_installed_codex_profile() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        marker = tmp_path / "invoked.log"
        make_stub(tmp_path, "codex", marker)

        missing = run_launcher(["--codex", "hello"], marker, tmp_path, codex_home=tmp_path / "missing-codex-home")
        assert_equal(missing.returncode, 2, "Codex launcher without profile")
        assert_contains(missing.stderr, "missing Codex profile", "missing profile message")
        assert not marker.exists(), "missing profile invoked codex stub"


def test_runbook_references_profiles_and_canary() -> None:
    runbook = RUNBOOK.read_text(encoding="utf-8")
    for path in [
        "claude-main-agent.settings.example.json",
        "codex-main-agent.config.example.toml",
        "codex-main-agent.rules.example",
        "saihai-frontend-session.sh",
        "verify_enforcement.md",
    ]:
        assert_contains(runbook, path, "runbook profile reference")
    for phrase in [
        "Ask the session to edit a scratch file",
        "python3 scripts/configure_organization.py workflow-frontdoor --state-root /tmp/saihai-frontdoor-canary bridge-read-projection --request-id req-canary",
        "git status",
        "explicitly refused by the profile",
        "Any other explicit state root",
        "R57 action gateway",
    ]:
        assert_contains(runbook, phrase, "runbook canary/limits")


def test_verify_doc_copy_pasteable() -> None:
    text = VERIFY_DOC.read_text(encoding="utf-8")
    assert_contains(text, "saihai-frontend-session.sh", "verify launcher")
    assert_contains(text, "--codex", "verify codex launcher")
    assert_contains(text, "exact disposable canary root", "verify Codex canary root")
    assert_contains(text, "Bypass detector", "verify bypass detector")
    assert_contains(text, "Hooks may log", "verify hook boundary")


def test_launcher_is_posix_sh() -> None:
    text = LAUNCHER.read_text(encoding="utf-8")
    assert text.startswith("#!/bin/sh\n")
    forbidden = ["[[", "]]", "function ", "declare ", "local "]
    for token in forbidden:
        assert token not in text, f"launcher contains non-POSIX token {token!r}"


def main() -> None:
    tests = [
        test_profiles_parse,
        test_claude_deny_covers_mutation_tools,
        test_claude_allow_only_bridge_prefixes,
        test_claude_pins_default_mode,
        test_claude_disables_bypass_mode,
        test_codex_profile_pins_readonly_and_approval,
        test_codex_rules_allow_only_bridge_prefixes,
        test_codex_rules_with_installed_execpolicy,
        test_launcher_refuses_bypass_flags,
        test_launcher_passes_clean_args,
        test_launcher_requires_installed_codex_profile,
        test_runbook_references_profiles_and_canary,
        test_verify_doc_copy_pasteable,
        test_launcher_is_posix_sh,
    ]
    for test in tests:
        test()
    print(json.dumps({"result": "pass", "cases": len(tests)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
