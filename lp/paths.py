"""This profile's files: runtime state, the event feed, the operator triggers."""

import json
import pathlib
from datetime import datetime, timezone

import config
import engine

ROOT = pathlib.Path(__file__).resolve().parent.parent   # the bot's root: lp/ lives in it
RUN = config.RUN_DIR            # run/<profile>: this profile's state, feed and triggers
STATE = RUN / 'runtime.json'
FEED = RUN / 'events.jsonl'
HALT = RUN / 'HALT'             # stops this profile
HALT_ALL = ROOT / 'HALT'        # stops every profile, and every signer (they check it themselves)
REBALANCE = RUN / 'REBALANCE'
REOPT = RUN / 'REOPT'           # run the board and band review on the next poll
MIGRATE = RUN / 'MIGRATE'       # "<dex> <pool>": move there on the next poll
CLOSE = RUN / 'CLOSE'           # a disabled profile: harvest and close its position, open nothing
engine.use_network(config.CAPS['gecko_network'])


def stamp():
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def halted():
    """The text of the HALT that stops this profile (the global one first),
    or None."""
    for f in (HALT_ALL, HALT):
        if f.exists():
            return f.read_text().strip() or str(f)
    return None


STATE_DEFAULTS = {'last_rebalance': 0, 'rebalance_times': [], 'failures': 0,
                  'read_failures': 0, 'last_reopt': 0, 'last_harvest': 0,
                  'calm_times': [], 'calm': False}


def load():
    """runtime.json, with every key the loop indexes present. A corrupt file
    is set aside (runtime.json.corrupt) and the loop starts from defaults: a
    crash loop on a bad file is worse than forgetting the rebalance clock."""
    if STATE.exists():
        try:
            s = json.loads(STATE.read_text())
            if not isinstance(s, dict):
                raise ValueError('not an object')
            return {**STATE_DEFAULTS, **s}
        except Exception:
            try:
                STATE.replace(STATE.with_suffix('.json.corrupt'))
            except Exception:
                pass
    return dict(STATE_DEFAULTS)


def save(s):
    STATE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE.with_suffix('.tmp')
    tmp.write_text(json.dumps(s, indent=1, default=str))
    tmp.replace(STATE)
