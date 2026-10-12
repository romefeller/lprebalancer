// The onboarding page in a real browser (Chromium, headless, from
// onboard/web: npm ci && PLAYWRIGHT_BROWSERS_PATH=0 npx playwright install
// chromium), with a wallet injected the way Phantom injects one (the Wallet
// Standard) or an EIP-1193 provider, each signing in node with a test key.
// The page's bundle is built for the test's own bot keys (build.mjs --pins).
// Tests skip when the browser is not installed.
//
// LPBOT_SHOTS=<dir>: every shot() is saved there as <name>-<phone|desktop>.png.
import crypto from 'node:crypto';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { createRequire } from 'module';
import { fileURLToPath } from 'node:url';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const WEB = path.join(HERE, '..', 'onboard', 'web');
const webRequire = createRequire(path.join(WEB, 'package.json'));
const require = createRequire(import.meta.url);
const { Transaction, VersionedTransaction } = require('@solana/web3.js');
const bs58 = require('bs58').default;

process.env.PLAYWRIGHT_BROWSERS_PATH ??= '0';

// Chromium, or null when it is not installed here.
export async function browser() {
  let pw;
  try { pw = webRequire('playwright'); } catch { return null; }
  try { return await pw.chromium.launch({ headless: true }); } catch { return null; }
}

// The page's bundle with these pins over the repository's (cached by content).
export async function testBundle(over) {
  const key = crypto.createHash('sha256').update(JSON.stringify(over)).update(fs.readFileSync(path.join(WEB, 'main.mjs'))).digest('hex').slice(0, 16);
  const out = path.join(os.tmpdir(), `onboard-bundle-${key}.js`);
  if (fs.existsSync(out)) return out;
  const { build } = await import(path.join(WEB, 'build.mjs'));
  await build({ out, over });
  return out;
}

export const PHONE = { viewport: { width: 390, height: 844 }, deviceScaleFactor: 2, isMobile: true, hasTouch: true,
  userAgent: 'Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1' };
export const DESKTOP = { viewport: { width: 1280, height: 900 }, deviceScaleFactor: 1 };

// ed25519 over raw bytes with a web3.js Keypair.
export function edSign(key, bytes) {
  const pk = crypto.createPrivateKey({ format: 'jwk', key: { kty: 'OKP', crv: 'Ed25519',
    d: Buffer.from(key.secretKey.subarray(0, 32)).toString('base64url'), x: Buffer.from(key.publicKey.toBytes()).toString('base64url') } });
  return crypto.sign(null, Buffer.from(bytes), pk);
}

// A Solana wallet in the page: `owner` signs; `send(tx)` lands a signed
// transaction (the fork) and returns its signature. `mode`: {offchain: sign
// messages as a Ledger does (the off-chain message), reject: decline}.
export async function solanaWallet(context, owner, send, mode = {}) {
  const log = [];
  await context.exposeFunction('__testWallet', async (kind, b64) => {
    if (mode.reject) return { error: 'User rejected the request.' };
    const bytes = Buffer.from(b64, 'base64');
    if (kind === 'message') {
      const { offchainShort } = await import('../onboard/intent.mjs');
      const signed = mode.offchain ? offchainShort(bytes.toString('utf8'), 1) : bytes;
      log.push({ kind, text: bytes.toString('utf8') });
      return { signature: edSign(owner, signed).toString('base64'), signed: mode.offchain ? signed.toString('base64') : null };
    }
    const tx = Transaction.from(bytes);
    tx.partialSign(owner);
    log.push({ kind, tx });
    // `send` answers the base58 signature, as a node does; the wallet hands the page its bytes
    try { return { signature: Buffer.from(bs58.decode(await send(tx))).toString('base64') }; } catch (e) { return { error: String(e.message).split('\n')[0] }; }
  });
  await context.addInitScript(({ address }) => {
    const b64 = (u) => btoa(String.fromCharCode(...u));
    const unb64 = (s) => Uint8Array.from(atob(s), c => c.charCodeAt(0));
    const call = async (kind, bytes) => {
      const r = await window.__testWallet(kind, b64(bytes));
      if (r.error) throw Object.assign(new Error(r.error), /reject/i.test(r.error) ? { code: 4001 } : {});
      return r;
    };
    const account = { address, publicKey: new Uint8Array(32), chains: ['solana:mainnet'], features: ['solana:signMessage', 'solana:signAndSendTransaction'] };
    const wallet = { version: '1.0.0', name: 'Test Wallet', icon: 'data:image/svg+xml;base64,PHN2Zy8+', chains: ['solana:mainnet'], accounts: [account],
      features: {
        'standard:connect': { version: '1.0.0', connect: async () => ({ accounts: [account] }) },
        'standard:disconnect': { version: '1.0.0', disconnect: async () => {} },
        'solana:signMessage': { version: '1.0.0', signMessage: async ({ message }) => {
          const r = await call('message', message);
          return [{ signedMessage: r.signed ? unb64(r.signed) : message, signature: unb64(r.signature) }];
        } },
        'solana:signAndSendTransaction': { version: '1.0.0', supportedTransactionVersions: ['legacy', 0], signAndSendTransaction: async ({ transaction }) => {
          const r = await call('tx', transaction);
          return [{ signature: unb64(r.signature) }];
        } },
      } };
    window.addEventListener('wallet-standard:app-ready', (e) => e.detail.register(wallet));
  }, { address: owner.publicKey.toBase58() });
  return log;
}

