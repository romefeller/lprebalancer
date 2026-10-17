// Builds the page's script: onboard/web/main.mjs and everything it imports
// (the decoders, the vault rules, WalletConnect, web3.js) into ONE file,
// onboard/bundle.js, which the server serves as it is (CSP: script-src 'self').
//
//   cd onboard/web && npm ci && node build.mjs              # onboard/bundle.js
//   node build.mjs --pins <file.json> --out <bundle.js>      # tests: other bot keys
//
// The bundle carries the PINS: the bot's public keys, the operator's wallets,
// the approved pools and token addresses (onboard.json, solana.json, the
// Polygon module) and the vault rules (rules.json), with the SHA-256 of
// rules.json. The page checks every transaction against these, never against
// what the server says. Exact versions (package-lock.json); the output is
// deterministic for one checkout, so a reviewer can rebuild and compare.
import crypto from 'node:crypto';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import * as esbuild from 'esbuild';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const ONBOARD = path.dirname(HERE), ROOT = path.dirname(ONBOARD);
const MODULES = path.join(HERE, 'node_modules');

const arg = (name) => { const i = process.argv.indexOf(name); return i >= 0 ? process.argv[i + 1] : null; };

// The pins, from the files in this checkout. `over` (tests) replaces keys.
export async function pins(over = {}) {
  const cfg = JSON.parse(fs.readFileSync(path.join(ONBOARD, 'onboard.json'), 'utf8'));
  const solana = JSON.parse(fs.readFileSync(path.join(ROOT, 'chains/solana/solana.json'), 'utf8'));
  const rulesBytes = fs.readFileSync(path.join(ROOT, 'chains/solana/vault/rules.json'));
  const P = await import(path.join(ROOT, 'chains/evm/polygon.mjs'));
  const sol = cfg.chains.solana, poly = cfg.chains.polygon;
  const out = {
    rulesSha256: crypto.createHash('sha256').update(rulesBytes).digest('hex'),
    site: cfg.site,
    solana: {
      payer: sol.botKeys[0], policyKey: sol.botKeys[1], operator: solana[sol.operatorWallet],
      pools: Object.fromEntries(Object.entries(solana[sol.poolsKey]).map(([v, p]) => [v, [p, ...(solana.vault_extra_pools?.[v] ?? [])]])),
      tokens: Object.fromEntries(Object.entries(sol.tokens).map(([s, t]) => [s, { mint: solana[t.mintKey], decimals: t.decimals, native: Boolean(t.native) }])),
    },
    polygon: {
      chainId: P.CHAIN_ID, botKey: poly.botKeys[0], operator: poly.operatorWallet, npm: P.V3.positionManager ?? P.V3.npm ?? P.V3.nonfungiblePositionManager,
      wrapped: { WPOL: P.WPOL },
      // the owner's own vault (decode_evm.mjs createVault): the Safe and Zodiac contracts, the router
      // and stables the rules name, and the one pool (onboard.json's, WPOL/USDT0 at 0.05%)
      vault: { ...P.VAULT }, router: P.V3.router, stables: [...P.STABLES], venue: P.DEX,
      pool: { token0: P.WPOL, token1: P.USDT0, fee: 500 },
      tokens: Object.fromEntries(Object.entries(poly.tokens).map(([s, t]) => [s, { address: t.native ? null : P[s], decimals: t.decimals, native: Boolean(t.native) }])),
    },
  };
  return { ...out, ...over, solana: { ...out.solana, ...(over.solana ?? {}) }, polygon: { ...out.polygon, ...(over.polygon ?? {}) } };
}

// Bare imports resolve from onboard/web/node_modules only (the versions this
// package-lock pins), whatever else is installed above the checkout.
const pinnedModules = {
  name: 'pinned-modules',
  setup(b) {
    b.onResolve({ filter: /^module$/ }, () => ({ path: path.join(HERE, 'shims/module.mjs') }));
    b.onResolve({ filter: /^(node:)?crypto$/ }, () => ({ path: path.join(HERE, 'shims/crypto.mjs') }));
    b.onResolve({ filter: /^[@a-z]/ }, async (a) => {
      if (a.pluginData?.pinned || a.path.startsWith('node:')) return undefined;
      if (a.importer.startsWith(MODULES)) return undefined;
      const r = await b.resolve(a.path, { kind: a.kind, resolveDir: HERE, pluginData: { pinned: true } });
      return r.errors.length ? { errors: r.errors } : r;
    });
  },
};

export async function build({ out = path.join(ONBOARD, 'bundle.js'), over = {} } = {}) {
  const p = await pins(over);
  const result = await esbuild.build({
    entryPoints: [path.join(HERE, 'main.mjs')], bundle: true, format: 'iife', platform: 'browser', target: ['es2022'],
    minify: true, legalComments: 'none', outfile: out, write: true, metafile: true, logLevel: 'warning',
    define: { __PINS__: JSON.stringify(p), 'process.env.NODE_ENV': '"production"', global: 'globalThis', 'import.meta.url': '"bundle:"' },
    inject: [path.join(HERE, 'shims/buffer.mjs')],
    plugins: [pinnedModules],
  });
  const bytes = fs.statSync(out).size;
  return { out, bytes, pins: p, inputs: Object.keys(result.metafile.inputs).length };
}

if (process.argv[1] === fileURLToPath(import.meta.url)) {
  const over = arg('--pins') ? JSON.parse(fs.readFileSync(arg('--pins'), 'utf8')) : {};
  const r = await build({ out: arg('--out') ? path.resolve(arg('--out')) : undefined, over });
  console.log(JSON.stringify({ out: path.relative(process.cwd(), r.out), bytes: r.bytes, rulesSha256: r.pins.rulesSha256 }));
}
