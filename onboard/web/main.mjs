// The page at romefeller.app/bot (onboard/index.html). Built into
// onboard/bundle.js by build.mjs; it talks to its own server and, for
// WalletConnect, to the WalletConnect relay. Every transaction goes through
// the owner's wallet, and every one is decoded here first and checked
// against the pins built into this bundle (__PINS__: the bot's keys, the
// approved pools, the vault rules): a transaction that differs from what the
// owner asked for is refused before the wallet sees it.
//
// One vault per wallet per chain. The page shows, after the wallet connects:
//   create   no vault yet: amount, profit wallet, name -> the wallet approves
//            ONE set-up (its own vault, the deposit, the bot's gas; on
//            Solana this page's one-time set-up key signs the bot's rules,
//            too long for a hardware wallet), the bot finishes the vault
//            and starts
//   setup    a set-up that stopped: it resumes, nothing is paid twice
//   active   the bot runs: its book as Telegram shows it; Add capital,
//            Withdraw, Revoke
//   revoked  only Add capital, which gives the bot its rules back
import { SignClient } from '@walletconnect/sign-client';
import qrcode from 'qrcode-generator';
import bs58 from 'bs58';
import { Keypair, Transaction, VersionedTransaction, PublicKey } from '@solana/web3.js';
import RULES from '../../chains/solana/vault/rules.json' with { type: 'json' };
import SOLANA from '../../chains/solana/solana.json' with { type: 'json' };
import { decodeSol, decodeSolSet } from '../decode_sol.mjs';
import { answerOf, cardLimit, walletLink } from './wallet_link.mjs';
import { fromRaw, toRaw } from '../units.mjs';

/* global __PINS__ */
const PINS = __PINS__;
const SOL_CHAINS = ['solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp', 'solana:4sGjMW1sUnHzSxGspuhpqLDx6wiyjNtZ'];
const SOL_PINS = { payer: PINS.solana.payer, policyKey: PINS.solana.policyKey, operator: PINS.solana.operator, pools: PINS.solana.pools,
  rules: RULES, solana: SOLANA };

// --- small helpers ------------------------------------------------------------------------

const $ = (id) => document.getElementById(id);
const sleep = (ms) => new Promise(r => setTimeout(r, ms));
const api = (p) => new URL(`api/${p}`, location.href.replace(/[?#].*$/, '').replace(/\/?$/, '/')).toString();
const b64 = (bytes) => { let s = ''; for (const b of bytes) s += String.fromCharCode(b); return btoa(s); };
const unb64 = (s) => Uint8Array.from(atob(s), c => c.charCodeAt(0));
const short = (a) => (a && a.length > 14 ? `${a.slice(0, 6)}…${a.slice(-4)}` : a ?? '');
const usd = (x, d = 2) => (x == null ? '—' : `$${Number(x).toLocaleString('en-US', { minimumFractionDigits: d, maximumFractionDigits: d })}`);
const mobile = () => /Android|iPhone|iPad|iPod/i.test(navigator.userAgent);

function el(tag, attrs = {}, ...kids) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v === undefined || v === null || v === false) continue;
    if (k === 'text') e.textContent = v;
    else if (k.startsWith('on')) e[k] = v;
    else e.setAttribute(k, v === true ? '' : v);
  }
  for (const k of kids.flat()) if (k !== null && k !== undefined) e.append(k);
  return e;
}

const show = (id, on = true) => { $(id).hidden = !on; };
const VIEWS = ['v-connect', 'v-wc', 'v-loading', 'v-create', 'v-setup', 'v-active', 'v-revoked', 'v-sheet', 'v-flow'];
function view(id) {
  for (const v of VIEWS) show(v, v === id);
  document.body.dataset.view = id;
  window.scrollTo?.({ top: 0 });
}

function alertBox(msg, kind = 'error') {
  const a = $('alert');
  a.textContent = msg || '';
  a.className = `alert ${kind}`;
  a.hidden = !msg;
}

// An error as a person reads it: no stack, no JSON, the wallet's refusal named.
function human(e) {
  const m = String(e?.message ?? e ?? 'something went wrong');
  const refusal = e?.code === 4001 || e?.code === 5000;
  if (/reject|denied|declin|cancel|user (closed|abort)/i.test(m) || (refusal && !e?.message)) return 'You declined in your wallet. Nothing was sent.';
  // a wallet's own error under a refusal's code (a card that cannot sign): its words, never "you declined"
  if (refusal) return `Your wallet refused: ${m.split('\n')[0].trim().slice(0, 280)}. Nothing was sent.`;
  if (/Failed to fetch|NetworkError|Load failed/i.test(m)) return 'The server does not answer. Check your connection and try again.';
  if (/blockhash not found|block height exceeded|expired/i.test(m)) return 'The network moved on before it landed. Try again: a fresh transaction is built.';
  // a signer's error carries its decoded program error, then log lines: the first line says it
  const first = m.replace(/^Error:\s*/, '').replace(/\s+at\s.*$/s, '').split('\n').find(l => l.trim()) ?? m;
  return first.replace(/\s+/g, ' ').trim().slice(0, 300);
}

class Refusal extends Error {}

// --- the server ---------------------------------------------------------------------------

const fromHash = new URLSearchParams(location.hash.slice(1)).get('invite') || '';
const invite = () => fromHash || $('invite').value.trim() || (() => { try { return localStorage.getItem('aperture-invite') || ''; } catch { return ''; } })();

async function post(p, body) {
  let r;
  try {
    r = await fetch(api(p), { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ invite: invite(), ...body }) });
  } catch (e) { throw new Error('Failed to fetch'); }
  const j = await r.json().catch(() => ({ error: `the server answered ${r.status}` }));
  if (!r.ok) throw Object.assign(new Error(j.error || `the server answered ${r.status}`), { status: r.status });
  return j;
}

// --- wallets ------------------------------------------------------------------------------
// A wallet: {chain, kind, name, address, signMessage(text) -> {signature, signedMessage?},
// send(tx) -> signature, signAll?(txs) -> signed txs (Solana: every
// transaction of a set in one approval, base64)}. Injected wallets (Wallet
// Standard, Phantom's provider) and WalletConnect.

const standard = [];
const register = (...ws) => { for (const w of ws) standard.push(w); return () => {}; };
window.addEventListener('wallet-standard:register-wallet', (e) => e.detail?.({ register }));
window.dispatchEvent(new CustomEvent('wallet-standard:app-ready', { detail: { register } }));

const solStandard = () => standard.find(x => x.chains?.some(c => c.startsWith('solana:')) && x.features?.['solana:signMessage']
  && (x.features?.['solana:signAndSendTransaction'] || x.features?.['solana:signTransaction']));
const solInjected = () => solStandard() || window.phantom?.solana || window.solana;

async function broadcastSigned(bytes) {
  return (await post('broadcast', { chain: 'solana', transaction: b64(bytes) })).signature;
}

