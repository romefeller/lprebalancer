"""Describe one archived OHLCV sample. Do not trade or access the database.

Run: python3 research/profile_audit.py research/sol_usdc_2026-09-25.json
The final candle is excluded because it can be incomplete.
"""
import json
import math
import pathlib
import sys
from datetime import datetime, timezone

import numpy as np

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import engine


def analyze(path):
    raw = json.loads(pathlib.Path(path).read_text())
    bars = np.asarray(sorted(raw['data']['attributes']['ohlcv_list']), dtype=float)[:-1]
    if len(bars) < 300 or not np.all(np.diff(bars[:, 0]) == 3600):
        raise ValueError('The audit requires at least 300 consecutive hourly candles.')
    if not np.all(np.isfinite(bars)) or np.any(bars[:, 1:5] <= 0):
        raise ValueError('The audit requires finite values and positive prices.')
    prices = bars[:, 4]
    logp = np.log(prices)
    returns = np.diff(logp)
    sigma = engine.trailing_vol(logp)
    velocity = np.full(len(prices), np.nan)
    velocity[30:] = np.log(sigma[30:] / sigma[24:-6]) / 6
    split = int(len(prices) * 0.7)
    horizon = 6
    train = np.arange(30, split - horizon)
    test = np.arange(split, len(prices) - horizon)
    vol_cut = float(np.median(sigma[train]))
    level = (sigma >= vol_cut).astype(int)
    rising = (velocity > 0).astype(int)
    state = level * 2 + rising
    iso = lambda t: datetime.fromtimestamp(t, timezone.utc).isoformat()
    out = {
        'scope': 'Descriptive sample and one chronological holdout. Not a profit backtest.',
        'first_candle_start': iso(bars[0, 0]),
        'last_candle_start': iso(bars[-1, 0]),
        'holdout_first_candle_start': iso(bars[split, 0]),
        'completed_candles': len(bars),
        'train_origins': len(train), 'test_origins': len(test),
        'training_median_hourly_sigma_pct': vol_cut * 100,
        'last_hourly_sigma_pct': float(sigma[-1] * 100),
        'last_log_sigma_velocity_per_hour': float(velocity[-1]),
        'squared_return_lag1_correlation': float(np.corrcoef(returns[:-1] ** 2, returns[1:] ** 2)[0, 1]),
        'method': '24h RMS hourly returns. Velocity = log(sigma[t]/sigma[t-6])/6. '
                  'First 70% for training. Training labels end before holdout. '
                  '20 pseudo-observations shrink each bucket toward training exit frequency. '
                  'Origins overlap. Scores have no independence claim or confidence interval.',
        'bands': [],
    }
    for k in (1.01, 1.03, 1.05):
        close_hit = np.zeros(len(prices), dtype=bool)
        range_hit = np.zeros(len(prices), dtype=bool)
        for i in np.r_[train, test]:
            future = bars[i + 1:i + horizon + 1]
            lo, hi = prices[i] / k, prices[i] * k
            close_hit[i] = np.any((future[:, 4] < lo) | (future[:, 4] > hi))
            range_hit[i] = np.any((future[:, 3] < lo) | (future[:, 2] > hi))
        y = range_hit.astype(float)
        base = float(y[train].mean())
        scores = {'unconditional': float(np.mean((y[test] - base) ** 2))}
        for name, groups in (('volatility', level), ('volatility_and_velocity', state)):
            pred = np.full(len(test), base)
            for group in np.unique(groups):
                sample = train[groups[train] == group]
                prob = (y[sample].sum() + 20 * base) / (len(sample) + 20)
                pred[groups[test] == group] = prob
            scores[name] = float(np.mean((y[test] - pred) ** 2))
        cells = []
        for group, name in enumerate(('low_falling', 'low_rising', 'high_falling', 'high_rising')):
            sample = test[state[test] == group]
            cells.append({'state': name, 'origins': len(sample),
                          'exit_rate': float(y[sample].mean()) if len(sample) else None})
        out['bands'].append({
            'k': k, 'horizon_hours': horizon,
            'test_close_exit_rate': float(close_hit[test].mean()),
            'test_high_low_exit_rate': float(y[test].mean()),
            'test_exits_missed_by_closes': int(np.sum(range_hit[test] & ~close_hit[test])),
            'brier_lower_is_better': scores, 'holdout_states': cells,
        })

    rng = np.random.default_rng(2)
    synthetic = 100 * np.exp(np.cumsum(rng.normal(0, .01, 1500)))
    args = (synthetic, 100, 100 / 1.05, 105)
    live6 = engine.band_forecast(*args, horizon=6, threshold=.5)
    live12 = engine.band_forecast(*args, horizon=12, threshold=.5)
    M, m = engine.forward_extrema(np.log(synthetic), 6)
    replay6 = engine.p_exit(M, m, len(synthetic) - 1, math.log(1.05), math.log(1.05),
                           6, vol24=engine.trailing_vol(np.log(synthetic)))
    liquidity = engine.liquidity_for(1000, 100, 100 / 1.05, 105)
    x, y = engine.amounts(liquidity, 102, 100 / 1.05, 105)
    out['synthetic_code_checks'] = {
        '12h_live_probability': live12['p_exit_horizon'], '12h_live_act': live12['act'],
        '6h_live_probability': live6['p_exit_horizon'], '6h_replay_probability': replay6,
        'in_band_example': {'initial_value': 1000, 'price_start': 100, 'price_end': 102,
                            'k': 1.05, 'lp_value_excluding_fees': x * 102 + y,
                            'hold_value': 1010, 'loss_vs_hold': 1010 - x * 102 - y},
    }
    return out


if __name__ == '__main__':
    print(json.dumps(analyze(sys.argv[1]), indent=2, allow_nan=False))
