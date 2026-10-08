// The chain module of the EVM Uniswap signer, by chain name (config.CHAIN, passed to the
// signer as LPBOT_CHAIN). No name means Unichain, the first chain this signer served.
import * as unichain from './unichain.mjs';
import * as polygon from './polygon.mjs';

export const MODULES = Object.freeze({ unichain, polygon });

export function chainModule(name) {
  const key = name == null || name === '' ? 'unichain' : String(name);
  // own keys only: 'constructor' or '__proto__' must not resolve to Object.prototype's
  const m = Object.hasOwn(MODULES, key) ? MODULES[key] : null;
  if (!m) throw new Error(`the Uniswap signer does not serve chain ${key}; it serves ${Object.keys(MODULES).join(', ')}`);
  return m;
}