// An EIP-1193 wallet in the page (window.ethereum), each request answered
// in node by `provider.request({method, params})`.
export async function evmWallet(context, provider) {
  await context.exposeFunction('__testEth', async (method, params) => {
    try { return { result: await provider.request({ method, params }) }; } catch (e) { return { error: String(e.message).split('\n')[0] }; }
  });
  await context.addInitScript(() => {
    window.ethereum = { isTest: true, request: async ({ method, params }) => {
      const r = await window.__testEth(method, params ?? []);
      if (r.error) throw Object.assign(new Error(r.error), /reject/i.test(r.error) ? { code: 4001 } : {});
      return r.result;
    } };
  });
}

// The signature of a signed transaction, base64 (the wallet's answer).
export const sigOf = (tx) => Buffer.from(tx.signatures?.[0]?.signature ?? tx.signatures[0]).toString('base64');
export const b58sig = (tx) => bs58.encode(tx.signatures?.[0]?.signature ?? tx.signatures[0]);
export { VersionedTransaction };

// A page open at the server's /bot/ with the invite in the fragment.
export async function openPage(b, base, invite, { device = PHONE, setup = async () => {} } = {}) {
  const context = await b.newContext({ ...device });
  const errors = [];
  const wallet = await setup(context);
  const page = await context.newPage();
  page.on('pageerror', e => errors.push(String(e)));
  page.on('console', m => { if (m.type() === 'error') errors.push(m.text()); });
  await page.goto(`${base}/bot/#invite=${invite}`);
  await page.waitForFunction(() => document.querySelector('#chains .tab'));
  return { context, page, errors, wallet };
}

// A screenshot of the page in its present state, when LPBOT_SHOTS names a folder.
export async function shot(page, name) {
  const dir = process.env.LPBOT_SHOTS;
  if (!dir) return null;
  fs.mkdirSync(dir, { recursive: true });
  const vp = page.viewportSize();
  const file = path.join(dir, `${name}-${vp.width < 600 ? 'phone' : 'desktop'}.png`);
  await page.screenshot({ path: file, fullPage: true });
  return file;
}

// Wait until the flow's last step is done (or a step failed: its message).
export async function flowDone(page, ms = 240_000) {
  await page.waitForFunction(() => {
    const steps = [...document.querySelectorAll('#f-steps .step')];
    return !document.getElementById('v-flow').hidden && steps.length && (steps.every(s => s.dataset.state === 'done')
      || steps.some(s => s.dataset.state === 'failed'));
  }, null, { timeout: ms });
  return page.evaluate(() => ({
    steps: [...document.querySelectorAll('#f-steps .step')].map(s => ({ label: s.querySelector('.label').textContent, state: s.dataset.state,
      detail: s.querySelector('.detail').textContent })),
    alert: document.getElementById('alert').hidden ? '' : document.getElementById('alert').textContent,
    review: [...document.querySelectorAll('#f-lines li')].map(l => l.textContent),
  }));
}