async function solanaInjected() {
  const w = solStandard();
  if (w) {
    const { accounts } = await w.features['standard:connect'].connect();
    const account = accounts.find(a => a.chains?.includes('solana:mainnet')) ?? accounts[0];
    if (!account) throw new Error('the wallet shared no account');
    return { chain: 'solana', kind: 'injected', name: w.name, address: account.address,
      async signMessage(text) {
        const [out] = await w.features['solana:signMessage'].signMessage({ account, message: new TextEncoder().encode(text) });
        return { signature: b64(out.signature), signedMessage: out.signedMessage ? b64(out.signedMessage) : undefined };
      },
      async send(tx) {
        const f = w.features['solana:signAndSendTransaction'];
        if (f) {
          const [out] = await f.signAndSendTransaction({ account, transaction: unb64(tx), chain: 'solana:mainnet' });
          return bs58.encode(out.signature);
        }
        const [out] = await w.features['solana:signTransaction'].signTransaction({ account, transaction: unb64(tx), chain: 'solana:mainnet' });
        return broadcastSigned(out.signedTransaction);
      },
      // every input in one call: the wallet asks once
      signAll: w.features['solana:signTransaction'] ? async (txs) => {
        const outs = await w.features['solana:signTransaction'].signTransaction(...txs.map(t => ({ account, transaction: unb64(t), chain: 'solana:mainnet' })));
        return outs.map(o => b64(o.signedTransaction));
      } : null,
      async disconnect() { await w.features['standard:disconnect']?.disconnect?.(); } };
  }
  const p = window.phantom?.solana ?? window.solana;
  if (!p?.signMessage) throw new Error('No Solana wallet in this browser. Open this page in Phantom, or use WalletConnect.');
  const r = await p.connect();
  return { chain: 'solana', kind: 'injected', name: 'Phantom', address: String(r?.publicKey ?? p.publicKey),
    async signMessage(text) { return { signature: b64((await p.signMessage(new TextEncoder().encode(text), 'utf8')).signature) }; },
    async send(tx) {
      const bytes = unb64(tx);
      const message = bytes.slice(1 + 64 * bytes[0]);
      return (await p.request({ method: 'signAndSendTransaction', params: { message: bs58.encode(message) } })).signature;
    },
    signAll: p.signAllTransactions ? async (txs) => (await p.signAllTransactions(txs.map(t => Transaction.from(unb64(t)))))
      .map(t => b64(t.serialize({ requireAllSignatures: false, verifySignatures: false }))) : null,
    async disconnect() { await p.disconnect?.(); } };
}

// --- WalletConnect ------------------------------------------------------------------------

let wcClient = null;
async function wcInit() {
  if (wcClient) return wcClient;
  const wc = state.cfg.walletConnect;
  wcClient = await SignClient.init({ projectId: wc.projectId, relayUrl: wc.relayUrl, telemetryEnabled: false,
    metadata: { name: 'Aperture', description: 'Your own vault for the LP bot', url: location.origin, icons: [] } });
  // no verify service the CSP allows: skip its attestation, which would wait out its timer on every request
  if (!wc.verify?.length) { wcClient.core.verify.register = async () => undefined; wcClient.core.verify.resolve = async () => undefined; }
  return wcClient;
}

const NAMESPACES = { solana: { chains: SOL_CHAINS, methods: ['solana_signMessage', 'solana_signTransaction', 'solana_signAllTransactions', 'solana_signAndSendTransaction'], events: [] } };

// The session's Solana account: [chainId, address] or null.
function sessionAccount(session) {
  const ns = session.namespaces?.solana;
  const acct = (ns?.accounts ?? []).find(a => SOL_CHAINS.some(c => a.startsWith(`${c}:`)));
  if (!acct) return null;
  const i = acct.lastIndexOf(':');
  return [acct.slice(0, i), acct.slice(i + 1)];
}

function wcWallet(client, session) {
  const acct = sessionAccount(session);
  if (!acct) throw new Error('The wallet shared no Solana account. Add Solana in the wallet app and connect again.');
  const [chainId, address] = acct;
  const methods = session.namespaces.solana.methods ?? [];
  // every request brings the wallet forward (walletCall): a phone's wallet app does not by itself
  const request = async (method, params) => {
    const call = client.request({ topic: session.topic, chainId, request: { method, params } });
    walletCall(session.peer?.metadata);
    try { return await call; } finally { walletCall(null); }
  };
  const name = session.peer?.metadata?.name || 'WalletConnect';
  return { chain: 'solana', kind: 'walletconnect', name, address, topic: session.topic,
      async signMessage(text) {
        const out = await request('solana_signMessage', { message: bs58.encode(new TextEncoder().encode(text)), pubkey: address });
        return { signature: b64(bs58.decode(out.signature)) };
      },
      // Sign here, send from the page: the path every wallet's setup takes. A
      // wallet's own send fails where its signature does not (Tangem's
      // signAndSend, 2026-10-14: the withdraw's second step), so the wallet
      // sends only when it offers no plain signing.
      async send(tx) {
        if (!methods.includes('solana_signTransaction')) {
          const out = answerOf(await request('solana_signAndSendTransaction', { transaction: tx }));
          if (out?.signature) return out.signature;
          throw new Error(`${name} answered without a signature. Check its activity; then try again here: nothing is paid twice.`);
        }
        const out = answerOf(await request('solana_signTransaction', { transaction: tx }));
        if (out?.transaction) return broadcastSigned(unb64(out.transaction));
        // Tangem's app sends a transaction its card cannot sign whole in another form, and answers no signature
        if (!out?.signature) throw new Error(`${name} answered without a signature. Check its activity; then try again here: nothing is paid twice.`);
        const t = Transaction.from(unb64(tx));
        t.addSignature(new PublicKey(address), Buffer.from(bs58.decode(out.signature)));
        return broadcastSigned(t.serialize());
      },
      // one request for the whole set, when the wallet offers it (else send() one by one)
      signAll: methods.includes('solana_signAllTransactions') ? async (txs) => {
        const out = answerOf(await request('solana_signAllTransactions', { transactions: txs }));
        const list = Array.isArray(out) ? out : out?.transactions;
        if (!Array.isArray(list) || list.length !== txs.length) throw new Error(`${name} did not return every transaction signed`);
        return list;
      } : null,
      async disconnect() { await client.disconnect({ topic: session.topic, reason: { code: 6000, message: 'disconnected' } }).catch(() => {}); } };
}

// A WalletConnect session: an earlier one this browser keeps, or a new one
// through a QR code (desktop) or a link that opens the wallet app.
async function walletConnect() {
  const client = await wcInit();
  const kept = client.session.getAll().filter(s => s.expiry * 1000 > Date.now() && sessionAccount(s));
  if (kept.length) return wcWallet(client, kept.at(-1));
  const { uri, approval } = await client.connect({ optionalNamespaces: NAMESPACES });
  view('v-wc');
  const qr = qrcode(0, 'M');
  qr.addData(uri);
  qr.make();
  $('wc-qr').src = qr.createDataURL(6, 12);
  $('wc-open').href = uri;
  $('wc-status').textContent = mobile() ? 'Tap "Open wallet app", approve in the wallet, then come back here.' : 'Scan the code with your wallet app, then approve there.';
  $('wc-copy').onclick = async () => {
    try { await navigator.clipboard.writeText(uri); $('wc-copy').textContent = 'Copied'; } catch { $('wc-copy').textContent = 'Copy failed'; }
    setTimeout(() => { $('wc-copy').textContent = 'Copy link'; }, 2000);
  };
  const cancelled = new Promise((_, no) => { $('wc-cancel').onclick = () => no(new Error('You cancelled the connection.')); });
  const session = await Promise.race([approval(), cancelled]);
  return wcWallet(client, session);
}

// The wallet is asked: on a phone, its app is brought forward (its deep link
// from the session, walletLink) and a big button opens it in case the browser
// did not; on a computer, the person is told to look at the phone. `null`:
// the answer came.
let calls = 0;
function walletCall(metadata) {
  const box = $('f-wallet');
  if (!box) return;
  if (!metadata) { if (--calls <= 0) { calls = 0; box.hidden = true; } return; }
  calls++;
  const link = walletLink(metadata);
  if (mobile()) {
    $('f-wallet-text').textContent = `${link.name} asks you to sign. If it did not open by itself, tap below, sign there, then come back here.`;
    $('f-open').href = link.href;
    $('f-open').textContent = `Open ${link.name} to sign`;
    $('f-open').hidden = false;
    // after the request left for the relay; a bare wc: link is only offered, never opened
    if (link.kind !== 'wc') setTimeout(() => { if (calls > 0) { try { window.location.href = link.href; } catch { /* the button stays */ } } }, 800);
  } else {
    $('f-wallet-text').textContent = `Check your phone: ${link.name} asks you to sign.`;
    $('f-open').hidden = true;
  }
  box.hidden = false;
}

