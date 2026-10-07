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
  python3 -m unittest -v test_engine test_rebalancer test_db test_calm test_payout test_rewards test_regime test_tape_surrogate test_move_books test_health test_edges_0930 test_observability test_hardening test_venues test_touches test_fee_integrity test_money_paths test_rate_limit test_band_profile test_daily test_deploy_all test_deploy_idle test_sweep test_audit test_audit_more test_audit_runner test_audit_edges test_rpc_key test_jupiter_gate test_wallets test_multi_loop test_scaled test_claims_units test_reward_payout test_review_edges test_audit_wallet test_since_start_scope test_deploy_script test_no_dead_code test_signer_live test_dexes_base test_stats test_stats_equality test_shared_wallet_books test_tape_prune test_quiet_pool test_swing test_add_profile test_unsettled_guard test_edge_watch test_reopen_shape test_increase_idle test_hot_pause test_unichain test_unichain_loop test_replay
fi
node --test test_uniswap.mjs test_uniswap_fork.mjs test_halt_guard.mjs test_payout_send.mjs test_sleeve.mjs test_swap_orca.mjs test_jupiter_gate.mjs test_signer_helpers.mjs test_security.mjs test_payout_pin.mjs test_slippage.mjs test_fee_snapshot.mjs test_orca_fees.mjs test_priority_fee.mjs test_janitor.mjs test_book_format.mjs test_rpc_policy.mjs test_signer_rpc.mjs test_token2022.mjs test_signer_stocks.mjs test_signer_stocks_live.mjs test_aerodrome.mjs test_evm_wallet.mjs test_aerodrome_live.mjs test_aerodrome_fork.mjs test_telegram_bridge.mjs test_increase.mjs test_raydium_landing.mjs
