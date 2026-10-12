// The venue instructions an owner's exit may run as the vault, per venue:
// take the liquidity out, collect fees and rewards, close the position.
// Names exactly as in rules.json. vault/exit.mjs refuses anything else, and
// the owners' signing page reads this list to name what it shows. Pure, no
// imports: a browser loads it as it is.
export const EXIT_INSTRUCTIONS = Object.freeze({
  'meteora-dlmm': Object.freeze(['remove_liquidity_by_range2', 'claim_fee2', 'claim_reward2', 'close_position2', 'close_position_if_empty']),
  'raydium-clmm': Object.freeze(['decrease_liquidity_v2', 'close_position']),
  byreal: Object.freeze(['decrease_liquidity_v2', 'close_position']),
  orca: Object.freeze(['decrease_liquidity_v2', 'collect_fees_v2', 'collect_reward_v2', 'close_position_with_token_extensions']),
});