// Back from the wallet app: the phone may have closed the relay's socket while
// the page was away. It reopens it, and the wallet's answer (the relay keeps it)
// comes in without a reload.
document.addEventListener('visibilitychange', () => {
  const r = wcClient?.core?.relayer;
  if (document.visibilityState !== 'visible' || !r || r.connected || r.connecting) return;
  r.restartTransport().catch(() => {});
});

// --- state and the flow runner -------------------------------------------------------------

const state = { cfg: null, chain: 'solana', wallet: null, vault: null, refresh: null, keysOk: {} };

function remember(k, v) { try { if (v === null) localStorage.removeItem(k); else localStorage.setItem(k, v); } catch { /* private mode */ } }
function recall(k) { try { return localStorage.getItem(k) || ''; } catch { return ''; } }

// A flow: numbered steps, each idempotent; a failed one can be tried again
// without paying or sending twice (each step checks what already happened).
class Flow {
  constructor(title, steps, { done, final } = {}) {
    this.title = title; this.steps = steps; this.data = {}; this.done = done; this.final = final;
  }

  render() {
    $('f-title').textContent = this.title;
    $('f-steps').replaceChildren(...this.steps.map((s, i) => {
      s.li = el('li', { class: 'step', 'data-state': s.state ?? 'todo' },
        el('span', { class: 'dot', text: String(i + 1) }),
        el('div', { class: 'what' }, el('div', { class: 'label', text: s.label }), s.detailEl = el('div', { class: 'detail muted small', text: s.detail ?? '' })));
      return s.li;
    }));
  }

  set(step, st, detail) {
    step.state = st;
    if (detail !== undefined) step.detail = detail;
    if (step.li) { step.li.dataset.state = st; step.detailEl.textContent = step.detail ?? ''; }
  }

  // Insert steps after the current one (the plan changed: the bot did not answer).
  insert(after, steps) { this.steps.splice(this.steps.indexOf(after) + 1, 0, ...steps); this.render(); }
  replaceRest(after, steps) { this.steps.splice(this.steps.indexOf(after) + 1, Infinity, ...steps); this.render(); }

  async run(from = 0) {
    view('v-flow');
    alertBox('');
    show('f-retry', false); show('f-alt', false); show('f-done', false); show('f-review', false); show('f-wallet', false);
    $('f-alt').className = '';
    // a resumed flow starts past what is already done
    for (const s of this.steps.slice(0, from)) if (!s.state || s.state === 'todo') s.state = 'done';
    this.render();
    for (let i = from; i < this.steps.length; i++) {
      const s = this.steps[i];
      if (s.state === 'done') continue;
      this.current = s;
      this.set(s, 'active', s.busy ?? '');
      try {
        await s.run(this, s);
        this.set(s, 'done', s.doneText ?? s.detail);
      } catch (e) {
        this.set(s, 'failed', human(e));
        calls = 0; show('f-wallet', false);
        alertBox(e instanceof Refusal ? `The page refused this transaction: ${human(e)}` : human(e));
        $('f-retry').onclick = () => this.run(this.steps.indexOf(s));
        show('f-retry', true);
        if (s.alt) { $('f-alt').textContent = s.alt.label; $('f-alt').onclick = () => s.alt.run(this, s); show('f-alt', true); }
        $('f-done').textContent = 'Back'; $('f-done').onclick = () => refresh(); show('f-done', true);
        return false;
      }
    }
    show('f-review', false);
    if (this.final) this.final(this);
    else { $('f-done').textContent = 'Done'; $('f-done').onclick = () => refresh(); show('f-done', true); }
    this.done?.(this);
    return true;
  }
}

// The decoded transaction, shown while the wallet asks. `parts`: what the
// wallet approves at once, [{tx: [...], expect}], copied as such for the verifier.
function review(result, tx, expect, parts = null) {
  $('f-lines').replaceChildren(...result.lines.map(l => (l.startsWith('  · ') ? el('li', { class: 'inner', text: l.slice(4) }) : el('li', { text: l }))));
  $('f-check').textContent = result.ok ? `Checked in this page against the rules it was built with (rules ${PINS.rulesSha256.slice(0, 12)}).` : '';
  $('f-check').className = result.ok ? 'ok small' : 'bad small';
  const copied = parts ? { chain: state.chain, parts } : { chain: state.chain, tx, expect };
  $('f-copy').onclick = async () => { try { await navigator.clipboard.writeText(JSON.stringify(copied)); $('f-copy').textContent = 'Copied: node onboard/verify.mjs --commit <id> --paste \'…\''; } catch { /* no clipboard */ } };
  $('f-copy').textContent = 'Copy the transaction (for the verifier)';
  show('f-review', true);
}

// Confirmation from the chain, through the server.
async function landed(sig, step, flow) {
  for (let i = 0; i < 120; i++) {
    const c = await post('confirm', { chain: state.chain, signature: sig }).catch(() => ({ state: 'pending' }));
    if (c.state === 'confirmed') return sig;
    if (c.state === 'failed') throw new Error(`The transaction failed on chain: ${c.reason}`);
    if (step && flow && i === 10) flow.set(step, 'active', 'Waiting for the network to confirm…');
    await sleep(2000);
  }
  throw new Error('The network has not confirmed it after 4 minutes. Check your wallet\'s activity, then try again: nothing is sent twice.');
}

// A Solana transaction's message length (a wire transaction: its signature
// count, the signatures, then the message the wallet signs).
const messageBytes = (b64) => { const b = unb64(b64); return b.length - 1 - 64 * b[0]; };

// Before the wallet is asked: a transaction longer than the wallet signs
// (a Tangem card: 930 bytes) is said so, never sent to fail on the card.
function fitsWallet(txs) {
  const w = state.wallet, limit = cardLimit(w?.name);
  if (!limit) return;
  for (const t of txs) {
    const n = messageBytes(t);
    if (n > limit) {
      throw new Error(`${w.name} cannot sign this step: one of its transactions is ${n} bytes, and the card signs no more than ${limit}. Nothing was sent. `
        + 'Your vault and your money are yours: Withdraw works with this wallet.');
    }
  }
}

// Decode, check, sign and send one transaction; wait until it lands.
async function signOne(tx, expect, step, flow) {
  const r = decodeSol(tx, expect, SOL_PINS);
  review(r, tx, expect);
  if (!r.ok) throw new Refusal(r.problems.join('; '));
  fitsWallet([tx]);
  flow.set(step, 'active', `Confirm in ${state.wallet.name}…`);
  // A Solana wallet that signs sets signs this one as a set of one, and the
  // page sends it: the path the setup proved on Tangem, whose app showed no
  // sign box for solana_signTransaction and failed its own signAndSend
  // (2026-10-14). What it returns is checked again before it is sent.
  if (state.wallet.signAll) {
    const [signed] = await state.wallet.signAll([tx]);
    const again = decodeSol(signed, expect, SOL_PINS);
    if (!again.ok) throw new Refusal(`the wallet returned another transaction: ${again.problems.join('; ')}`);
    flow.set(step, 'active', 'Sending…');
    return landed(await post('broadcast', { chain: 'solana', transaction: signed }).then(x => x.signature), step, flow);
  }
  const sig = await state.wallet.send(tx);
  flow.set(step, 'active', 'Sent. Waiting for the network…');
  return landed(sig, step, flow);
}

