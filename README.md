# Rebalancer

An autonomous band optimiser for concentrated liquidity on Orca whirlpools.

It holds one position, watches the price against that position's band, and when
the price leaves, it collects the fees, closes, re-optimises the band and opens
again. It signs its own transactions. It reports every move to Telegram, and it
keeps its accounts in Postgres so the money it has earned survives a rebalance,
a restart and a reboot.

Every parameter is a row in a table. The pool, the band ladder, the size, the
cadence and every safety limit come from `rebalancer.config`. Nothing about the
code is specific to SOL/USDC: token decimals, symbols, the fee tier and the
price all come from the pool itself, and the position is sized from what the
wallet holds of the pool's own two tokens.

```sh
python3 db.py add wif-usdc <pool address> capital_usd=200   # describe a pool
python3 db.py activate wif-usdc                              # run it
sudo systemctl restart lp-bot
```

`add` asks Orca what the pool is, fills the pair from the answer, and refuses an
adaptive-fee pool before you have bought a token for it.

---

## The band optimiser

This is the only decision the bot makes, so it is worth stating precisely.

### The trade-off

A concentrated liquidity position earns fees only while the price sits inside
the band you chose. For a band running from `P/k` to `P·k` around the current
price, the capital is concentrated by

```
C(k) = 1 / (1 − 1/√k)
```

A ±3% band (`k = 1.03`) concentrates about 68 times; ±12% concentrates about
18 times; full range concentrates once. Concentration is a multiplier on your
share of every fee the pool collects, so narrower is strictly better — right up
to the moment the price walks out.

When it walks out, three things happen at once. The position is now entirely in
the losing asset. Getting back to a balanced band costs a swap and slippage.
And the rebalance itself is a transaction that can fail, at an hour you are not
watching. So the band that earns most per day is not the narrowest band. It is
somewhere in the middle, and where exactly depends on how this pool's price has
actually been moving.

### Why there is no formula

The optimum is analytic only if you rebalance continuously and ignore costs.
Once a rebalance is discrete, costs money, and changes the token balances you
carry into the next band, the problem is path-dependent: what you hold after the
third rebalance depends on the exact sequence of prices that produced the first
two. Closed forms for that do not exist outside restrictive assumptions about
the price process, and the assumptions are exactly where the money is.

So the bot does not use one. It replays.

### The replay

For each candidate band `k` in the ladder:

1. **Fetch the pool's own history.** 1000 hourly candles from GeckoTerminal,
   priced in the pool's own quote token, not in dollars. This matters: the pool
   reports one liquidity figure, and a liquidity share computed from a dollar
   price against a pool `L` denominated in quote units is off by the quote
   token's price. On SOL/USDC the two coincide because USDC is a dollar, which
   is precisely why that bug survived calibration and then returned +12%/day on
   SOL/PUMP.

2. **Compute the fee share, dimensionlessly.**

   ```
   share = (capital / pool_TVL) × (C(k) / C_pool)
   ```

   `C_pool` is the pool's own implied average concentration: its active
   liquidity against the liquidity a full-range position of the same TVL would
   have. Both concentrations are pure numbers, so the ratio carries no units and
   cannot be wrong by a factor of 10^9 — which is the failure mode that produced
   a serene +179%/day on SOL/cbBTC. Two hard gates back this up: the candle
   price must agree with the pool's reported price, and `C_pool` must land
   between 1 and 500. A pool that fails either is not scored at all.

3. **Walk the candles, hour by hour.** While the price is inside the band,
   accrue `volume × feeRate × share`. When it leaves, compute the exact token
   amounts the position now holds from the CLMM amount formulas, mark them at
   the current price, charge 10 bps of round-trip swap cost, re-centre the band
   on the new price, and recompute liquidity from the value that survived. The
   token balances are carried forward, so the path is real.

4. **Score it.** Net return per day is `(final_value + fees − capital) /
   capital / days`, plus the rebalance count, the fraction of hours in range,
   and the result against simply holding 50/50.

### The churn gate

