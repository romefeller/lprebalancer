"""The tape prune with several processes on one database.

2026-10-02: each process deleted every other pool's bars older than a day.
mu-usdc's refresh left sol-usdc 287 of its 8640 bars, and a sol-usdc restart
in that window would have decided its width on a one-day tape. Every
profile's pool now stays; a pool no profile uses still goes after a day."""
import time
import unittest

import _fixtures  # noqa: F401  (first: it puts lp_bot on the path)
import db

POOLS = {'tp-sol': 'TPsolPOOLxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx', 'tp-mu': 'TPmuPOOLxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx'}
GONE = 'TPgonePOOLxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx'
DAY = 86400


def cleanup():
    with db.cursor(commit=True) as cur:
        cur.execute('delete from tape5 where pool = any(%s)', (list(POOLS.values()) + [GONE],))
        cur.execute('delete from config where name = any(%s)', (list(POOLS),))


class Prune(unittest.TestCase):
    def setUp(self):
        cleanup(); self.addCleanup(cleanup)
        now = int(time.time()) // 300 * 300
        with db.cursor(commit=True) as cur:
            for name, pool in POOLS.items():
                cur.execute("insert into config (name, pool, pair_label, capital_usd, max_usd, enabled) "
                            "values (%s, %s, 'X/USDC', 100, 1000, %s)", (name, pool, name == 'tp-sol'))
        self.old, self.new = now - 10 * DAY, now - 300
        for pool in list(POOLS.values()) + [GONE]:
            db.tape_store(pool, ([self.old, self.new], [1, 1], [1, 1], [1, 1], [1, 1], [0, 0]), self.old)

    def bars(self, pool):
        with db.cursor() as cur:
            cur.execute('select ts from tape5 where pool = %s order by ts', (pool,))
            return [r['ts'] for r in cur.fetchall()]

    def test_every_profiles_tape_stays_whoever_prunes(self):
        for pool in POOLS.values():                                   # each process in turn
            db.tape_prune_other_pools(db.config_pools() | {pool}, time.time() - DAY)
        for pool in POOLS.values():
            self.assertEqual(self.bars(pool), [self.old, self.new])  # the disabled profile's too

    def test_a_pool_no_profile_uses_goes_after_a_day(self):
        db.tape_prune_other_pools(db.config_pools() | {POOLS['tp-mu']}, time.time() - DAY)
        self.assertEqual(self.bars(GONE), [self.new])

    def test_config_pools_names_each_pool_once(self):
        pools = db.config_pools()
        self.assertTrue(set(POOLS.values()) <= pools)
        self.assertNotIn(GONE, pools)


if __name__ == '__main__':
    unittest.main()
