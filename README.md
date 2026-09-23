# Aperture

An autonomous rebalancer for concentrated liquidity on Orca whirlpools.

It holds one position, watches the price against the position's band, and when
the price leaves, it collects the fees, closes, re-optimises the band, and opens
again. It signs its own transactions. It reports everything to Telegram, and it
keeps a ledger so that the money it has earned survives every rebalance.

The name is the point: a camera aperture is a band you choose. Narrow it and
more light lands on less of the frame. Widen it and you capture more of the
scene, dimly. This picks that setting, repeatedly, from data.

## The one decision it makes

Concentrated liquidity earns fees only while the price sits inside the band you
chose. Narrow the band and your capital works harder — a ±3% band concentrates
about 68 times as hard as full range, so it earns roughly 68 times the fees per
dollar. But the price leaves a narrow band sooner, and every exit costs a swap,
realises the loss the position took getting there, and is one more chance for a
transaction to fail.

There is no clean formula for the optimum once rebalancing is path-dependent, so
Aperture does not use one. For every candidate band it replays the pool's own
hourly price and volume through exact concentrated-liquidity arithmetic,
tracking real token balances across rebalances, and keeps the band with the best
net return per day. It re-runs that every six hours and moves only when the
improvement clearly repays the round trip.

It will also refuse a band that churns. A band promising the highest yield at
one rebalance per day is a band promising a daily chance of a failed
transaction, and failed transactions are how this loses money. See
`MAX_REBALANCES_PER_DAY_MODELLED` in `config.py`.

## What it is not

It is not a way to earn without taking risk. An LP is short gamma: you are paid
for selling volatility, and the pools that pay most are the ones whose prices
move most. A SOL/USDC position is roughly half long SOL — if SOL falls 30%, the
position falls with it and the fees will not cover that.

Measured on this wallet: SOL/USDC fees ran about 0.22%/day at ±12%, while simply
holding 50/50 beat the LP by 13% over a rallying six weeks. Fee income is real.
It is not free, and it is not market-neutral.

## Running it

```sh
# see the configuration
python3 config.py

# the current position, straight from the chain
WALLET_SECRET_PATH=/path/to/key node signer2.mjs status

# cumulative accounting
python3 ledger.py
python3 ledger.py history

# run it
sudo systemctl start lp-bot          # the rebalancer
sudo systemctl start lp-kmnbot       # the Telegram bridge

# stop it, from anywhere, immediately
touch HALT
```

`HALT` is absolute: the loop exits on its next cycle and refuses to start again
until the file is removed.

## Using it on a different pool

Nothing here is specific to SOL/USDC. Token decimals, prices, fee tiers and
liquidity all come from the chain. To point it somewhere else:

```sh
LPBOT_POOL=<whirlpool address> LPBOT_PAIR=WIF/USDC python3 rebalancer.py
```

or set them in the service unit. Every tunable in `config.py` reads an
environment variable first, so a deployment retunes without editing code.

Two constraints on the pool you choose:

- **Adaptive-fee pools cannot be opened by this path.** Check
  `adaptiveFeeEnabled` on the pool before committing. ZEC/USDC is one, and the
  failure mode is an instant rejection with Whirlpool error 6069 that reads
  like a slippage problem and is not one.
- **Thin pools move when you enter and vanish when you leave.** `MIN_TVL_USD`
  defaults to $250k for that reason.

## Files

| file | what it does |
|---|---|
| `rebalancer.py` | the loop: read, decide, harvest, close, reopen |
| `config.py` | every tunable, overridable by environment |
| `ledger.py` | SQLite accounting that survives rebalances |
| `engine.py` | pool scanning, band simulation, Jev token screening |
| `signer2.mjs` | all chain I/O and signing, on `@orca-so/whirlpools` v8 |
| `kmnbot_bridge.mjs` | forwards the event feed to Telegram |
| `ledger.sqlite` | positions, harvests, snapshots, events |
| `kmnbot_feed.jsonl` | append-only event log the bridge tails |

