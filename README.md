# Rebalancer

An autonomous band and pool optimiser for concentrated liquidity on Solana.

It holds one position, watches the price against that position's band, and when
the price leaves, it collects the fees, closes, re-optimises the band and opens
again. It signs its own transactions. It reports every move to Telegram, and it
keeps its accounts in Postgres so the money it has earned survives a rebalance,
a restart and a reboot.

It also asks, every few hours, whether it is in the right pool at all. A scanner
lists the busiest pools on Orca, Raydium, Meteora, Byreal and PancakeSwap,
scores each one under the same replay the band optimiser uses, and when a pool
elsewhere pays materially better, the bot closes here and opens there. See
[The board](#the-board).

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

### Many origins, not one path

One replay over the whole window is one draw from the pool's history. A band
that happened to fit the last six weeks is not a band that fits this pool. So
every candidate is also replayed from every 24th hour over a 10-day horizon —
about 32 windows on 1000 candles — and the bot decides on the **median** net
return per day across those windows. The 25th percentile and the worst window
are reported next to it, so a band that is brilliant on average and ruinous
one time in four is visible as such.

The same origins give the band's **survival curve**: from each starting hour,
how long until the price first leaves a band of that width. Windows that never
saw an exit before the data ran out are censored, not dropped, and the curve is
a Kaplan-Meier estimate. The table reports the median lifetime and the
probability that the band is still intact after 24 hours, 72 hours and 7 days.

Next to it stands the one number that is analytic: the **edge loss**, the
value a position has given up against holding 50/50 by the time the price
reaches its edge. For a ±5% band it is 1.23%; for ±18%, 4.30%. A rebalance at
the edge makes that loss permanent. Narrow bands pay it often and small; wide
bands pay it rarely and large. The survival curve says how often.

```sh
python3 engine.py ladder <pool> [capital_usd] [bands]
```

```
   band    path  median     p25   worst  +win  beat  reb/d  exit50%  S24h  S72h   S7d   edge
+/-   3%   0.142   0.029  -0.152  -0.358   59%    9%   0.94      25h   50%   12%    0%  0.74%
+/-   5%   0.335   0.115  -0.073  -0.455   66%   25%   0.42      57h   76%   39%    7%  1.23%
+/-   8%   0.449   0.341   0.147  -0.296   91%   50%   0.14     121h   91%   69%   37%  1.96%
+/-  10%   0.490   0.361   0.189  -0.272   91%   53%   0.09     147h   96%   74%   45%  2.44%
+/-  12%   0.468   0.318   0.143  -0.283   91%   47%   0.08     159h   98%   80%   48%  2.91%
+/-  18%   0.438   0.395   0.112  -0.301   88%   59%   0.03     599h  100%   95%   72%  4.30%  <-
+/-  25%   0.404   0.369   0.083  -0.311   88%   59%   0.03     702h  100%   99%   87%  5.87%
+/-  40%   0.384   0.340   0.054  -0.321   84%   59%   0.01     >win  100%  100%  100%  9.03%
```

SOL/USDC, $190, 2026-09-23. `path` is the single full-window replay, the
figure the bot used to decide on; `median`, `p25`, `worst` are the rolling
windows; `+win` is the share of windows with a positive net; `beat` the share
that beat holding. Read the ±5% row: the single path says 0.335%/day, the
median says 0.115, a quarter of windows lose money, and the band has a 7%
chance of lasting a week. The ±10% row has the best single path and the ±18%
row the best median; the bot takes ±18%. No band beats holding in more than
59% of windows, which is the same fact as the `vs hold` column above, seen
from many angles instead of one.

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

### The board

A pool that pays 0.4%/day is not the right pool if one next door pays 0.6%.
So the optimiser does not stop at the band. A scanner thread lists, every
`scan_interval_seconds` (default 6 hours), the `scan_limit` busiest pools by
24-hour volume on every DEX in `dexes`, and scores each one with the ladder
above: its own candles, its own fee, its own implied concentration, the
rolling medians, the survival curve, the churn gate. The result is written to
`rebalancer.scan_runs` and `scan_pools`, ranked by the best band's median net
return per day.

```
python3 scanner.py            # scan now and print the board
python3 db.py board           # the last board, from the table
```

```
dex                   pair              fee  tvl$M  vol$M cpool  band  net%/d    p25 +win reb/d  S7d  screen
meteora-dlmm          SOL/USDC       0.048%   6.93  55.48  18.5    8%   0.582  0.292  88%  0.14  38%  ok both tokens are majors
pancakeswap-v3-solana SOL/USDC       0.030%   1.35   4.06   7.1   18%   0.433  0.122  91%  0.03  72%  ok both tokens are majors
raydium-clmm          SOL/USDC       0.040%   7.22  38.15  15.2   18%   0.397  0.083  91%  0.03  72%  ok both tokens are majors
orca                  SOL/USDC       0.040%  25.97 162.10  21.1   18%   0.386  0.080  81%  0.03  72%  ok both tokens are majors
orca                  SOL/PENGU      0.300%   3.97   3.55   3.9   40%   0.336 -0.456  66%  0.00  99%  ok PENGU: verified
raydium-clmm          SOL/RAY        0.050%   4.08  23.85   4.6   40%  -0.130 -2.745  41%  0.04  74%  NO below the screened top
orca                  SOL/STONK      0.650%   2.82   5.18   8.6   40%  -5.512 -8.680   0%  0.64   0%  NO below the screened top
orca                  ZEC/USDC                2.85  17.19  -- adaptive-fee pool: the Orca signer cannot open it
```

2026-09-23, $190. Read the first four rows: the same pair, SOL/USDC, on four
DEXes, and the modelled return runs from 0.39%/day on Orca to 0.58%/day on
Meteora. The difference is not the fee tier. It is how crowded the active
price is: Orca's pool is concentrated 21 times over full range, Meteora's 18
times, PancakeSwap's 7 times, and a dollar of yours earns in inverse proportion.
Volume per dollar of TVL is the other half, and the replay weighs both.

Then read the bottom. The pools with the highest headline fees — STONK, RAY,
ZEC — lose money at every band, because their prices move faster than any band
can follow and every exit realises the loss. The board says so before any
capital goes near them.

#### What makes a pool comparable

Every DEX publishes a different record, and two of them do not publish the one
number the fee model needs. `dexes.py` turns them into one shape:

| DEX | list | active liquidity | fee |
|---|---|---|---|
| Orca | its API | in the record | nominal |
| Raydium CLMM | its API | pool account, decoded on chain | nominal |
| Byreal | its API | pool account (Raydium layout) | nominal, or realised for dynamic-fee pools |
| PancakeSwap V3 | GeckoTerminal | pool account (Raydium layout) | AmmConfig account |
| Meteora DLMM | its API | bins around the active bin, via the SDK | realised (base plus variable) |

For a tick pool the implied concentration is the active liquidity against
what a full-range position of the same TVL would have. For a bin pool it is
the dollars per bin near the active bin against what a full-range position
would leave in one bin, which is `TVL × step / 4`. Both are pure numbers that
say the same thing, so the fee share formula does not care which kind of pool
it is scoring. On Meteora's SOL/USDC the figure lands at 18; on Orca's, 21.

Candles come from GeckoTerminal for every DEX, at the free tier's pace of one
request every two seconds, through one lock shared by everything in the
process. A full board of forty pools takes about three minutes.

Not on the board, and why: Meteora DAMM v2 sets one price range per pool that
every position shares, so there is no band to choose. Jupiter runs no
range-liquidity product a third party can deposit into; its price API prices
quote tokens and its token API feeds the screen below. Saros DLMM and
DeFiTuna's own pools carry a few thousand dollars a day. HumidiFi, ZeroFi,
SolFi and Lifinity take no outside liquidity.

#### The tape, and the day's rhythm

The model takes the pool's active liquidity as it is now and volume from six
weeks of candles. When liquidity floods into a pool, the modelled fee share is
stale until the next scan. So every scored pool is set against its own tape:
the fee yield its last-24h fees would have paid a position at the chosen band,
against the gross fee yield the replay averaged. Volume is common to the whole
market on a given day, so each pool's ratio is judged relative to the median
ratio across the board. A pool well under its peers has its decision figure
scaled down by that much; nothing is ever scaled up. The board's `use` column
is what the bot ranks and moves on; `real` is the ratio.

The same candles give the day's rhythm: the hour-of-day volume multiplier,
pooled across every scored pool after normalising each by its own mean.
Measured on thirty days of the five SOL/USDC pools, UTC 13 to 16 runs 1.3 to
1.7 times the average hour and UTC 04 to 11 runs 0.7 to 0.8. Hour-to-hour fee
yield is persistent (autocorrelation 0.5 to 0.8) but six hours ahead it is
not (0.05 to 0.4), and chasing whichever pool had the best last six hours
returned 0.2704% per window against 0.2723% for staying in the best pool by
average: the relative rank of pools is structural and moves over days, which
is what the six-hourly board already tracks. The rhythm is used for two
things only. The book shows the hour's multiplier and the expected rate for
the coming hours, so a noon APR reads as the trough it is. And a voluntary
move, a reband or a pool move, waits for an hour at or under the average
(`defer_moves_to_quiet_hours`, default on), when ten minutes out of market
costs least; an out-of-band rebalance never waits.

#### The token screen

The highest-yielding pool ever seen on the board was SOL/xSOL at 243%/yr, and
xSOL is Hylo's 3x leveraged SOL: a token engineered to decay, which no
volatility statistic flags. So every non-major token in the top of the board
is looked up on Jupiter, and a pool passes only if each such token is verified
there, carries no leveraged or structured tag, does not say so in its name,
and has its mint authority disabled. A token Jupiter does not know fails. A
pool of two majors passes by name.

#### The move

At every re-optimisation the loop takes the freshest board and, before it
compares bands, compares pools. A candidate must be scored, must have passed
the screen, and — unless `allow_swap` is set — must hold the same two tokens
the wallet already holds, because entering a different pair means buying it.
The best candidate's modelled net per day is set against the held pool's own
best band under the same model, and the bot moves only if the gain clears
`migrate_min_gain` (default 50%: a pool move is a close, an open on a venue
the bot has not been watching, and possibly a swap, and a modelled 20% does
not pay for that).

Then one more gate: the target's DEX must be in `execute_dexes`, the list of
DEXes the bot holds a signer for. When it is, the bot harvests, closes,
repoints the profile to the new pool (`config.dex`, `config.pool`), reloads
its configuration, and opens at the best band on the new pool. The events are
`MIGRATE`, `CLOSE`, `REPOINTED`, `OPEN`. When it is not, the bot says
`MIGRATE_RECOMMENDED` with the command to move by hand and stays where it is.

Five signers exist, one per DEX, all speaking the contract in
`SIGNER_CONTRACT.md` so the loop cannot tell which it is on:

| dex | signer | built on | signed live |
|---|---|---|---|
| orca | `signer2.mjs` | `@orca-so/whirlpools` v8 | yes, since day one |
| meteora-dlmm | `signer_dlmm.mjs` | `@meteora-ag/dlmm` 1.9 | yes: the first move, 2026-09-24 00:28 UTC, $194 into SOL/USDC ±8% as two position accounts |
| raydium-clmm | `signer_raydium.mjs` | `@raydium-io/raydium-sdk-v2` | not yet; open simulated green |
| byreal | `signer_byreal.mjs` | `@byreal-io/byreal-clmm-sdk` | not yet; open simulated to the token transfer, harvest and close simulated green on live positions |
| pancakeswap-v3-solana | `signer_pancake.mjs` | Raydium instruction builders on the fork's program, two PDA fixes | not yet; whole open simulated green |

`swap_jupiter.mjs` is Jupiter's place in the bot: a swap route, not a pool,
for the day `allow_swap` is on. Dry runs quote and build; it has not sent.

A DLMM band is several position accounts: the program refuses more than about
70 bins per account at creation, so a ±8% band on the 4 bp pool is two
accounts and six transactions. The Meteora signer treats every position the
wallet holds on a pool as one logical position, and the loop's status read
passes the pool in `LPBOT_POOL`, never as an argument. A positional argument
is a position filter; passing the pool there once made the signer answer "no
position" for a position it held, and the loop went to open a second one.

```sh
python3 db.py set sol-usdc execute_dexes=orca,meteora-dlmm   # which DEXes may be opened on
python3 db.py set sol-usdc pool_pinned=true                  # stay put whatever the board says
echo "raydium-clmm <pool>" > MIGRATE                         # move there on the next poll
touch REOPT                                                  # board and band review now
```

The board is advisory and the loop is not: a scan that fails leaves the last
board in place and reports `scan_failed`; a board older than three scan
intervals is ignored.

### Acting before the price leaves

A rebalance at the edge is the worst-priced trade the bot can make: it
happens at whatever price the exit lands on, it makes the position's whole
loss against holding permanent, and until it runs the position is one-sided
and earning nothing. So the bot does not wait for the edge.

Every poll it estimates, from the pool's own hourly tape, the probability
that the price is outside the held band within the next
`proactive_horizon_hours` (default 6). The estimate is a Kaplan-Meier
survival curve conditioned on the live position: from every origin in the
history, how long until the price moved as far up as the ceiling is now, or
as far down as the floor is now, with origins that never did censored. It is
computed twice, from all origins and from those whose trailing-24h
volatility was within a factor 1.5 of today's, and the regime-matched figure
decides. When it reaches `proactive_threshold` (default 0.5: leaving is more
likely than not), the bot harvests, closes and reopens centred on the current
price. It waits for a quiet hour to do so unless the probability has passed
90%, when the hour no longer matters. An actual exit still rebalances at
once. `proactive_threshold=0` switches the rule off.

The ladder and the board replay every band under the same rule, walk-forward
(each hour's probability uses only the hours before it), so a band is scored
as the loop will run it. `pro/d` in the ladder is how often the rule fired.

```sh
python3 db.py set sol-usdc proactive_horizon_hours=6 proactive_threshold=0.5
python3 db.py forecasts        # predicted vs realised exits, by decile, with Brier
```

Every forecast is stored with its snapshot (`p_exit_6h`, `p_exit_24h`,
`p_exit_72h`, `band_position`), so the model is checked against the tape it
ran on: `forecasts` buckets the predictions by decile and prints the realised
exit rate next to each, for the same position, with unresolved forecasts
censored rather than counted as survivors.

What the replay says about it, on 180 days of this pool's tape, for the
record: at ±8% and wider the rule fires rarely and scores the same as
rebalancing at the edge; at ±5% it fires often enough to cost about
0.05%/day in extra swaps and locked loss. The exit probability is honest; the
price is close to a martingale at every horizon from six hours to a week
(variance ratios 0.93 to 1.07), so knowing an exit is likely does not say
which way the price goes after it. The defaults are therefore the least
active setting the rule allows, six hours and a coin flip, and the figures are
in every book so the operator can see it fire and judge it.

### The dividend

Fees accrue on the position and are only realised when something touches
it. Every `harvest_interval_hours` (default 24) the bot harvests them into
the wallet, once at least `min_harvest_usd` (default 0.25) has accrued, and
reports `DIVIDEND` with the book. A rebalance harvests too and resets the
clock. Realised fees are the income: they are in the wallet, they do not move
with the price, and no later rebalance can give them back.

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
for migration in sql/*.sql; do psql -v ON_ERROR_STOP=1 -d rebalancer -f "$migration"; done
python3 db.py seed                          # a first profile, SOL/USDC
python3 db.py add wif-usdc <pool> capital_usd=200   # describe another pool
python3 db.py config                        # show the active profile
python3 db.py set sol-usdc capital_usd=250  # retune, then restart the bot
python3 db.py activate wif-usdc             # switch pools
```

| group | columns |
|---|---|
| what to trade | `dex`, `pool`; `pair_label`, `token_a`, `token_b` for display, filled by `add` |
| the board | `dexes`, `scan_limit`, `scan_interval_seconds`, `min_volume_24h_usd` |
| the move | `migrate_min_gain`, `execute_dexes`, `pool_pinned`, `allow_swap`, `defer_moves_to_quiet_hours` |
| size | `capital_usd`, `max_usd`, `gas_reserve_sol`, `side_cap_fraction` |
| band search | `bands` (the ladder), `max_modelled_rebal_per_day`, `swap_cost_bps` |
| cadence | `poll_seconds`, `min_rebalance_gap_seconds`, `max_rebalances_per_day`, `reopt_interval_seconds`, `reopt_min_gain` |
| acting early | `proactive_horizon_hours`, `proactive_threshold` |
| the dividend | `harvest_interval_hours`, `min_harvest_usd` |
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
| `positions` | one row per position ever opened, with its DEX, band, deposit and withdrawal |
| `scan_runs`, `scan_pools` | every board ever scanned: each pool's score, band, screen verdict, or the reason it was not scored |
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

### The band

Below the book, the survival block for the held band:

```
━━ BAND ━━
price       8.6% above the floor · 6.6% below the ceiling · alive 23h
P(exit)     6h 0%   24h 14%   72h 30%   7d 45%
life        median > 7d · vol 1.02x normal (614 origins)
if closed   locks 0.06% vs holding · price +1.22% since open
rule        act at P(exit ≤6h) ≥ 50% · now 0% → hold
```

`P(exit)` is one minus the conditional survival at each horizon, regime
matched. `life` is the median remaining lifetime of the band from here.
`if closed` is what a re-centre now would make permanent: the position's
value against holding 50/50 since the open, at this price. `rule` is the
proactive rule's current verdict. `python3 db.py` prints the same figures
from the last snapshot.

### By pool, and rent

The bot moves between pools and DEXes, so one running total says nothing
about which venue paid. `python3 db.py pools` splits the book: for every pool
ever held, the days there, fees realised and unrealised, fee APR on the
capital that sat there, position P&L (what came out against what went in),
and the share of polls in range. The Telegram book carries the same lines
when more than one pool has been held.

Equity counts rent. A Meteora DLMM position stores per-bin data, so a ±8%
band on a 4 bp pool is a 38 KB account holding 0.2 SOL of rent, refunded on
close. Every signer reports `rentSol`/`rentUsd` on `status` and the mark adds
it; before it did, the first move to Meteora read as a $27 loss that never
happened. At close, the mark taken just before is recorded as the
withdrawal, so per-pool P&L exists without a second read.

### Guards

`guards.py` is checked at the moment money is about to move, after the
tests and independent of them: a signer argument must be printable and
short, a signer script must live in the bot's directory, a pool must be a
base58 address, an open's band must contain the LIVE price the wallet read
and the model's price must agree with it within 3%, the caps must sit under
the capital and the ceiling, and a move's target must be a known DEX with an
armed signer and two distinct mints. A refusal is reported as `open_refused`
and counted like a failed open.

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

`REOPT` runs the board and band review on the next poll instead of waiting
for the interval. `MIGRATE`, containing `<dex> <pool>`, closes here and opens
there on the next poll, through the same gates as an automatic move: the DEX
must have a signer named in `execute_dexes`.

`REBALANCE` runs harvest → close → re-optimise → reopen on the next poll, under
the same minimum-gap and per-day limits as an automatic one, and is deleted
before it runs so a failure cannot loop on it. It exists because the rebalance
path is the one that runs unattended, and a path that has only run at 3am has
never been watched.

---

## Files

| file | what it does |
|---|---|
| `rebalancer.py` | the loop: read, decide, harvest, close, re-optimise, move pools, reopen |
| `engine.py` | the band simulator, the board scorer, the token screen |
| `dexes.py` | one pool record from five DEX APIs and their on-chain accounts |
| `scanner.py` | the board thread and its CLI |
| `dlmm_probe.mjs` | reads the bins around the active bin of Meteora pools |
| `db.py` | Postgres: configuration, accounting, the board, statistics, the CLI |
| `config.py` | loads the active profile, with environment overrides |
| `signer2.mjs` | Orca chain I/O and signing, on `@orca-so/whirlpools` v8 |
| `signer_dlmm.mjs` | Meteora DLMM chain I/O and signing, on `@meteora-ag/dlmm` 1.9; same commands and fields |
| `signer_raydium.mjs`, `signer_byreal.mjs`, `signer_pancake.mjs` | the same for Raydium CLMM, Byreal and PancakeSwap V3 |
| `swap_jupiter.mjs` | Jupiter swaps, for moving the wallet between pairs; `SWAP_HOOK.md` says where the loop will call it |
| `SIGNER_CONTRACT.md` | what every signer must accept and print |
| `telegram_bridge.mjs` | forwards `events.jsonl` to Telegram |
| `sql/001_schema.sql` | the schema, idempotent |
| `sql/002_any_pool.sql` | migration for databases created before the pool-agnostic sizing |
| `sql/003_multi_dex.sql` | the board tables and the pool-move parameters |
| `sql/004_seasonality.sql` | the hour-of-day profile on each scan and the quiet-hours switch |
| `sql/005_proactive.sql` | the proactive rule, the dividend schedule, and the forecast columns on snapshots |
| `sql/006_calm.sql` | tight CALM bands, their move budget, and pre-open swaps |
| `sql/007_fee_accounting.sql` | pending fees in equity and cumulative fee observations for UTC-day attribution |
| `ops/*.service` | systemd units |
| `tests/` | the test suite, below |

---

## Tests

```sh
createdb rebalancer_test
for migration in sql/*.sql; do psql -v ON_ERROR_STOP=1 -d rebalancer_test -f "$migration"; done
tests/run.sh                                        # offline suites
WALLET_SECRET_PATH=/path/to/key tests/run.sh        # plus the live signer reads
```

Four suites, 91 tests, about 20 seconds. They run against a database whose
name ends in `_test` and refuse to run against anything else, because the
ledger tests truncate tables.

| suite | what it proves |
|---|---|
| `test_engine` | the exit probability is walk-forward and monotone in distance; the proactive rule re-centres before the edge on a drift and never on a flat path; the band forecast fires at the ceiling and not at the centre; the CLMM arithmetic round-trips; a position at its edge has lost to holding; fees scale with concentration; flat, stepped and trending paths give the answers known in advance; Kaplan-Meier respects censoring; wider bands survive longer; the ladder refuses a pool whose candles disagree with its price |
| `test_rebalancer` | deposit sizing on a flush wallet, a short wallet, a native-B pool and a BTC-quoted pool; the gas reserve comes off the native side only; rate-limit dumps tidy to one line; signer output parses through bindings noise; a timeout is an error, not a crash |
| `test_db` | one active profile; parameters validate and the sanity constraint bites; `add` fills the pair from Orca and refuses adaptive-fee pools; a full lifecycle — open, accrue, harvest, close, reopen — keeps TOTAL monotonic and counts a harvest once; token amounts survive exactly |
| `test_signer_live` | read-only against mainnet: pool descriptions, both balances on a dollar-quoted and a BTC-quoted pool, status, dry-run open and close that build real instructions, refusal of adaptive-fee pools and of positions over the cap |

Three of these tests found bugs on the day they were written: `daily` had a
SQL syntax error and had never run; a harvest was counted as both realised and
unrealised until the next poll, so $0.26 read as $0.51; and the quote-token
price on SOL/cbBTC came back as the price of SOL, because GeckoTerminal orders
a pair by its own convention. Tokens are now priced by mint.

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

`@meteora-ag/dlmm` 1.9 for the Meteora signer and the bin probe. Its ESM build
imports a directory and fails to load under Node 24; both scripts load the
CommonJS build through `createRequire`.
