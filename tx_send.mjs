// Sending a signed Solana transaction until it lands or provably cannot.
//
// 2026-10-02: payouts and swaps sent once expired unconfirmed ("block height
// exceeded") under load. Each expiry left the outcome unknown, so the payout
// was booked 'uncertain' and the swap a partial, and every write of the
// wallet waited five minutes for the unknown signature. sendUntilLanded
// re-sends the same signed bytes every REBROADCAST_MS until they confirm,
// and after lastValidBlockHeight asks the chain once more: no record means
// the transaction never landed and never can (NeverLanded).
export const REBROADCAST_MS = 2_000;

// Thrown when a sent transaction's blockhash expired and the chain does not
// know its signature: it never landed and never can.
export class NeverLanded extends Error {}

// Send `raw` and send it again every REBROADCAST_MS until it is confirmed or
// its blockhash expires. Returns the signature once confirmed. An error of
// the first send is thrown as it is: nothing proves the transaction went
// out. Every later error carries `afterSend` and the signature, so the caller
// reports it as partial and never sends it twice; NeverLanded (the block
// height passed lastValidBlockHeight and a history search does not find the
// signature) is the one later error that proves nothing went out.
export async function sendUntilLanded(connection, raw, lastValidBlockHeight, { sleep = ms => new Promise(r => setTimeout(r, ms)) } = {}) {
  const signature = await connection.sendRawTransaction(raw, { skipPreflight: false, maxRetries: 0 });
  try {
    for (;;) {
      const st = (await connection.getSignatureStatuses([signature])).value?.[0];
      if (st?.err) throw new Error(`transaction failed on chain: ${JSON.stringify(st.err)}`);
      if (st && (st.confirmationStatus === 'confirmed' || st.confirmationStatus === 'finalized')) return signature;
      if ((await connection.getBlockHeight('confirmed')) > lastValidBlockHeight) {
        const last = (await connection.getSignatureStatuses([signature], { searchTransactionHistory: true })).value?.[0];
        if (last?.err) throw new Error(`transaction failed on chain: ${JSON.stringify(last.err)}`);
        if (last) return signature;                         // it landed after all
        throw new NeverLanded(`expired: block height passed and the chain has no record of ${signature}; nothing was sent`);
      }
      await sleep(REBROADCAST_MS);
      try { await connection.sendRawTransaction(raw, { skipPreflight: true, maxRetries: 0 }); } catch { /* the next status read decides */ }
    }
  } catch (e) {
    throw Object.assign(e, { afterSend: true, signature });
  }
}

