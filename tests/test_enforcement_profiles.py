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
CODEX_REQUIREMENTS = PROFILE_ROOT / "codex-main-agent.requirements.example.toml"
CODEX_RULES = PROFILE_ROOT / "codex-main-agent.rules.example"
LAUNCHER = PROFILE_ROOT / "saihai-frontend-session.sh"
VERIFY_DOC = PROFILE_ROOT / "verify_enforcement.md"
RUNBOOK = ROOT / "docs/runbooks/main-agent-enforcement.md"
ARBITRARY_STATE_ROOT = "/tmp/saihai-frontdoor-canary"

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
    "Bash(python3 scripts/configure_organization.py workflow-frontdoor scoped-worker-derive *)",
    "Bash(python3 scripts/configure_organization.py workflow-frontdoor scoped-worker-execute *)",
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
REQUIRED_CODEX_CLI_FEATURE_DENIES = {
    "multi_agent_v2",
    "enable_fanout",
    "code_mode",
    "code_mode_only",
    "memories",
    "exec_permission_approvals",
    "standalone_web_search",
    "realtime_conversation",
    "artifact",
    "prevent_idle_sleep",
}


def load_claude_profile() -> dict:
    return json.loads(CLAUDE_PROFILE.read_text(encoding="utf-8"))


def load_toml(path: Path) -> dict | None:
    try:
        import tomllib  # type: ignore[import-not-found]
    except ModuleNotFoundError:
        return None
    return tomllib.loads(path.read_text(encoding="utf-8"))


