// onboard/web/wallet_link.mjs: after each WalletConnect request the page
// brings the owner's wallet app forward. The link comes from the session's
// peer metadata, its own deep link first; a wallet the page knows by name
// gets its known link; anything else, a bare wc: link. A link that could run
// script in the page is never one.
import test from 'node:test';
import assert from 'node:assert/strict';
import fc from 'fast-check';
import { KNOWN, safeLink, walletLink } from '../onboard/web/wallet_link.mjs';

test('the wallet\'s own deep link first, then its universal link, then a known wallet, then wc:', () => {
  assert.deepEqual(walletLink({ name: 'Tangem', redirect: { native: 'tangem://', universal: 'https://app.tangem.com/wc' } }),
    { href: 'tangem://', kind: 'native', name: 'Tangem' });
  assert.deepEqual(walletLink({ name: 'Tangem', redirect: { universal: 'https://app.tangem.com/wc' } }),
    { href: 'https://app.tangem.com/wc', kind: 'universal', name: 'Tangem' });
  assert.deepEqual(walletLink({ name: 'Tangem Wallet' }), { href: 'tangem://', kind: 'known', name: 'Tangem Wallet' });
  assert.deepEqual(walletLink({ name: 'Ledger Live', redirect: {} }), { href: 'ledgerlive://', kind: 'known', name: 'Ledger Live' });
  assert.deepEqual(walletLink({ name: 'Phantom' }), { href: 'phantom://', kind: 'known', name: 'Phantom' });
  assert.deepEqual(walletLink({ name: 'Some Wallet' }), { href: 'wc:', kind: 'wc', name: 'Some Wallet' });
  assert.deepEqual(walletLink(undefined), { href: 'wc:', kind: 'wc', name: 'your wallet' });
  assert.deepEqual(walletLink({}), { href: 'wc:', kind: 'wc', name: 'your wallet' });
  // a wallet's own link wins over the page's knowledge of its name
  assert.equal(walletLink({ name: 'Tangem', redirect: { native: 'tangemwc://open' } }).href, 'tangemwc://open');
  assert.ok(KNOWN.every(k => safeLink(k.href) === k.href));
});

test('links that could run in the page, or are not links, are never offered', () => {
  for (const bad of ['javascript:alert(1)', 'JavaScript:alert(1)', ' javascript:x', 'data:text/html,x', 'vbscript:x', 'file:///etc/passwd',
    'blob:x', 'about:blank', 'http://evil.example', '/relative', '', null, undefined, 'no scheme', `x${'a'.repeat(600)}://`]) {
    assert.equal(safeLink(bad), null, String(bad));
    const l = walletLink({ name: 'Unknown', redirect: { native: bad, universal: bad } });
    assert.equal(l.kind, 'wc', String(bad));
  }
  // at most 512 characters
  const at = (n) => `tangem://${'a'.repeat(n - 'tangem://'.length)}`;
  assert.equal(safeLink(at(512)), at(512));
  assert.equal(safeLink(at(513)), null);
  // a universal link is https only
  assert.equal(walletLink({ name: 'X', redirect: { universal: 'ftp://x' } }).kind, 'wc');
  assert.equal(walletLink({ name: 'X', redirect: { universal: 'https://x.example/wc' } }).kind, 'universal');
  // a long name is cut, never trusted as markup (the page sets it as text)
  assert.equal(walletLink({ name: 'n'.repeat(200) }).name.length, 64);
  fc.assert(fc.property(fc.string(), fc.string(), fc.string(), (name, native, universal) => {
    const l = walletLink({ name, redirect: { native, universal } });
    assert.ok(['native', 'universal', 'known', 'wc'].includes(l.kind));
    assert.ok(!/^\s*(javascript|data|vbscript|file|blob|about|http):/i.test(l.href), l.href);
    if (l.kind === 'native') assert.equal(l.href, native.trim());
  }), { numRuns: 500 });
});