// A set of Solana transactions, decoded and checked as a whole, signed in ONE
// approval when the wallet can (signAll), then sent in order, each after the
// one before it landed. A wallet that signs one at a time is asked once per
// transaction. What the wallet returns is checked again: a wallet that
// changed a transaction (a priority fee of its own) is held to the same rules.
async function sendSet(txs, expect, step, flow) {
  const w = state.wallet;
  const r = decodeSolSet(txs, expect, SOL_PINS);
  review(r, txs, expect, [{ tx: txs, expect }]);
  if (!r.ok) throw new Refusal(r.problems.join('; '));
  fitsWallet(txs);
  const sigs = [];
  if (w.signAll) {
    flow.set(step, 'active', txs.length > 1 ? `Approve in ${w.name}: ${txs.length} transactions, one approval…` : `Confirm in ${w.name}…`);
    const signed = await w.signAll(txs);
    const again = decodeSolSet(signed, expect, SOL_PINS);
    if (!again.ok) throw new Refusal(`the wallet returned other transactions: ${again.problems.join('; ')}`);
    for (const [i, t] of signed.entries()) {
      flow.set(step, 'active', txs.length > 1 ? `Sending ${i + 1} of ${txs.length}…` : 'Sending…');
      sigs.push(await landed(await post('broadcast', { chain: 'solana', transaction: t }).then(x => x.signature), step, flow));
    }
    return sigs;
  }
  for (const [i, t] of txs.entries()) {
    flow.set(step, 'active', `${i + 1} of ${txs.length}: confirm in ${w.name}…`);
    sigs.push(await landed(await w.send(t), step, flow));
  }
  return sigs;
}

// The page's one-time set-up key for this wallet's Solana set-up
// (admin.mjs ownerSetupPlan): a policy is too long for a hardware wallet to
// sign (a Tangem card signs 930 bytes at most), so this key signs and pays
// the bot's rules on the owner's new vault, then takes itself off it. It
// holds only the SOL for the rules' rent, never the deposit. Kept in this
// browser until the set-up is done, so a page reloaded half-way goes on
// with it; without storage it lives as long as the page.
function setupKeyOf(owner) {
  const saved = recall(`aperture-setup-key-${owner}`);
  if (saved) { try { return Keypair.fromSecretKey(bs58.decode(saved)); } catch { /* a bad entry: a new key */ } }
  const key = Keypair.generate();
  remember(`aperture-setup-key-${owner}`, bs58.encode(key.secretKey));
  return key;
}
const forgetSetupKey = (owner) => remember(`aperture-setup-key-${owner}`, null);
const expired = (e) => /blockhash not found|block height exceeded|expired/i.test(String(e?.message ?? e));

// Sign `txs` in the wallet: in one approval when it can; null when it signs
// and sends one at a time (wallet.send).
async function walletSigns(txs, step, flow, why = '') {
  const w = state.wallet;
  if (!w.signAll || !txs.length) return null;
  flow.set(step, 'active', why + (txs.length > 1 ? `Approve in ${w.name}: ${txs.length} transactions, one approval…` : `Confirm in ${w.name}…`));
  return w.signAll(txs);
}

// Send transactions signed already, in order, each after the one before landed.
async function sendSigned(txs, step, flow, what) {
  const sigs = [];
  for (const [i, t] of txs.entries()) {
    flow.set(step, 'active', txs.length > 1 ? `${what}: sending ${i + 1} of ${txs.length}…` : `${what}: sending…`);
    sigs.push(await landed((await post('broadcast', { chain: 'solana', transaction: t })).signature, step, flow));
  }
  return sigs;
}

// The owner's Solana set-up (solana.mjs setupTxs), from where the chain
// stands: the wallet's part (the vault's creation with this page's set-up
// key on it, and the key's SOL; with the funding, signed in the same
// approval and held), then the set-up key's part (each of the bot's rules,
// then it takes itself off the vault and sends its SOL back), then the
// funding, sent last. After each part the server is asked again what the
// chain still lacks: a page that stopped half-way resumes there, and nothing
// is paid twice. Every transaction is decoded and checked here first, and
// again as the wallet returns it. Returns the funding's signature, or null
// when the funding is on chain already.
async function solanaSetup(first, typed, key, step, flow) {
  const w = state.wallet, name = first.name, setupKey = key.publicKey.toBase58();
  let it = first, held = null, why = '';
  // every part read so far stays in view (and in the copy for the verifier)
  const lines = [], parts = [];
  for (let round = 0; round < 8; round++) {
    if (it.done) { forgetSetupKey(w.address); return null; }
    const expect = { ...setupExpect(it, typed), setupKey };
    const set = [...it.owner, ...it.key, ...it.fund];
    const r = decodeSolSet(set, expect, SOL_PINS);
    if (lines.length) lines.push('Then, as the chain stands now:');
    lines.push(...r.lines);
    parts.push({ tx: set, expect });
    review({ ok: r.ok, lines, problems: r.problems }, set, expect, parts);
    if (!r.ok) throw new Refusal(r.problems.join('; '));
    // the wallet's part, with the funding when it signs a set (the funding is sent last)
    fitsWallet([...it.owner, ...it.fund]);
    if (it.owner.length || (!held && w.signAll && it.fund.length)) {
      const mine = [...it.owner, ...(held ? [] : it.fund)];
      const signed = await walletSigns(mine, step, flow, why);
      if (signed) {
        const back = [...signed.slice(0, it.owner.length), ...it.key, ...signed.slice(it.owner.length)];
        const again = decodeSolSet(back, expect, SOL_PINS);
        if (!again.ok) throw new Refusal(`the wallet returned other transactions: ${again.problems.join('; ')}`);
        if (!held && it.fund.length) held = signed.slice(it.owner.length);
        await sendSigned(signed.slice(0, it.owner.length), step, flow, 'Your vault');
      } else {
        for (const [i, t] of it.owner.entries()) {
          flow.set(step, 'active', `${i + 1} of ${it.owner.length}: confirm in ${w.name}…`);
          await landed(await w.send(t), step, flow);
        }
      }
      if (it.owner.length) {
        // the wallet may have taken a while (a card is tapped): the set-up key's part is built again, fresh
        flow.set(step, 'active', 'Reading your vault…');
        it = await post('setup', { name, setupKey });
        continue;
      }
    }
    // the set-up key's part: signed in this page, sent in order
    for (const [i, t] of it.key.entries()) {
      flow.set(step, 'active', `The set-up key adds the bot's rules: ${i + 1} of ${it.key.length}…`);
      const tx = Transaction.from(unb64(t));
      tx.sign(key);
      await landed((await post('broadcast', { chain: 'solana', transaction: b64(tx.serialize()) })).signature, step, flow);
    }
    if (it.owner.length || it.key.length) {
      flow.set(step, 'active', 'Reading your vault…');
      it = await post('setup', { name, setupKey });
      continue;
    }
    // only the funding is left: last, the vault whole and yours alone
    if (held) {
      try {
        const sigs = await sendSigned(held, step, flow, 'Your deposit and the bot\'s gas');
        forgetSetupKey(w.address);
        return sigs.at(-1);
      } catch (e) {
        if (!expired(e)) throw e;
        // signed too long ago: built again, the wallet asked once more
        held = null;
        why = 'The network moved on while the rules were added: your deposit and the bot\'s gas once more. ';
        it = await post('setup', { name, setupKey });
        continue;
      }
    }
    let sig = null;
    for (const [i, t] of it.fund.entries()) {
      flow.set(step, 'active', why + (it.fund.length > 1 ? `Your deposit and the bot's gas, ${i + 1} of ${it.fund.length}: confirm in ${w.name}…`
        : `Your deposit and the bot's gas: confirm in ${w.name}…`));
      sig = await landed(await w.send(t), step, flow);
    }
    forgetSetupKey(w.address);
    return sig;
  }
  throw new Error('The set-up did not finish after several rounds. Try again: nothing is paid twice.');
}

