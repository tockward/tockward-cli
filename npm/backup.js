'use strict';
// `tockward backup`: checks the backup files in a folder and returns what's
// wrong, for a heartbeat. Only looks at names, sizes and times; never opens
// or changes a file. The customer says what kind of backup it is:
//
//   full (default)  every file is a full backup
//   incremental     fulls (--full pattern or --full-day) plus increments
//   differential    the same, but each differential grows until the next full
//
// `--detect` prints what the folder looks like and suggests options; it
// sends nothing. Same behaviour and output as the python client
// (tockward/backup.py).

const crypto = require('crypto');
const fs = require('fs');
const os = require('os');
const path = require('path');

class CheckError extends Error {}

const DAYS = ['sun', 'mon', 'tue', 'wed', 'thu', 'fri', 'sat'];
const DAY_NAMES = ['Sunday', 'Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday'];
const SETTLE_SECONDS = 60; // younger files may still be being written
const PARTIAL = /\.(tmp|part|partial|inprogress|lock)$/i;

// "30m", "26h", "8d" → seconds (a bare number is minutes, like tockward git).
function parseDuration(text) {
  const m = /^(\d+)\s*([smhd]?)$/i.exec(String(text || '').trim());
  if (!m) throw new CheckError(`can't read the duration "${text}" (use e.g. 26h, 8d)`);
  return Number(m[1]) * { '': 60, s: 1, m: 60, h: 3600, d: 86400 }[m[2].toLowerCase()];
}

// "50MB", "2G", "500k", "1.5GB", "1000" → bytes (powers of 1024).
function parseSize(text) {
  const m = /^(\d+(?:\.\d+)?)\s*([kmgt]?)i?b?$/i.exec(String(text || '').trim());
  if (!m) throw new CheckError(`can't read the size "${text}" (use e.g. 50MB, 2GB)`);
  return Math.round(Number(m[1]) * 1024 ** ' kmgt'.indexOf(m[2].toLowerCase() || ' '));
}

function parsePercent(text) {
  const m = /^(\d+)\s*%?$/.exec(String(text || '').trim());
  if (!m || Number(m[1]) > 99) throw new CheckError(`can't read the percentage "${text}" (use e.g. 50%, or 0 for off)`);
  return Number(m[1]);
}

// A shell-style pattern (* and ?) on the file name, ignoring case.
function globRe(pattern) {
  const body = String(pattern).split('').map(c => (c === '*' ? '.*' : c === '?' ? '.' : c.replace(/[.+^${}()|[\]\\]/g, '\\$&'))).join('');
  return new RegExp(`^${body}$`, 'i');
}

function parseArgs(args) {
  const o = { folder: null, detect: false, kind: 'full', match: null, full: null, fullDay: null,
    maxAge: 26 * 3600, fullMaxAge: 8 * 86400, minSize: 1, drop: 50, depth: 0 };
  for (let i = 0; i < args.length; i++) {
    const a = args[i];
    const value = () => {
      if (i + 1 >= args.length) throw new CheckError(`${a} needs a value`);
      return args[++i];
    };
    if (a === '--detect') o.detect = true;
    else if (a === '--kind') {
      o.kind = value().toLowerCase();
      if (!['full', 'incremental', 'differential'].includes(o.kind)) throw new CheckError(`--kind is full, incremental or differential, not ${o.kind}`);
    } else if (a === '--match') o.match = value();
    else if (a === '--full') o.full = value();
    else if (a === '--full-day') {
      const d = value().toLowerCase().slice(0, 3);
      if (!DAYS.includes(d)) throw new CheckError('--full-day is a weekday: mon, tue, wed, thu, fri, sat or sun');
      o.fullDay = DAYS.indexOf(d);
    } else if (a === '--max-age') o.maxAge = parseDuration(value());
    else if (a === '--full-max-age') o.fullMaxAge = parseDuration(value());
    else if (a === '--min-size') o.minSize = Math.max(1, parseSize(value()));
    else if (a === '--drop') o.drop = parsePercent(value());
    else if (a === '--depth') o.depth = Math.max(0, parseInt(value(), 10) || 0);
    else if (a.startsWith('--')) throw new CheckError(`unknown option ${a}`);
    else if (o.folder === null) o.folder = a;
    else throw new CheckError(`one folder only (got ${o.folder} and ${a})`);
  }
  if (o.folder === null) throw new CheckError('which folder? e.g. tockward backup <token> /backups/db');
  if (o.kind !== 'full' && o.full === null && o.fullDay === null) {
    throw new CheckError(`--kind ${o.kind} needs --full <pattern> or --full-day <day> to tell the full backups apart`);
  }
  return o;
}

