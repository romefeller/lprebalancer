// The self-serve onboarding server behind romefeller.app/bot. Caddy proxies
// /bot* here; the server listens on 127.0.0.1 only.
//
//   node onboard/server.mjs [onboard/onboard.json]
//
// An owner opens the page, connects a wallet (an injected one, or any
// WalletConnect wallet: Tangem, Ledger Live), fills chain, amount, profit
// wallet and name, and approves ONE set-up in the wallet: the owner's own
// transactions create its vault (the owner its creator, rent payer and only
// signer), give the bot's key its rules, deposit and pay the bot's gas. The
// bot is never a signer of the vault and spends nothing before all of that
// is on chain; then it makes the vault's lookup table (Solana), registers
// the vault and one new profile copied from the chain's template, and the
// profile's service starts. The page also lists the owner's vaults,
// with withdraw and revoke built for the owner's wallet to sign, and reads
// the wallet's balances for its Max button. For a vault that runs it shows
// the bot's book as Telegram shows it (the same rows, the same formatter),
// and lets the owner add capital, withdraw (the bot closes its position into
// the vault first, or the owner closes it), revoke, and re-grant.
//
// The page (bundle.js, built from onboard/web/) decodes every transaction it
// asks the wallet to sign and refuses one that differs from what the owner
// asked for under the rules pinned in git: this server is not trusted for that.
//
// What it never does: hold an owner's key, send a transaction the owner did
// not sign (other than the bot's own lookup table), spend anything before the
// owner's set-up and gas are on chain, act without the invite, change a
// profile or wallet it did not create.
//
// Environment: LPBOT_DSN (the database), the RPC variables the chain modules
// read (LPBOT_RPC / SOLANA_RPC_URL, LPBOT_POLYGON_RPC), the key PATHS the
// config names (payerKeyEnv, policyKeyEnv, keyEnv) and the invite hashes
// (inviteEnv: comma-separated SHA-256 hex of the invite codes).
import http from 'node:http';
import fs from 'node:fs';
import path from 'node:path';
import { spawn } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { createRequire } from 'module';
import * as I from './intent.mjs';
import * as S from './solana.mjs';
import * as P from './polygon.mjs';
import { saltOf } from '../chains/evm/vault/owner_setup.mjs';
import { isEntry } from '../shared/rpc_policy.mjs';
import { message as telegramMessage } from '../telegram_bridge.mjs';
import { pricedView, shownBand, shownPrice } from '../book_format.mjs';

const require = createRequire(import.meta.url);
const { Connection } = require('@solana/web3.js');

const HERE = path.dirname(fileURLToPath(import.meta.url));
const STORE = path.join(HERE, 'store.py');

// --- the config ---------------------------------------------------------------

// Two values the service environment may set over the file (ops/onboard/setup.sh):
// LPBOT_ONBOARD_START, the start command as words, for the sandboxed unit,
// which cannot sudo (NoNewPrivileges) and asks a root helper through a spool
// file instead; LPBOT_ONBOARD_PORT, a loopback port to try the new unit on
// beside the running one.
export function loadConfig(file = path.join(HERE, 'onboard.json'), env = process.env) {
  const raw = JSON.parse(fs.readFileSync(file, 'utf8'));
  if (env.LPBOT_ONBOARD_START) raw.service = { ...raw.service, start: env.LPBOT_ONBOARD_START.trim().split(/\s+/) };
  if (env.LPBOT_ONBOARD_PORT) {
    const port = Number(env.LPBOT_ONBOARD_PORT);
    if (!(Number.isInteger(port) && port >= 1024 && port <= 65535)) throw new Error(`LPBOT_ONBOARD_PORT ${env.LPBOT_ONBOARD_PORT}`);
    raw.listen = { ...raw.listen, port };
  }
  const chains = {};
  if (raw.chains.solana) chains.solana = S.resolve(raw.chains.solana);
  if (raw.chains.polygon) chains.polygon = P.resolve(raw.chains.polygon);
  if (!(raw.fee.bps >= 0 && raw.fee.bps <= 5000)) throw new Error(`fee.bps ${raw.fee.bps}`);
  for (const [k, c] of Object.entries(chains)) {
    for (const [s, t] of Object.entries(c.tokens)) {
      if (typeof t.fees !== 'string' || !/^\d+(\.\d+)?$/.test(t.fees)) throw new Error(`chains.${k}.tokens.${s}.fees: a decimal (what Max keeps back)`);
    }
  }
  if (!raw.service.start.some(a => a.includes('{name}'))) throw new Error('service.start must name {name}');
  if (!['127.0.0.1', '::1'].includes(raw.listen.host)) throw new Error(`listen.host ${raw.listen.host}: loopback only (Caddy proxies)`);
  if (!(raw.capHeadroom >= 1)) throw new Error(`capHeadroom ${raw.capHeadroom}: at least 1`);
  const wc = raw.walletConnect ?? {};
  if (!/^wss?:\/\/[a-z0-9.-]+(:\d+)?(\/.*)?$/.test(wc.relayUrl ?? '')) throw new Error(`walletConnect.relayUrl ${wc.relayUrl}: a ws(s) URL`);
  return { ...raw, chains };
}

// The WalletConnect project id: the environment's (walletConnect.projectIdEnv),
// else the config's. Not a secret (every dapp page carries its own), but it
// names the operator's Reown project. Empty: the page offers no WalletConnect.
export function walletConnectOf(cfg, env = process.env) {
  const wc = cfg.walletConnect ?? {};
  const id = String(env[wc.projectIdEnv] || wc.projectId || '').trim();
  if (id && !/^[0-9a-f]{32}$/.test(id)) throw new Error('the WalletConnect project id is 32 hex characters');
  return { projectId: id, relayUrl: wc.relayUrl, verify: wc.verify ?? [] };
}

