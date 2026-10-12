import { collectLogs, describeFailure } from './program_errors.mjs';

// Program failures must take precedence over incidental RPC retry messages.
// The text is one bounded line with the decoded program error and the last
// program log lines (shared/program_errors.mjs); a slippage refusal keeps its
// fixed head, which the loop and rebuildOnRefusal match.
export function signerError(error) {
  const text = error?.described ? String(error.message) : describeFailure(error);
  const scan = [error?.message ?? String(error), ...collectLogs(error)].join(' ');
  if (/PriceSlippageCheck|price slippage check|custom program error: 0x1781|["']?Custom["']?\s*:\s*6017\b/i.test(scan)) {
    const detail = text.indexOf(' | program error: ');
    return 'PriceSlippageCheck (6017): price moved beyond the slippage limit' + (detail >= 0 ? text.slice(detail) : '');
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