def load_codex_profile() -> dict | None:
    return load_toml(CODEX_PROFILE)


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
    codex_home.mkdir(parents=True, exist_ok=True)
    (codex_home / "saihai-main-agent.config.toml").write_text(
        CODEX_PROFILE.read_text(encoding="utf-8"),
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
    if codex is None:
        assert_contains(CODEX_PROFILE.read_text(encoding="utf-8"), 'approval_policy = "never"', "codex approval policy")
        return
    assert_equal(codex["approval_policy"], "never", "codex approval policy")


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
    if profile is None:
        return
    assert_equal(profile["approval_policy"], "never", "Codex approval policy")
    assert_equal(profile["approvals_reviewer"], "user", "Codex approvals reviewer")
    assert_equal(profile["default_permissions"], "saihai_frontend", "Codex default permissions")
    assert "sandbox_mode" not in profile
    assert_equal(profile["allow_login_shell"], False, "Codex login shell")
    assert_equal(profile["web_search"], "disabled", "Codex web search")
    assert_equal(profile["notify"], [], "Codex legacy notify argv")
    for feature in [
        "shell_tool",
        "shell_snapshot",
        "unified_exec",
        "code_mode_host",
        "apps",
        "plugins",
        "remote_plugin",
        "plugin_sharing",
        "tool_suggest",
        "multi_agent",
        "in_app_browser",
        "browser_use",
        "browser_use_full_cdp_access",
        "browser_use_external",
        "computer_use",
        "auth_elicitation",
        "tool_call_mcp_elicitation",
        "guardian_approval",
        "workspace_dependencies",
        "hooks",
    ]:
        assert_equal(profile["features"][feature], False, f"Codex feature {feature}")
    for feature in REQUIRED_CODEX_CLI_FEATURE_DENIES:
        assert_equal(profile["features"][feature], False, f"Codex CLI feature {feature}")
    bridge = profile["mcp_servers"]["saihai_bridge"]
    assert_equal(bridge["required"], True, "Saihai MCP required")
    assert_equal(
        bridge["enabled_tools"],
        ["submit_request", "read_projection", "ack_output"],
        "Saihai MCP tools",
    )
    assert_equal(
        bridge["command"],
        "/usr/local/libexec/saihai-codex-main-agent-bridge",
        "Saihai MCP root-owned wrapper",
    )
    assert_equal(bridge["args"], [], "Saihai MCP client argv")
    assert {"approve", "execute", "shell"}.issubset(set(bridge["disabled_tools"]))
    assert "model_instructions_file" not in profile
    assert profile["developer_instructions"] == "@@SAIHAI_DEVELOPER_INSTRUCTIONS@@"


def test_codex_requirements_pin_action_boundary() -> None:
    requirements = load_toml(CODEX_REQUIREMENTS)
    if requirements is None:
        text = CODEX_REQUIREMENTS.read_text(encoding="utf-8")
        for needle in [
            'default_permissions = "saihai_frontend"',
            'allowed_web_search_modes = []',
            '[mcp_servers.saihai_bridge.identity]',
            'executable = "/usr/local/libexec/saihai-codex-main-agent-bridge"',
        ]:
            assert_contains(text, needle, "Codex requirements")
        return
    assert_equal(requirements["default_permissions"], "saihai_frontend", "managed default permissions")
    assert_equal(requirements["allowed_approval_policies"], ["never"], "managed approval policy")
    assert "allowed_sandbox_modes" not in requirements
    assert_equal(requirements["allowed_permission_profiles"], {"saihai_frontend": True}, "managed profile allowlist")
    assert_equal(requirements["allowed_web_search_modes"], [], "managed web search")
    assert_equal(requirements["allow_appshots"], False, "managed appshots")
    assert_equal(requirements["allow_remote_control"], False, "managed remote control")
    assert_equal(requirements["features"]["shell_tool"], False, "managed shell tool")
    assert_equal(requirements["features"]["unified_exec"], False, "managed unified exec")
    assert_equal(requirements["features"]["code_mode_host"], False, "managed code mode host")
    assert_equal(requirements["features"]["apps"], False, "managed apps")
    assert_equal(requirements["features"]["plugins"], False, "managed plugins")
    for feature in REQUIRED_CODEX_CLI_FEATURE_DENIES:
        assert_equal(
            requirements["features"][feature],
            False,
            f"managed Codex CLI feature {feature}",
        )
    identity = requirements["mcp_servers"]["saihai_bridge"]["identity"]["command"]
    assert_equal(
        identity["executable"],
        "/usr/local/libexec/saihai-codex-main-agent-bridge",
        "managed MCP executable",
    )
    assert_equal(identity["args"], [], "managed MCP zero argv matcher")
    deny_read = requirements["permissions"]["filesystem"]["deny_read"]
    for token in [
        ".ssh",
        ".aws",
        ".codex/auth.json",
        ".codex-saihai-main-agent/auth.json",
        ".git-credentials",
        "Keychains",
    ]:
        assert any(token in item for item in deny_read), f"managed deny-read missing {token}"
    assert_equal(requirements["plugins"], {}, "managed plugin requirements")
    assert_equal(
        requirements["marketplaces"],
        {"restrict_to_allowed_sources": True},
        "managed marketplace deny-all",
    )


def test_codex_notify_limit_is_documented_without_overclaim() -> None:
    profile_text = CODEX_PROFILE.read_text(encoding="utf-8")
    requirements_text = CODEX_REQUIREMENTS.read_text(encoding="utf-8")
    # ConfigToml has a top-level `notify` argv, while ConfigRequirementsToml in
    # stock Codex 0.144.1 has no matching constraint. The fixed root launcher
    # must inject the empty argv; a profile file alone cannot prove the claim.
    assert_contains(profile_text, "fixed root launcher must inject", "profile notify limit")
    assert_contains(profile_text, "not claim evidence", "profile claim limit")
    assert_contains(requirements_text, "requirements schema cannot constrain", "requirements notify limit")
    assert_contains(requirements_text, "not action-enforcement claim evidence", "requirements claim limit")
    requirements = load_toml(CODEX_REQUIREMENTS)
    if requirements is not None:
        assert "notify" not in requirements, "requirements must not pretend to constrain notify"


def test_codex_rules_allow_only_bridge_prefixes() -> None:
    rules = CODEX_RULES.read_text(encoding="utf-8")
    assert_contains(
        rules,
        '["bridge-submit-request", "bridge-read-projection", "bridge-ack-output"]',
        "bridge command alternatives",
    )
    assert_contains(rules, 'decision = "allow"', "allow decision")
    assert_equal(rules.count("prefix_rule("), 1, "only the root-omitting bridge rule is allowed")
    assert '"--state-root", "*"' not in rules, "Codex rules must not treat '*' as a wildcard"
    assert "scripts/saihai.py\", \"frontdoor" not in rules
    assert_contains(
        rules,
        f"workflow-frontdoor --state-root {ARBITRARY_STATE_ROOT} bridge-read-projection",
        "explicit state-root negative example",
    )
    assert_contains(
        rules,
        "workflow-frontdoor --state-root /tmp/frontdoor-state bridge-read-projection",
        "arbitrary state-root negative example",
    )
    assert_contains(rules, "frontdoor approve", "negative direct frontdoor approval example")
    assert_contains(rules, "frontdoor status", "negative direct frontdoor status example")
    assert_contains(rules, "workflow-frontdoor create-run", "negative facade example")
    assert_contains(rules, "run-provider", "negative provider-dispatch example")
    assert_contains(rules, "child-thread-create", "negative child-thread action example")
    assert_contains(rules, "scoped-worker-derive", "negative scoped capability example")
    assert_contains(rules, "scoped-worker-execute", "negative scoped execution example")
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
            ARBITRARY_STATE_ROOT,
            "bridge-read-projection",
            "--request-id",
            "req-example",
        ],
        [
            "python3",
            "scripts/configure_organization.py",
            "workflow-frontdoor",
            "--state-root",
            ARBITRARY_STATE_ROOT,
            "drain",
            "--run-id",
            "run-example",
        ],
        [
            "python3",
            "scripts/configure_organization.py",
            "workflow-frontdoor",
            "--state-root",
            ARBITRARY_STATE_ROOT,
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
            ARBITRARY_STATE_ROOT,
            "child-thread-create",
            "--plan-json",
            "{}",
            "--result-json",
            "{}",
        ],
        [
            "python3",
            "scripts/configure_organization.py",
            "workflow-frontdoor",
            "scoped-worker-derive",
            "--run-id",
            "run-example",
            "--step-id",
            "implement",
        ],
        [
            "python3",
            "scripts/configure_organization.py",
            "workflow-frontdoor",
            "--state-root",
            ARBITRARY_STATE_ROOT,
            "scoped-worker-execute",
            "--capability-id",
            "cap-example",
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
            ["--codex", "-a", "on-request"],
            ["--codex", "--config", "features.shell_tool=true"],
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
        assert_contains(second, "codex --strict-config --profile saihai-main-agent", "Codex profile arg")
        assert_contains(second, "--strict-config", "Codex strict config arg")
        assert "--sandbox" not in second
        assert_contains(second, "--ask-for-approval never", "Codex approval arg")
        assert_contains(second, 'default_permissions="saihai_frontend"', "Codex permissions override")


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
        "main_agent_bridge_mcp.py",
        "codex-main-agent.config.example.toml",
        "codex-main-agent.requirements.example.toml",
        "codex-main-agent.rules.example",
        "saihai-frontend-session.sh",
        "verify_enforcement.md",
        "agent-integration-canary.md",
    ]:
        assert_contains(runbook, path, "runbook profile reference")
    for phrase in [
        "does not require Saihai to own every product's normal UI",
        "Codex App and IDE sessions are outside that claim",
        "`advisory`",
        "`ingress_enforced`",
        "`action_enforced`",
        "`managed_worker`",
        "/etc/codex/requirements.toml",
        "The file is machine-wide",
        "Another repository is unsupported",
    ]:
        assert_contains(runbook, phrase, "runbook canary/limits")


def test_verify_doc_copy_pasteable() -> None:
    text = VERIFY_DOC.read_text(encoding="utf-8")
    for needle in [
        "commission-begin",
        "commission-run-frontend",
        "commission-observe-worker",
        "commission-seal",
        "/usr/bin/sudo /usr/local/bin/saihai-codex-main-agent",
        "waiting_human",
        "worker_denial_facts_not_promotable",
        "no automatic transport",
        "accepted as blocking evidence",
    ]:
        assert_contains(text, needle, "verify action canary")


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
        test_codex_requirements_pin_action_boundary,
        test_codex_notify_limit_is_documented_without_overclaim,
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
