// The page's script (onboard/index.html). It talks to its own server only;
// every transaction goes through the owner's wallet, which signs and sends.
(() => {
  const $ = (id) => document.getElementById(id);
  const api = (p) => new URL(`api/${p}`, location.href.replace(/[?#].*$/, '').replace(/\/?$/, '/')).toString();
  const sleep = (ms) => new Promise(r => setTimeout(r, ms));

  // An amount box shows a point for the decimal comma a phone keyboard types (iPhone,
  // pt-BR): "0,18" reads 0.18. The server takes either.
  document.addEventListener('input', (e) => {
    const box = e.target;
    if (box.matches?.('input[inputmode="decimal"]') && box.value.includes(',')) box.value = box.value.replace(/,/g, '.');
  });

  // --- small helpers ----------------------------------------------------------
  function el(tag, attrs = {}, ...kids) {
    const e = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs)) {
      if (k === 'onclick') e.onclick = v; else if (k === 'text') e.textContent = v; else e.setAttribute(k, v);
    }
    for (const k of kids) e.append(k);
    return e;
  }
  const say = (msg) => { $('error').textContent = msg ? String(msg) : ''; };
  const step = (msg, cls = '') => $('progress').append(el('p', { class: cls, text: msg }));
  const b64 = (bytes) => btoa(String.fromCharCode(...bytes));
  const unb64 = (s) => Uint8Array.from(atob(s), c => c.charCodeAt(0));
  const B58 = '123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz';
  function b58(bytes) {
    let n = 0n;
    for (const b of bytes) n = n * 256n + BigInt(b);
    let s = '';
    while (n > 0n) { s = B58[Number(n % 58n)] + s; n /= 58n; }
    for (const b of bytes) { if (b) break; s = '1' + s; }
    return s;
  }
  const hexUtf8 = (s) => '0x' + Array.from(new TextEncoder().encode(s), b => b.toString(16).padStart(2, '0')).join('');

  // --- the invite: in the URL fragment (never sent to a server) or typed ------
  const fromHash = new URLSearchParams(location.hash.slice(1)).get('invite') || '';
  if (!fromHash) $('invite-box').hidden = false;
  const invite = () => fromHash || $('invite').value.trim();

  async function post(p, body) {
    const r = await fetch(api(p), { method: 'POST', headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ invite: invite(), ...body }) });
    const j = await r.json().catch(() => ({ error: `HTTP ${r.status}` }));
    if (!r.ok) throw Object.assign(new Error(j.error || `HTTP ${r.status}`), { status: r.status });
    return j;
  }

  // --- wallets ------------------------------------------------------------------
  // Solana: the Wallet Standard (Phantom, Solflare, Backpack), else Phantom's
  // injected provider. Polygon: the EIP-1193 provider Phantom or another wallet injects.
  const standard = [];
  const register = (...ws) => { for (const w of ws) standard.push(w); return () => {}; };
  window.addEventListener('wallet-standard:register-wallet', (e) => e.detail?.({ register }));
  window.dispatchEvent(new CustomEvent('wallet-standard:app-ready', { detail: { register } }));

  let wallet = null;      // {chain, address, signMessage(text), sendAll(txs)}

  async function solanaWallet() {
    const w = standard.find(x => x.chains?.includes('solana:mainnet') && x.features?.['solana:signMessage']
      && x.features?.['solana:signAndSendTransaction']);
    if (w) {
      const { accounts } = await w.features['standard:connect'].connect();
      const account = accounts.find(a => a.chains?.includes('solana:mainnet')) ?? accounts[0];
      if (!account) throw new Error('the wallet shared no account');
      return { chain: 'solana', name: w.name, address: account.address,
        async signMessage(text) {
          const [out] = await w.features['solana:signMessage'].signMessage({ account, message: new TextEncoder().encode(text) });
          return b64(out.signature);
        },
        async send(tx) {
          const [out] = await w.features['solana:signAndSendTransaction'].signAndSendTransaction({ account, transaction: unb64(tx), chain: 'solana:mainnet' });
          return b58(out.signature);
        } };
    }
    const p = window.phantom?.solana ?? window.solana;
    if (!p?.signMessage) throw new Error('no Solana wallet here: open this page in Phantom');
    const r = await p.connect();
    return { chain: 'solana', name: 'Phantom', address: String(r?.publicKey ?? p.publicKey),
      async signMessage(text) { return b64((await p.signMessage(new TextEncoder().encode(text), 'utf8')).signature); },
      async send(tx) {
        const bytes = unb64(tx);
        const message = bytes.slice(1 + 64 * bytes[0]);     // a wire transaction: signatures, then the message
        return (await p.request({ method: 'signAndSendTransaction', params: { message: b58(message) } })).signature;
      } };
  }

  async function polygonWallet() {
    const p = window.phantom?.ethereum ?? window.ethereum;
    if (!p?.request) throw new Error('no Polygon wallet here: open this page in Phantom');
    const [address] = await p.request({ method: 'eth_requestAccounts' });
    if (!address) throw new Error('the wallet shared no account');
    const onPolygon = async (chainId) => {
      if ((await p.request({ method: 'eth_chainId' })).toLowerCase() === chainId) return;
      await p.request({ method: 'wallet_switchEthereumChain', params: [{ chainId }] });
    };
    return { chain: 'polygon', name: 'wallet', address,
      async signMessage(text) { return p.request({ method: 'personal_sign', params: [hexUtf8(text), address] }); },
      async send(tx, chainId) {
        await onPolygon(`0x${chainId.toString(16)}`);
        const hash = await p.request({ method: 'eth_sendTransaction', params: [{ from: address, to: tx.to, data: tx.data, value: tx.value }] });
        for (let i = 0; i < 90; i++) {                      // wait for it: the next one may need it
          const r = await p.request({ method: 'eth_getTransactionReceipt', params: [hash] });
          if (r && r.status === '0x1') return hash;
          if (r) throw new Error(`transaction ${hash} reverted`);
          await sleep(2000);
        }
        throw new Error(`transaction ${hash} not confirmed after 3 minutes`);
      } };
  }

  // Each transaction of an action, one after the other, by the wallet. A
  // Solana answer holds one transaction of `count`: the next is asked for when
  // its turn comes, so its blockhash is fresh.
  async function signAndSend(request) {
    const sent = [];
    let answer = await post('tx', { ...request, index: 0 });
    for (let i = 0; i < answer.count; i++) {
      if (i >= answer.transactions.length && answer.index !== undefined) answer = await post('tx', { ...request, index: i });
      const tx = answer.index !== undefined ? answer.transactions[0] : answer.transactions[i];
      step(`Confirm ${i + 1} of ${answer.count} in your wallet: ${answer.summary}`);
      sent.push(await wallet.send(tx, answer.chainId));
    }
    return sent;
  }

  // --- the form -----------------------------------------------------------------
  let cfg = null;

  function fillTokens() {
    const c = cfg.chains[$('chain').value];
    $('token').replaceChildren(...Object.keys(c.tokens).map(s => el('option', { value: s, text: s })));
    showBounds();
  }
  function showBounds() {
    const c = cfg.chains[$('chain').value], t = c.tokens[$('token').value];
    const fee = $('profit').value.trim() && $('profit').value.trim().toLowerCase() !== c.operatorWallet.toLowerCase()
      ? ` Fee share: ${cfg.feeBps / 100}% of the ${c.payoutToken} fees.` : '';
    const gas = t.native ? `, of which ${c.gas} ${c.gasToken} pays the bot's gas` : `; you also pay ${c.gas} ${c.gasToken} for the bot's gas`;
    $('bounds').textContent = `${c.pair} pool, the strategy of ${c.template}. From ${t.min} to ${t.max} ${$('token').value}${gas}.${fee}`;
  }

  $('connect').onclick = async () => {
    say('');
    try {
      wallet = $('chain').value === 'solana' ? await solanaWallet() : await polygonWallet();
      $('wallet').textContent = `Connected: ${wallet.address}`;
      if (!$('profit').value) $('profit').value = wallet.address;
      $('form').hidden = false;
      showBounds();
      await listVaults();
    } catch (e) { say(e.message ?? e); }
  };

  // Max: the most the connected wallet can put in with the token, as the
  // server reads its balances (the owner's fees kept back, never above the
  // token's max). Not enough leaves the box as it is and says why.
  $('max').onclick = async () => {
    say('');
    const token = $('token').value, w = wallet;
    $('max').disabled = true;
    try {
      if (!w || w.chain !== $('chain').value) throw new Error('connect a wallet on this chain first');
      const m = await post('max', { chain: w.chain, owner: w.address, token });
      if (wallet === w && $('token').value === token) {
        if (m.amount) $('amount').value = m.amount; else say(m.short);
      }
    } catch (e) { say(e.message ?? e); }
    $('max').disabled = false;
  };

  $('sign').onclick = async () => {
    say('');
    $('progress').replaceChildren();
    $('sign').disabled = true;
    try {
      if (!wallet || wallet.chain !== $('chain').value) throw new Error('connect a wallet on this chain first');
      const it = await post('intent', { chain: wallet.chain, owner: wallet.address, token: $('token').value,
        amount: $('amount').value.trim(), profitWallet: $('profit').value.trim(), name: $('name').value.trim() });
      $('message').textContent = it.message;
      $('message').hidden = false;
      step('Sign the message in your wallet. It only asks; it moves nothing.');
      const signature = await wallet.signMessage(it.message);
      const pay = await post('sign', { nonce: it.nonce, message: it.message, signature });
      step(`Confirm in your wallet: ${pay.summary}`);
      const payment = await wallet.send(pay.transactions[0], pay.chainId);
      remember(pay.name, payment);
      await setUp(pay.name, payment);
      await deposit(pay.name);
    } catch (e) { say(e.message ?? e); }
    $('sign').disabled = false;
    await listVaults().catch(() => {});
  };

  // The gas payment of a request, kept in this browser so a setup that stops
  // can resume with it (the server never takes a payment twice).
  const remember = (name, payment) => { try { localStorage.setItem(`aperture-payment-${name}`, payment); } catch { /* private mode */ } };
  const recall = (name) => { try { return localStorage.getItem(`aperture-payment-${name}`) || ''; } catch { return ''; } };

  // The server checks the payment on chain, then creates and registers the vault.
  async function setUp(name, payment) {
    step('Payment sent. Creating your vault once it is confirmed; this takes about a minute…');
    for (let i = 0; i < 60; i++) {
      try {
        const made = await post('pay', { name, payment });
        step(`Vault ready: ${made.vault}`, 'ok');
        return made;
      } catch (e) {
        if (e.status !== 409) throw e;
      }
      await sleep(3000);
    }
    throw new Error('the payment is not confirmed after 3 minutes; press Resume under Your vaults later. Do not pay again.');
  }

  // Deposit the signed amount, then start the bot once the vault holds it. A
  // vault that holds a deposit already (a resume) only starts.
  async function deposit(name) {
    try {
      await post('start', { name });
      step(`The bot runs ${name}.`, 'ok');
      return;
    } catch (e) {
      if (e.status !== 409 || /revoked/.test(e.message)) throw e;
    }
    await signAndSend({ name, action: 'deposit' });
    await startWhenFunded(name);
  }

  // Start the bot once the money sent to the vault lands.
  async function startWhenFunded(name) {
    step('Deposit sent. Starting the bot when it lands…');
    for (let i = 0; i < 40; i++) {
      try {
        await post('start', { name });
        step(`The bot runs ${name}.`, 'ok');
        return;
      } catch (e) {
        if (e.status !== 409 || /revoked/.test(e.message)) throw e;
      }
      await sleep(3000);
    }
    throw new Error('the deposit has not landed yet; press Start under Your vaults later');
  }

  // --- the owner's vaults -----------------------------------------------------------
  async function listVaults() {
    if (!wallet) return;
    const { vaults } = await post('vaults', { chain: wallet.chain, owner: wallet.address });
    // one pool per wallet: a wallet with a vault adds capital to it, never makes another
    $('mine').hidden = vaults.length === 0;
    $('form').hidden = vaults.length > 0;
    $('vaults').replaceChildren(...vaults.map(card));
  }

  function card(v) {
    const status = v.state === 'signed' ? 'waiting for the gas payment'
      : v.state === 'connecting' ? 'being created'
        : v.state === 'stuck' ? `setup stopped: ${v.error}`
          : !v.vault || !v.balances ? v.state
        : v.botActive === false ? 'bot revoked'
          : v.state === 'started' ? (v.service === 'active' ? 'running' : `service ${v.service}`) : 'waiting for a deposit';
    const holds = v.balances ? Object.entries(v.balances).map(([s, a]) => `${a} ${s}`).join(' · ') : '';
    const box = el('div', { class: 'vault' },
      el('strong', { text: v.name }), ' ', el('span', { class: 'muted', text: status }),
      el('p', { class: 'mono muted', text: v.vault || '' }),
      el('p', { class: 'small', text: holds ? `In the vault (not in a position): ${holds}` : '' }));
    const act = async (what, fn) => {
      say('');
      $('progress').replaceChildren();
      try { await fn(); step(`${what}: done.`, 'ok'); } catch (e) { say(`${what}: ${e.message ?? e}`); }
      await sleep(4000);
      await listVaults().catch(() => {});
    };
    if (['signed', 'connecting', 'stuck'].includes(v.state)) {
      const payment = el('input', { placeholder: 'the gas payment (signature or hash)', value: recall(v.name) });
      if (v.state === 'signed') box.append(payment);
      box.append(el('button', { text: 'Resume', onclick: () => act('Resume', async () => {
        await setUp(v.name, payment.value.trim());
        await deposit(v.name);
      }) }));
      return box;
    }
    if (!v.balances) return box;
    if (v.state === 'registered' && v.botActive) {
      box.append(el('button', { text: 'Deposit', onclick: () => act('Deposit', () => deposit(v.name)) }));
      box.append(el('button', { text: 'Start', onclick: () => act('Start', () => post('start', { name: v.name })) }));
    }
    if (v.state === 'registered' || v.state === 'started') box.append(addCapital(v, act));
    const token = el('select', {}, ...Object.keys(v.balances || {}).map(s => el('option', { value: s, text: s })));
    const amount = el('input', { placeholder: 'amount or all', inputmode: 'decimal', value: 'all' });
    box.append(el('div', { class: 'row' }, amount, token));
    box.append(el('button', { text: 'Withdraw', onclick: () => act('Withdraw', async () =>
      signAndSend({ name: v.name, action: 'withdraw', token: token.value, amount: amount.value.trim() })) }));
    if (v.rulesCurrent === false || (v.botActive === false && v.chain === 'solana')) {
      box.append(el('p', { class: 'warn', text: v.botActive
        ? 'The bot\'s rules were updated: approve them, or the bot stays paused on this vault.'
        : 'The bot is revoked on this vault: approve to let it run again.' }));
      box.append(el('button', { text: 'Approve bot rules', onclick: () => act('Approve', () => signAndSend({ name: v.name, action: 'approve' })) }));
    }
    if (v.botActive) {
      box.append(el('button', { text: 'Revoke bot', onclick: () => {
        if (!confirm(`Revoke the bot on ${v.name}? It stops acting on this vault; your money stays in it.`)) return;
        act('Revoke', () => signAndSend({ name: v.name, action: 'revoke' }));
      } }));
    }
    return box;
  }

  // Add capital: more money into the vault this wallet has, any amount, at any
  // time. The owner's wallet signs the transfer; the bot puts it in the pool.
  function addCapital(v, act) {
    const tokens = cfg.chains[v.chain].tokens;
    const amount = el('input', { placeholder: 'amount', inputmode: 'decimal', autocomplete: 'off' });
    const token = el('select', {}, ...Object.keys(tokens).map(s => el('option', { value: s, text: s })));
    const max = el('button', { type: 'button', text: 'Max', onclick: async () => {
      say('');
      const t = token.value;
      max.disabled = true;
      try {
        const m = await post('max', { chain: v.chain, owner: wallet.address, token: t, add: true });
        if (token.value === t) { if (m.amount) amount.value = m.amount; else say(m.short); }
      } catch (e) { say(e.message ?? e); }
      max.disabled = false;
    } });
    return el('div', { class: 'add' }, el('label', { text: 'Add capital to this vault' }),
      el('div', { class: 'row' }, amount, max, token),
      el('button', { class: 'primary', text: 'Add capital', onclick: () => act('Add capital', async () => {
        await signAndSend({ name: v.name, action: 'add', token: token.value, amount: amount.value.trim() });
        if (v.state === 'started') step('The bot puts it in the pool at its next check.', 'ok');
        else await startWhenFunded(v.name);
      }) }));
  }

  // --- start ------------------------------------------------------------------------
  fetch(api('config')).then(r => r.json()).then(c => {
    cfg = c;
    $('chain').replaceChildren(...Object.entries(c.chains).map(([k, x]) => el('option', { value: k, text: `${x.label} · ${x.pair}` })));
    if (!Object.keys(c.chains).length) say('No chain is open right now.');
    else fillTokens();
  }).catch(() => say('The server does not answer. Try again later.'));
  $('chain').onchange = () => { wallet = null; $('wallet').textContent = ''; $('form').hidden = true; $('mine').hidden = true; fillTokens(); };
  $('token').onchange = showBounds;
  $('profit').oninput = showBounds;
})();
