"""Time spans and tuning numbers shared by the loop."""

# --- time spans and tuning constants ---------------------------------------
HOUR_S = 3600
DAY_S = 86400
SIGNER_TIMEOUT_S = 420           # one signer call, sends and confirmations included
POOL_RECORD_TTL_S = HOUR_S       # a pool record is refetched after this (reward programs change)
BARS_PER_DAY = 288               # five-minute bars
GECKO_PAGE_BARS = 1000           # GeckoTerminal's OHLCV page
TAPE_REORIENT_JUMP = 0.15        # a stored tape this far from the live price is the other orientation: rebuild
# the touch threshold after the liquidity factor. The ceiling was 0.40; with the
# threshold at 0.60 (sql/046) the liquidity factor reaches 1.6 x 0.60 (2026-10-17)
REGIME_THETA_MIN, REGIME_THETA_MAX = 0.05, 0.96
LIQ_WINDOW_BARS = 72             # six hours of five-minute bars
TOUCH_CALIBRATION_EVERY_S = HOUR_S
HOT_PAUSE_MIN_COVERAGE = 0.8     # fee samples must span this share of the hot-pause window
R_SHADOW_HOURS = 3               # the shadow pause reads fees over in-band loss over this window (sql/056)
R_SHADOW_OUT = 1.0               # shadow OUT below this ratio ...
R_SHADOW_IN = 1.5                # ... and back IN above this one (hysteresis)
MOMENT_BAD_RATIO = 0.8           # a BAD fee moment: fees under this x the in-band loss over the shadow hours (sql/057)
MOMENT_REF_S = 600               # the 7-day percentiles behind the moment labels are re-read this often
MOMENT_MIN_REF = 288             # no GREAT/BAD label before this many reference polls (a day): a thin p90 labels noise
PENDING_REOPEN_MAX_S = DAY_S     # a CALM reopen intent older than this is dropped
PENDING_REOPEN_WIDE_S = 1800     # after this without fresh data, reopen at the widest width
BALANCED_SIDE_SHARE = 0.97       # a side holding this share of its target needs no swap
BALANCED_SPREAD = 0.04           # nor two sides this close in value
REWARD_PRICE_MAX_AGE_S = 6 * HOUR_S
REOPEN_MAX_BAR_AGE_S = 900       # a tight reopen needs a bar this fresh
BUSY_HOUR_MAX_P_EXIT = 0.9       # in a busy hour, a proactive move waits unless the exit is this likely
REOPT_RETRY_S = 1800             # a deferred re-optimisation comes back after this, not a full interval