// The page's Content-Security-Policy: its own scripts and styles only; the
// WalletConnect relay (and its verify service, whose hidden frame lets a
// wallet show this site as verified) when a project id is set. Pure.
export function csp(wc) {
  const relay = wc.projectId ? [new URL(wc.relayUrl).origin] : [];
  const verify = wc.projectId ? wc.verify : [];
  return ["default-src 'none'", "script-src 'self'", "style-src 'self'", `connect-src 'self' ${[...relay, ...verify].join(' ')}`.trim(),
    "img-src 'self' data:", verify.length ? `frame-src ${verify.join(' ')}` : null,
    "base-uri 'none'", "form-action 'none'", "frame-ancestors 'none'"].filter(Boolean).join('; ');
}

// --- rate limits ----------------------------------------------------------------

// Events per key over a sliding window. Bounded: at most `keys` keys, the
// stalest dropped first.
export class Windows {
  constructor({ max, windowS }, now = () => Date.now(), keys = 10_000) {
    this.max = max; this.ms = windowS * 1000; this.now = now; this.keys = keys; this.seen = new Map();
  }

  recent(key) {
    const t = this.now(), list = (this.seen.get(key) ?? []).filter(x => t - x < this.ms);
    if (list.length) this.seen.set(key, list); else this.seen.delete(key);
    return list;
  }

  full(key) { return this.recent(key).length >= this.max; }

  add(key) {
    const list = this.recent(key);
    list.push(this.now());
    this.seen.delete(key);
    this.seen.set(key, list);
    while (this.seen.size > this.keys) this.seen.delete(this.seen.keys().next().value);
  }
}

// --- the database (onboard/store.py) and the service -------------------------------

export function run(cmd, args, { input = '', timeoutMs = 120_000, env = process.env } = {}) {
  return new Promise((resolve) => {
    const p = spawn(cmd, args, { env, stdio: ['pipe', 'pipe', 'pipe'] });
    let out = '', err = '';
    const timer = setTimeout(() => p.kill('SIGKILL'), timeoutMs);
    p.stdout.on('data', d => { out += d; });
    p.stderr.on('data', d => { err += d; });
    p.on('error', e => { clearTimeout(timer); resolve({ code: -1, out, err: String(e.message) }); });
    p.on('close', code => { clearTimeout(timer); resolve({ code, out, err }); });
    p.stdin.end(input);
  });
}

export class StoreError extends Error {}

export function pythonStore(env = process.env) {
  return async (command, request) => {
    const r = await run('python3', [STORE, command], { input: JSON.stringify(request), env });
    const last = r.out.trim().split('\n').pop() || '';
    let j = null;
    try { j = JSON.parse(last); } catch { /* below */ }
    if (j?.error) throw new StoreError(j.error);
    if (r.code !== 0 || !j) throw new Error(`store ${command} failed: ${(r.err.trim().split('\n').pop() || 'no output').slice(0, 300)}`);
    return j;
  };
}

// The command with {name} replaced; the name is checked again here.
export function serviceCommand(template, name) {
  if (!I.NAME.test(name) || name.includes('--')) throw new Error(`refused: profile name ${JSON.stringify(name)}`);
  return template.map(a => a.replaceAll('{name}', name));
}

// --- HTTP ----------------------------------------------------------------------------

class HttpError extends Error {
  constructor(status, message) { super(message); this.status = status; }
}

const BASE_HEADERS = {
  'x-content-type-options': 'nosniff',
  'referrer-policy': 'no-referrer',
  'cache-control': 'no-store',
};

// A response writer with this page's headers.
function sender(policy) {
  const headers = { ...BASE_HEADERS, 'content-security-policy': policy };
  return (res, status, body, type = 'application/json; charset=utf-8') => {
    const data = typeof body === 'string' ? body : JSON.stringify(body);
    res.writeHead(status, { ...headers, 'content-type': type, 'content-length': Buffer.byteLength(data) });
    res.end(data);
  };
}

async function readJson(req, limit) {
  if (!String(req.headers['content-type'] ?? '').startsWith('application/json')) throw new HttpError(415, 'send JSON');
  let size = 0;
  const chunks = [];
  for await (const c of req) {
    size += c.length;
    if (size > limit) throw new HttpError(413, 'request too large');
    chunks.push(c);
  }
  try {
    const v = JSON.parse(Buffer.concat(chunks).toString('utf8') || '{}');
    if (!v || typeof v !== 'object' || Array.isArray(v)) throw new Error('not an object');
    return v;
  } catch { throw new HttpError(400, 'the body is not a JSON object'); }
}

// The client's address. Caddy (the only thing that reaches 127.0.0.1) sets
// X-Forwarded-For to the address it saw.
export function clientOf(req) {
  const peer = req.socket.remoteAddress ?? '';
  const fwd = String(req.headers['x-forwarded-for'] ?? '').split(',')[0].trim();
  return /^(::ffff:)?127\.|^::1$/.test(peer) && fwd ? fwd : peer;
}

// The profile's venues from a connect report: a Solana vault names each
// venue's pools under `venues`, a Safe its one venue. The profile opens on the
// first approved venue in the config's order (`order`) and may move to every
// approved one. {dex, pool, execute}. Pure.
export function profileVenues(connected, order) {
  const v = connected.venues ?? { [connected.venue]: { pools: connected.pools } };
  const names = [...order.filter(n => I.own(v, n)), ...Object.keys(v).filter(n => !order.includes(n))];
  const pool = names.length ? v[names[0]].pools?.[0] : undefined;
  if (!pool) throw new Error('the connect approved no pool');
  return { dex: names[0], pool, execute: names };
}