Then one rule overrides the score. A band whose replay needed more rebalances
per day than `max_modelled_rebal_per_day` (default 0.5) is discarded however
good its yield looks. A high yield bought with a rebalance a day is a high yield
bought with a daily chance of a failed transaction, and failed transactions are
how this loses real money rather than modelled money. Only if every candidate
fails the gate does the bot fall back to ranking on yield alone.

### A worked example

The live ladder for SOL/USDC (0.04% tier, $190, 41.6 days of history):

```
   band    fees$  pos P&L$   cost$  net%/day  rebal/day  in range  vs hold$
  ±  3%    65.66    -40.91    6.79     0.313       0.94       96%    -25.14
  ±  5%    41.71     -9.10    2.82     0.412       0.36       98%    -17.28
  ±  8%    27.81     10.24    1.38     0.481       0.17       99%    -11.85
  ± 12%    19.16     19.60    0.60     0.490       0.07      100%    -11.14   <-
  ± 18%    13.36     22.91    0.40     0.459       0.05      100%    -13.62
  ± 25%    10.10     22.51    0.20     0.412       0.02      100%    -17.29
  ± 40%     6.87     24.94    0.21     0.402       0.02      100%    -18.09
```

Read it left to right and the trade-off is visible. The ±3% band earns three and
a half times the fees of ±12% — and gives back more than all of it, because
rebalancing 39 times over the window sold SOL low and bought it back high on
every one. ±12% wins on net, comfortably under the churn gate, and it is also
the least bad against holding.

Note the last column. **Every band lost to simply holding 50/50 over this
window**, by $11 to $25 on $190. That is not a defect in the optimiser; it is
what an LP is. See below.

### Re-optimisation

The ladder is re-run every `reopt_interval_seconds` (default 6 hours) against a
fresh window. The band actually held is scored under the same model as the
candidates, so the comparison is like for like, and the bot moves only if the
improvement clears `reopt_min_gain` (default 25%). A rebalance costs a swap and
a transaction; a 5% modelled improvement does not repay that, and chasing one
turns an optimiser into a churner.

The held band's half-width is measured as `√(upper/lower)` — a property of the
band, independent of where the price currently sits inside it. Measuring it
against the current price instead gives a number that drifts off the ladder as
soon as the price moves off centre, so the comparison silently finds no match
and the re-optimiser quietly does nothing. That bug was live in this code.

---

## What it is not

It is not a way to earn without taking risk.

An LP is short gamma. You are paid to sell volatility, and the pools that pay
most are the ones whose prices move most. A SOL/USDC position is roughly half
long SOL: if SOL falls 30%, the position falls with it and no plausible fee
income covers that.

A pool pays an LP only if its daily fee yield exceeds its daily variance divided
by eight. SOL/USDC fees run about 0.22%/day at ±12% by Orca's own figures, and
the replay above puts the total at about 0.49%/day gross — while the same
replay says holding beat it by 6% over a rallying six weeks. Fee income is real
and it is measurable. It is not free, and it is not market-neutral.

---

## Configuration

Every tunable is a column in `rebalancer.config`. One row per named profile;
exactly one row is active, enforced by a partial unique index, so the bot never
has to guess which parameters are its own.

```sh
psql -d rebalancer -f sql/001_schema.sql -f sql/002_any_pool.sql   # schema (idempotent)
python3 db.py seed                          # a first profile, SOL/USDC
python3 db.py add wif-usdc <pool> capital_usd=200   # describe another pool
python3 db.py config                        # show the active profile
python3 db.py set sol-usdc capital_usd=250  # retune, then restart the bot
python3 db.py activate wif-usdc             # switch pools
```

| group | columns |
|---|---|
| what to trade | `pool`; `pair_label`, `token_a`, `token_b` for display, filled by `add` |
| size | `capital_usd`, `max_usd`, `gas_reserve_sol`, `side_cap_fraction` |
| band search | `bands` (the ladder), `max_modelled_rebal_per_day`, `swap_cost_bps` |
| cadence | `poll_seconds`, `min_rebalance_gap_seconds`, `max_rebalances_per_day`, `reopt_interval_seconds`, `reopt_min_gain` |
| safety | `max_consecutive_failures`, `max_unreadable_polls`, `slippage_bps` |
| pool screening | `max_leveraged`, `min_established`, `min_net_day_pct`, `min_tvl_usd` |