// Backup files in the folder (and depth levels of subfolders), oldest first.
// Hidden files, partial uploads and files still being written are skipped.
function listFiles(root, o, now) {
  const match = o.match ? globRe(o.match) : null;
  const files = [];
  const walk = (dir, level) => {
    let entries;
    try { entries = fs.readdirSync(dir, { withFileTypes: true }); }
    catch (err) { throw new CheckError(`can't read ${dir}: ${err.code === 'EACCES' ? 'permission denied' : err.message}`); }
    for (const e of entries) {
      if (e.name.startsWith('.')) continue;
      const full = path.join(dir, e.name);
      let st; // stat follows symlinks: a linked backup counts like any other
      try { st = fs.statSync(full); } catch { continue; }
      if (st.isDirectory()) { if (level < o.depth) walk(full, level + 1); continue; }
      if (!st.isFile() || PARTIAL.test(e.name) || (match && !match.test(e.name))) continue;
      const mtime = Math.floor(st.mtimeMs / 1000);
      if (now - mtime < SETTLE_SECONDS) continue;
      files.push({ name: path.relative(root, full), size: st.size, mtime });
    }
  };
  walk(root, 0);
  return files.sort((a, b) => a.mtime - b.mtime || a.name.localeCompare(b.name));
}

function isFull(f, o) {
  if (o.kind === 'full') return true;
  if (o.full !== null) return globRe(o.full).test(path.basename(f.name));
  return new Date(f.mtime * 1000).getDay() === o.fullDay;
}

const fmtSize = b => {
  if (b < 1024) return `${b} B`;
  const units = ['KB', 'MB', 'GB', 'TB'];
  let v = b / 1024, i = 0;
  while (v >= 1024 && i < units.length - 1) { v /= 1024; i++; }
  return `${v >= 100 ? Math.round(v) : Math.round(v * 10) / 10} ${units[i]}`;
};
const fmtAge = s => {
  if (s < 3600) return `${Math.max(1, Math.round(s / 60))} min`;
  if (s < 2 * 86400) return `${Math.floor(s / 3600)} h`;
  const d = Math.floor(s / 86400), h = Math.floor((s % 86400) / 3600);
  return h ? `${d} d ${h} h` : `${d} d`;
};
const fmtLimit = s => (s % 86400 === 0 ? `${s / 86400} d` : s % 3600 === 0 ? `${s / 3600} h` : `${Math.round(s / 60)} min`);

// The newest full from the last run, per token and folder, so a backup that
// is overwritten in place (one file) can still be compared with last time.
function stateFile(token, folder) {
  const dir = path.join(process.env.XDG_CACHE_HOME || path.join(os.homedir(), '.cache'), 'tockward');
  return { dir, file: path.join(dir, `backup-${crypto.createHash('sha256').update(`${token}\n${folder}`).digest('hex').slice(0, 16)}.json`) };
}
function readState(token, folder) {
  try { return JSON.parse(fs.readFileSync(stateFile(token, folder).file, 'utf8')); } catch { return null; }
}
function writeState(token, folder, f) {
  const { dir, file } = stateFile(token, folder);
  try { fs.mkdirSync(dir, { recursive: true }); fs.writeFileSync(file, JSON.stringify({ name: f.name, mtime: f.mtime, size: f.size })); } catch {}
}

// Is `now` more than `drop`% smaller than `before`? Returns the text or null.
function dropText(f, before, drop, what) {
  if (!drop || !before || !before.size || f.size >= before.size * (1 - drop / 100)) return null;
  const pct = Math.round((1 - f.size / before.size) * 100);
  return `${f.name} is ${fmtSize(f.size)}, ${pct}% smaller than ${what} (${fmtSize(before.size)})`;
}

