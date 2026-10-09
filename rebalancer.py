"""Rebalancer — a concentrated-liquidity rebalancer across Solana DEXes.

Holds one position, watches the price against its band, and when the price
leaves, collects the fees, closes, re-optimises the band and opens again. Every
number it reports comes from the chain or from its own ledger, never from a
simulation of what it thinks happened.

It also optimises the pool, not only the band. A scanner thread lists the
busiest pools on every DEX in the profile's `dexes`, scores each under the same
replay the band optimiser uses, and writes the board to Postgres. At each
re-optimisation the loop compares the pool it holds with the best pool on the
board; when the board's best beats it by `migrate_min_gain` and its DEX has a
signer (`execute_dexes`), the bot closes here and opens there. When it has no
signer for that DEX it says so, with the command to move by hand.

Every parameter comes from the `rebalancer.config` table and every observation
goes back into Postgres, so the pool, the band ladder and the guards are data,
not code.

    read chain -> in band?  yes -> forecast: P(exit within H hours) from the pool's own tape
                                   high -> harvest, close, re-centre NOW (quiet hour if one is near)
                                   low  -> record a snapshot, wait
                            no  -> harvest, close, re-optimise, reopen

The bot acts before the price leaves, not after. A rebalance at the edge makes
the position's whole loss against holding permanent and leaves it one-sided
and earning nothing until it runs; a re-centre while still inside is done at
a price of the bot's choosing, and the survival figures that drive it are in
every book it sends. Accrued fees are harvested into the wallet on a schedule
(the dividend), so income is realised and permanent rather than a number on
an open position.

Three things it does that a simpler loop gets wrong:

**A failed write is not a known outcome.** A transaction can land and still
report failure, because confirmation runs over the same rate-limited RPC that
just timed out. This happened in production: a close succeeded, reported
`close_failed`, and the bot left the capital idle. So after any write error the
bot re-reads the chain and believes what it finds there, not the error.

**A failed read is not an empty wallet.** Treating an RPC error as "no position"
makes a bot open a second one, and on a flaky endpoint it keeps opening them
until the wallet is empty. Reads that fail hold; only a read that succeeds and
reports nothing may open.

**Fees belong to you, not to the position.** A position's fee counter resets
every time you rebalance. Cumulative earnings live in the ledger, split into
realised (harvested into the wallet) and unrealised (still in the position).

One process runs one profile (LPBOT_PROFILE). Its runtime files and the
operator triggers live in run/<profile>/:

Stop this profile:              touch run/<profile>/HALT
Stop every profile and signer:  touch HALT
Force one rebalance now with:   touch run/<profile>/REBALANCE
Force the pool and band review: touch run/<profile>/REOPT
Move to a pool by hand:         echo "<dex> <pool>" > run/<profile>/MIGRATE

Profiles that share a wallet share its tokens through sleeves (wallets.py):
every write runs under the wallet's lock, and the wallet read the loop sizes
from is the profile's own sleeve, not the whole wallet.

The trigger exists because the rebalance path is the one that runs unattended,
and a path that has only ever run at 3am has never been watched. Touch the file
while you are looking and the next poll runs harvest -> close -> reopen at the
best band, subject to the same gap and daily limits as an automatic one.


The loop and everything it calls live in lp/, one module per job (lp/loop.py
names them). This file is the service's entry point (ops/lp-bot@.service)."""
import sys

from lp import loop

if __name__ == '__main__':
    sys.exit(loop.main() or 0)