A `config_sane` check constraint refuses values that would be nonsense rather
than merely aggressive: a poll interval under 30 seconds, a cap below the
capital, an empty band ladder.

Two values stay out of the table on purpose:

- `LPBOT_WALLET` — the path to the signing key. Deployment-specific, and a bot
  that guesses where your key lives is a bot that might find the wrong one.
  There is no default; startup fails loudly without it.
- `LPBOT_RPC` — the endpoint, which often carries an API key inside the URL and
  so belongs in the service environment, not in a table anyone can `select`.

Any column can still be overridden for a single run with the matching `LPBOT_`
variable. The table is the source of truth; the environment is the escape hatch.

### Pointing it at a different pool

`db.py add <name> <pool>`, then `activate`. Nothing else changes — the signer
reads the pair's decimals, symbols and fee tier from the pool, and values fees
in the quote token before converting to dollars, so a pool whose quote token is
not a dollar is priced correctly rather than silently by a factor.

Sizing follows the pool too. Before an open the bot reads the wallet's balance
of both pool tokens and caps each side at `side_cap_fraction` of the capital in
that token's own units, less the gas reserve when the token is native SOL. A
wallet short of one side opens a smaller position instead of failing. Equity
counts both balances plus the position, so a close that returns the quote token
to the wallet does not read as a loss.

`gas_reserve_sol` is native SOL that is never deposited, whatever the pool
holds: a WIF/USDC position still needs SOL to close itself.

Two constraints on the pool you choose:

- **Adaptive-fee pools cannot be opened by this path.** `db.py add` and the
  signer both refuse them. ZEC/USDC is one, and the failure mode is an instant
  rejection with Whirlpool error 6069 that reads like a slippage problem and is
  not one.
- **Thin pools move when you enter and vanish when you leave.** `min_tvl_usd`
  defaults to $250k for that reason.

---

## Statistics

Accounting lives in four Postgres tables and is cumulative by construction.

| table | holds |
|---|---|
| `positions` | one row per position ever opened, with its band, deposit and withdrawal |
| `harvests` | every fee collection, in both tokens, with its signature |
| `snapshots` | the time series: price, range status, liquidity, accrual, equity |
| `events` | rebands, failures, breakers — anything worth explaining later |

Token amounts are `numeric`, never float. A fee of 0.000265 SOL has to survive a
round trip exactly, and a float does not guarantee that.

```sh
python3 db.py            # the current book
python3 db.py daily      # fees per UTC day
python3 db.py history    # the snapshot series
python3 db.py json       # the same figures as JSON
```

Real output from a position 37 minutes old:

```
SOL/USDC   1 open   in range 100.0%   over 0.026d
FEES
  today           0.000374 SOL       0.049223 USDC  $0.0757
  realised        0.000000 SOL       0.000000 USDC  $0.0000
  unrealised      0.000374 SOL       0.049223 USDC  $0.0923
  TOTAL           0.000374 SOL       0.049223 USDC  $0.0923
BOOK
  equity      $242.49   P&L 2.10
  rate        $None/day   APR None%
  activity    1 positions · 0 rebands · 0 harvests · 0 failures
```

`rate` and `APR` read `None` on purpose for the first hour. They are a total
divided by an elapsed time, and dividing by a near-zero window is how a bot
reports $16,600,000/day with a straight face.

Fees are reported in both tokens because they are earned in both. A position
pays you token A and token B in whatever proportion the trading happened to
take, so a single dollar figure hides what you hold and moves with the price
even in an hour when you earned nothing.

- **today** — since 00:00 UTC: harvested today, plus what has accrued since the
  first snapshot of the morning
- **realised** — harvested into the wallet. Permanent.
- **unrealised** — still in the position. Resets to zero when it closes.
- **TOTAL** — realised plus unrealised. Only goes up.
- **rate / APR** — annualised on the equity actually at work, not on notional,
  and suppressed entirely until the position has run for an hour

### Why the ledger exists

