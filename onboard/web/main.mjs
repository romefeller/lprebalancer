// The page at romefeller.app/bot (onboard/index.html). Built into
// onboard/bundle.js by build.mjs; it talks to its own server and, for
// WalletConnect, to the WalletConnect relay. Every transaction goes through
// the owner's wallet, and every one is decoded here first and checked
// against the pins built into this bundle (__PINS__: the bot's keys, the
// approved pools, the vault rules): a transaction that differs from what the
// owner asked for is refused before the wallet sees it.
//
// One vault per wallet per chain. The page shows, after the wallet connects:
//   create   no vault yet: amount, profit wallet, name -> sign, pay the gas,
//            the bot creates the vault, deposit, the bot starts
//   setup    a setup that stopped: it resumes, nothing is paid twice
//   active   the bot runs: its book as Telegram shows it; Add capital,
//            Withdraw, Revoke
//   revoked  only Add capital, which gives the bot its rules back
import { SignClient } from '@walletconnect/sign-client';
import qrcode from 'qrcode-generator';
import bs58 from 'bs58';
import { Transaction, VersionedTransaction, PublicKey } from '@solana/web3.js';
import RULES from '../../chains/solana/vault/rules.json' with { type: 'json' };
import SOLANA from '../../chains/solana/solana.json' with { type: 'json' };
import { decodeSol } from '../decode_sol.mjs';
import { decodeEvm } from '../decode_evm.mjs';
import { fromRaw, toRaw } from '../units.mjs';

/* global __PINS__ */
const PINS = __PINS__;
const SOL_CHAINS = ['solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp', 'solana:4sGjMW1sUnHzSxGspuhpqLDx6wiyjNtZ'];
const EVM_CHAIN = `eip155:${PINS.polygon.chainId}`;
const SOL_PINS = { payer: PINS.solana.payer, policyKey: PINS.solana.policyKey, operator: PINS.solana.operator, pools: PINS.solana.pools,
  rules: RULES, solana: SOLANA };
const EVM_PINS = { chainId: PINS.polygon.chainId, wrapped: PINS.polygon.wrapped, tokens: PINS.polygon.tokens, botKey: PINS.polygon.botKey,
  npm: PINS.polygon.npm };

// --- small helpers ------------------------------------------------------------------------

