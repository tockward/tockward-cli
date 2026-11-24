'use strict';
// Tockward heartbeat client. No dependencies; needs Node 18+ (built-in fetch).
//
//   const tockward = require('tockward');
//   await tockward.ping('abc123');            // "I ran"
//   await tockward.fail('abc123');            // "I failed"
//   await tockward.ping('abc123', { ms: 4200 }); // with the run's duration
//
// A token can also be a full ping URL (https://.../v1/beat/<token>), so URLs
// copied from the dashboard work as they are. TOCKWARD_URL overrides the base.

const DEFAULT_BASE = 'https://ping.tockward.com';

function beatUrl(token, ok, ms) {
  if (!token || typeof token !== 'string') throw new Error('missing token');
  let url;
  if (/^https?:\/\//i.test(token)) {
    url = token.replace(/\/+$/, '').replace(/\/fail$/, '');
  } else {
    const base = (process.env.TOCKWARD_URL || DEFAULT_BASE).replace(/\/+$/, '');
    url = `${base}/v1/beat/${encodeURIComponent(token)}`;
  }
  if (!ok) url += '/fail';
  if (Number.isFinite(ms)) url += `?ms=${Math.max(0, Math.round(ms))}`;
  return url;
}

async function send(token, ok, { ms, timeout = 10000, retries = 2 } = {}) {
  const url = beatUrl(token, ok, ms);
  let lastError;
  for (let attempt = 0; attempt <= retries; attempt++) {
    if (attempt) await new Promise(r => setTimeout(r, 1000 * attempt));
    try {
      const res = await fetch(url, { method: 'POST', signal: AbortSignal.timeout(timeout) });
      if (res.ok) return true;
      if (res.status === 404) throw Object.assign(new Error('unknown monitor (check the token)'), { final: true });
      lastError = new Error(`HTTP ${res.status}`);
    } catch (err) {
      if (err.final) throw err;
      lastError = err;
    }
  }
  throw lastError;
}

module.exports = {
  ping: (token, opts) => send(token, true, opts),
  fail: (token, opts) => send(token, false, opts),
  beatUrl,
};
