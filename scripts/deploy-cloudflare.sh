#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
environment=""
execute=0
local_checks=0
activate_production=0

usage() {
  echo "Usage: $0 --environment preview|production [--execute --local-checks-passed] [--activate-production]" >&2
}

while (($#)); do
  case "$1" in
    --environment)
      (($# >= 2)) || { usage; exit 2; }
      environment="$2"
      shift 2
      ;;
    --execute) execute=1; shift ;;
    --local-checks-passed) local_checks=1; shift ;;
    --activate-production) activate_production=1; shift ;;
    *) usage; exit 2 ;;
  esac
done

[[ "$environment" == preview || "$environment" == production ]] || { usage; exit 2; }
if ((activate_production)) && [[ "$environment" != production ]]; then
  echo "--activate-production requires --environment production." >&2
  exit 2
fi
if ((execute && !local_checks)); then
  echo "Remote execution requires completed local checks (--local-checks-passed)." >&2
  exit 2
fi

cd "$repo_root"
checker_args=(--environment "$environment")
if ((activate_production)); then checker_args+=(--activate-production); fi
"${PULLWISE_PYTHON:-python3}" scripts/check-ledger-s01.py "${checker_args[@]}"
config="wrangler.${environment}.jsonc"
deploy_args=(--config "$config")
if ((activate_production)); then deploy_args+=(--var PULLWISE_D1_ACCESS_ENABLED:1); fi
wrangler="cloudflare/server/node_modules/wrangler/wrangler-dist/cli.js"
if ((execute)); then
  [[ -f "$wrangler" ]] || { echo "Pinned local Wrangler installation is missing." >&2; exit 2; }
  command -v uv >/dev/null || { echo "uv is required for pinned Python Worker packaging." >&2; exit 2; }
  "${PULLWISE_PYTHON:-python3}" cloudflare/server/sync_server_modules.py
  echo "D1 migrations remain outside this command."
  if ((activate_production)); then
    echo "Explicit production D1 activation overlay; the checked-in config remains paused."
  else
    echo "Runtime switches stay as configured."
  fi
  echo "Deploying Server Worker for $environment"
  cd cloudflare/server
  uv run --frozen --python 3.14.2 pywrangler deploy "${deploy_args[@]}"
else
  echo "Dry run only. After local verification, --execute --local-checks-passed would run:"
  echo "cd cloudflare/server && uv run --frozen --python 3.14.2 pywrangler deploy ${deploy_args[*]}"
fi