// The server. `deps`: {chains: {solana: adapter, polygon: adapter}, store, start, status, now, log}.
export function makeServer(cfg, deps) {
  const now = deps.now ?? (() => Date.now());
  const log = deps.log ?? ((o) => console.log(JSON.stringify({ t: new Date(now()).toISOString(), ...o })));
  const L = cfg.limits;
  const limits = { requests: new Windows(L.requests, now), badInvites: new Windows(L.badInvites, now),
    creates: new Windows(L.creates, now), createsTotal: new Windows(L.createsTotal, now) };
  const hashes = String(deps.inviteHashes ?? '').split(',').map(s => s.trim().toLowerCase()).filter(Boolean);
  const page = fs.readFileSync(path.join(HERE, 'index.html'), 'utf8');
  // the page's script, built from onboard/web/ (tests serve one built with their own pins)
  const bundle = fs.readFileSync(deps.bundle ?? path.join(HERE, 'bundle.js'), 'utf8');
  const style = fs.readFileSync(path.join(HERE, 'style.css'), 'utf8');
  const wc = deps.walletConnect ?? walletConnectOf(cfg);
  const send = sender(csp(wc));
  const exits = new Map();                // nonce -> {name, text, expires}: exit requests to sign
  let queue = Promise.resolve();          // one finish at a time: one payer
  const serial = (fn) => { const p = queue.then(fn, fn); queue = p.catch(() => {}); return p; };

  const chainOf = (name) => {
    const a = I.own(deps.chains, name);
    if (!a) throw new HttpError(400, `chain ${name} is not open here`);
    return a;
  };

  function gate(ip, body) {
    if (limits.badInvites.full(ip)) throw new HttpError(429, 'too many wrong invite codes; wait an hour');
    if (!I.inviteOk(body.invite, hashes)) {
      limits.badInvites.add(ip);
      throw new HttpError(403, 'this page needs a valid invite code');
    }
  }

  function prune() {
    const t = now();
    for (const [k, v] of exits) if (v.expires <= t) exits.delete(k);
  }

  async function checkRoom(f) {
    const wallet = f.name + I.WALLET_SUFFIX;
    if ((await deps.store('taken', { name: f.name, wallet_id: wallet })).taken) throw new HttpError(409, `the name ${f.name} is taken`);
    // one pool per wallet: more capital goes into the vault it has (Add capital)
    if ((await deps.store('owned', { owner: f.owner })).count >= L.vaultsPerOwner) {
      throw new HttpError(403, 'this wallet has its vault already: put more capital in with Add capital under Your vaults');
    }
  }

  // The set-up of a request for the owner's wallet: the transactions (Solana)
  // or calls (EVM) that create the owner's vault with the bot's rules, deposit
  // and pay the bot's gas. Solana: the page's one-time set-up key
  // (`setupKey`, its public key) signs the bot's policies, which are too long
  // for a hardware wallet (solana.mjs setupTxs); the owner signs the rest.
  // The request's terms, with the vault the set-up creates.
  const setupOf = (adapter, r, planned, more = {}) => adapter.setup({ owner: r.owner, profitWallet: r.profitWallet ?? r.profit_wallet,
    feeWallet: r.feeWallet ?? r.fee_wallet ?? null, token: r.token, deposit: r.deposit, gas: r.gas, planned, ...more });

  function setupAnswer(r, s, expires) {
    return { name: r.name, chain: r.chain, done: s.done, transactions: s.transactions ?? [], calls: s.calls ?? null, chainId: s.chainId ?? null,
      owner: s.owner ?? [], key: s.key ?? [], fund: s.fund ?? [], keyFund: s.keyFund ?? null,
      cost: s.cost ?? null, expires: new Date(expires).toISOString(),
      terms: { token: r.token, amount: r.amount, deposit: r.deposit, gas: r.gas, payer: r.payer,
        profitWallet: r.profitWallet ?? r.profit_wallet, feeWallet: r.feeWallet ?? r.fee_wallet ?? null },
      context: { ...s.planned, report: s.report ?? null } };
  }

  // POST /api/intent: a new request and its set-up for the owner's wallet.
  // No message to sign: the set-up's transactions are the owner's signature
  // (only the owner's key can create its vault and pay from its wallet), and
  // the page checks every one of them against its own pins. The name is held
  // while the request waits (intentTtlS); a wallet that cannot pay it all
  // holds nothing.
  async function intent(ip, body) {
    gate(ip, body);
    const adapter = chainOf(body.chain);
    const f = I.fields(body, cfg, adapter.keys.public);
    if (!adapter.venueReady()) throw new HttpError(503, `no venue on ${body.chain} can hold a vault yet`);
    await checkRoom(f);
    if (limits.creates.full(ip) || limits.createsTotal.full('all')) throw new HttpError(429, 'too many new vaults today; try tomorrow');
    const s = await setupOf(adapter, { ...f, payer: adapter.keys.payee }, null, { setupKey: I.text(body.setupKey) || null,
      notBefore: Math.floor(now() / 1000) - cfg.paymentSlackS });
    const nonce = I.newNonce(), expires = now() + cfg.intentTtlS * 1000;
    const text = I.message(f, cfg.chains[f.chain], { site: cfg.site, nonce, expires, payer: adapter.keys.payee });
    limits.creates.add(ip); limits.createsTotal.add('all');
    await deps.store('sign', { chain: f.chain, owner: f.owner, name: f.name, wallet_id: f.name + I.WALLET_SUFFIX, token: f.token,
      amount: f.amount, deposit: f.deposit, gas: f.gas, payer: adapter.keys.payee, profit_wallet: f.profitWallet, special: f.special,
      fee_bps: f.feeBps, fee_wallet: f.feeWallet, intent: text, signature: '', nonce, expires, connected: { planned: s.planned } });
    log({ event: 'requested', chain: f.chain, name: f.name, owner: f.owner, vault: s.planned.vault });
    return { ...setupAnswer({ ...f, payer: adapter.keys.payee }, s, expires), terms_text: text };
  }

  // POST /api/setup: the set-up of a request still waiting for it, built
  // again: what the chain lacks of it (a resume after a partial set-up, the
  // set-up key's last step once the policies exist, or a blockhash that
  // expired), with today's gas. Solana: `setupKey`, the page's set-up key. A
  // request whose set-up is on chain answers done: the page goes on to
  // /api/pay.
  async function setupAgain(ip, body) {
    gate(ip, body);
    const r = await requestOf(body.name, false);
    if (r.state !== 'signed') return { name: r.name, chain: r.chain, done: true, state: r.state };
    const adapter = chainOf(r.chain);
    // today's gas: the page pins it, and a request from before a change carries the old figure
    const terms = I.fields({ chain: r.chain, token: r.token, amount: r.amount, owner: r.owner, profitWallet: r.profit_wallet, name: r.name },
      { ...cfg, reservedNames: [] }, adapter.keys.public);
    const s = await setupOf(adapter, { ...r, gas: terms.gas, deposit: terms.deposit }, r.connected?.planned ?? null,
      { setupKey: I.text(body.setupKey) || null, notBefore: Math.floor(r.created_at) - cfg.paymentSlackS });
    const expires = now() + cfg.intentTtlS * 1000;
    await deps.store('planned', { id: r.id, connected: { planned: s.planned }, gas: terms.gas, deposit: terms.deposit, expires });
    log({ event: 'setup_again', chain: r.chain, name: r.name, vault: s.planned.vault, done: s.done });
    return setupAnswer({ ...r, gas: terms.gas, deposit: terms.deposit, payer: adapter.keys.payee }, s, expires);
  }

  const running = new Set();              // names whose setup runs in this process

  // POST /api/pay: the owner's set-up is on chain; the bot finishes it. The
  // bot spends nothing before: the vault exists as planned with every rule,
  // and the gas reached the bot's key from the owner (the transaction that
  // pays it, `payment`, is read from the chain; when the page lost it, the
  // vault's recent transactions are searched), newer than the request, never
  // used before. A request whose finish stopped resumes here.
  async function pay(ip, body) {
    gate(ip, body);
    const name = I.name(body.name);
    // claimed before the first await: two calls for one request never both run
    if (running.has(name)) throw new HttpError(409, 'the setup of this vault is running; wait');
    running.add(name);
    try { return await payOrResume(name, body); } finally { running.delete(name); }
  }

  async function payOrResume(name, body) {
    const r = await requestOf(name, false);
    const adapter = chainOf(r.chain);
    if (r.state === 'registered' || r.state === 'started') return { name: r.name, state: r.state, vault: r.vault };
    if (r.state === 'signed') {
      const st = await adapter.setupState(r);
      if (st.state === 'replan') throw new HttpError(409, 'the vault planned for this request was taken by another account; sign the set-up again');
      if (st.state !== 'done') throw new HttpError(409, `your set-up is not all on chain yet (${st.missing} steps to come); the page asks again`);
      const v = await adapter.funding(r, { payment: body.payment == null ? null : I.text(body.payment),
        deposit: body.deposit == null ? null : I.text(body.deposit), notBefore: Math.floor(r.created_at) - cfg.paymentSlackS });
      if (v.state === 'pending') throw new HttpError(409, `${v.reason}; the page asks again`);
      if (v.state !== 'ok') {
        log({ event: 'payment_refused', name: r.name, reason: v.reason });
        throw new HttpError(400, v.reason);
      }
      await deps.store('paid', { id: r.id, payment: v.payment });
      log({ event: 'paid', chain: r.chain, name: r.name, payment: v.payment });
      return setup(r.name, adapter, v.deposit);
    }
    await deps.store('resume', { id: r.id });
    log({ event: 'resume', chain: r.chain, name: r.name });
    return setup(r.name, adapter, r.connected?.deposit ?? null);
  }

  // The bot's side of a set-up that is on chain (its lookup table, unless an
  // earlier try made it), then the rows and the profile, then the deposit the
  // set-up carried. One at a time: one payer. The row is read inside the
  // turn. A failure leaves the request stuck, resumable.
  function setup(name, adapter, deposit) {
    return serial(async () => {
      const r = await requestOf(name, false);
      if (r.state !== 'connecting') throw new HttpError(409, `the request is ${r.state}, not being set up`);
      let connected = r.connected?.sent ? r.connected : null;
      let kept = { ...(r.connected ?? {}), deposit };
      try {
        if (!connected) {
          log({ event: 'finish', chain: r.chain, name: r.name, owner: r.owner });
          // the table is saved before it is sent, so a resume finishes it, never a second one
          connected = await adapter.finish(r, { onLookup: (lookup) => {
            kept = { ...kept, lookup };
            return deps.store('connected', { id: r.id, connected: kept });
          } });
          connected = { ...connected, planned: r.connected?.planned, deposit };
          await deps.store('connected', { id: r.id, connected });
        }
        const { dex, pool, execute } = profileVenues(connected, cfg.chains[r.chain].venues);
        const reg = await deps.store('register', { id: r.id, wallet_id: r.wallet_id, label: r.name, secret_env: adapter.keys.secretEnv,
          policy_key_env: adapter.keys.policyKeyEnv, connected,
          profile: { name: r.name, template: cfg.chains[r.chain].template, settings: cfg.chains[r.chain].settings ?? {}, dex, pool, execute_dexes: execute,
            rec: await adapter.record(dex, pool) },
          cap: { mint: adapter.mintOf(r.token), headroom: cfg.capHeadroom } });
        log({ event: 'registered', chain: r.chain, name: r.name, vault: reg.vault });
        // the cap follows the deposit; one not readable yet is read when the page reports it (/api/deposited)
        const funded = deposit ? await recordDeposit(adapter, { ...r, vault: reg.vault }, deposit).catch((e) => {
          log({ event: 'deposit_unread', name: r.name, error: e.message });
          return {};
        }) : {};
        return { name: r.name, chain: r.chain, state: 'registered', vault: reg.vault, token: r.token, deposit: r.deposit, ...funded };
      } catch (e) {
        connected ??= e.connected ?? null;
        await deps.store('stuck', { id: r.id, error: e.message, connected: connected ?? kept }).catch(() => {});
        log({ event: 'setup_stuck', chain: r.chain, name: r.name, error: e.message, vault: connected?.vault ?? null });
        throw new HttpError(502, `the setup stopped: ${e.message}. Your vault and your money are yours and safe: press Resume (nothing is paid twice)`);
      }
    });
  }

  // A deposit transaction of the owner's, read from the chain (from the
  // owner, into this vault), kept once; the profile's cap follows the sum of
  // every deposit. [{token, amount}], or an error when it is not readable.
  async function recordDeposit(adapter, r, signature) {
    const v = await adapter.checkDeposit(signature, { owner: r.owner, vault: r.vault });
    if (v.state === 'pending') throw new HttpError(409, `${v.reason}; the page asks again`);
    if (v.state !== 'ok') throw new HttpError(400, v.reason);
    let maxUsd = null;
    for (const [i, d] of v.deposits.entries()) {
      const out = await deps.store('deposited', { id: r.id, signature: i ? `${signature}:${i}` : signature, token: d.token,
        mint: adapter.mintOf(d.token), amount: d.amount, headroom: cfg.capHeadroom });
      maxUsd = out.max_usd ?? maxUsd;
    }
    log({ event: 'deposited', name: r.name, deposits: v.deposits, max_usd: maxUsd });
    return { deposits: v.deposits, maxUsd };
  }

  async function requestOf(name, registered = true) {
    const r = (await deps.store('get', { name: I.name(name) })).request;
    if (!r || (registered && !r.vault_row)) throw new HttpError(404, `no vault named ${name}`);
    return r;
  }

  // POST /api/tx: an owner's transaction, unsigned. The deposit is the signed
  // amount; 'add' is more capital, any amount, into a vault that is set up;
  // 'close' closes the vault's positions into it (the owner's way out, with
  // or without the bot); 'withdraw' moves idle money to the owner's wallet
  // (token ALL: everything). A chain whose transactions expire (Solana: a
  // blockhash lives about a minute) answers one at a time, `index` of
  // `count`, each built when it is asked for, unless `all` asks for the whole
  // set (one wallet approval, sent right after it). `context` names the vault's
  // accounts for the page's decoder, which checks them against its own pins.
  async function tx(ip, body) {
    gate(ip, body);
    const r = await requestOf(body.name);
    const adapter = chainOf(r.chain);
    const action = I.text(body.action);
    let answer;
    if (action === 'deposit') answer = await adapter.deposit({ owner: r.owner, vault: r.vault, token: r.token, amount: r.deposit });
    else if (action === 'add') {
      const token = I.text(body.token), t = I.own(cfg.chains[r.chain].tokens, token);
      if (!t) throw new HttpError(400, `token: one of ${Object.keys(cfg.chains[r.chain].tokens).join(', ')}`);
      answer = await adapter.deposit({ owner: r.owner, vault: r.vault, token, amount: I.addAmount(body.amount, t) });
    }
    else if (action === 'approve' && body.token != null) {
      // a re-grant with more capital: the rules, then the deposit, one approval
      const token = I.text(body.token), t = I.own(cfg.chains[r.chain].tokens, token);
      if (!t) throw new HttpError(400, `token: one of ${Object.keys(cfg.chains[r.chain].tokens).join(', ')}`);
      answer = await adapter.owner(r.vault_row, { action, owner: r.owner, token, amount: I.addAmount(body.amount, t) });
    } else if (['withdraw', 'revoke', 'approve', 'close'].includes(action)) {
      const amount = I.decimal(body.amount), token = I.text(body.token);
      if (action === 'withdraw') {
        if (token === 'ALL' ? amount !== 'all' : amount !== 'all' && !/^\d{1,12}(\.\d{1,18})?$/.test(amount)) throw new HttpError(400, 'amount: a number or all');
        if (token !== 'ALL' && !I.own(cfg.chains[r.chain].tokens, token)) throw new HttpError(400, `token: ALL or one of ${Object.keys(cfg.chains[r.chain].tokens).join(', ')}`);
      }
      answer = await adapter.owner(r.vault_row, { action, owner: r.owner, token, amount });
    } else throw new HttpError(400, 'action: deposit, add, withdraw, close, revoke or approve');
    const count = answer.transactions.length;
    const context = adapter.context(r);
    // all: a set the wallet signs in one approval (approve, revoke), sent at once in order
    if (!adapter.expires || body.all === true) return { ...answer, count, context };
    const index = body.index === undefined ? 0 : body.index;
    if (count === 0) return { ...answer, count, context };
    if (!(Number.isInteger(index) && index >= 0 && index < count)) throw new HttpError(400, `index: 0 to ${count - 1}`);
    return { ...answer, count, index, context, transactions: [answer.transactions[index]] };
  }

  // POST /api/start: the profile's service, once the vault holds a deposit.
  // Idempotent: a running service stays as it is; one the owner stopped (an
  // exit, a revoke) starts again, and the exit it asked for is over.
  async function start(ip, body) {
    gate(ip, body);
    const r = await requestOf(body.name);
    const st = await chainOf(r.chain).status(r.vault_row);
    if (!chainOf(r.chain).hasDeposit(st) && !(st.positions > 0)) throw new HttpError(409, 'the vault holds nothing yet; deposit first');
    if (!st.botActive) throw new HttpError(409, 'the bot is revoked on this vault');
    const out = await deps.start(serviceCommand(cfg.service.start, r.name));
    if (out.code !== 0) {
      log({ event: 'start_failed', name: r.name, error: out.err.trim().slice(0, 300) });
      throw new HttpError(502, 'the bot could not be started; the operator is told');
    }
    await deps.store('started', { id: r.id });
    log({ event: 'started', name: r.name });
    return { name: r.name, started: true };
  }

  // What the page shows of a vault: the setup still running, the bot active
  // on it, or the bot revoked. Pure.
  function viewOf(r, v) {
    if (['signed', 'connecting', 'stuck'].includes(r.state)) return 'setup';
    if (!v.balances) return 'unknown';
    if (v.botActive === false) return 'revoked';
    if (r.state === 'registered' && !chainOf(r.chain).hasDeposit(v) && !(v.positions > 0)) return 'setup';
    return 'active';
  }

  // The bot's book for the page: the newest rows the profile wrote, rendered
  // by the Telegram bridge (the same text the owner gets in Telegram), and a
  // few figures from the same row for the page's tiles.
  async function statsOf(name) {
    const { feed } = await deps.store('feed', { name });
    const book = feed.book?.row, daily = feed.DAILY?.row;
    if (!book) return null;
    const pv = pricedView(book);
    const band = book.lower != null && book.upper != null ? shownBand(book, book.lower, book.upper) : null;
    const forecast = book.forecast ?? book.band ?? {};
    return {
      at: book.t ?? null, text: telegramMessage(book), daily: daily ? telegramMessage(daily) : null,
      revoked: feed.REVOKED?.row ? telegramMessage(feed.REVOKED.row) : null, exit: feed.OWNER_EXIT?.row ? telegramMessage(feed.OWNER_EXIT.row) : null,
      tiles: { symbol: pv.symbol, price: shownPrice(book, book.last_price ?? book.price), band,
        inRange: book.band?.in_range ?? (book.event === 'in_band' ? true : book.event === 'OUT_OF_BAND' ? false : null),
        equityUsd: book.equity_usd ?? null, feesTodayUsd: book.fees_today_usd ?? null, feesTotalUsd: book.fees_total_usd ?? null,
        paidUsd: book.split?.paid_usd ?? null, feesPerDayUsd: book.fees_per_day_usd ?? null, aprPct: book.apr_pct ?? null,
        mode: book.regime?.mode ?? null, pExit24h: forecast.p_exit_24h_regime ?? forecast.p_exit_24h ?? null,
        pnlUsd: book.since_start?.profit_usd ?? book.pnl_usd ?? null, event: book.event },
    };
  }

  // POST /api/vaults: the owner's vaults, what each holds, its view and, for
  // a vault the bot runs, its book.
  async function vaults(ip, body) {
    gate(ip, body);
    const chain = I.text(body.chain);
    const owner = I.address(chain, body.owner, 'owner');
    const rows = (await deps.store('list', { owner })).requests.filter(r => r.chain === chain);
    const out = [];
    for (const r of rows) {
      const v = { name: r.name, chain: r.chain, state: r.state, vault: r.vault, token: r.token, amount: r.amount, deposit: r.deposit,
        gas: r.gas, payer: r.payer, error: r.state === 'stuck' ? r.error : undefined, created: r.created_at,
        profitWallet: r.profit_wallet, feeWallet: r.fee_wallet };
      if (r.vault_row) {
        try { Object.assign(v, await chainOf(r.chain).status(r.vault_row)); } catch (e) {
          v.error = `the chain could not be read: ${e.message}`;
          log({ event: 'status_unread', name: r.name, error: String(e?.message ?? e).slice(0, 300) });
        }
        const s = await deps.status(serviceCommand(cfg.service.status, r.name));
        v.service = s.out.trim() || 'unknown';
        v.botReachable = v.service === 'active';
        try { v.stats = await statsOf(r.name); } catch (e) { v.stats = null; log({ event: 'stats_failed', name: r.name, error: e.message }); }
        v.exit = (await deps.store('exits', { name: r.name })).exit;
      }
      v.view = viewOf(r, v);
      out.push(v);
    }
    return { vaults: out };
  }

  // POST /api/exit-intent: the message an owner signs to ask the bot to close
  // its positions into the vault and stop. It moves nothing out of the vault.
  async function exitIntent(ip, body) {
    gate(ip, body);
    const r = await requestOf(body.name);
    prune();
    if (exits.size >= L.openIntents) throw new HttpError(503, 'too many open requests; try again in a few minutes');
    const nonce = I.newNonce(), expires = now() + cfg.intentTtlS * 1000;
    const text = [`${cfg.site}: close the positions of ${r.name} into its vault and stop the bot.`, '',
      'This moves no money out of the vault and changes no rule.', `Vault: ${r.vault}`, `Owner: ${r.owner}`,
      `Nonce: ${nonce}`, `Expires: ${new Date(expires).toISOString()}`].join('\n');
    exits.set(nonce, { name: r.name, text, expires });
    return { nonce, message: text };
  }

  // The bytes a Solana wallet reports it signed (base64), or null: a
  // hardware wallet signs an off-chain message around the text (intent.mjs).
  const signedOf = (body) => (typeof body.signedMessage === 'string' && body.signedMessage ? body.signedMessage : null);

  // POST /api/exit: the signed exit request. The bot reads it at its next poll.
  async function exit(ip, body) {
    gate(ip, body);
    const nonce = I.text(body.nonce), e = exits.get(nonce);
    exits.delete(nonce);
    if (!e) throw new HttpError(400, 'unknown or used request; sign a new one');
    if (e.expires <= now()) throw new HttpError(400, 'the request expired; sign a new one');
    if (body.message !== e.text) throw new HttpError(400, 'the signed message is not the one this server issued');
    const r = await requestOf(e.name);
    if (!await I.verify(r.chain, r.owner, e.text, body.signature, signedOf(body))) throw new HttpError(401, 'the signature is not the owner\'s');
    await deps.store('exit', { name: r.name, nonce, signature: String(body.signature) });
    log({ event: 'exit_requested', name: r.name });
    return { name: r.name, requested: true };
  }

  // POST /api/broadcast: a transaction the owner's wallet signed but did not
  // send (WalletConnect's solana_signTransaction), sent as it is.
  async function broadcast(ip, body) {
    gate(ip, body);
    const adapter = chainOf(I.text(body.chain));
    if (!adapter.broadcast) throw new HttpError(400, 'this chain\'s wallet sends its own transactions');
    const t = I.text(body.transaction);
    if (!/^[A-Za-z0-9+/]{100,1700}={0,2}$/.test(t)) throw new HttpError(400, 'transaction: a signed transaction, base64');
    return { signature: await adapter.broadcast(t) };
  }

  // POST /api/confirm: whether a transaction landed: {state: confirmed | pending | failed}.
  async function confirm(ip, body) {
    gate(ip, body);
    const adapter = chainOf(I.text(body.chain));
    return adapter.confirm(I.text(body.signature));
  }

  // POST /api/deposited: a deposit the owner's wallet sent. Its amount is read
  // from the chain (from the owner, into this vault); the profile's cap
  // follows the sum of every deposit, so all of the capital is deployed.
  async function deposited(ip, body) {
    gate(ip, body);
    const r = await requestOf(body.name);
    return { name: r.name, ...await recordDeposit(chainOf(r.chain), r, I.text(body.signature)) };
  }

  // POST /api/max: the most the owner's wallet can put in with `token`
  // (intent.mjs maxAmount), from its balances on chain; `add: true` for more
  // capital into its vault (no gas, no minimum). Reads only.
  async function max(ip, body) {
    gate(ip, body);
    const chain = I.text(body.chain);
    const adapter = chainOf(chain);
    const owner = I.address(chain, body.owner, 'owner');
    const token = I.text(body.token), c = cfg.chains[chain];
    if (!I.own(c.tokens, token)) throw new HttpError(400, `token: one of ${Object.keys(c.tokens).join(', ')}`);
    return { token, ...I.maxAmount(c, token, await adapter.holdings(owner, token), { add: body.add === true }) };
  }

  function publicConfig() {
    return { site: cfg.site, feeBps: cfg.fee.bps, walletConnect: { projectId: wc.projectId, relayUrl: wc.relayUrl, verify: wc.projectId ? wc.verify : [] },
      chains: Object.fromEntries(Object.keys(deps.chains).map(k => {
        const c = cfg.chains[k];
        return [k, { label: c.label, pair: c.pair, template: c.template, payoutToken: c.payoutToken, operatorWallet: c.operatorWallet,
          gas: I.fromRaw(I.gasTotal(c), I.nativeOf(c)[1].decimals), gasToken: I.nativeOf(c)[0], botKeys: deps.chains[k].keys.public,
          tokens: Object.fromEntries(Object.entries(c.tokens).map(([s, t]) => [s, { min: I.minimum(c, s), max: t.max, decimals: t.decimals,
            native: Boolean(t.native), address: t.mint ?? t.address ?? null }])) }];
      })) };
  }

  const routes = { '/api/intent': intent, '/api/setup': setupAgain, '/api/pay': pay, '/api/tx': tx, '/api/start': start,
    '/api/vaults': vaults, '/api/max': max, '/api/exit-intent': exitIntent, '/api/exit': exit, '/api/broadcast': broadcast,
    '/api/confirm': confirm, '/api/deposited': deposited };

  return http.createServer(async (req, res) => {
    const ip = clientOf(req);
    let url;
    try { url = new URL(req.url, 'http://x'); } catch { return send(res, 400, { error: 'bad URL' }); }
    const p = url.pathname;
    if (p !== cfg.base && !p.startsWith(cfg.base + '/')) return send(res, 404, { error: 'not found' });
    const sub = p.slice(cfg.base.length) || '/';
    try {
      if (limits.requests.full(ip)) throw new HttpError(429, 'too many requests; wait a few minutes');
      limits.requests.add(ip);
      if (req.method === 'GET' || req.method === 'HEAD') {
        // the page's links are relative: /bot must be /bot/
        if (p === cfg.base) { res.writeHead(308, { location: `${cfg.base}/` }); return res.end(); }
        if (sub === '/') return send(res, 200, page, 'text/html; charset=utf-8');
        if (sub === '/bundle.js') return send(res, 200, bundle, 'text/javascript; charset=utf-8');
        if (sub === '/style.css') return send(res, 200, style, 'text/css; charset=utf-8');
        if (sub === '/api/config') return send(res, 200, publicConfig());
        return send(res, 404, { error: 'not found' });
      }
      const fn = req.method === 'POST' && routes[sub];
      if (!fn) return send(res, 404, { error: 'not found' });
      const body = await readJson(req, L.bodyBytes);
      return send(res, 200, await fn(ip, body));
    } catch (e) {
      if (e instanceof HttpError) return send(res, e.status, { error: e.message });
      if (e instanceof I.Invalid || e instanceof StoreError) return send(res, 400, { error: e.message });
      log({ event: 'error', path: sub, error: String(e?.message ?? e).slice(0, 500) });
      return send(res, 500, { error: 'the server failed; nothing more was done' });
    }
  });
}

