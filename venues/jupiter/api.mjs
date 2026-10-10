// Jupiter's API for every script: the keyed endpoint when the environment holds
// a key (venue.json key_env), else the free one. The key travels in a header,
// never in a URL, so a logged URL never shows it.
import VENUE from './venue.json' with { type: 'json' };

// The base URL for `env`. Pure.
export function jupiterBase(env = process.env) {
  return env[VENUE.key_env] ? VENUE.keyed_api : VENUE.free_api;
}

// `headers` plus the key header when `env` holds a key. Pure.
export function jupiterHeaders(headers = {}, env = process.env) {
  const key = env[VENUE.key_env];
  return key ? { ...headers, [VENUE.key_header]: key } : { ...headers };
}

export const JUPITER = jupiterBase();
