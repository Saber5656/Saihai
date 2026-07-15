#!/bin/sh
# Optional Saihai orchestrator-frontend launcher.
#
# This wrapper and its named profile/requirements are migration conveniences;
# they do not establish an action_enforced boundary.  A mechanical claim also
# requires the root-owned deployment attestation, launch session, and bridge
# gate.  This wrapper remains only a CLI flag guard for deliberate migration.

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

exec codex \
  --strict-config \
  --profile "$codex_profile_name" \
  --ask-for-approval never \
  --config 'default_permissions="saihai_frontend"' \
  --config 'approvals_reviewer="user"' \
  "$@"