// Every transaction of an owner's action, one at a time (a Solana answer holds
// one; the next is built when its turn comes, with a fresh blockhash).
// `expectOf(context, index)` is what the page expects of each.
async function signAll(request, expectOf, step, flow) {
  const sigs = [];
  let answer = await post('tx', { ...request, index: 0 });
  if (!answer.count) return { sigs, answer };
  for (let i = 0; i < answer.count; i++) {
    if (i > 0 && answer.index !== undefined) answer = await post('tx', { ...request, index: i });
    const tx = answer.index !== undefined ? answer.transactions[0] : answer.transactions[i];
    flow.set(step, 'active', answer.count > 1 ? `${i + 1} of ${answer.count}: checking…` : 'Checking…');
    sigs.push(await signOne(tx, expectOf(answer.context, i, answer), step, flow));
  }
  return { sigs, answer };
}

// --- expectations: what each transaction must be ------------------------------------------

const solToken = (sym) => PINS.solana.tokens[sym];
function depositExpect(ctx, token, amount) {
  const t = solToken(token);
  return { kind: 'deposit', owner: state.wallet.address, vault: ctx.vault, settings: ctx.settings, mint: t.mint, raw: toRaw(amount, t.decimals).toString() };
}
function withdrawExpect(ctx, token, amount) {
  const t = token === 'ALL' ? null : solToken(token);
  return { kind: 'withdraw', owner: state.wallet.address, vault: ctx.vault, settings: ctx.settings, mint: t?.mint ?? null,
    raw: token === 'ALL' || amount === 'all' ? 'all' : toRaw(amount, t.decimals).toString() };
}
const vaultExpect = (kind) => (ctx) => ({ kind, owner: state.wallet.address, ...ctx });

// --- connect -------------------------------------------------------------------------------

function chainTabs() {
  const chains = Object.entries(state.cfg.chains);
  $('chains').replaceChildren(...chains.map(([k, c]) => el('button', { type: 'button', role: 'tab', class: 'tab', 'aria-selected': String(k === state.chain),
    onclick: () => { state.chain = k; chainTabs(); walletButtons(); } }, el('span', { text: c.label }), el('span', { class: 'sub', text: c.pair }))));
}

function walletButtons() {
  const inj = solInjected();
  const wc = Boolean(state.cfg.walletConnect?.projectId);
  show('w-injected', Boolean(inj));
  $('w-injected-name').textContent = solStandard()?.name ?? 'Phantom';
  show('w-wc', wc);
  show('wc-off', !wc);
  const phantom = !inj && mobile();
  show('w-phantom', phantom);
  if (phantom) $('w-phantom').href = `https://phantom.app/ul/browse/${encodeURIComponent(location.href)}?ref=${encodeURIComponent(location.origin)}`;
  show('no-wallet', !inj && !wc && !phantom);
  if (!state.keysOk[state.chain]) alertBox('This server\'s bot keys differ from the keys this page was built with. Do not sign anything here; tell the operator.');
}

async function connectWith(kind) {
  alertBox('');
  if (!state.keysOk[state.chain]) return walletButtons();
  try {
    state.wallet = kind === 'wc' ? await walletConnect() : await solanaInjected();
    remember('aperture-wallet', `${state.chain}:${kind}`);
    $('who-addr').textContent = short(state.wallet.address);
    $('who-chain').textContent = state.cfg.chains[state.chain].label;
    $('who-wallet').textContent = state.wallet.name;
    show('who', true);
    await refresh();
  } catch (e) {
    view('v-connect');
    alertBox(human(e));
  }
}

async function disconnect() {
  clearInterval(state.refresh);
  await state.wallet?.disconnect?.().catch(() => {});
  state.wallet = null; state.vault = null;
  remember('aperture-wallet', null);
  show('who', false);
  view('v-connect');
  walletButtons();
}

// --- the vault view -----------------------------------------------------------------------

async function refresh(quiet = false) {
  clearInterval(state.refresh);
  if (!state.wallet) return view('v-connect');
  if (!quiet) { alertBox(''); view('v-loading'); $('loading-text').textContent = 'Reading your vault…'; }
  let vaults;
  try {
    ({ vaults } = await post('vaults', { chain: state.chain, owner: state.wallet.address }));
  } catch (e) {
    if (!quiet) { view('v-loading'); $('loading-text').textContent = human(e); alertBox(human(e)); showRetry(); }
    return;
  }
  const v = vaults[0] ?? null;
  state.vault = v;
  if (!v) return showCreate();
  if (v.view === 'setup') return showSetup(v);
  if (v.view === 'revoked') return showRevoked(v);
  if (v.view === 'active') { showActive(v); state.refresh = setInterval(() => { if (document.visibilityState === 'visible' && document.body.dataset.view === 'v-active') refresh(true); }, 30_000); return; }
  view('v-loading');
  $('loading-text').textContent = v.error ?? 'The vault could not be read right now.';
  showRetry();
}

function showRetry() {
  $('loading-retry').hidden = false;
  $('loading-retry').onclick = () => { $('loading-retry').hidden = true; refresh(); };
}

function balancesOf(v) {
  return el('div', { class: 'bal' }, ...Object.entries(v.balances ?? {}).map(([s, a]) => el('div', { class: 'b' },
    el('span', { class: 'muted small', text: `${s} idle in the vault` }), el('strong', { text: a }))));
}

function tile(label, value, cls = '') { return el('div', { class: `tile ${cls}` }, el('span', { class: 'muted small', text: label }), el('strong', { text: value })); }

function showActive(v) {
  view('v-active');
  $('a-name').textContent = v.name;
  const t = v.stats?.tiles;
  const running = v.botReachable && v.botActive;
  $('a-pill').textContent = !v.botReachable ? (v.positions ? 'Bot stopped · position open' : 'Bot stopped') : t?.inRange === false ? 'Out of range' : 'Running';
  $('a-pill').className = `pill ${running && t?.inRange !== false ? 'ok' : 'warn'}`;
  const tiles = [];
  if (t) {
    tiles.push(tile('Equity', usd(t.equityUsd)), tile('Fees today', usd(t.feesTodayUsd, 4)), tile('Fees total', usd(t.feesTotalUsd, 4)),
      tile('Paid to you', usd(t.paidUsd, 4)), tile(`${t.symbol} price`, t.price == null ? '—' : usd(t.price, 2)),
      tile('Band', t.band ? `${usd(t.band[0])} – ${usd(t.band[1])}` : '—'),
      tile('In range', t.inRange == null ? '—' : t.inRange ? 'Yes' : 'No', t.inRange === false ? 'warn' : ''),
      tile('Mode', t.mode ?? '—'), tile('Exit risk 24h', t.pExit24h == null ? '—' : `${Math.round(t.pExit24h * 100)}%`),
      tile('Rate', t.feesPerDayUsd == null ? '—' : `${usd(t.feesPerDayUsd)}/day`));
  }
  $('a-tiles').replaceChildren(...tiles);
  $('a-empty').hidden = Boolean(t);
  $('a-at').textContent = v.stats?.at ? `Updated ${new Date(v.stats.at).toLocaleString()} · refreshes every 30 s` : '';
  $('a-text').textContent = v.stats?.text ?? '';
  $('a-report').hidden = !v.stats?.text;
  $('a-daily').textContent = v.stats?.daily ?? '';
  $('a-bal').replaceChildren(balancesOf(v), el('div', { class: 'muted small', text: `${v.positions ?? 0} position${v.positions === 1 ? '' : 's'} open · vault ${short(v.vault)}` }));
  const stale = v.rulesCurrent === false;
  $('a-banner').hidden = !stale;
  $('a-banner').textContent = stale ? 'The bot\'s rules were updated: approve them, or the bot stays paused on this vault.' : '';
  $('a-approve').hidden = !stale;
  $('a-approve').onclick = () => approveFlow(v).run();
  $('a-add').onclick = () => addSheet(v, false);
  $('a-withdraw').onclick = () => withdrawSheet(v);
  $('a-revoke').onclick = () => revokeSheet(v);
}

