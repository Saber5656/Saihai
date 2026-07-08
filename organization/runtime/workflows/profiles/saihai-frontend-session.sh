#!/bin/sh
# Saihai enforced orchestrator-frontend launcher.
# Verified with Claude Code 2.1.172 and codex-cli 0.141.0 on 2026-07-08.

set -u

mode="claude"
if [ "${1:-}" = "--codex" ]; then
  mode="codex"
  shift
elif [ "${1:-}" = "--claude" ]; then
  shift
fi

script_dir=$(CDPATH= cd "$(dirname "$0")" && pwd)
codex_home="${CODEX_HOME:-$HOME/.codex}"
codex_profile_name="saihai-main-agent"
codex_profile_path="$codex_home/$codex_profile_name.config.toml"
codex_rules_path="$codex_home/rules/$codex_profile_name.rules"

refuse() {
  echo "refused: $1 is forbidden in orchestrator-frontend sessions" >&2
  exit 2
}

if [ "$mode" = "claude" ]; then
  for arg in "$@"; do
    case "$arg" in
      --dangerously*|--allow-dangerously-skip-permissions|--permission-mode|--permission-mode=*|--settings|--settings=*|--allowedTools|--allowedTools=*|--allowed-tools|--allowed-tools=*)
        refuse "$arg"
        ;;
    esac
  done
  exec claude --settings "$script_dir/claude-main-agent.settings.example.json" "$@"
fi

for arg in "$@"; do
  case "$arg" in
    --dangerously*|--yolo|--sandbox|--sandbox=*|-s|--ask-for-approval|--ask-for-approval=*|-a|--config|--config=*|-c|--profile|--profile=*|-p)
      refuse "$arg"
      ;;
  esac
done

if [ ! -r "$codex_profile_path" ]; then
  refuse "missing Codex profile $codex_profile_path"
fi

if [ ! -r "$codex_rules_path" ]; then
  refuse "missing Codex rules $codex_rules_path"
fi

exec codex \
  --profile "$codex_profile_name" \
  --sandbox read-only \
  --ask-for-approval on-request \
  --config 'default_permissions=":read-only"' \
  --config 'approvals_reviewer="user"' \
  "$@"
