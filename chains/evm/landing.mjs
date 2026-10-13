// Send signed EVM transactions in order and learn, from the chain, whether each one landed.
//
// 2026-10-13 20:28Z (poly-wpol-usdt): three swaps landed on Polygon and the signer reported
// each one as failed. viem's waitForTransactionReceipt polls eth_getTransactionReceipt; for a
// hash it does not have yet, polygon-bor-rpc.publicnode.com answers HTTP 403 with JSON-RPC
// -32602 "Archive requests require a personal token" (viem: InvalidParamsRpcError, "Invalid
// parameters were provided to the RPC method."). The wait stops on any error that is not
// "receipt not found", so the transaction was reported failed, the next poll swapped again,
// and three such reports wrote HALT.
//
// The rules here:
//   1. The receipt is asked of every endpoint in turn, until one has it or the time is up. An
//      error of one endpoint (a refusal, a missing receipt, a transport failure) is "not yet".
//   2. A send that threw may still have reached a node: its receipt is looked for all the same.
//   3. A step whose receipt says success landed, whatever an RPC said before. Only a receipt
//      with another status (a revert), or no receipt from any endpoint, stops the sequence.
//   4. Nothing is sent again: a step that did not land ends the sequence, partial.

const RECEIPT_POLL_MS = 1_000;
// After a send that threw: how long to look for its receipt before calling it not sent.
export const SEND_ERROR_LOOKUP_MS = 60_000;

const sameHash = (a, b) => String(a).toLowerCase() === String(b).toLowerCase();

// The receipt of `hash` from the first client that has it, polling every client each round
// until `timeoutMs` passes. Never throws: {receipt, error}, where receipt is null when no
// client had it in time and error is the last error any client gave.
export async function findReceipt(clients, hash, opts = {}) {
  const { timeoutMs = 120_000, pollMs = RECEIPT_POLL_MS, sleep = ms => new Promise(r => setTimeout(r, ms)), now = Date.now } = opts;
  const end = now() + timeoutMs;
  let error = null;
  for (;;) {
    for (const c of clients) {
      try {
        const r = await c.getTransactionReceipt({ hash });
        // a receipt of another transaction is no answer (a wrong or broken endpoint)
        if (r && (r.transactionHash == null || sameHash(r.transactionHash, hash))) return { receipt: r, error: null };
      } catch (e) {
        error = e;
      }
    }
    if (now() >= end) return { receipt: null, error };
    await sleep(pollMs);
  }
}

// Send the steps in order, each after the previous one's receipt. Throws only if NOTHING was
// sent; after the first send a failure comes back as {sent, error} so the caller prints a
// partial report instead of letting the endpoint loop retry the whole operation.
//
// Each entry of `sent`: {label, hash, receipt, status}, status 'success' (landed), 'reverted'
// (landed, reverted) or 'unknown' (no endpoint had a receipt in time). `recovered` names the
// steps that landed although their send reported an error.
//
// deps: sign(ctx, step, cfg) -> {serialized, hash}; clients() -> the clients asked for
// receipts (ctx.pub first); errorLine(e) -> one line; timeoutMs; sendErrorLookupMs;
// receipt (findReceipt's options: sleep, now, pollMs).
export async function sendSteps(ctx, steps, cfg, deps) {
  const { sign, clients, errorLine, timeoutMs = 120_000, sendErrorLookupMs = SEND_ERROR_LOOKUP_MS, receipt: rOpts = {} } = deps;
  const sent = [], recovered = [];
  for (const step of steps) {
    let s;
    try {
      s = await sign(ctx, step, cfg);
    } catch (e) {
      if (!sent.length) throw e;
      return { sent, recovered, error: errorLine(e) };
    }
    let sendError = null;
    try {
      await ctx.pub.sendRawTransaction({ serializedTransaction: s.serialized });
    } catch (e) {
      sendError = e;
    }
    const found = await findReceipt(clients(), s.hash, { ...rOpts, timeoutMs: sendError ? sendErrorLookupMs : timeoutMs });
    if (!found.receipt) {
      // A signed transaction whose send threw may still reach the chain: it counts as sent.
      sent.push({ label: step.label, hash: s.hash, receipt: null, status: 'unknown' });
      const why = sendError ?? new Error(`no endpoint has a receipt for ${s.hash} (${step.label}): ${found.error ? errorLine(found.error) : 'not found'}`);
      return { sent, recovered, error: errorLine(why) };
    }
    ctx.nonce += 1;
    if (found.receipt.status !== 'success') {
      sent.push({ label: step.label, hash: s.hash, receipt: found.receipt, status: 'reverted' });
      return { sent, recovered, error: `transaction ${s.hash} (${step.label}) reverted on chain` };
    }
    sent.push({ label: step.label, hash: s.hash, receipt: found.receipt, status: 'success' });
    if (sendError) recovered.push(`${step.label} landed although its send reported: ${errorLine(sendError)}`);
    ctx.minBlock = found.receipt.blockNumber;          // the next step is checked at or past this block
  }
  return { sent, recovered, error: null };
}

// What a report says of each sent transaction: label, hash, status. Pure.
export function receiptsOf(sent) {
  return sent.map(s => ({ label: s.label, hash: s.hash, status: s.status }));
}