// --- the chains as the server sees them -------------------------------------------------

export async function solanaAdapter(c, { connection, env = process.env, log }) {
  const keys = await S.botKeys(c, env);
  const report = (line) => log({ event: 'connect_report', line: String(line).slice(0, 4000) });
  return { keys, expires: true, venueReady: () => S.vaultVenues(c).length > 0, gas: () => S.gas(connection, keys, c),
    setup: (a) => S.setupTxs(connection, keys, c, a), setupState: (r) => S.setupState(connection, keys, c, r),
    funding: (r, hint) => S.funding(connection, c, r, hint),
    finish: (r, { onLookup }) => S.finishVault(connection, keys, c, r, { onLookup, log: report }),
    record: (venue, pool) => S.poolRecord(connection, venue, pool, c),
    deposit: (a) => S.depositTxs(connection, c, a), owner: (row, a) => S.ownerTxs(connection, c, row, keys, a),
    holdings: (owner, token) => S.holdings(connection, c, owner, token),
    status: (row) => S.status(connection, c, row, keys), hasDeposit: S.hasDeposit,
    context: (r) => S.context(r), mintOf: (token) => I.own(c.tokens, token)?.mint ?? null,
    checkDeposit: (sig, expect) => S.checkDeposit(connection, c, sig, expect),
    broadcast: (tx) => S.broadcast(connection, tx), confirm: (sig) => S.confirm(connection, sig) };
}

