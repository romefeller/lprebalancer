Analysis date: 2026-09-25. Source revision: `cdca06f`.

The completed five-minute experiments are in [the research report](income_experiment/REPORT.md).


The objective is recurring fee income. The user wants to spend 50% of that income and reinvest 50% into LP positions.
Slow equity growth is acceptable. Beating a token-holding benchmark is not a requirement.
Inventory losses are acceptable when fee income and retained earnings support the intended distributions and remaining capital.
The proposed strategy narrows bands during calm periods and widens them when conditional exit risk increases.

The first version gave terminal wealth and comparison with holding too much priority.
This revision treats spendable income as the objective. Capital retention is a supporting constraint.

This document applies the asd-ste100 skill's plain-language rules. It does not claim compliance with the official ASD dictionary.
The work includes source review, an archived market sample, and reproducible calculations. It does not change the trading engine or its configuration.

The following terms have distinct meanings:

| Term | Meaning in this analysis |
|---|---|
| Volatility | The size of return fluctuations over a stated interval. |
| Conditional variance | The variance forecast from information available now. |
| Heteroskedasticity | Variance that changes with time or market conditions. |
| Volatility velocity | A defined measure of how fast estimated volatility changes. This is a proposed feature, not a standard trading signal. |
| Volatility of volatility | The variability of changes in volatility. This differs from their signed direction. |
| Survival | The probability that price does not cross either band boundary before a stated horizon. |
| IL | Impermanent loss. The position's value shortfall against holding its original token quantities, before fees. |
| LVR | Loss versus rebalancing. A different benchmark that measures the cost of trading against better-informed arbitrageurs. |

**The project already contains part of the design.**

The engine computes trailing volatility, historical exit probabilities, Kaplan–Meier survival, and a rolling band replay.
The rebalancer can act before an exit. It can compare pools across venues.

| Existing component | What it does | Relevant limit |
|---|---|---|
| `engine.py:294`, `trailing_vol()` | Computes RMS hourly log returns over 24 hours. | It measures volatility level. It does not forecast variance changes. Its initial window uses zero padding. |
| `engine.py:300`, `p_exit()` | Uses completed historical paths and similar volatility levels. | It uses a 720-origin window. It silently uses all origins when the matched sample is small. |
| `engine.py:476`, `conditional_survival()` | Estimates survival for the current distances to both boundaries. | It uses a different sample and estimator from the replay. |
| `engine.py:594`, `ladder()` | Scores fixed widths across historical windows. | It does not evaluate a policy that changes width with the current regime. |
| `rebalancer.py:410`, `consider_migration()` | Compares candidate pool scores. | A relative score improvement does not establish that migration repays its costs. |
| `db.py:604`, `forecasts()` | Compares recorded probabilities with observed exits. | It drops unresolved observations. Policy-driven closures can bias the remaining sample. |

The current `sigma_24h_pct` field means hourly sigma estimated from 24 hours. It does not mean a 24-hour return standard deviation.
Future fields should state both the return interval and the estimation window.

**The sample does not yet establish a profitable volatility pattern.**

I fetched 1,000 hourly candles for Orca SOL/USDC from the same public API that the engine uses.
The calculation excludes the last candle because it can be incomplete.
The remaining 999 candles run from August 15, 05:00 UTC through September 25, 19:00 UTC. These timestamps identify candle starts.
The sample contains no gaps between hourly candles.

The first 70% supplies the training data. The held-out period starts on September 13, 08:00 UTC.
Each label asks whether price crosses a fixed band during the next six hours.
Training labels end before the held-out period. The calculation uses 663 training origins and 294 held-out origins.
These origins overlap. They are not 294 independent experiments.

The band convention is `[P/k, P*k]`. A `k=1.03` band has a 3% upper distance and a 2.91% lower distance.

| Band multiplier | Six-hour exits from closes | Six-hour exits from highs/lows | Exits missed by closes |
|---|---:|---:|---:|
| 1.01 | 58.84% | 84.69% | 76 of 294 origins |
| 1.03 | 10.20% | 17.69% | 22 of 294 origins |
| 1.05 | 1.70% | 3.40% | 5 of 294 origins |