function showRevoked(v) {
  view('v-revoked');
  $('r-name').textContent = v.name;
  $('r-bal').replaceChildren(balancesOf(v), el('div', { class: 'muted small', text: `vault ${short(v.vault)}` }));
  $('r-add').onclick = () => addSheet(v, true);
}

function showSetup(v) {
  view('v-setup');
  const what = v.state === 'signed' ? 'Your set-up is not all on chain yet. Continue: your wallet approves what is missing, once.'
    : v.state === 'connecting' ? 'Your vault is on chain. The bot is finishing it.'
      : v.state === 'stuck' ? `The bot's finish stopped: ${v.error ?? 'unknown reason'}. Your vault and your money are yours; nothing is paid twice.`
        : 'Your vault is ready. Next: the bot starts.';
  $('s-text').textContent = what;
  $('s-name').textContent = v.name;
  $('s-go').onclick = () => createFlow({ resume: v }).run(resumeIndex(v));
}

const resumeIndex = (v) => (v.state === 'signed' ? 0 : ['connecting', 'stuck'].includes(v.state) ? 1 : 2);

// --- create ----------------------------------------------------------------------------------

function showCreate() {
  view('v-create');
  const c = state.cfg.chains[state.chain];
  $('c-token').replaceChildren(...Object.keys(c.tokens).map(s => el('option', { value: s, text: s })));
  if (!$('c-profit').value) $('c-profit').value = state.wallet.address;
  bounds();
}

function bounds() {
  const c = state.cfg.chains[state.chain], sym = $('c-token').value, t = c.tokens[sym];
  if (!t) return;
  const own = $('c-profit').value.trim().toLowerCase() === c.operatorWallet.toLowerCase();
  const gas = t.native ? `${c.gas} ${c.gasToken} of it pays the bot's gas` : `you also pay ${c.gas} ${c.gasToken} for the bot's gas`;
  $('c-bounds').textContent = `${c.pair} pool. From ${t.min} to ${t.max} ${sym}; ${gas}.${own ? '' : ` Fee share: ${state.cfg.feeBps / 100}% of the ${c.payoutToken} fees.`}`;
}

async function maxInto(input, token, add, button) {
  alertBox('');
  button.disabled = true;
  try {
    const m = await post('max', { chain: state.chain, owner: state.wallet.address, token, add });
    if (m.amount) input.value = m.amount; else alertBox(m.short, 'info');
  } catch (e) { alertBox(human(e)); }
  button.disabled = false;
}

// What the page expects of a Solana set-up: the vault the server planned
// (its seed, settings and vault, which the decoder derives again), the venues
// and pools (each one the bundle pins), the payouts (the profit wallet, and
// the operator's fee share), the deposit and the bot's gas. The terms must be
// those the owner typed and the page shows: a server that changed them is
// refused here.
function setupExpect(it, typed) {
  const c = state.cfg.chains[state.chain], t = it.terms, ctx = it.context;
  if (t.gas !== c.gas) throw new Refusal(`the server asks ${t.gas} ${c.gasToken} of gas, not the ${c.gas} this page shows`);
  if (typed) {
    if (t.token !== typed.token || t.amount !== typed.amount || t.profitWallet !== typed.profitWallet) throw new Refusal('the request differs from what you typed');
  }
  const tok = solToken(t.token);
  if (!tok) throw new Refusal(`the token ${t.token} is not one this page knows`);
  const total = toRaw(t.amount, tok.decimals), gas = toRaw(t.gas, 9), dep = toRaw(t.deposit, tok.decimals);
  if (dep !== (c.tokens[t.token].native ? total - gas : total)) throw new Refusal('the deposit is not your amount less the bot\'s gas');
  if (t.feeWallet && t.feeWallet !== PINS.solana.operator) throw new Refusal('the fee share goes to a wallet that is not the operator\'s');
  const venues = Object.keys(ctx.report?.venues ?? {});
  return { kind: 'setup', owner: state.wallet.address, seed: ctx.seed, settings: ctx.settings, vault: ctx.vault, venues,
    pools: Object.fromEntries(venues.map(v => [v, ctx.report.venues[v].pools])), payoutWallets: [t.profitWallet, ...(t.feeWallet ? [t.feeWallet] : [])],
    mint: tok.mint, raw: dep.toString(), gasRaw: gas.toString() };
}

// Create: the wallet approves the set-up (its own vault, the bot's rules,
// the deposit, the bot's gas: one approval), the bot finishes the vault, the
// bot starts and opens. `resume`: a request that exists (Continue setup).
function createFlow({ resume = null } = {}) {
  const w = state.wallet, chain = state.chain;
  const f = new Flow(resume ? `Finish ${resume.name}` : 'Create your position', [
    { label: 'Approve your set-up', busy: 'Building it…', async run(fl) {
      const name = fl.data.name ?? resume?.name;
      let it, typed = null;
      // this page's one-time key signs the bot's rules (too long for a hardware wallet)
      const setupKey = setupKeyOf(w.address);
      const more = { setupKey: setupKey.publicKey.toBase58() };
      if (!name) {
        typed = { token: $('c-token').value, amount: $('c-amount').value.trim(), profitWallet: $('c-profit').value.trim() };
        it = await post('intent', { chain, owner: w.address, ...typed, name: $('c-name').value.trim(), ...more });
        // the server's amount is the typed one, normalised
        typed.amount = it.terms.amount;
      } else {
        it = await post('setup', { name, ...more });
      }
      fl.data.name = it.name;
      if (it.done) { forgetSetupKey(w.address); this.doneText = 'Already on chain'; return; }
      const paid = await solanaSetup(it, typed, setupKey, this, fl);
      if (paid) {
        fl.data.payment = paid;
        remember(`aperture-payment-${it.name}`, paid);
      }
      this.doneText = `Your vault ${short(it.context.vault)}: yours alone, the bot's rules on it, funded`;
    } },
    { label: 'The bot finishes your vault', busy: 'Under a minute…', async run(fl) {
      const name = fl.data.name ?? resume.name;
      for (let i = 0; i < 80; i++) {
        try {
          const made = await post('pay', { name, payment: fl.data.payment ?? (recall(`aperture-payment-${name}`) || null) });
          fl.data.vault = made.vault;
          this.doneText = `Vault ${short(made.vault)} ready`;
          return;
        } catch (e) {
          if (e.status !== 409) throw e;
          if (/sign the set-up again/.test(e.message)) throw e;
        }
        await sleep(3000);
      }
      throw new Error('Your set-up is not confirmed after 4 minutes. Try again later: nothing is paid twice.');
    } },
    { label: 'Deposit into your vault', async run(fl) {
      const name = fl.data.name ?? resume.name;
      const st = (await post('vaults', { chain, owner: w.address })).vaults.find(x => x.name === name);
      if (st && (Object.values(st.balances ?? {}).some(x => Number(x) > 0) || st.positions > 0)) { this.doneText = 'In your vault'; return; }
      const token = st?.token ?? $('c-token').value, amount = st?.deposit;
      const { sigs } = await signAll({ name, action: 'deposit' }, (ctx) => depositExpect(ctx, token, amount), this, fl);
      for (const s of sigs) await recordDeposit(name, s, this, fl);
      this.doneText = `Deposited ${amount} ${token}`;
    } },
    { label: 'Start the bot', async run(fl) { await startBot(fl.data.name ?? resume.name, this, fl); } },
    { label: 'The bot opens the position', busy: 'At its next check, usually within a few minutes.', async run(fl) {
      this.doneText = 'The position is open';
      await waitFor(fl.data.name ?? resume.name, (v) => v.positions > 0, 2 * 60_000, this, fl).catch(() => {
        this.doneText = 'Not yet: the bot opens it at its next check. This page shows it when it does.';
      });
    } },
  ]);
  return f;
}

