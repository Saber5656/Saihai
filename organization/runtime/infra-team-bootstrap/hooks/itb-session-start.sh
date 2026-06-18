#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=itb-hook-common.sh
source "${SCRIPT_DIR}/itb-hook-common.sh"

itb_run_builder session-start --launch-agents "$@"