A position's fee counter belongs to the position, not to you. Harvest, close,
reopen, and it reads zero again — which is why a bot that reports the live
position's accrual claims to have earned nothing every time it rebalances.
Splitting realised from unrealised is what makes the total monotonic.

One subtlety worth knowing: Whirlpool settles fee accounting only when a
position is touched, so a live, earning position's own `feeOwed` fields read
zero. The signer takes the real figure from a close quote instead, and reports
the stale field separately so the difference stays visible.

---

## Running it

```sh
# see the configuration and the book
python3 config.py
python3 db.py

# the current position and the wallet, straight from the chain
WALLET_SECRET_PATH=/path/to/key node signer2.mjs status
WALLET_SECRET_PATH=/path/to/key node signer2.mjs balance <pool>

# run it
sudo systemctl start lp-bot          # the rebalancer
sudo systemctl start lp-telegram     # the Telegram bridge

# stop it, from anywhere, immediately
touch HALT

# run one full rebalance now, while you watch
touch REBALANCE
```

`HALT` is absolute: the loop exits on its next cycle, the signer refuses to
build a transaction, and neither restarts until the file is removed.

`REBALANCE` runs harvest → close → re-optimise → reopen on the next poll, under
the same minimum-gap and per-day limits as an automatic one, and is deleted
before it runs so a failure cannot loop on it. It exists because the rebalance
path is the one that runs unattended, and a path that has only run at 3am has
never been watched.

---

## Files

| file | what it does |
|---|---|
| `rebalancer.py` | the loop: read, decide, harvest, close, re-optimise, reopen |
| `engine.py` | pool scanning, the band simulator, Jev token screening |
| `db.py` | Postgres: configuration, accounting, statistics, the CLI |
| `config.py` | loads the active profile, with environment overrides |
| `signer2.mjs` | all chain I/O and signing, on `@orca-so/whirlpools` v8 |
| `telegram_bridge.mjs` | forwards `events.jsonl` to Telegram |
| `sql/001_schema.sql` | the schema, idempotent |
| `sql/002_any_pool.sql` | migration for databases created before the pool-agnostic sizing |
| `ops/*.service` | systemd units |

---

## Safety

| guard | default |
|---|---|
| `HALT` file | stops everything, blocks restart |
| minimum gap between rebalances | 1 hour |
| rebalances per day | 6, then HALT |
| position size cap | `max_usd`, refused above it, priced at the pool's own price |
| gas reserve | `gas_reserve_sol`, never deposited — a wallet that cannot pay fees cannot close its own position |
| consecutive failures | 3, then HALT |
| unreadable polls | 12, then HALT |

Three failure modes it handles specifically, each of which cost real money
before it did:

**A failed write is not a known outcome.** A transaction can land and still
report failure, because the confirmation runs over the same rate-limited RPC
that just timed out. A close did exactly this: it succeeded, reported
`close_failed`, and the capital sat idle. After any write error the bot re-reads
the chain and believes what it finds there, not the error.

**A failed read is not an empty wallet.** An RPC error once made the bot
conclude it held no position — and its response to holding no position is to
open one. On a flaky endpoint that repeats until the wallet is empty. Reads that
fail now hold; only a read that succeeds and reports nothing may open.

**Raw errors are not messages.** Provider errors arrive as a nested dump of
headers and cookies with a status code buried inside. Forwarded verbatim, they
made a healthy Telegram bridge look broken. They are summarised to one line.

---

## Requirements

Postgres 14+, Node 18+, Python 3.9+ with `psycopg2`, `@orca-so/whirlpools` v8,
and a funded Solana keypair whose path is given by `LPBOT_WALLET`. The key is
read by the signer at runtime and never logged.

The legacy `@orca-so/whirlpools-sdk` cannot open positions — every attempt fails
with custom error 6069 after ~1390 compute units, and 0.22.0 is its final
release. The current package is the fix, not a version bump.

Token screening through Jev is optional and consulted only when the scanner
picks the pool rather than a pinned one. It exists because the highest-yielding
pool on the board advertised 243%/yr and its second asset was 3x leveraged SOL —
a token engineered to decay, which no volatility statistic flags.
