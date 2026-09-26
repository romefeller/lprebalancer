// Refundable rent for Raydium-layout positions. Each address is counted once.
import { PublicKey } from '@solana/web3.js';

const TOKEN_2022 = new PublicKey('TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb');

export async function positionRent(connection, owner, nftMints, programId,
                                   { refundMint = false } = {}) {
  const mints = [...new Map(nftMints.map(m => [m.toBase58(), m])).values()];
  if (!mints.length) return 0;
  const pdas = mints.map(m => PublicKey.findProgramAddressSync(
    [Buffer.from('position'), m.toBuffer()], programId)[0]);
  const accounts = new Map();
  const infos = await connection.getMultipleAccountsInfo([...pdas, ...mints]);
  for (let i = 0; i < mints.length; i++) {
    if (!infos[i] || !infos[i + mints.length]) throw new Error('position rent accounts unavailable');
    accounts.set(pdas[i].toBase58(), infos[i].lamports);
    const mintInfo = infos[i + mints.length];
    // Raydium closes Token-2022 NFT mints. Legacy mint/metadata rent is not refunded.
    if (refundMint && mintInfo.owner.equals(TOKEN_2022)) {
      accounts.set(mints[i].toBase58(), mintInfo.lamports);
    }
    const result = await connection.getTokenAccountsByOwner(owner, { mint: mints[i] });
    for (const { pubkey, account } of result.value) {
      accounts.set(pubkey.toBase58(), account.lamports);
    }
  }
  return [...accounts.values()].reduce((a, b) => a + b, 0) / 1e9;
}
