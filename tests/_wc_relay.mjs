// A WalletConnect relay on 127.0.0.1 and a wallet behind it, for the page's
// WalletConnect path without the real relay or a Reown project. The relay
// routes the clients' encrypted messages by topic, as the real one does:
//   irn_subscribe / irn_batchSubscribe   a client listens on topics; what was
//                                        published there before is delivered
//   irn_publish {topic, message}         to every listener of the topic
//   wc_proposeSession, wc_approveSession the Sign API's own publishes (2.2x),
//                                        each part to its topic; the sender is
//                                        subscribed to the topic it opens
// It never reads a message: it cannot (they are encrypted between the page
// and the wallet). The wallet is @walletconnect/sign-client from onboard/web,
// with its storage in memory.
import crypto from 'node:crypto';
import path from 'node:path';
import { createRequire } from 'module';
import { fileURLToPath } from 'node:url';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const webRequire = createRequire(path.join(HERE, '..', 'onboard', 'web', 'package.json'));

export async function relay() {
  const { WebSocketServer } = webRequire('ws');
  const wss = new WebSocketServer({ host: '127.0.0.1', port: 0 });
  await new Promise(r => wss.once('listening', r));
  const listeners = new Map();          // topic -> Map(ws -> subscription id)
  const history = new Map();            // topic -> [data]
  const log = [];
  const deliver = (ws, sub, data) => ws.send(JSON.stringify({ id: Date.now() * 1000 + Math.floor(Math.random() * 1000), jsonrpc: '2.0',
    method: 'irn_subscription', params: { id: sub, data } }));
  const publish = (topic, message, extra = {}) => {
    const data = { topic, message, publishedAt: Date.now(), tag: extra.tag ?? 0, ...(extra.attestation ? { attestation: extra.attestation } : {}) };
    history.set(topic, [...(history.get(topic) ?? []), data]);
    for (const [ws, sub] of listeners.get(topic) ?? []) deliver(ws, sub, data);
  };
  const subscribe = (ws, topic) => {
    const sub = crypto.randomBytes(32).toString('hex');
    if (!listeners.has(topic)) listeners.set(topic, new Map());
    listeners.get(topic).set(ws, sub);
    setTimeout(() => { for (const d of history.get(topic) ?? []) deliver(ws, sub, d); }, 20);
    return sub;
  };
  wss.on('connection', (ws) => {
    ws.on('message', (raw) => {
      let m;
      try { m = JSON.parse(String(raw)); } catch { return; }
      if (!m.method) return;                                      // an acknowledgement
      log.push(m.method);
      const p = m.params ?? {};
      const answer = (result) => ws.send(JSON.stringify({ id: m.id, jsonrpc: '2.0', result }));
      switch (m.method) {
        case 'irn_subscribe': return answer(subscribe(ws, p.topic));
        case 'irn_batchSubscribe': return answer(p.topics.map(t => subscribe(ws, t)));
        case 'irn_unsubscribe': listeners.get(p.topic)?.delete(ws); return answer(true);
        case 'irn_batchUnsubscribe': for (const s of p.subscriptions ?? []) listeners.get(s.topic)?.delete(ws); return answer(true);
        case 'irn_publish': publish(p.topic, p.message, p); return answer(true);
        case 'irn_batchFetchMessages': return answer({ messages: [], hasMore: false });
        // the relay subscribes the proposer to the pairing topic, the approver to the session topic
        case 'wc_proposeSession': subscribe(ws, p.pairingTopic); publish(p.pairingTopic, p.sessionProposal, p); return answer(true);
        case 'wc_approveSession':
          subscribe(ws, p.sessionTopic);
          publish(p.pairingTopic, p.sessionProposalResponse, p);
          publish(p.sessionTopic, p.sessionSettlementRequest, p);
          return answer(true);
        default: return answer(true);
      }
    });
    ws.on('close', () => { for (const l of listeners.values()) l.delete(ws); });
  });
  return { url: `ws://127.0.0.1:${wss.address().port}`, log, close: () => new Promise(r => { for (const c of wss.clients) c.terminate(); wss.close(r); }) };
}

// Storage in memory, the shape @walletconnect/keyvaluestorage has.
function memory() {
  const m = new Map();
  return { getKeys: async () => [...m.keys()], getEntries: async () => [...m.entries()], getItem: async (k) => m.get(k),
    setItem: async (k, v) => { m.set(k, v); }, removeItem: async (k) => { m.delete(k); } };
}

// A wallet on the relay: it pairs with a URI, approves with `accounts` for
// `methods`, and answers each request with `handle(method, params)`.
export async function wallet(relayUrl, { accounts, methods, handle, name = 'Tangem' }) {
  const { SignClient } = webRequire('@walletconnect/sign-client');
  const client = await SignClient.init({ projectId: 'f'.repeat(32), relayUrl, storage: memory(), telemetryEnabled: false, logger: 'silent',
    metadata: { name, description: 'a test wallet', url: 'https://wallet.test', icons: [] } });
  const requests = [];
  client.on('session_proposal', async ({ id, params }) => {
    const ns = {};
    for (const [key, acct] of Object.entries(accounts)) {
      ns[key] = { accounts: acct, methods: methods[key], events: [], chains: [...new Set(acct.map(a => a.split(':').slice(0, 2).join(':')))] };
    }
    try { await client.approve({ id, namespaces: ns }); } catch (e) { requests.push({ error: e.message }); }
  });
  client.on('session_request', async ({ topic, id, params }) => {
    const { method, params: p } = params.request;
    requests.push({ method, params: p });
    try {
      const result = await handle(method, p);
      await client.respond({ topic, response: { id, jsonrpc: '2.0', result } });
    } catch (e) {
      await client.respond({ topic, response: { id, jsonrpc: '2.0', error: { code: 5000, message: e.message } } });
    }
  });
  return { client, requests, pair: (uri) => client.core.pairing.pair({ uri }), close: async () => { client.core.heartbeat?.stop?.(); await client.core.relayer.transportClose().catch(() => {}); } };
}