These are observed frequencies for hypothetical bands. They are not current forecasts or estimates of continuous fee occupancy.
Highs and lows detect crossings that hourly closes miss. They do not identify the complete trade sequence within a candle.

The sample's lag-one correlation of squared hourly returns is 0.183. This is consistent with volatility clustering, but does not prove trading value.
The final hourly sigma estimate is 0.552%. Its six-hour log velocity is -0.0169 per hour.
That velocity implies an approximately 9.6% decline in estimated sigma over six hours. It does not imply a 9.6% price decline.

I compared three simple forecasts on the same held-out labels.
The volatility model splits origins at the training median sigma.
The velocity model divides each volatility group into rising and falling sigma.
Each group receives 20 pseudo-observations at the unconditional training exit frequency to reduce sparse-sample effects.

| Band multiplier | Unconditional Brier score | Volatility groups | Volatility plus velocity groups |
|---|---:|---:|---:|
| 1.01 | 0.12970 | 0.12999 | 0.13074 |
| 1.03 | 0.14698 | 0.15129 | 0.15657 |
| 1.05 | 0.03355 | 0.03360 | 0.03443 |

A lower Brier score means a smaller mean squared probability error.
Neither simple conditional model improves the score in this sample.
This result rejects this particular simple rule as evidence for deployment. It does not reject every volatility model.
The experiment does not estimate fees, execution costs, or strategy profit.

This experiment does not directly test the proposed calm-period policy.
Its six-hour outcomes span all conditions in the held-out period.
Its coarse groups do not isolate low volatility, low instability, adequate volume, and a short review interval together.
A band can have poor aggregate six-hour survival and still be useful during selected calm periods.
The next experiment must test that conditional strategy and include the cost of widening afterward.

The archived data, audit script, and results accompany this document:

- [Raw API response](sol_usdc_2026-09-25.json).
- [Reproducible calculation](profile_audit.py).
- [Calculated results](profile_audit_2026-09-25.json).

Run the calculation from `lp_bot`:

```sh
python3 research/profile_audit.py research/sol_usdc_2026-09-25.json
```

**The volatility profile should describe several different risks.**

Let `r_t = log(P_t/P_(t-1))`. Use the pair's price ratio, with a fixed token orientation.
For a pair A/B, USD volatility of A alone does not describe the relevant price risk.
The variance of the relative log return is `Var(r_A) + Var(r_B) - 2 Cov(r_A,r_B)`.

Estimate the following features from data available at the decision time:

| Feature | Proposed definition | Purpose |
|---|---|---|
| Variance forecast | `v_(t+1) = lambda*v_t + (1-lambda)*r_t^2` | Provides a simple EWMA baseline. |
| Volatility level | `sigma_t = sqrt(v_t)` | Measures the current scale of price fluctuations. |
| Volatility velocity | `g_t = [log(sigma_t)-log(sigma_(t-m))]/(m*Delta)` | Distinguishes increasing and decreasing volatility. |
| Volatility instability | Trailing standard deviation of `Delta log(sigma)` | Identifies unstable variance estimates even when average velocity is near zero. |
| Drift | A regularized forecast of relative returns | Identifies directional pressure. A noisy recent trend is insufficient evidence. |
| Jump pressure | Frequency and size of large standardized returns | Captures moves that ordinary variance forecasts can understate. |
| Pool conditions | Active liquidity, LP fee growth, volume, and reference-price deviation | Separates price risk from the pool's earning opportunity. |
| Data quality | Observation age, missing intervals, and sample support | Prevents stale or unsupported estimates from permitting tighter bands. |

Here, `Delta` is the observation interval in hours. Use a documented floor for sigma before taking logarithms.
Compare a fast EWMA with a slower EWMA. Select decay parameters on earlier data.
Do not treat statistical significance in an ARCH test as a direct trading threshold.

After the baseline, test a GARCH(1,1) model with heavy-tailed innovations:

`v_(t+1) = omega + alpha*epsilon_t^2 + beta*v_t`.

GARCH models forecast conditional variance. Simulation or residual bootstrap can produce future return paths.
The `arch` documentation describes these methods and their horizon limits. [Source](https://arch.readthedocs.io/en/latest/univariate/forecasting.html).

Keep the simpler model unless the more complex model improves calibration and net results on later data.
Simulate joint price and fee conditions. High volatility and high volume often occur together, so independent forecasts can misprice the opportunity.

**Survival should estimate risk from the current state.**

Define `tau` as the first future crossing of either fixed boundary.
Estimate `S_t(h) = Pr(tau > h | information available at t)`.
The conditioning state should include boundary distances, variance forecasts, volatility velocity, drift, jump pressure, and data quality.

For each candidate band, simulate future paths from that state. Record each path's first crossing time and crossing side.
Use the same forecast function in research and live decisions.
Test simulated distributions against empirical survival estimates and observed future crossings.

Survival is a probability distribution. It is not a countdown.
Subtracting position age from the historical median lifetime does not estimate remaining life.
For a fixed cohort model, conditional survival after age `a` is `S(a+h)/S(a)`, where `S(a)>0`.
A live position also requires its current boundary distances and updated market state.
The existing `hours_alive` field only reports age. It does not condition the forecast.

Report exit probabilities at operational horizons, a supported median, and restricted mean survival over a fixed horizon.
Report sample support and uncertainty with each estimate. An unreached median does not mean infinite survival.

Under a driftless continuous Brownian model, a centered log band with half-width `w` has expected exit time `w^2/sigma^2`.
This is a model identity, not a market guarantee.
Halving width divides expected survival by four. Doubling sigma also divides expected survival by four.
Jumps, drift, and delayed execution can make this approximation optimistic.

A practical band constraint is `Pr(exit before h + D) <= epsilon`.
Here, `h` is the planned review interval. `D` includes observation delay, decision time, confirmation time, and recovery time.
Estimate `D` from actual operations. Integrate over its distribution or use a conservative delay estimate.
Choose `epsilon` as an explicit risk budget, then test calibration.
Finite bands under unpredictable price changes cannot guarantee continuous survival.

**The policy should compare complete actions.**

For each action, estimate cumulative spendable fee income under the 50/50 distribution policy.
Also estimate remaining productive capital after distributions, reinvestment, price changes, and execution costs.
Maximize expected spendable income subject to a minimum capital requirement and a limit on severe capital depletion.
Use a sufficiently long planning horizon to value future income capacity.
Short decision intervals do not require a short economic objective.

For proposed accounting, reserve operating costs before splitting available fee income.
Let `F` be realized fee value, `C` be operating costs, and `D=max(F-C,0)`.
Then `spend=0.5*D` and `reinvest=0.5*D`.
Costs above fees reduce remaining capital. Record that reduction separately.
Reinvestment means an actual deposit into LP liquidity. A fee harvest alone does not perform that deposit.

An illustrative $10,000 position earns $1,000 in fees and incurs $100 in operating costs during one period.
It distributes $450 and reinvests $450 under this convention.
If its inventory loses $300 during that period, ending capital is $10,150, alongside the $450 distribution.
The example ignores additional flows and assumes all operating costs are already included.
Inventory loss does not by itself invalidate an income strategy.

Report gross fee APR, net income APR, spendable income, reinvested fees, and remaining equity separately.
A 100% gross fee APR is a target, not an observed outcome in this analysis.
Comparison with holding is a diagnostic. It is not the acceptance criterion.
Do not subtract IL or LVR again if exact inventory accounting already includes the corresponding loss.

The candidate actions should include:

1. Keep the current position.
2. Recenter at the same width.
3. Change width, with optional asymmetry after separate validation.
4. Change pool or fee tier for the same token pair.
5. Change venue for the same pair.
6. Change pair under explicit inventory limits.
7. Withdraw liquidity and retain specified tokens or convert to the permitted reserve asset.

Compare the actual held inventory with each candidate. Replaying a new centered position at the held width is not the same comparison.
Withdrawing liquidity alone retains token exposure. Converting that exposure adds a trade and its costs.

| Observed state | Candidate policy to test |
|---|---|
| Low volatility, stable variance, sufficient net fees | Permit tighter bands if survival and cost constraints pass. |
| Low volatility, rapidly rising variance | Prevent further narrowing. Compare an early wider band with retaining the current position. |
| High volatility, stable variance | Compare a wider band with withdrawal. High fees can still justify some liquidity. |
| High volatility, falling variance | Require sustained evidence before narrowing. Falling volatility can remain high. |
| Strong trend, jumps, or unstable variance | Reduce exposure or withdraw when no candidate passes the loss limit. |
| Same pair, different pool economics | Migrate only when the expected benefit exceeds total transition cost and uncertainty. |

Use different entry and exit thresholds to prevent repeated switching near a boundary.
A risk limit should take precedence over a preference for quiet trading hours.

**Test a dedicated policy for calm periods.**

The sequence is calm conditions, a narrow band, fee collection, rising exit risk, and a wider band.
Treat time of day as a feature. Confirm calm conditions from current observations before narrowing.
Pool-specific activity can differ from the average nightly pattern.

1. Detect low short-term volatility, low volatility instability, and no rapid increase in volatility.
2. Check that remaining trade volume can produce enough fees at the proposed width.
3. Estimate survival for a 1% band over a short review interval plus execution delay.
4. Compare incremental fee income with the costs of narrowing, rebalancing, and eventually widening.
5. Enter the narrow band when this complete cycle improves expected income within the capital constraints.
6. Refresh the forecast from new pool observations while the position remains open.
7. Widen when the narrow band loses its advantage or its conditional exit risk exceeds the selected limit.

Test review horizons such as 5, 15, 30, and 60 minutes. These are research candidates, not calibrated settings.
Minute data or pool events are necessary to evaluate those horizons.
Hourly candles cannot establish the profitability of this short-interval policy.

Low volatility helps survival. Remaining trading activity supplies fees.
The target state combines limited price movement with sufficient fee-generating volume.
Measure execution costs during that same state. Low volatility alone does not establish cheap swaps.

Survival can trigger an earlier review or widening action. It does not specify an exact future exit time.
Refresh the probability estimate instead of waiting for a historical median lifetime.

For migration, require a positive conservative estimate of:

`net benefit of moving versus staying > transition cost + uncertainty margin`.

Transition cost includes swaps, price impact, priority fees, failed attempts, account costs, and income lost during downtime.
Treat refundable rent separately from permanent expense. Use current executable quotes for the intended size.
An illustrative extra income of $0.20 per day needs two days to repay a $0.40 transition cost.
A market regime that lasts two hours cannot support that migration on these assumptions.

Same-pair Orca and Raydium positions share much of their price risk.
Their active liquidity, fee allocation, order flow, and execution reliability can differ.
Use the fee amount that reaches LPs. Raydium documents a split between LPs and protocol recipients. [Source](https://docs.raydium.io/raydium/protocol/protocol-fees).

**Remaining inside the band does not remove inventory loss.**

The engine's own CLMM formulas give a direct counterexample.
Start with $1,000 at price 100, with boundaries 95.238 and 105.
At price 102, the position remains inside the band and holds value $1,007.95 before fees.
Holding the original tokens gives $1,010.00. The position already trails holding by $2.05.
These figures use the concentrated-liquidity inventory equations. [Source](https://app.uniswap.org/whitepaper-v3.pdf).

Fees can offset this difference. Early rebalancing cannot retroactively remove it.
Closing changes the future exposure. A later recovery remains possible, but a new band follows a different strategy.

LVR describes another source of underperformance: arbitrageurs trade against stale AMM prices.
Remaining in range does not prevent that process. [Source](https://arxiv.org/abs/2208.06046).

For the ideal continuous diffusion model, local LVR rate is `0.5*(-V''(P))*P^2*sigma^2`.
For a centered CLMM band, this implies a fractional rate `C(k)*sigma^2/8`, where `C(k)=1/(1-1/sqrt(k))`.
This derivation assumes an active band and excludes fees, jumps, and discrete execution.
The README's variance-divided-by-eight rule omits concentration when applied to a centered narrow band.
Use this relation as a diagnostic. Use exact inventory accounting for the strategy comparison.

**Resolve these implementation gaps before a narrow-band trial.**

| Priority | Finding | Required change |
|---|---|---|
| 1 | `candles()` discards highs and lows. Survival treats array steps as hours without checking timestamp gaps. | Preserve timestamps and OHLC data. Add finer pool observations. Reject unsupported time intervals. |
| 1 | Live survival and replay use different estimators and samples. A synthetic check gives 4.7% versus 5.70% for the same six-hour question. | Use one causal estimator and one fallback policy. Record when a fallback occurs. |
| 1 | `band_forecast()` creates only 6/24/72/168-hour probabilities. A 12-hour configuration returns no probability and does not act. | Calculate the configured horizon or reject it explicitly. |
| 1 | Replay immediately reopens at a fixed width. Live code reoptimizes width, limits action frequency, and sometimes defers action. | Replay the full controller, including delays, failures, inventory limits, and policy changes. |
| 1 | `SWAP_HOOK.md` describes a swap integration that `reopen()` does not call. A registered Jupiter signer alone does not integrate it. | Model actual available balances. Complete and verify inventory conversion before relying on cross-pair migration or full redeployment after exits. |
| 1 | `choose()` ignores the churn limit when every candidate fails it. | Allow an explicit no-action or withdrawal result. Do not silently relax a hard risk limit. |
| 2 | The fee replay uses current pool concentration and TVL across historical candles. | Archive liquidity and fee growth over time. Reconcile estimates with actual LP receipts. |
| 2 | The fee share is an approximation and can exceed 100% for sufficiently large capital or narrow bands. | Use position liquidity divided by total active liquidity, including the proposed position. Model each venue's fee allocation. |
| 2 | `realised_check()` multiplies total net return by a fee adjustment. This can make negative returns less negative. | Adjust fee income separately from inventory P&L and costs. |
| 2 | The hourly tape can remain cached after repeated fetch failures. | Record observation age and disable unsupported narrow-band decisions. |
| 2 | Pool comparisons include different quote tokens but lack historical USD marks for those quotes. | Use consistent capital units and time-specific USD valuations for cross-pair wealth comparisons. |

CLMM inventory equations do not establish an exact Meteora DLMM replay.
Bin inventory, fee allocation, and dynamic fees need their own simulation before using DLMM scores as equivalent execution forecasts.

**Validate prediction and profit separately.**

Use chronological training, tuning, and untouched test periods. Remove training outcomes that extend into a later evaluation period.
Use blocks for uncertainty estimates because adjacent origins share price moves.
Do not count the same pair across venues as independent price evidence.

Compare fixed widths, the existing controller, volatility-only control, and volatility-plus-velocity control.
Evaluate each addition separately. Refit only with information available at each decision time.

Track spendable income, reinvested fees, remaining capital, survival calibration, Brier scores, tail loss, action count, and execution costs.
Compare calm-period income with income from retaining a wider band over the same periods.
Include the complete transition cycle and the 50/50 distribution rule in every strategy replay.
Brier scores evaluate probability accuracy. Censoring-aware versions require assumptions about the censoring process. [Source](https://scikit-survival.readthedocs.io/en/stable/user_guide/evaluating-survival-models.html).

Continue observing hypothetical fixed boundaries after a real position closes.
This provides price-crossing labels without selecting only the positions that the policy chose to retain.
It does not reveal the counterfactual fees or execution result of retaining those positions.

Run the new policy in observation mode before it controls capital.
Accept it only if later data shows calibrated risk and a net benefit after realistic costs.
The 38 existing engine tests pass. They validate existing cases, not the proposed strategy's profitability.