// Polygon: the owner's wallet deploys its Safe and, in a second transaction,
// the bot's Roles module and role (a CREATE2 address cannot name the other
// in one); then the deposit and the bot's gas, last (polygon.mjs setupTxs).
// The bot sends nothing for a vault. `now` numbers a request's salt.
export function polygonAdapter(c, { env = process.env, deps = null, log, now = () => Date.now() }) {
  const keys = P.botKeys(c, env, deps);
  const args = (r) => ({ owner: r.owner, profitWallet: r.profitWallet ?? r.profit_wallet, feeWallet: r.feeWallet ?? r.fee_wallet ?? null });
  // what verifySetup finds wrong with the request's planned vault ([]: all of it on chain, as built)
  const problems = (r) => {
    const p = r.connected?.planned;
    return P.verifySetup(keys, c, { ...args(r), safe: p.safe, roles: p.roles, fromBlock: p.fromBlock });
  };
  return { keys, venueReady: () => c.venues.length > 0, gas: () => P.gas(keys, c),
    setup: async (a) => {
      const saltNonce = a.planned?.saltNonce ?? String(saltOf(a.owner, now()));
      const out = await P.setupTxs(keys, c, { ...args(a), token: a.token, deposit: a.deposit, gas: a.gas, saltNonce });
      // the first build's block: verifySetup reads the Roles module's events from it
      const planned = { saltNonce: out.saltNonce, safe: out.safe, roles: out.roles, vault: out.safe, settings: out.safe,
        fromBlock: a.planned?.fromBlock ?? out.fromBlock, report: out.report };
      return { planned, done: false, calls: { setup: out.setup, fund: out.fund }, chainId: out.chainId, report: out.report };
    },
    setupState: async (r) => {
      if (!r.connected?.planned?.safe) return { state: 'replan' };
      const found = await problems(r);
      return found.length ? { state: 'pending', missing: found.length } : { state: 'done' };
    },
    // the bot's gas: the transaction the page names (an EVM transfer leaves no trace to search for)
    funding: async (r, hint) => {
      if (!hint.payment) return { state: 'pending', reason: 'the bot\'s gas is not paid yet' };
      const v = await P.checkPayment(keys, c, hint.payment, { owner: r.owner, payer: r.payer, amount: r.gas, notBefore: hint.notBefore });
      return v.state === 'ok' ? { state: 'ok', payment: hint.payment, deposit: hint.deposit } : v;
    },
    finish: async (r) => {
      const found = await problems(r);
      if (found.length) throw new Error(`the vault is not as built: ${found.join('; ')}`);
      log({ event: 'connect_report', line: JSON.stringify(r.connected.planned.report).slice(0, 4000) });
      return { ...r.connected.planned.report, sent: true };
    },
    record: () => P.poolRecord(keys, c),
    deposit: (a) => P.depositTxs(keys, c, a), owner: (row, a) => P.ownerTxs(keys, c, row, a),
    holdings: (owner, token) => P.holdings(keys, c, owner, token),
    status: (row) => P.status(keys, c, row), hasDeposit: P.hasDeposit,
    context: (r) => ({ safe: r.vault_row.settings, roles: Object.values(r.vault_row.venues)[0].policies[0] }),
    mintOf: (token) => I.own(c.tokens, token)?.address ?? null,
    checkDeposit: (hash, expect) => P.checkDeposit(keys, c, hash, { owner: expect.owner, safe: expect.vault }),
    confirm: (hash) => P.confirm(keys, hash) };
}

