"""An open's leftover goes into the position where the venue can add (owner,
2026-10-08: "almost 99% in LP, idle money is bad"; the same strategy on every
pool but the DJT swing test): rebalancer.adds_open_leftover and its use in
deploy_idle.

Covered: the venues that add (Raydium, Polygon's Uniswap) deploy the first
reading of a fresh band, not excuse it; the swing and the venues that cannot
add keep it excused; an add waits for no move gap and spends no move budget,
but the venue's breaker and the band's age hold it; adds have their own daily
cap; what an add leaves out is excused, so the next poll adds nothing."""
import datetime as dt
import unittest
from unittest import mock

import _fixtures  # noqa: F401
import config
import rebalancer
from test_deploy_all import bal
from test_deploy_idle import Hook, NOW

# SOL/USDC after a +/-1.25% open on 2026-10-08: $27 beside a $198 band, $6 of it the gas reserve
LEFTOVER = dict(bal(0.059 + 0.10, 9.0, price=119.5), walletUsd=(0.059 + 0.10) * 119.5 + 9.0)


class Leftover(Hook):
    def go_dex(self, dex, swing=(), state=None, venue_ok=True, budget=5, allowed=True, opened_min=30, wbal=LEFTOVER,
               deploys=None):
        added, moves, books, events = [], [], [], []
        st = {} if state is None else state
        if deploys is not None:
            st['idle_deploys'] = deploys

        class FakeDT(dt.datetime):
            @classmethod
            def now(cls, tz=None):
                return NOW
        opened = None if opened_min is None else NOW - dt.timedelta(minutes=opened_min)
        with mock.patch.object(config, 'DEX', dex), mock.patch.object(config, 'SWING_POOLS', tuple(swing)), \
                mock.patch.object(rebalancer, 'datetime', FakeDT), \
                mock.patch.object(rebalancer.db, 'position_opened', lambda m: opened), \
                mock.patch.object(rebalancer.db, 'event', lambda *a: events.append(a)), \
                mock.patch.object(rebalancer, 'calm_budget_left', lambda s: budget), \
                mock.patch.object(rebalancer, 'voluntary_move_allowed', lambda s: allowed), \
                mock.patch.object(rebalancer.health, 'allowed',
                                  lambda key, now: ((venue_ok if key.startswith('venue:') else True), None, 0, {})), \
                mock.patch.object(rebalancer, 'notify', lambda ev, **kw: books.append((ev, kw))), \
                mock.patch.object(rebalancer, 'notify_book', lambda ev, **kw: books.append((ev, kw))), \
                mock.patch.object(rebalancer, 'add_idle', lambda *a: (added.append(a) or True)), \
                mock.patch.object(rebalancer, 'rebalance', lambda *a, **k: moves.append((a, k))):
            out = rebalancer.deploy_idle(st, dict(self.STATUS), wbal, {'choice': 1.0125}, 119.5)
        return out, added, moves, st, books

    def test_which_profiles_add_the_leftover(self):
        for dex, swing, want in (('raydium-clmm', (), True), ('uniswap-v3-polygon', (), True),
                                 ('raydium-clmm', ('A', 'B'), False), ('orca', (), False),
                                 ('uniswap-v3-unichain', (), False), ('aerodrome-slipstream', (), False)):
            with mock.patch.object(config, 'DEX', dex), mock.patch.object(config, 'SWING_POOLS', swing):
                self.assertIs(rebalancer.adds_open_leftover(), want, (dex, swing))

    def test_sol_usdc_adds_the_open_leftover(self):
        out, added, moves, st, _ = self.go_dex('raydium-clmm')
        self.assertTrue(out)
        self.assertEqual(moves, [])                                         # an add, not a re-centre
        self.assertEqual(len(added), 1)
        self.assertAlmostEqual(added[0][3], 0.10 * 119.5 + 9.0, places=1)   # the leftover above the reserve
        self.assertEqual(st['idle_baseline'], {'mint': 'M', 'usd': 0.0})   # nothing excused

    def test_polygon_adds_it_too(self):
        out, added, _, _, _ = self.go_dex('uniswap-v3-polygon')
        self.assertTrue(out); self.assertEqual(len(added), 1)

    def test_the_swing_and_venues_without_increase_keep_it_excused(self):
        for dex, swing in (('raydium-clmm', ('A', 'B')), ('orca', ()), ('meteora-dlmm', ())):
            out, added, moves, st, _ = self.go_dex(dex, swing=swing)
            self.assertFalse(out, dex)
            self.assertEqual((added, moves), ([], []), dex)
            with mock.patch.object(config, 'DEX', dex):
                want = rebalancer.deployable_usd(LEFTOVER)               # Meteora also keeps its rent headroom
            self.assertGreater(want, 10.0)
            self.assertAlmostEqual(st['idle_baseline']['usd'], want, places=3)

    def test_an_add_needs_no_move_gap_nor_budget(self):
        out, added, _, _, _ = self.go_dex('raydium-clmm', budget=0, allowed=False)
        self.assertTrue(out); self.assertEqual(len(added), 1)
        # a re-centre venue still needs both
        self.assertFalse(self.go_dex('orca', state={'idle_baseline': {'mint': 'M', 'usd': 0.0}}, budget=0)[0])
        self.assertFalse(self.go_dex('orca', state={'idle_baseline': {'mint': 'M', 'usd': 0.0}}, allowed=False)[0])

    def test_the_venue_breaker_and_the_band_age_still_hold_an_add(self):
        self.assertEqual(self.go_dex('raydium-clmm', venue_ok=False)[1], [])
        self.assertEqual(self.go_dex('raydium-clmm', opened_min=5)[1], [])                 # under IDLE_MIN_AGE_S
        self.assertEqual(self.go_dex('raydium-clmm', opened_min=None)[1], [])

    def test_a_small_leftover_stays(self):
        tiny = dict(bal(0.059 + 0.01, 1.0, price=119.5), walletUsd=(0.059 + 0.01) * 119.5 + 1.0)   # $2.20
        self.assertEqual(self.go_dex('raydium-clmm', wbal=tiny)[1], [])

    def test_adds_have_their_own_daily_cap(self):
        now = NOW.timestamp()
        with mock.patch.object(rebalancer.time, 'time', lambda: now):
            self.assertEqual(len(self.go_dex('raydium-clmm', deploys=[now - 60] * 23)[1]), 1)
            out, added, _, _, books = self.go_dex('raydium-clmm', deploys=[now - 60] * 24)
            self.assertIs(out, False); self.assertEqual(added, [])
            self.assertIn('24 idle deploys', [kw for ev, kw in books if ev == 'deploy_idle_deferred'][0]['reason'])
            # re-centre venues keep 3
            st = {'idle_baseline': {'mint': 'M', 'usd': 0.0}}
            self.assertEqual(self.go_dex('orca', state=st, deploys=[now - 60] * 3)[2], [])
        self.assertEqual(rebalancer.idle_deploys_left([1.0] * 3, 2.0), 0)
        self.assertEqual(rebalancer.idle_deploys_left([1.0] * 3, 2.0, 24), 21)
        self.assertEqual(rebalancer.idle_deploys_left([1.0], 86401.0, 24), 24)            # a day old: gone

    def test_what_an_add_leaves_out_is_not_added_again(self):
        # add_idle excuses its own remainder: the next poll sees nothing new
        st = {'idle_baseline': {'mint': 'M', 'usd': round(0.10 * 119.5 + 9.0, 4)}}
        out, added, moves, _, _ = self.go_dex('raydium-clmm', state=st)
        self.assertFalse(out); self.assertEqual((added, moves), ([], []))


    def test_unreadable_or_unpriced_wallets_do_nothing(self):
        for wb in ({'walletUsd': 30.0}, dict(LEFTOVER, walletUsd=None)):
            out, added, moves, _, _ = self.go_dex('raydium-clmm', wbal=wb)
            self.assertIs(out, False); self.assertEqual((added, moves), ([], []))
        with mock.patch.object(rebalancer, 'deployable_usd', lambda b: None):
            out, added, moves, st, _ = self.go_dex('raydium-clmm')
        self.assertIs(out, False); self.assertEqual((added, moves), ([], []))
        self.assertNotIn('idle_baseline', st)                                  # nothing valued, nothing written

    def test_a_failing_swap_breaker_defers_the_add_alone(self):
        def allowed(key, now):
            return (False, None, 600, {'fails': 3, 'last_fail': 'x'}) if key == 'swap' else (True, None, 0, {})
        with mock.patch.object(rebalancer.health, 'allowed', allowed):
            st = {}
            class FakeDT(dt.datetime):
                @classmethod
                def now(cls, tz=None):
                    return NOW
            added, books = [], []
            with mock.patch.object(config, 'DEX', 'raydium-clmm'), mock.patch.object(config, 'SWING_POOLS', ()), \
                    mock.patch.object(rebalancer, 'datetime', FakeDT), \
                    mock.patch.object(rebalancer.db, 'position_opened', lambda m: NOW - dt.timedelta(minutes=30)), \
                    mock.patch.object(rebalancer.db, 'event', lambda *a: None), \
                    mock.patch.object(rebalancer, 'notify', lambda ev, **kw: books.append((ev, kw))), \
                    mock.patch.object(rebalancer, 'add_idle', lambda *a: (added.append(a) or True)):
                out = rebalancer.deploy_idle(st, dict(self.STATUS), LEFTOVER, {'choice': 1.0125}, 119.5)
        self.assertIs(out, False); self.assertEqual(added, [])
        self.assertIn('the swap failed 3x', books[0][1]['reason'])

    def test_the_deploy_list_keeps_24_hours(self):
        now = NOW.timestamp()
        with mock.patch.object(rebalancer.time, 'time', lambda: now):
            _, added, _, st, _ = self.go_dex('raydium-clmm', deploys=[now - 86400, now - 86399, now - 200000])
        self.assertEqual(len(added), 1)
        self.assertEqual(st['idle_deploys'], [now - 86399, now])                # a day old and older: dropped


if __name__ == '__main__':
    unittest.main()