// --- vault actions ------------------------------------------------------------------------------

// The server reads the deposit back from the chain and sets the bot's cap
// from it (all of the capital deploys). A chain that wants more blocks says
// "pending": asked again. A refusal leaves the deposit where it is.
async function recordDeposit(name, signature, step, fl) {
  for (let i = 0; i < 40; i++) {
    try { await post('deposited', { name, signature }); return; } catch (e) { if (e.status !== 409) return; }
    if (i === 2) fl.set(step, 'active', 'Waiting for the network to settle the deposit…');
    await sleep(3000);
  }
}

async function startBot(name, step, fl) {
  for (let i = 0; i < 40; i++) {
    try { await post('start', { name }); step.doneText = 'Running'; return; } catch (e) {
      if (e.status !== 409 || /revoked/.test(e.message)) throw e;
    }
    fl.set(step, 'active', 'Waiting for the deposit to show in the vault…');
    await sleep(3000);
  }
  throw new Error('The deposit does not show in the vault yet. Try again in a minute.');
}

async function waitFor(name, pred, ms, step, fl) {
  const end = Date.now() + ms;
  while (Date.now() < end) {
    const v = (await post('vaults', { chain: state.chain, owner: state.wallet.address })).vaults.find(x => x.name === name);
    if (v && pred(v)) return v;
    await sleep(5000);
  }
  throw new Error('timed out');
}

function sheet(title, body, go, label, cls = 'primary') {
  view('v-sheet');
  alertBox('');
  $('sh-title').textContent = title;
  $('sh-body').replaceChildren(...body);
  $('sh-go').textContent = label;
  $('sh-go').className = cls;
  $('sh-go').onclick = go;
  $('sh-cancel').onclick = () => refresh();
}

function amountRow(tokens, { add, allOption = false } = {}) {
  const amount = el('input', { inputmode: 'decimal', autocomplete: 'off', placeholder: '0.0', 'aria-label': 'Amount' });
  const token = el('select', { 'aria-label': 'Token' }, ...(allOption ? [el('option', { value: 'ALL', text: 'Everything' })] : []),
    ...tokens.map(s => el('option', { value: s, text: s })));
  const max = el('button', { type: 'button', class: 'small', text: 'Max' });
  max.onclick = () => maxInto(amount, token.value, add, max);
  const row = el('div', { class: 'row' }, amount, add ? max : null, token);
  const sync = () => { amount.hidden = token.value === 'ALL'; max.hidden = token.value === 'ALL'; };
  token.onchange = sync; sync();
  return { row, amount, token };
}

function addSheet(v, revoked) {
  const { row, amount, token } = amountRow(Object.keys(state.cfg.chains[state.chain].tokens), { add: true });
  const policies = 'one approval';
  sheet(revoked ? 'Add capital and give the bot its rules back' : 'Add capital', [
    el('label', { text: 'Amount' }), row,
    el('p', { class: 'muted small', text: revoked
      ? 'Your wallet approves, at once, the bot\'s key back on your vault\'s rules (each checked here against the rules this page was built with) and the deposit after them. The bot then starts again.'
      : 'Your wallet signs the deposit into your vault. The bot puts it in the pool at its next check.' }),
  ], () => {
    const a = amount.value.trim().replace(',', '.'), t = token.value;
    if (!/^\d+(\.\d+)?$/.test(a) || Number(a) <= 0) return alertBox('Type an amount, like 1.5.');
    addFlow(v, t, a, revoked).run();
  }, revoked ? 'Approve and deposit' : 'Deposit');
}

// The bot's rules approved (after a revoke, or new rules), and with `deposit`
// ({token, amount}) more capital after them: one set, one approval.
function approveSteps(v, deposit = null) {
  return [{ label: deposit ? `Approve the bot's rules and deposit ${deposit.amount} ${deposit.token}` : 'Approve the bot\'s rules', async run(fl) {
    if (fl.data.approved) return;
    const a = await post('tx', { name: v.name, action: 'approve', all: true, ...(deposit ?? {}) });
    const t = deposit && solToken(deposit.token);
    const expect = { ...vaultExpect('approve')(a.context), ...(t ? { deposit: { mint: t.mint, raw: toRaw(deposit.amount, t.decimals).toString() } } : {}) };
    const sigs = await sendSet(a.transactions, expect, this, fl);
    fl.data.approved = true;
    if (deposit) { fl.data.deposited = true; await recordDeposit(v.name, sigs.at(-1), this, fl); }
    this.doneText = deposit ? `The bot's key is back on your vault's rules; ${deposit.amount} ${deposit.token} deposited`
      : 'The bot\'s key is back on your vault\'s rules';
  } }];
}

function approveFlow(v) { return new Flow('Approve the updated rules', [...approveSteps(v), { label: 'Start the bot', async run(fl) { await startBot(v.name, this, fl); } }]); }

function addFlow(v, token, amount, revoked) {
  // the rules and the deposit in one approval (one set)
  const together = revoked;
  return new Flow(revoked ? 'Give the bot its rules back' : 'Add capital', [
    ...(revoked ? approveSteps(v, together ? { token, amount } : null) : []),
    ...(together ? [] : [{ label: `Deposit ${amount} ${token}`, async run(fl) {
      if (fl.data.deposited) return;
      const { sigs } = await signAll({ name: v.name, action: 'add', token, amount }, (ctx) => depositExpect(ctx, token, amount), this, fl);
      fl.data.deposited = true;
      for (const s of sigs) await recordDeposit(v.name, s, this, fl);
      this.doneText = `Deposited ${amount} ${token}`;
    } }]),
    { label: 'Start the bot', async run(fl) { await startBot(v.name, this, fl); } },
  ]);
}

function withdrawSheet(v) {
  const { row, amount, token } = amountRow(Object.keys(state.cfg.chains[state.chain].tokens), { add: false, allOption: true });
  sheet('Withdraw to your wallet', [
    el('label', { text: 'What to withdraw' }), row,
    el('p', { class: 'muted small', text: v.positions
      ? 'The bot first closes its position into your vault (or, if it does not answer, you close it from here). Then your wallet signs the withdrawal to itself. If you withdraw part, the bot starts again with the rest.'
      : 'Your wallet signs the withdrawal from your vault to itself.' }),
  ], () => {
    const t = token.value, a = t === 'ALL' ? 'all' : amount.value.trim().replace(',', '.');
    if (t !== 'ALL' && !/^\d+(\.\d+)?$/.test(a)) return alertBox('Type an amount, like 1.5, or choose Everything.');
    withdrawFlow(v, t, a).run();
  }, 'Withdraw');
}

