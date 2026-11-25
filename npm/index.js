'use strict';
// Tockward heartbeat client. No dependencies; needs Node 18+ (built-in fetch).
//
//   const tockward = require('tockward');
//   await tockward.ping('abc123');            // "I ran"
//   await tockward.fail('abc123');            // "I failed"
//   await tockward.ping('abc123', { ms: 4200 }); // with the run's duration
//   await tockward.fail('abc123', { message: 'disk full' }); // say why
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

// The server keeps up to 500 characters; the first line is the alert's reason.
const MESSAGE_MAX = 500;

async function send(token, ok, { ms, message, timeout = 10000, retries = 2 } = {}) {
  const url = beatUrl(token, ok, ms);
  const body = message ? String(message).slice(0, MESSAGE_MAX) : undefined;
  const headers = body ? { 'Content-Type': 'text/plain; charset=utf-8' } : undefined;
  let lastError;
  for (let attempt = 0; attempt <= retries; attempt++) {
    if (attempt) await new Promise(r => setTimeout(r, 1000 * attempt));
    try {
      const res = await fetch(url, { method: 'POST', headers, body, signal: AbortSignal.timeout(timeout) });
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

// Remembers the last lines a command printed, for the failure message.
// Colour codes and progress-bar redraws (\r) are left out.
class OutputTail {
  constructor(maxLines = 20) { this.max = maxLines; this.lines = []; this.partial = ''; }
  write(chunk) {
    const text = this.partial + String(chunk).replace(/\x1b\[[0-9;?]*[ -\/]*[@-~]/g, '');
    const parts = text.split('\n');
    this.partial = parts.pop().slice(-1000);
    for (const p of parts) this.push(p);
  }
  push(line) {
    const clean = line.split('\r').filter(Boolean).pop() || '';
    if (!clean.trim()) return;
    this.lines.push(clean.trimEnd().slice(0, 300));
    if (this.lines.length > this.max) this.lines.shift();
  }
  get last() { if (this.partial) { this.push(this.partial); this.partial = ''; } return this.lines; }
}

// "exited with code 3: <last line>", then as much of the last output as fits.
function failureMessage(headline, lines) {
  const first = (lines.length ? `${headline}: ${lines[lines.length - 1].trim()}` : headline).slice(0, 200);
  const kept = [];
  let size = first.length + '\n\nLast output:'.length;
  for (let i = lines.length - 1; i >= 0; i--) {
    if (size + lines[i].length + 1 > MESSAGE_MAX) break;
    size += lines[i].length + 1;
    kept.unshift(lines[i]);
  }
  return kept.length ? `${first}\n\nLast output:\n${kept.join('\n')}` : first;
}

module.exports = {
  OutputTail,
  failureMessage,
  ping: (token, opts) => send(token, true, opts),
  fail: (token, opts) => send(token, false, opts),
  beatUrl,
};