const $ = (id) => document.getElementById(id);
const sleep = (ms) => new Promise(r => setTimeout(r, ms));
const api = (p) => new URL(`api/${p}`, location.href.replace(/[?#].*$/, '').replace(/\/?$/, '/')).toString();
const b64 = (bytes) => { let s = ''; for (const b of bytes) s += String.fromCharCode(b); return btoa(s); };
const unb64 = (s) => Uint8Array.from(atob(s), c => c.charCodeAt(0));
const hexUtf8 = (s) => '0x' + Array.from(new TextEncoder().encode(s), b => b.toString(16).padStart(2, '0')).join('');
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
  if (e?.code === 4001 || e?.code === 5000 || /reject|denied|declin|cancel|user (closed|abort)/i.test(m)) return 'You declined in your wallet. Nothing was sent.';
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
// send(tx) -> signature or hash}. Injected wallets (Wallet Standard, Phantom's
// provider, EIP-1193) and WalletConnect.

const standard = [];
const register = (...ws) => { for (const w of ws) standard.push(w); return () => {}; };
window.addEventListener('wallet-standard:register-wallet', (e) => e.detail?.({ register }));
window.dispatchEvent(new CustomEvent('wallet-standard:app-ready', { detail: { register } }));

const solStandard = () => standard.find(x => x.chains?.some(c => c.startsWith('solana:')) && x.features?.['solana:signMessage']
  && (x.features?.['solana:signAndSendTransaction'] || x.features?.['solana:signTransaction']));
const solInjected = () => solStandard() || window.phantom?.solana || window.solana;
const evmInjected = () => window.phantom?.ethereum ?? window.ethereum;
const injectedFor = (chain) => (chain === 'solana' ? solInjected() : evmInjected());

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
    async disconnect() { await p.disconnect?.(); } };
}

async function polygonInjected() {
  const p = evmInjected();
  if (!p?.request) throw new Error('No Polygon wallet in this browser. Open this page in your wallet\'s browser, or use WalletConnect.');
  const [address] = await p.request({ method: 'eth_requestAccounts' });
  if (!address) throw new Error('the wallet shared no account');
  return { chain: 'polygon', kind: 'injected', name: 'Wallet', address,
    async signMessage(text) { return { signature: await p.request({ method: 'personal_sign', params: [hexUtf8(text), address] }) }; },
    async send(tx, chainId) {
      const want = `0x${chainId.toString(16)}`;
      if ((await p.request({ method: 'eth_chainId' })).toLowerCase() !== want) {
        await p.request({ method: 'wallet_switchEthereumChain', params: [{ chainId: want }] });
      }
      return p.request({ method: 'eth_sendTransaction', params: [{ from: address, to: tx.to, data: tx.data, value: tx.value }] });
    },
    async disconnect() {} };
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

const namespaces = (chain) => (chain === 'solana'
  ? { solana: { chains: SOL_CHAINS, methods: ['solana_signMessage', 'solana_signTransaction', 'solana_signAndSendTransaction'], events: [] } }
  : { eip155: { chains: [EVM_CHAIN], methods: ['personal_sign', 'eth_sendTransaction'], events: ['chainChanged', 'accountsChanged'] } });

// The session's account on `chain`: [chainId, address] or null.
function sessionAccount(session, chain) {
  const ns = session.namespaces?.[chain === 'solana' ? 'solana' : 'eip155'];
  const acct = (ns?.accounts ?? []).find(a => chain === 'solana' ? SOL_CHAINS.some(c => a.startsWith(`${c}:`)) : a.startsWith(`${EVM_CHAIN}:`));
  if (!acct) return null;
  const i = acct.lastIndexOf(':');
  return [acct.slice(0, i), acct.slice(i + 1)];
}

function wcWallet(client, session, chain) {
  const acct = sessionAccount(session, chain);
  if (!acct) throw new Error(chain === 'solana' ? 'The wallet shared no Solana account. Add Solana in the wallet app and connect again.'
    : 'The wallet shared no Polygon account. Add Polygon in the wallet app and connect again.');
  const [chainId, address] = acct;
  const methods = session.namespaces[chain === 'solana' ? 'solana' : 'eip155'].methods ?? [];
  const request = (method, params) => client.request({ topic: session.topic, chainId, request: { method, params } });
  const name = session.peer?.metadata?.name || 'WalletConnect';
  if (chain === 'solana') {
    return { chain, kind: 'walletconnect', name, address, topic: session.topic,
      async signMessage(text) {
        const out = await request('solana_signMessage', { message: bs58.encode(new TextEncoder().encode(text)), pubkey: address });
        return { signature: b64(bs58.decode(out.signature)) };
      },
      async send(tx) {
        if (methods.includes('solana_signAndSendTransaction')) {
          const out = await request('solana_signAndSendTransaction', { transaction: tx });
          if (out?.signature) return out.signature;
        }
        const out = await request('solana_signTransaction', { transaction: tx });
        if (out?.transaction) return broadcastSigned(unb64(out.transaction));
        const t = Transaction.from(unb64(tx));
        t.addSignature(new PublicKey(address), Buffer.from(bs58.decode(out.signature)));
        return broadcastSigned(t.serialize());
      },
      async disconnect() { await client.disconnect({ topic: session.topic, reason: { code: 6000, message: 'disconnected' } }).catch(() => {}); } };
  }
  return { chain, kind: 'walletconnect', name, address, topic: session.topic,
    async signMessage(text) { return { signature: await request('personal_sign', [hexUtf8(text), address]) }; },
    async send(tx) { return request('eth_sendTransaction', [{ from: address, to: tx.to, data: tx.data, value: tx.value }]); },
    async disconnect() { await client.disconnect({ topic: session.topic, reason: { code: 6000, message: 'disconnected' } }).catch(() => {}); } };
}

// A WalletConnect session for `chain`: an earlier one this browser keeps, or
// a new one through a QR code (desktop) or a link that opens the wallet app.
async function walletConnect(chain) {
  const client = await wcInit();
  const kept = client.session.getAll().filter(s => s.expiry * 1000 > Date.now() && sessionAccount(s, chain));
  if (kept.length) return wcWallet(client, kept.at(-1), chain);
  const { uri, approval } = await client.connect({ optionalNamespaces: namespaces(chain) });
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
  return wcWallet(client, session, chain);
}

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
    show('f-retry', false); show('f-alt', false); show('f-done', false); show('f-review', false);
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

// The decoded transaction, shown while the wallet asks.
function review(result, tx, expect) {
  $('f-lines').replaceChildren(...result.lines.map(l => (l.startsWith('  · ') ? el('li', { class: 'inner', text: l.slice(4) }) : el('li', { text: l }))));
  $('f-check').textContent = result.ok ? `Checked in this page against the rules it was built with (rules ${PINS.rulesSha256.slice(0, 12)}).` : '';
  $('f-check').className = result.ok ? 'ok small' : 'bad small';
  $('f-copy').onclick = async () => { try { await navigator.clipboard.writeText(JSON.stringify({ chain: state.chain, tx, expect })); $('f-copy').textContent = 'Copied: node onboard/verify.mjs --commit <id> --paste \'…\''; } catch { /* no clipboard */ } };
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

// Decode, check, sign and send one transaction; wait until it lands.
async function signOne(tx, expect, chainId, step, flow) {
  const r = state.chain === 'solana' ? decodeSol(tx, expect, SOL_PINS) : decodeEvm(tx, expect, EVM_PINS);
  review(r, tx, expect);
  if (!r.ok) throw new Refusal(r.problems.join('; '));
  flow.set(step, 'active', `Confirm in ${state.wallet.name}…`);
  const sig = await state.wallet.send(tx, chainId);
  flow.set(step, 'active', 'Sent. Waiting for the network…');
  return landed(sig, step, flow);
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
    sigs.push(await signOne(tx, expectOf(answer.context, i, answer), answer.chainId, step, flow));
  }
  return { sigs, answer };
}

// --- expectations: what each transaction must be ------------------------------------------

const solToken = (sym) => PINS.solana.tokens[sym];
function depositExpect(ctx, token, amount, i = 0) {
  const owner = state.wallet.address;
  if (state.chain === 'solana') {
    const t = solToken(token);
    return { kind: 'deposit', owner, vault: ctx.vault, settings: ctx.settings, mint: t.mint, raw: toRaw(amount, t.decimals).toString() };
  }
  const t = PINS.polygon.tokens[token];
  const sym = t.native && i === 1 ? Object.keys(PINS.polygon.wrapped)[0] : token;
  return { kind: 'deposit', owner, safe: ctx.safe, token: sym, raw: toRaw(amount, t.decimals).toString() };
}
function withdrawExpect(ctx, token, amount) {
  const owner = state.wallet.address;
  if (state.chain === 'solana') {
    const t = token === 'ALL' ? null : solToken(token);
    return { kind: 'withdraw', owner, vault: ctx.vault, settings: ctx.settings, mint: t?.mint ?? null,
      raw: token === 'ALL' || amount === 'all' ? 'all' : toRaw(amount, t.decimals).toString() };
  }
  const t = PINS.polygon.tokens[token];
  return { kind: 'withdraw', owner, safe: ctx.safe, token, raw: token === 'ALL' || amount === 'all' ? 'all' : toRaw(amount, t.decimals).toString() };
}
const vaultExpect = (kind) => (ctx) => (state.chain === 'solana'
  ? { kind, owner: state.wallet.address, ...ctx }
  : { kind, owner: state.wallet.address, safe: ctx.safe, roles: ctx.roles });

// --- connect -------------------------------------------------------------------------------

function chainTabs() {
  const chains = Object.entries(state.cfg.chains);
  $('chains').replaceChildren(...chains.map(([k, c]) => el('button', { type: 'button', role: 'tab', class: 'tab', 'aria-selected': String(k === state.chain),
    onclick: () => { state.chain = k; chainTabs(); walletButtons(); } }, el('span', { text: c.label }), el('span', { class: 'sub', text: c.pair }))));
}

function walletButtons() {
  const inj = injectedFor(state.chain);
  const wc = Boolean(state.cfg.walletConnect?.projectId);
  show('w-injected', Boolean(inj));
  $('w-injected-name').textContent = state.chain === 'solana' ? (solStandard()?.name ?? 'Phantom') : 'Browser wallet';
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
    state.wallet = kind === 'wc' ? await walletConnect(state.chain) : state.chain === 'solana' ? await solanaInjected() : await polygonInjected();
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
  const what = v.state === 'signed' ? 'Your request is signed. Next: pay the bot\'s gas.'
    : v.state === 'connecting' ? 'The bot is creating your vault.'
      : v.state === 'stuck' ? `The setup stopped: ${v.error ?? 'unknown reason'}. Your gas payment is kept.`
        : 'Your vault is ready. Next: the deposit.';
  $('s-text').textContent = what;
  $('s-name').textContent = v.name;
  $('s-go').onclick = () => createFlow({ resume: v }).run(resumeIndex(v));
}

const resumeIndex = (v) => (v.state === 'signed' ? 1 : ['connecting', 'stuck'].includes(v.state) ? 2 : 3);

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

// Create: sign the request, pay the gas, the bot makes the vault, deposit,
// the bot starts and opens. `resume`: a request that exists (Continue setup).
function createFlow({ resume = null } = {}) {
  const w = state.wallet, chain = state.chain;
  const f = new Flow(resume ? `Finish ${resume.name}` : 'Create your position', [
    { label: 'Sign your request', busy: 'Check the message in your wallet: it moves nothing.', async run(fl) {
      const body = { chain, owner: w.address, token: $('c-token').value, amount: $('c-amount').value.trim(), profitWallet: $('c-profit').value.trim(),
        name: $('c-name').value.trim() };
      const it = await post('intent', body);
      fl.data.intent = it;
      fl.set(this, 'active', 'Confirm the message in your wallet…');
      const s = await w.signMessage(it.message);
      const pay = await post('sign', { nonce: it.nonce, message: it.message, signature: s.signature, signedMessage: s.signedMessage });
      fl.data.name = pay.name; fl.data.pay = pay;
      this.doneText = `Signed: ${pay.name}`;
    } },
    { label: 'Pay the bot\'s gas', async run(fl) {
      const name = fl.data.name ?? resume.name, key = `aperture-payment-${name}`;
      let sig = recall(key);
      if (!sig) {
        const pay = fl.data.pay ?? await post('payment', { name });
        const gas = fl.data.intent?.gas ?? pay.gas;
        const expect = chain === 'solana' ? { kind: 'payment', owner: w.address, raw: toRaw(gas, 9).toString() }
          : { kind: 'payment', owner: w.address, lamports: toRaw(gas, 18).toString() };
        const r = chain === 'solana' ? decodeSol(pay.transactions[0], expect, SOL_PINS) : decodeEvm(pay.transactions[0], expect, EVM_PINS);
        review(r, pay.transactions[0], expect);
        if (!r.ok) throw new Refusal(r.problems.join('; '));
        fl.set(this, 'active', `Confirm the payment in ${w.name}…`);
        sig = await w.send(pay.transactions[0], pay.chainId);
        remember(key, sig);
      }
      fl.data.payment = sig;
      await landed(sig, this, fl);
      this.doneText = 'Paid';
    } },
    { label: 'The bot creates your vault', busy: 'About a minute…', async run(fl) {
      const name = fl.data.name ?? resume.name;
      for (let i = 0; i < 80; i++) {
        try {
          const made = await post('pay', { name, payment: fl.data.payment ?? recall(`aperture-payment-${name}`) });
          fl.data.vault = made.vault;
          this.doneText = `Vault ${short(made.vault)}`;
          return;
        } catch (e) { if (e.status !== 409) throw e; }
        await sleep(3000);
      }
      throw new Error('The payment is not confirmed after 4 minutes. Try again later: nothing is paid twice.');
    } },
    { label: 'Deposit into your vault', async run(fl) {
      const name = fl.data.name ?? resume.name;
      const st = (await post('vaults', { chain, owner: w.address })).vaults.find(x => x.name === name);
      if (st && (Object.values(st.balances ?? {}).some(x => Number(x) > 0) || st.positions > 0)) { this.doneText = 'Already in the vault'; return; }
      const token = st?.token ?? $('c-token').value, amount = st?.deposit;
      const { sigs } = await signAll({ name, action: 'deposit' }, (ctx, i) => depositExpect(ctx, token, amount, i), this, fl);
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
  const policies = state.chain === 'solana' ? 'one approval per rule set' : 'one approval';
  sheet(revoked ? 'Add capital and give the bot its rules back' : 'Add capital', [
    el('label', { text: 'Amount' }), row,
    el('p', { class: 'muted small', text: revoked
      ? `Your wallet first signs ${policies} that puts the bot's key back on your vault's rules (each checked here against the rules this page was built with), then the deposit. The bot then starts again.`
      : 'Your wallet signs the deposit into your vault. The bot puts it in the pool at its next check.' }),
  ], () => {
    const a = amount.value.trim().replace(',', '.'), t = token.value;
    if (!/^\d+(\.\d+)?$/.test(a) || Number(a) <= 0) return alertBox('Type an amount, like 1.5.');
    addFlow(v, t, a, revoked).run();
  }, revoked ? 'Approve and deposit' : 'Deposit');
}

function approveSteps(v) {
  return [{ label: 'Approve the bot\'s rules', async run(fl) {
    await signAll({ name: v.name, action: 'approve' }, vaultExpect('approve'), this, fl);
    this.doneText = 'The bot\'s key is back on your vault\'s rules';
  } }];
}

function approveFlow(v) { return new Flow('Approve the updated rules', [...approveSteps(v), { label: 'Start the bot', async run(fl) { await startBot(v.name, this, fl); } }]); }

function addFlow(v, token, amount, revoked) {
  return new Flow(revoked ? 'Give the bot its rules back' : 'Add capital', [
    ...(revoked ? approveSteps(v) : []),
    { label: `Deposit ${amount} ${token}`, async run(fl) {
      if (fl.data.deposited) return;
      const { sigs } = await signAll({ name: v.name, action: 'add', token, amount }, (ctx, i) => depositExpect(ctx, token, amount, i), this, fl);
      fl.data.deposited = true;
      for (const s of sigs) await recordDeposit(v.name, s, this, fl);
      this.doneText = `Deposited ${amount} ${token}`;
    } },
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
  const n = state.chain === 'solana' ? Object.values(v.policies ?? {}).flat().length || 'a few' : 1;
  sheet('Revoke the bot', [
    el('p', { text: 'Revoke takes every right from the bot. Your money stays in your vault, under your wallet alone.' }),
    el('ol', { class: 'plain' },
      v.positions ? el('li', { text: v.botReachable ? 'The bot closes its position into your vault (you sign a request that moves nothing).' : 'You close the position into your vault from here.' }) : null,
      el('li', { text: `Your wallet signs ${n === 1 ? 'one update' : 'one update per rule set'} that removes the bot's key from your vault.` }),
      el('li', { text: 'Then you can withdraw everything to your wallet, or keep it in the vault. Add capital later gives the bot its rules back.' })),
  ], () => revokeFlow(v).run(), 'Revoke', 'danger');
}

function revokeFlow(v) {
  const revoke = { label: 'Remove the bot\'s key from your vault', async run(fl) {
    if (fl.data.revoked) return;
    await signAll({ name: v.name, action: 'revoke' }, vaultExpect('revoke'), this, fl);
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
  const sol = cfg.chains.solana?.botKeys, poly = cfg.chains.polygon?.botKeys;
  if (sol) ok.solana = sol.length === 2 && sol[0] === PINS.solana.payer && sol[1] === PINS.solana.policyKey;
  if (poly) ok.polygon = poly.length === 1 && poly[0].toLowerCase() === PINS.polygon.botKey.toLowerCase();
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
      const kept = client?.session.getAll().filter(s => s.expiry * 1000 > Date.now() && sessionAccount(s, chain));
      if (kept?.length) connectWith('wc');
    } else if (kind === 'injected' && injectedFor(chain)) {
      // an injected wallet reconnects without a prompt only when it trusts this site already
      const p = chain === 'solana' ? (window.phantom?.solana ?? window.solana) : null;
      if (p?.isConnected || p?.connect) p.connect?.({ onlyIfTrusted: true }).then(() => connectWith('injected')).catch(() => {});
    }
  }
}

boot();
