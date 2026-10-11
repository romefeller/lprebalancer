// Orca Whirlpool instructions for an owner that does not sign for itself: a
// Squads vault (chains/solana/vault/account.mjs wraps what it signs).
//
// The SDK builds for one signer that both funds and owns. For a vault it is
// handed a signer that never signs (the vault's address), and what it built
// is then turned into what the vault runs:
//   - rent is the payer key's: a position (and a new tick array) is funded by
//     the payer, and a closed position's rent goes back to it. The vault
//     funds only its own token accounts, which it gets back when it closes
//     one. Funding needs no vault signature, so an open runs outside the
//     policy; the owner of the position is still the vault.
//   - the position mint is a fresh keypair the SDK made; it signs the
//     transaction the vault's instructions are wrapped into.
import { createRequire } from 'node:module';
import { kitToWeb3 } from './swap.mjs';
import SOLANA from '../../chains/solana/solana.json' with { type: 'json' };

const require = createRequire(import.meta.url);
const { PublicKey } = require('@solana/web3.js');

const WHIRLPOOL = new PublicKey(SOLANA.programs.orca_whirlpool);
// discriminator -> the account that pays (funder) or is paid (receiver) rent
const RENT_ACCOUNT = {
  d42f5f5c726683fa: 0,      // open_position_with_token_extensions: funder
  '0bbcc1d68d5b95b8': 1,    // initialize_tick_array: funder
  '2921a5c878e78e32': 1,    // initialize_dynamic_tick_array: funder
  '01b6873b9b1963df': 1,    // close_position_with_token_extensions: receiver
};

// The SDK's instructions as web3.js ones, the rent the payer's. Pure.
export function forVault(kitIxs, payer) {
  return kitIxs.map(kitToWeb3).map(ix => {
    const at = ix.programId.equals(WHIRLPOOL) ? RENT_ACCOUNT[Buffer.from(ix.data.subarray(0, 8)).toString('hex')] : undefined;
    if (at === undefined) return ix;
    ix.keys[at] = { ...ix.keys[at], pubkey: payer };
    return ix;
  });
}

// The keypairs the SDK made and named as signers (a position mint): every
// signer of the instructions that can sign, other than `owner`.
export function madeSigners(kitIxs, owner) {
  const out = new Map();
  for (const ix of kitIxs) {
    for (const a of ix.accounts ?? []) {
      if (a.signer?.signMessages && String(a.address) !== owner.toBase58()) out.set(String(a.address), a.signer);
    }
  }
  return [...out.values()];
}

// Add their signatures to a built transaction (the payer and policy key
// signed it already).
export async function signWith(tx, signers) {
  const message = tx.message.serialize();
  for (const s of signers) {
    const [sigs] = await s.signMessages([{ content: message, signatures: {} }]);
    tx.addSignature(new PublicKey(String(s.address)), sigs[s.address]);
  }
  return tx;
}
