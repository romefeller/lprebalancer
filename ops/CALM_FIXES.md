CALM recovery and accounting fixes, 2026-09-26.

When a CALM close succeeds but its swap or open fails, the next poll resumes
the saved band intent. `runtime.json` stores that intent atomically before the
close. A restart retains it. Recovery checks current CALM conditions before
opening; stale or missing observations defer the retry. Changed conditions
can select the wide band. Completing the interrupted move does not spend a
second move or run a pool migration review.

The entry/exit volatility thresholds, 25% touch threshold, minimum gap,
capital sizing, and normal CALM decisions are unchanged. A position that earns
less than its inventory loss is not, by itself, evidence of an execution bug.

Program errors now take precedence over incidental RPC logs. After Raydium
execution may have submitted a transaction, endpoint fallback cannot execute
the entire operation again. The controller treats partial/nonzero signer
results as failures and checks the position before retrying an open or close.

Equity includes pending fees. Apply `sql/007_fee_accounting.sql` before starting
the new controller: it also recomputes historical equity from recorded wallet,
position, and accrual values, without adding the fees twice on repeat runs.
Today's fees and the daily table use cumulative fees by position at the UTC
boundaries. A harvest of yesterday's accrual cannot create income today.

Refundable position rent counts each account once. Raydium Token-2022 mints
are included; legacy metadata and mint rent are excluded. Historical rent
errors require transaction evidence, rather than a global guessed adjustment.
The deployment audit outside the repository archives the backup, verified
historical corrections, and before/after values.

CALM open reports no longer show forecasts belonging to a different ladder
band. The opened band label has its own field so the book's `band` object
cannot overwrite it.

Validation: run `tests/run.sh test_engine test_rebalancer test_db test_calm
test_guards test_dexes` against an isolated test database with all migrations.
This also runs Node tests for rent and transaction error handling. Read-only
mainnet status can verify the new rent total without sending a transaction.