function check(token, folder, o, now) {
  const files = listFiles(folder, o, now);
  if (!files.length) return { problems: [`no backups found in ${o.folder}${o.match ? ` matching ${o.match}` : ''}`], summary: null, size: null };
  const problems = [];
  const newest = files[files.length - 1];
  const fulls = files.filter(f => isFull(f, o));
  const full = fulls[fulls.length - 1] || null;

  if (now - newest.mtime > o.maxAge) problems.push(`newest backup is ${fmtAge(now - newest.mtime)} old (limit ${fmtLimit(o.maxAge)}): ${newest.name}`);
  if (newest.size === 0 && newest !== full) problems.push(`${newest.name} is empty`);

  if (!full) {
    problems.push(`no full backup found (${o.full !== null ? `fulls look like ${o.full}` : `fulls are made on ${DAY_NAMES[o.fullDay]}`}): increments are useless without one`);
  } else {
    if (o.kind !== 'full' && now - full.mtime > o.fullMaxAge) problems.push(`newest full backup is ${fmtAge(now - full.mtime)} old (limit ${fmtLimit(o.fullMaxAge)}): ${full.name}`);
    if (full.size === 0) problems.push(`${full.name} is empty`);
    else if (full.size < o.minSize) problems.push(`${full.name} is ${fmtSize(full.size)}, below the minimum of ${fmtSize(o.minSize)}`);
    else {
      // Compared with the full before it, or with last run's if it was
      // overwritten in place.
      const state = readState(token, folder);
      const before = fulls.length > 1 ? fulls[fulls.length - 2]
        : state && state.mtime < full.mtime ? state : null;
      const t = dropText(full, before, o.drop, 'the full backup before it');
      if (t) problems.push(t);
    }
    writeState(token, folder, full);

    if (o.kind === 'differential') {
      const diffs = files.filter(f => f.mtime > full.mtime && !isFull(f, o));
      if (diffs.length > 1) {
        const t = dropText(diffs[diffs.length - 1], diffs[diffs.length - 2], o.drop, 'the differential before it, while differentials should grow until the next full');
        if (t) problems.push(t);
      }
    }
  }

  const n = `${files.length} backup${files.length === 1 ? '' : 's'}`;
  const describe = f => `${f.name}, ${fmtSize(f.size)}, ${fmtAge(now - f.mtime)} old`;
  const summary = o.kind === 'full' || !full || newest === full
    ? `${describe(newest)} (${n})`
    : `newest ${describe(newest)}; full ${describe(full)} (${n})`;
  // The size that goes along for the chart: the newest full, the backup
  // whose size means something from night to night.
  return { problems, summary, size: (full || newest).size };
}

// --- --detect: a suggestion, not a decision -------------------------------

const median = xs => { const s = [...xs].sort((a, b) => a - b); return s.length ? s[Math.floor(s.length / 2)] : 0; };
const TOKENS = ['full', 'incr', 'inc', 'diff', 'weekly', 'daily', 'base'];

// A suggested --max-age for files about `gap` seconds apart.
function suggestMaxAge(gap) {
  const s = gap + Math.max(gap * 0.08, 3600);
  return s >= 2 * 86400 ? `${Math.ceil(s / 86400)}d` : `${Math.ceil(s / 3600)}h`;
}