// The position closed into the vault: by the bot when it runs (the owner
// signs a request; the bot closes and stops), else by the owner's wallet.
function closeSteps(v, { revokeFirst = false } = {}) {
  const own = { label: 'Close the position yourself', async run(fl) {
    const r = await signAll({ name: v.name, action: 'close' }, vaultExpect('close'), this, fl);
    if (!r.sigs.length) { this.doneText = 'Nothing to close'; return; }
    await waitFor(v.name, (x) => !x.positions, 120_000, this, fl);
    this.doneText = 'Closed into your vault';
  } };
  if (!v.positions) return [];
  if (!v.botReachable || revokeFirst) return [own];
  const ask = { label: 'The bot closes its position into your vault', async run(fl) {
    if (!fl.data.exitAsked) {
      const it = await post('exit-intent', { name: v.name });
      fl.set(this, 'active', 'Confirm the request in your wallet: it moves nothing.');
      const s = await state.wallet.signMessage(it.message);
      await post('exit', { nonce: it.nonce, message: it.message, signature: s.signature, signedMessage: s.signedMessage });
      fl.data.exitAsked = true;
    }
    fl.set(this, 'active', 'Asked. The bot closes at its next check (up to a few minutes)…');
    await waitFor(v.name, (x) => !x.positions && (x.exit?.done_at || !x.botReachable), 6 * 60_000, this, fl);
    this.doneText = 'Closed into your vault; the bot stopped';
  }, alt: { label: 'Close it myself now', run(fl, s) { fl.set(s, 'done', 'Skipped: you close it yourself'); fl.insert(s, [own]); fl.run(fl.steps.indexOf(s) + 1); } } };
  return [ask];
}

function withdrawFlow(v, token, amount) {
  const all = token === 'ALL';
  const needClose = v.positions > 0 && (all || amount === 'all' || Number(amount) > Number(v.balances?.[token] ?? 0));
  return new Flow('Withdraw', [
    ...(needClose ? closeSteps(v) : []),
    { label: all ? 'Withdraw everything to your wallet' : `Withdraw ${amount === 'all' ? 'all' : amount} ${token}`, async run(fl) {
      if (fl.data.withdrawn) return;
      await signAll({ name: v.name, action: 'withdraw', token, amount }, (ctx) => withdrawExpect(ctx, token, amount), this, fl);
      fl.data.withdrawn = true;
      this.doneText = 'In your wallet';
    } },
    ...(needClose && !all ? [{ label: 'The bot starts again with the rest', async run(fl) {
      try { await startBot(v.name, this, fl); } catch (e) { if (/holds nothing/.test(e.message)) { this.doneText = 'Nothing left for the bot'; return; } throw e; }
    } }] : []),
  ]);
}

function revokeSheet(v) {
  const n = Object.values(v.policies ?? {}).flat().length || 'a few';
  sheet('Revoke the bot', [
    el('p', { text: 'Revoke takes every right from the bot. Your money stays in your vault, under your wallet alone.' }),
    el('ol', { class: 'plain' },
      v.positions ? el('li', { text: v.botReachable ? 'The bot closes its position into your vault (you sign a request that moves nothing).' : 'You close the position into your vault from here.' }) : null,
      el('li', { text: `Your wallet approves ${n === 1 ? 'one update' : `${n} updates at once`} that remove${n === 1 ? 's' : ''} the bot's key from your vault.` }),
      el('li', { text: 'Then you can withdraw everything to your wallet, or keep it in the vault. Add capital later gives the bot its rules back.' })),
  ], () => revokeFlow(v).run(), 'Revoke', 'danger');
}

function revokeFlow(v) {
  const revoke = { label: 'Remove the bot\'s key from your vault', async run(fl) {
    if (fl.data.revoked) return;
    const a = await post('tx', { name: v.name, action: 'revoke', all: true });
    await sendSet(a.transactions, vaultExpect('revoke')(a.context), this, fl);
    fl.data.revoked = true;
    this.doneText = 'The bot can no longer act on your vault';
  } };
  const steps = v.positions && v.botReachable ? [...closeSteps(v), revoke] : [revoke, ...closeSteps(v, { revokeFirst: true })];
  // the bot did not answer: revoke first, then close it yourself (the bot cannot reopen)
  if (steps[0].alt) steps[0].alt = { label: 'Close it myself now', run(fl, s) { fl.set(s, 'done', 'Skipped: you close it yourself'); fl.replaceRest(s, [revoke, ...closeSteps(v, { revokeFirst: true })]); fl.run(fl.steps.indexOf(s) + 1); } };
  return new Flow('Revoke the bot', steps, { final: (fl) => {
    $('f-done').textContent = 'Keep it in the vault'; $('f-done').onclick = () => refresh(); show('f-done', true);
    $('f-alt').textContent = 'Withdraw everything to my wallet'; $('f-alt').className = 'primary';
    $('f-alt').onclick = () => withdrawFlow({ ...v, positions: 0 }, 'ALL', 'all').run();
    show('f-alt', true);
    alertBox('The bot is revoked. Your money is in your vault, under your wallet alone.', 'info');
  } });
}

// --- start --------------------------------------------------------------------------------------

function pinsMatch(cfg) {
  const ok = {};
  const sol = cfg.chains.solana?.botKeys;
  if (sol) ok.solana = sol.length === 2 && sol[0] === PINS.solana.payer && sol[1] === PINS.solana.policyKey;
  return ok;
}

async function boot() {
  $('foot-rules').textContent = `Vault rules ${PINS.rulesSha256.slice(0, 16)} · checked in this page before every signature`;
  if (!fromHash && !recall('aperture-invite')) show('invite-box', true);
  $('invite').oninput = () => remember('aperture-invite', $('invite').value.trim() || null);
  try {
    state.cfg = await (await fetch(api('config'))).json();
  } catch {
    view('v-loading'); $('loading-text').textContent = 'The server does not answer. Try again later.'; showRetry();
    $('loading-retry').onclick = () => location.reload();
    return;
  }
  state.keysOk = pinsMatch(state.cfg);
  if (!Object.keys(state.cfg.chains).length) { alertBox('No chain is open right now.'); return; }
  if (!state.cfg.chains[state.chain]) state.chain = Object.keys(state.cfg.chains)[0];
  chainTabs();
  walletButtons();
  $('w-injected').onclick = () => connectWith('injected');
  $('w-wc').onclick = () => connectWith('wc');
  $('disconnect').onclick = disconnect;
  $('c-token').onchange = bounds;
  $('c-profit').oninput = bounds;
  $('c-max').onclick = () => maxInto($('c-amount'), $('c-token').value, false, $('c-max'));
  $('c-go').onclick = () => {
    if (!$('c-amount').value.trim()) return alertBox('Type an amount, like 1.5.');
    if (!$('c-name').value.trim()) return alertBox('Give the pool a name: a-z, 0-9 and dashes.');
    createFlow().run();
  };
  document.addEventListener('input', (e) => {
    const box = e.target;
    if (box.matches?.('input[inputmode="decimal"]') && box.value.includes(',')) box.value = box.value.replace(/,/g, '.');
  });
  view('v-connect');
  // the wallet this browser used last, when it is still there
  const last = recall('aperture-wallet');
  if (last) {
    const [chain, kind] = last.split(':');
    if (state.cfg.chains[chain]) { state.chain = chain; chainTabs(); walletButtons(); }
    if (kind === 'wc' && state.cfg.walletConnect?.projectId) {
      const client = await wcInit().catch(() => null);
      const kept = client?.session.getAll().filter(s => s.expiry * 1000 > Date.now() && sessionAccount(s));
      if (kept?.length) connectWith('wc');
    } else if (kind === 'injected' && solInjected()) {
      // an injected wallet reconnects without a prompt only when it trusts this site already
      const p = window.phantom?.solana ?? window.solana;
      if (p?.isConnected || p?.connect) p.connect?.({ onlyIfTrusted: true }).then(() => connectWith('injected')).catch(() => {});
    }
  }
}

boot();