// The bot's public keys pinned in onboard.json (botKeys) against the key files
// the environment names: a server whose keys differ from the ones the page's
// bundle pins would only build transactions the page refuses. Pure.
export function keysMatch(c, keys) {
  const pinned = c.botKeys ?? [];
  return pinned.length === keys.public.length && pinned.every((k, i) => k.toLowerCase() === keys.public[i].toLowerCase());
}

async function main() {
  const cfg = loadConfig(process.argv[2]);
  const log = (o) => console.log(JSON.stringify({ t: new Date().toISOString(), ...o }));
  const chains = {};
  const rpc = process.env.LPBOT_RPC || process.env.SOLANA_RPC_URL || 'https://api.mainnet-beta.solana.com';
  for (const [name, make] of [['solana', () => solanaAdapter(cfg.chains.solana, { connection: new Connection(rpc, 'confirmed'), log })],
    ['polygon', () => polygonAdapter(cfg.chains.polygon, { log })]]) {
    if (!cfg.chains[name]) continue;
    try {
      const a = await make();
      if (!keysMatch(cfg.chains[name], a.keys)) throw new Error(`the bot's keys ${a.keys.public.join(', ')} are not the ones onboard.json pins`);
      chains[name] = a;
    } catch (e) { log({ event: 'chain_closed', chain: name, reason: e.message }); }
  }
  const hashes = process.env[cfg.inviteEnv] ?? '';
  if (!hashes) log({ event: 'no_invite', reason: `${cfg.inviteEnv} is not set: every request is refused` });
  const server = makeServer(cfg, { chains, store: pythonStore(), inviteHashes: hashes, log,
    start: (cmd) => run(cmd[0], cmd.slice(1), { timeoutMs: 60_000 }), status: (cmd) => run(cmd[0], cmd.slice(1), { timeoutMs: 10_000 }) });
  server.requestTimeout = 300_000;
  server.listen(cfg.listen.port, cfg.listen.host, () => log({ event: 'listening', host: cfg.listen.host, port: cfg.listen.port,
    chains: Object.keys(chains) }));
}

if (isEntry(import.meta.url)) main().catch(e => { console.error('ERROR:', e.message); process.exitCode = 1; });
