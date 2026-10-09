// Program failures must take precedence over incidental RPC retry messages.
export function signerError(error) {
  const text = [error?.message ?? String(error), ...(error?.logs ?? [])].join(' ');
  if (/PriceSlippageCheck|price slippage check|custom program error: 0x1781|["']?Custom["']?\s*:\s*6017\b/i.test(text)) {
    return 'PriceSlippageCheck (6017): price moved beyond the slippage limit';
  }
  return text;
}

export function isProgramFailure(error) {
  return /PriceSlippageCheck|InstructionError|custom program error|failed on chain|simulation failed|transaction .* failed/i.test(signerError(error));
}

export async function executeBuilt(built) {
  try {
    const { txId } = await built.execute({ sendAndConfirm: true, skipPreflight: false });
    return txId;
  } catch (error) {
    // Execution may already have submitted a transaction. Let the controller
    // read its outcome; never execute the whole operation on another endpoint.
    throw Object.assign(new Error(signerError(error)), { sent: true, cause: error });
  }
}
