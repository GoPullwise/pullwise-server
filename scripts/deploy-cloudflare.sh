#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
environment=""
execute=0
local_checks=0

usage() {
  echo "Usage: $0 --environment preview|production [--execute --local-checks-passed]" >&2
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
    *) usage; exit 2 ;;
  esac
done

[[ "$environment" == preview || "$environment" == production ]] || { usage; exit 2; }
if ((execute && !local_checks)); then
  echo "Remote execution requires completed local checks (--local-checks-passed)." >&2
  exit 2
fi

cd "$repo_root"
"${PULLWISE_PYTHON:-python3}" scripts/check-ledger-s01.py --environment "$environment"
config="cloudflare/server/wrangler.${environment}.jsonc"
wrangler="cloudflare/server/node_modules/wrangler/wrangler-dist/cli.js"
if ((execute)); then
  [[ -f "$wrangler" ]] || { echo "Pinned local Wrangler installation is missing." >&2; exit 2; }
  echo "Applying remote D1 migrations for $environment"
  node "$wrangler" d1 migrations apply DB --remote --config "$config"
  echo "Deploying Server Worker for $environment"
  node "$wrangler" deploy --config "$config"
else
  echo "Dry run only. After local verification, --execute --local-checks-passed would run:"
  echo "node $wrangler d1 migrations apply DB --remote --config $config"
  echo "node $wrangler deploy --config $config"
fi