function detect(folder, o, now) {
  const all = listFiles(folder, o, now);
  const files = all.filter(f => now - f.mtime <= 35 * 86400).slice(-120);
  const lines = [];
  if (!files.length) {
    return [all.length ? `No backups in the last 35 days in ${o.folder} (newest is ${fmtAge(now - all[all.length - 1].mtime)} old).` : `No files found in ${o.folder}.`];
  }
  const gaps = files.slice(1).map((f, i) => f.mtime - files[i].mtime).filter(g => g > 0);
  const gap = median(gaps) || 86400;
  const maxAge = suggestMaxAge(gap);
  const max = Math.max(...files.map(f => f.size));
  const large = files.filter(f => f.size >= max * 0.5);
  const small = files.filter(f => f.size < max * 0.5);
  const range = list => {
    const lo = fmtSize(Math.min(...list.map(f => f.size))), hi = fmtSize(Math.max(...list.map(f => f.size)));
    return lo === hi ? lo : `${lo}-${hi}`;
  };
  const dayOf = f => new Date(f.mtime * 1000).getDay();
  const days = fs_ => { const c = {}; for (const f of fs_) c[dayOf(f)] = (c[dayOf(f)] || 0) + 1; return c; };
  const base = `tockward backup <token> ${o.folder}${o.match ? ` --match '${o.match}'` : ''}${o.depth ? ` --depth ${o.depth}` : ''}`;

  lines.push(`Found ${files.length} file${files.length === 1 ? '' : 's'} in the last 35 days, about one every ${fmtAge(gap)}, newest ${fmtAge(now - files[files.length - 1].mtime)} old.`);

  // All about the same size (or too few to tell): a full backup each time.
  if (files.length < 3 || !small.length || median(small.map(f => f.size)) > median(large.map(f => f.size)) * 0.3) {
    lines.push(`  ${files.length === 1 ? 'one' : 'all'} ${range(files)}: looks like a full backup each time`);
    lines.push(`Suggestion: ${base} --max-age ${maxAge}`);
    return lines;
  }

  // Only the newest is large: the newest is always full (reverse incremental).
  if (large.length === 1 && large[0] === files[files.length - 1]) {
    const ext = path.extname(large[0].name);
    lines.push(`  newest ${large[0].name}, ${fmtSize(large[0].size)}: looks like the full backup, kept up to date`);
    lines.push(`  ${small.length} smaller (${range(small)}): look like the history kept beside it`);
    lines.push(`Suggestion: ${base.replace(/ --match '[^']*'/, '')} --match '*${ext || path.basename(large[0].name)}' --max-age ${maxAge}`);
    return lines;
  }

  const lc = f => path.basename(f.name).toLowerCase();
  const token = TOKENS.find(t => large.every(f => lc(f).includes(t)) && small.every(f => !lc(f).includes(t)));
  const largeExt = [...new Set(large.map(f => path.extname(f.name).toLowerCase()))];
  const ext = largeExt.length === 1 && largeExt[0] && small.every(f => path.extname(f.name).toLowerCase() !== largeExt[0]) ? largeExt[0] : null;
  const ld = days(large), topDay = Object.keys(ld).find(d => ld[d] === large.length);

  // Differential when the small ones clearly grow between fulls.
  let steps = 0, grows = 0;
  for (let i = 1; i < files.length; i++) {
    const [a, b] = [files[i - 1], files[i]];
    if (large.includes(a) || large.includes(b)) continue;
    steps++;
    if (b.size > a.size * 1.02) grows++;
  }
  const kind = steps >= 2 && grows / steps >= 0.8 ? 'differential' : 'incremental';

  const when = topDay !== undefined ? `, on ${DAY_NAMES[topDay]}${large.length > 1 ? 's' : ''}` : '';
  lines.push(`  ${large.length} large (${range(large)}${when}): look like full backups`);
  lines.push(`  ${small.length} smaller (${range(small)}): look like ${kind === 'differential' ? 'differentials (they grow until the next full)' : 'incrementals'}`);
  const fullGap = large.length > 1 ? median(large.slice(1).map((f, i) => f.mtime - large[i].mtime)) : 7 * 86400;
  const fullOpt = token ? `--full '*${token}*'` : ext ? `--full '*${ext}'` : topDay !== undefined ? `--full-day ${DAYS[topDay]}` : null;
  if (!fullOpt) {
    lines.push('  The names don\'t show which files are fulls; give a pattern with --full, e.g. --full \'*full*\'.');
    lines.push(`Suggestion: ${base} --kind ${kind} --full '<pattern>' --max-age ${maxAge} --full-max-age ${suggestMaxAge(fullGap)}`);
    return lines;
  }
  lines.push(`Suggestion: ${base} --kind ${kind} ${fullOpt} --max-age ${maxAge} --full-max-age ${suggestMaxAge(fullGap)}`);
  return lines;
}

// Runs the check (or --detect). Returns { code, message, lines }: code 0 ok,
// 1 problems, 2 the check itself failed; message is what goes to Tockward.
function run(token, args, now = Math.floor(Date.now() / 1000)) {
  try {
    const o = parseArgs(args);
    const folder = path.resolve(o.folder);
    let st;
    try { st = fs.statSync(folder); } catch { throw new CheckError(`${o.folder} does not exist (not mounted?)`); }
    if (!st.isDirectory()) throw new CheckError(`${o.folder} is not a folder`);
    if (o.detect) return { code: 0, detect: true, message: null, lines: detect(folder, o, now) };
    const { problems, summary, size } = check(token, folder, o, now);
    if (!problems.length) {
      const ok = `ok: ${summary}`;
      return { code: 0, message: ok, lines: [ok], value: size };
    }
    return { code: 1, message: problems.join('\n'), lines: problems, value: size };
  } catch (err) {
    if (!(err instanceof CheckError)) throw err;
    return { code: 2, message: `backup check failed: ${err.message}`, lines: [`backup check failed: ${err.message}`] };
  }
}

module.exports = { run, parseArgs, parseSize, parseDuration, globRe, fmtSize, fmtAge };