## Accounting

A position's fee counter belongs to the position, not to you. Harvest, close,
reopen, and it reads zero again — which is why a bot that reports the live
position's accrual claims to have earned nothing every time it rebalances.

`ledger.sqlite` fixes that. Fees are split into **realised** (harvested into the
wallet, permanent) and **unrealised** (accrued in the open position, resets when
you close). Every Telegram message that mentions money carries both plus their
total, so the number only ever goes up.

One subtlety worth knowing: Whirlpool only settles fee accounting when a
position is touched, so the position's own `feeOwed` fields read zero on a live,
earning position. Aperture reads the real figure from a close quote instead.

## What it reports

Every message carrying money shows the same block, so a rebalance can never make
the earnings look like they reset:

```
IN RANGE · 113.7400
band 108.7100 — 119.9000
━━ FEES ━━
today       0.000265 SOL   0.0332 USDC   $0.0469
realised    0 SOL          0 USDC        $0.0000
unrealised  0.000265 SOL   0.0332 USDC   $0.0635
TOTAL       0.000265 SOL   0.0332 USDC   $0.0635
━━ BOOK ━━
equity      $241.55   P&L +1.17
rate        $0.41/day   APR 62.1%
in range    100%   over 1.34d
activity    1 position · 0 rebands · 0 harvests
```

Fees are shown in both tokens because they are earned in both. A position pays
you token A and token B in whatever proportion the trading happened to take, so
a single dollar figure hides what you hold and moves with the price even in an
hour when you earned nothing.

- **today** — since 00:00 UTC, harvested plus accrued since this morning
- **realised** — harvested into the wallet. Permanent.
- **unrealised** — still in the position. Resets to zero when it closes.
- **TOTAL** — realised plus unrealised, since the ledger began. Only goes up.
- **APR** — annualised on equity actually at work, not on notional.

`python3 ledger.py` prints the same figures as JSON; `python3 ledger.py history`
prints the snapshot series.

## Safety

| guard | default |
|---|---|
| `HALT` file | stops everything, blocks restart |
| minimum gap between rebalances | 1 hour |
| rebalances per day | 6, then HALT |
| position size cap | `MAX_USD`, refused above it |
| SOL reserve | never spent — a wallet that cannot pay fees cannot close its own position |
| consecutive failures | 3, then HALT |
| unreadable polls | 12, then HALT |

Three failure modes it handles specifically, each of which cost real money
before it did:

**A failed write is not a known outcome.** A transaction can land and still
report failure, because the confirmation runs over the same rate-limited RPC
that just timed out. A close did exactly this: it succeeded, reported
`close_failed`, and the capital sat idle. After any write error Aperture
re-reads the chain and believes what it finds there.

**A failed read is not an empty wallet.** An RPC error once made the bot
conclude it held no position — and its response to holding no position is to
open one. On a flaky endpoint that repeats until the wallet is empty. Reads that
fail now hold; only a read that succeeds and reports nothing may open.

**Raw errors are not messages.** Provider errors arrive as a nested dump of
headers and cookies with a status code buried inside. Forwarded verbatim they
made a healthy Telegram bridge look broken. They are now summarised to one line.

## Requirements

Node 18+, Python 3.9+, `@orca-so/whirlpools` v8 (the legacy
`@orca-so/whirlpools-sdk` cannot open positions — every attempt fails with error
6069, and 0.22.0 is its final release), and a funded Solana keypair whose path
is given by `WALLET_SECRET_PATH`. The key is read by the signer at runtime and
never logged.

Token screening through Jev is optional and only consulted when the scanner
picks the pool rather than a pinned one. It exists because the highest-yielding
pool on the board advertised 243%/yr and its second asset was 3x leveraged SOL —
a token engineered to decay, which no volatility statistic flags.
