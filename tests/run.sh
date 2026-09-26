#!/usr/bin/env bash
# Run the tests against an isolated database. Never against the live book.
#
#   createdb rebalancer_test
#   for migration in sql/*.sql; do psql -d rebalancer_test -f "$migration"; done
#   WALLET_SECRET_PATH=/path/to/key tests/run.sh          # everything, incl. live signer reads
#   tests/run.sh test_engine test_db                      # a subset
set -euo pipefail
cd "$(dirname "$0")"
export LPBOT_DSN="${LPBOT_TEST_DSN:-dbname=rebalancer_test}"
export SOLANA_RPC_URL="${SOLANA_RPC_URL:-https://api.mainnet-beta.solana.com}"
if [ $# -gt 0 ]; then
  python3 -m unittest -v "$@"
else
  python3 -m unittest -v test_engine test_rebalancer test_db test_calm test_signer_live
fi
node --test test_signer_helpers.mjs
