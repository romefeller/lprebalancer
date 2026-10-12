// The bundle's `node:crypto`: squads.mjs only hashes (Anchor discriminators)
// with createHash('sha256'). @noble/hashes does it in the browser.
import { sha256 } from '@noble/hashes/sha256';
import { Buffer } from 'buffer';

export function createHash(algorithm) {
  if (algorithm !== 'sha256') throw new Error(`${algorithm} is not in the page's bundle`);
  const parts = [];
  const h = {
    update(data) { parts.push(Buffer.from(typeof data === 'string' ? new TextEncoder().encode(data) : data)); return h; },
    digest(enc) {
      const d = Buffer.from(sha256(Buffer.concat(parts)));
      return enc ? d.toString(enc) : d;
    },
  };
  return h;
}

export default { createHash };
