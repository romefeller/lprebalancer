// The bundle's `module`: the vault modules load their CommonJS dependencies
// with createRequire(import.meta.url)(name), as node does. In the browser
// the same names map to the bundled modules, and only these.
import * as web3 from '@solana/web3.js';
import bs58 from 'bs58';

const MODULES = { '@solana/web3.js': web3, bs58: { default: bs58 } };

export function createRequire() {
  return (name) => {
    if (!MODULES[name]) throw new Error(`${name} is not in the page's bundle`);
    return MODULES[name];
  };
}
