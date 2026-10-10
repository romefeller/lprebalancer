// Jupiter's keyed API (venues/jupiter/api.mjs): the base URL and headers by
// environment, and every script's Jupiter fetch carrying them.
import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import VENUE from '../venues/jupiter/venue.json' with { type: 'json' };
import { jupiterBase, jupiterHeaders } from '../venues/jupiter/api.mjs';

const KEY = 'k'.repeat(68);

test('a key selects the keyed API; no key, or an empty one, the free API', () => {
  assert.equal(jupiterBase({ [VENUE.key_env]: KEY }), VENUE.keyed_api);
  assert.equal(jupiterBase({}), VENUE.free_api);
  assert.equal(jupiterBase({ [VENUE.key_env]: '' }), VENUE.free_api);
});

test('the key is added as a header, and the headers given are not changed', () => {
  const base = { accept: 'application/json' };
  assert.deepEqual(jupiterHeaders(base, { [VENUE.key_env]: KEY }), { accept: 'application/json', [VENUE.key_header]: KEY });
  assert.deepEqual(base, { accept: 'application/json' });
  assert.deepEqual(jupiterHeaders(base, {}), base);
  assert.notEqual(jupiterHeaders(base, {}), base);                 // a copy: a caller cannot leak a key into the shared object
});

test('every Jupiter fetch of every script sends the key headers', () => {
  const files = ['venues/raydium_clmm/signer.mjs', 'venues/meteora_dlmm/signer.mjs', 'venues/pancakeswap_v3/signer.mjs',
                 'venues/byreal/signer.mjs', 'venues/jupiter/swap.mjs'];
  for (const f of files) {
    const src = fs.readFileSync(new URL(`../${f}`, import.meta.url), 'utf8');
    assert.ok(!src.includes('lite-api.jup.ag'), `${f}: no hard-coded Jupiter URL`);
    const calls = src.split("\n").filter(l => /fetch[A-Za-z]*\(`\$\{JUPITER\}/.test(l) || /return jfetch\(url,/.test(l));
    assert.ok(calls.length > 0, f);
    for (const l of calls) assert.match(l, /jupiterHeaders\(/, `${f}: ${l.trim()}`);
  }
});
