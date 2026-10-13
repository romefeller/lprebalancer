// Where to send the owner so the wallet app shows a WalletConnect request
// (a phone's wallet does not come forward by itself, 2026-10-13 with Tangem).
// From the session's peer metadata: its own deep link (redirect.native), else
// its universal link (redirect.universal), else a wallet this page knows by
// name, else a bare wc: link, which opens whichever wallet app handles
// WalletConnect. Pure.
//
//   walletLink(peerMetadata) -> {href, kind: 'native' | 'universal' | 'known' | 'wc', name}

// Wallets whose deep link is known when their metadata names none.
export const KNOWN = [
  { match: /tangem/i, href: 'tangem://' },
  { match: /ledger/i, href: 'ledgerlive://' },
  { match: /phantom/i, href: 'phantom://' },
];

// A link a page may open: a scheme of letters (a wallet's own), or https.
// Anything else (javascript:, data:, a relative path) is no link.
export function safeLink(href) {
  const s = String(href ?? '').trim();
  if (!s || s.length > 512) return null;
  const m = /^([a-z][a-z0-9+.-]*):/i.exec(s);
  if (!m) return null;
  const scheme = m[1].toLowerCase();
  if (['javascript', 'data', 'vbscript', 'file', 'blob', 'about', 'http'].includes(scheme)) return null;
  return s;
}

export function walletLink(metadata) {
  const name = String(metadata?.name ?? '').slice(0, 64) || 'your wallet';
  const r = metadata?.redirect ?? {};
  const native = safeLink(r.native);
  if (native) return { href: native, kind: 'native', name };
  const universal = safeLink(r.universal);
  if (universal && /^https:\/\//i.test(universal)) return { href: universal, kind: 'universal', name };
  const known = KNOWN.find(k => k.match.test(name));
  if (known) return { href: known.href, kind: 'known', name };
  return { href: 'wc:', kind: 'wc', name };
}
