"""Accounting and information-timing checks for the research simulator."""
import unittest
import numpy as np
from simulator import run, amounts, bounds, unit_value
from models import features, Model

class ResearchTests(unittest.TestCase):
    def bars(self,n=900,volume=1e6):
        a=np.zeros((n,6)); a[:,0]=np.arange(n)*300+1780000200
        a[:,1:5]=100; a[:,5]=volume
        return a

    def test_flat_zero_fees_no_costs_preserves_capital(self):
        b=self.bars(volume=0)
        r=run(b,features(b),None,0,len(b),{'name':'fixed','mode':'fixed','k':1.03},
              bps=0,fixed=0)
        self.assertAlmostEqual(r['ending_capital'],10000)
        self.assertEqual(r['paid'],0)
        self.assertEqual(r['rebalances'],0)

    def test_flat_fee_accounting_and_half_split(self):
        b=self.bars()
        r=run(b,features(b),None,0,len(b),{'name':'fixed','mode':'fixed','k':1.03},
              bps=0,fixed=0)
        self.assertAlmostEqual(r['paid'],r['retained_fees'],places=7)
        self.assertAlmostEqual(r['ending_capital']+r['paid']-10000,r['gross_fees'],places=6)

    def test_costs_are_not_counted_twice(self):
        b=self.bars()
        r=run(b,features(b),None,0,len(b),{'name':'fixed','mode':'fixed','k':1.03},
              bps=20,fixed=.5)
        self.assertAlmostEqual(r['paid'],r['retained_fees'],places=6)
        self.assertAlmostEqual(r['ending_capital']+r['paid']-10000,
                               r['gross_fees']-r['costs'],places=5)

    def test_unpaid_costs_reduce_principal_once(self):
        b=self.bars(volume=0)
        r=run(b,features(b),None,0,len(b),{'name':'fixed','mode':'fixed','k':1.03},
              bps=10,fixed=.5)
        self.assertAlmostEqual(r['ending_capital'],10000-r['costs'],places=6)
        self.assertAlmostEqual(r['unrecovered_costs'],r['costs'])
        self.assertEqual(r['paid'],0)

    def test_bounds_and_inventory(self):
        lo,hi=bounds(100,1.01)
        self.assertLessEqual(lo,100/1.01); self.assertGreaterEqual(hi,101)
        L=10000/unit_value(100,lo,hi)
        x,y=amounts(L,100,lo,hi)
        self.assertAlmostEqual(x*100+y,10000)
        self.assertEqual(amounts(L,lo-1,lo,hi)[1],0)
        self.assertEqual(amounts(L,hi+1,lo,hi)[0],0)

    def test_future_data_does_not_change_past_features_or_training(self):
        b=self.bars(4000)
        rng=np.random.default_rng(7)
        p=100*np.exp(np.cumsum(rng.normal(0,.001,len(b))))
        b[:,1:5]=p[:,None]; b[:,2]*=1.0002; b[:,3]*=.9998
        f=features(b); m=Model(b,f,2500)
        altered=b.copy(); altered[3001:,1:5]*=2; altered[3001:,5]*=20
        f2=features(altered); m2=Model(altered,f2,2500)
        for k in f:
            np.testing.assert_allclose(f[k][:3001],f2[k][:3001])
        self.assertEqual(m.config(),m2.config())
        self.assertEqual(m.probability(3000,6,.01,.01),m2.probability(3000,6,.01,.01))
        self.assertLessEqual(m.probability(3000,6,.02,.02),m.probability(3000,6,.01,.01))

    def test_action_latency_and_downtime(self):
        b=self.bars(20,volume=0); b[2:,1:5]=104
        r=run(b,features(b),None,0,len(b),{'name':'fixed','mode':'fixed','k':1.01},
              bps=0,fixed=0,latency=2,downtime=2,keep_path=True)
        # Exit observed at 2. Close at 4. Open at 6.
        rows={int((x[0]-b[0,0])/300):x for x in r['path']}
        self.assertEqual(rows[5][5],0)
        self.assertEqual(rows[6][5],1)
        self.assertEqual(r['rebalances'],1)

if __name__=='__main__':
    unittest.main()
