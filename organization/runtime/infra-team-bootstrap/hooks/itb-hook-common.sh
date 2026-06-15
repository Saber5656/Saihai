#!/usr/bin/env bash
set -euo pipefail

itb_hook_dir() {
  CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd
}

itb_skill_dir() {
  local hook_dir
  hook_dir="$(itb_hook_dir)"
  CDPATH= cd -- "${hook_dir}/.." && pwd
}

itb_runtime() {
  if [[ -n "${ITB_RUNTIME:-}" ]]; then
    printf '%s\n' "${ITB_RUNTIME}"
    return 0
  fi
  case "${0}" in
    *".claude"*) printf '%s\n' "claude" ;;
    *) printf '%s\n' "codex" ;;
  esac
}

itb_state_root() {
  if [[ -n "${ITB_STATE_ROOT:-}" ]]; then
    printf '%s\n' "${ITB_STATE_ROOT}"
    return 0
  fi
  case "$(itb_runtime)" in
    claude) printf '%s\n' "${HOME}/.claude/state/itb" ;;
    *) printf '%s\n' "${HOME}/.codex/state/itb" ;;
  esac
}

itb_builder() {
  if [[ -n "${ITB_BUILDER:-}" ]]; then
    printf '%s\n' "${ITB_BUILDER}"
    return 0
  fi
  if [[ -n "${ITB_BOOTSTRAP_BUILDER:-}" ]]; then
    printf '%s\n' "${ITB_BOOTSTRAP_BUILDER}"
    return 0
  fi
  printf '%s\n' "$(itb_skill_dir)/scripts/itb_bootstrap_builder.py"
}

itb_run_builder() {
  local command="$1"
  shift
  "${ITB_PYTHON:-python3}" "$(itb_builder)" "${command}" \
    --runtime "$(itb_runtime)" \
    --state-root "$(itb_state_root)" \
    "$@"
}
