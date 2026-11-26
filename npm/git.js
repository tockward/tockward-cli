'use strict';
// `tockward git`: checks repos on this machine against their remote and
// returns what's wrong, for a heartbeat. Read-only: only git status,
// rev-parse, for-each-ref, cat-file, merge-base and ls-remote; never fetch
// or pull. Uses this machine's own git setup and access.
//
//   mirror (default)  every repo under the folder (a Git server's repos):
//                     are its branches and tags the same as the hosted
//                     remote (GitHub, GitLab, Bitbucket, else origin)?
//                     With --compare-with: the same, against a URL per
//                     repo, for servers that are only pushed to (push to
//                     both) and so have no remote.
//   deploy (--deploy) one checkout (a production server): nothing changed
//                     by hand, on the expected branch, on the remote's
//                     latest commit.
//
// Same behaviour and output as the python client (tockward/git.py).

const { spawnSync } = require('child_process');
const crypto = require('crypto');
const fs = require('fs');
const os = require('os');
const path = require('path');

const HOSTED = /(^|[@/.])(github\.com|gitlab\.com|bitbucket\.org)[:/]/i;
const GIT_TIMEOUT = 60000;

class CheckError extends Error {}

// Extra git config for every git call (the tokens for --compare-with), as
// GIT_CONFIG_* environment variables: never on a command line, where other
// users could see it in ps, and never in a URL that can end up in a message.
let gitEnv = {};

function git(dir, args, { allowFail = false } = {}) {
  const r = spawnSync('git', ['-C', dir, ...args], {
    encoding: 'utf8',
    timeout: GIT_TIMEOUT,
    stdio: ['ignore', 'pipe', 'pipe'],
    env: { ...process.env, GIT_TERMINAL_PROMPT: '0', LC_ALL: 'C', ...gitEnv },
  });
  if (r.error) {
    if (r.error.code === 'ENOENT') throw new CheckError('git is not installed');
    if (allowFail) return null;
    throw new CheckError(r.error.code === 'ETIMEDOUT' ? `git ${args[0]} timed out` : r.error.message);
  }
  if (r.status !== 0) {
    if (allowFail) return null;
    const lines = (r.stderr || '').split('\n').map(l => l.trim()).filter(Boolean);
    const why = lines.find(l => /^(fatal|error):/.test(l)) || lines[0] || `exit ${r.status}`;
    throw new CheckError(why.replace(/^(fatal|error): /, ''));
  }
  return r.stdout;
}

// "30m", "2h", "90s", "1d" → seconds.
function parseDuration(text) {
  const m = /^(\d+)\s*([smhd]?)$/i.exec(String(text || '').trim());
  if (!m) throw new CheckError(`can't read the duration "${text}" (use e.g. 30m, 2h, 1d)`);
  return Number(m[1]) * { '': 60, s: 1, m: 60, h: 3600, d: 86400 }[m[2].toLowerCase()];
}

function parseArgs(args) {
  const o = { folder: '.', deploy: false, branch: null, remote: null, behindAfter: 0, depth: 3, compareWith: null, map: {}, skip: [], tokenFile: null };
  for (let i = 0; i < args.length; i++) {
    const a = args[i];
    const value = () => {
      if (i + 1 >= args.length) throw new CheckError(`${a} needs a value`);
      return args[++i];
    };
    if (a === '--deploy') o.deploy = true;
    else if (a === '--branch') o.branch = value();
    else if (a === '--remote') o.remote = value();
    else if (a === '--behind-after') o.behindAfter = parseDuration(value());
    else if (a === '--depth') o.depth = Math.max(0, parseInt(value(), 10) || 0);
    else if (a === '--compare-with') o.compareWith = value();
    else if (a === '--map') {
      const m = /^([^=]+)=(.+)$/.exec(value());
      if (!m) throw new CheckError('--map needs repo=url, e.g. --map app=https://github.com/acme/app');
      o.map[repoName(m[1].trim()).toLowerCase()] = m[2].trim();
    }
    else if (a === '--skip') o.skip.push(...value().split(',').map(s => repoName(s.trim()).toLowerCase()).filter(Boolean));
    else if (a === '--token-file') o.tokenFile = value();
    else if (a.startsWith('--')) throw new CheckError(`unknown option ${a}`);
    else o.folder = a;
  }
  if (o.compareWith || Object.keys(o.map).length || o.skip.length || o.tokenFile) {
    if (o.deploy) throw new CheckError('--compare-with, --map, --skip and --token-file are for a folder of repos, not --deploy');
    if (o.remote) throw new CheckError('use --remote or --compare-with, not both');
    if (!o.compareWith) throw new CheckError('--map, --skip and --token-file go with --compare-with');
    if (!o.compareWith.includes('{repo}')) throw new CheckError('--compare-with needs {repo} in it, e.g. https://github.com/acme/{repo}');
  }
  return o;
}

// A repo's name: its folder without .git ("app.git" -> "app").
const repoName = p => path.basename(p).replace(/\.git$/i, '');

// "GitHub" for a github.com URL, and so on; else the host.
function urlName(url) {
  const host = (/^[a-z+]+:\/\/(?:[^@/]*@)?([^/:]+)/i.exec(url) || /^[^@]+@([^:]+):/.exec(url) || [])[1] || url;
  return { 'github.com': 'GitHub', 'gitlab.com': 'GitLab', 'bitbucket.org': 'Bitbucket' }[host.toLowerCase()] || host;
}

// --token-file: one token for the --compare-with host, or lines
// "<url prefix> <token>" (a GitHub fine-grained token covers one owner, so
// two owners need two). Sent as HTTP basic auth, only to matching URLs.
function tokenConfig(o) {
  let text = process.env.TOCKWARD_GIT_TOKEN || '';
  if (o.tokenFile) {
    try { text = fs.readFileSync(o.tokenFile, 'utf8'); } catch (err) {
      const why = { ENOENT: 'no such file', EACCES: 'permission denied', EISDIR: 'is a folder' }[err.code] || err.message;
      throw new CheckError(`can't read the token file ${o.tokenFile} (${why})`);
    }
  }
  const pairs = [];
  for (const raw of text.split('\n')) {
    const line = raw.trim();
    if (!line || line.startsWith('#')) continue;
    const parts = line.split(/\s+/);
    if (parts.length === 1) {
      const m = /^(https?:\/\/[^/]+\/)/i.exec(o.compareWith);
      if (!m) throw new CheckError('a token works with an https:// --compare-with URL only');
      pairs.push([m[1], parts[0]]);
    } else pairs.push([parts[0].endsWith('/') ? parts[0] : `${parts[0]}/`, parts[1]]);
  }
  const env = {};
  pairs.forEach(([prefix, token], i) => {
    env[`GIT_CONFIG_KEY_${i}`] = `http.${prefix}.extraHeader`;
    env[`GIT_CONFIG_VALUE_${i}`] = `Authorization: Basic ${Buffer.from(`x-access-token:${token}`).toString('base64')}`;
  });
  if (pairs.length) env.GIT_CONFIG_COUNT = String(pairs.length);
  return env;
}

// git's own words for "no access" made into what to do about it.
function accessHint(message) {
  if (/could not read Username|terminal prompts disabled|Authentication failed|403/i.test(message)) {
    return gitEnv.GIT_CONFIG_COUNT ? 'no access: the token was refused, or it can\'t read this repo' : 'no access: a private repo needs a token (--token-file)';
  }
  if (/not found/i.test(message)) return 'not found, or the token can\'t read it';
  return message;
}

const isRepo = dir => fs.existsSync(path.join(dir, '.git'))
  || (fs.existsSync(path.join(dir, 'HEAD')) && fs.existsSync(path.join(dir, 'objects')) && fs.existsSync(path.join(dir, 'refs')));

// Repos at or under the folder, up to depth levels down; hidden folders and
// node_modules are skipped, and nothing inside a repo is searched.
function findRepos(root, depth) {
  const found = [];
  const walk = (dir, level) => {
    if (isRepo(dir)) return found.push(dir);
    if (level >= depth) return;
    let entries;
    try { entries = fs.readdirSync(dir, { withFileTypes: true }); } catch { return; }
    for (const e of entries.sort((a, b) => a.name.localeCompare(b.name))) {
      if (e.isDirectory() && !e.name.startsWith('.') && e.name !== 'node_modules') walk(path.join(dir, e.name), level + 1);
    }
  };
  walk(root, 0);
  return found;
}

// The remote to compare with: --remote, else a hosted one, else origin.
function pickRemote(dir, wanted) {
  const names = (git(dir, ['remote']) || '').split('\n').filter(Boolean);
  if (wanted) return names.includes(wanted) ? wanted : null;
  for (const n of names) {
    const url = git(dir, ['remote', 'get-url', n], { allowFail: true }) || '';
    if (HOSTED.test(url.trim())) return n;
  }
  return names.includes('origin') ? 'origin' : null;
}

function refMap(text) {
  const map = new Map();
  for (const line of (text || '').split('\n')) {
    const [hash, ref] = line.split(/\s+/);
    if (hash && ref && !ref.endsWith('^{}')) map.set(ref, hash);
  }
  return map;
}

const hasObject = (dir, hash) => git(dir, ['cat-file', '-e', `${hash}^{commit}`], { allowFail: true }) !== null;
const isAncestor = (dir, a, b) => git(dir, ['merge-base', '--is-ancestor', a, b], { allowFail: true }) !== null;
const short = h => h.slice(0, 7);
const shortRef = r => r.replace(/^refs\/(heads|tags)\//, '');

function names(list) {
  const shown = list.slice(0, 3).join(', ');
  return list.length > 3 ? `${shown} and ${list.length - 3} more` : shown;
}

// One repo against its remote, or against a URL (--compare-with). Returns
// { problems: [{kind, text}], skipped }.
function checkMirror(dir, label, remoteWanted, url = null) {
  const remote = url ? urlName(url) : pickRemote(dir, remoteWanted);
  if (!remote && remoteWanted) return { problems: [{ kind: 'error', text: `${label}: no remote ${remoteWanted}` }] };
  if (!remote) return { skipped: true };
  let theirs;
  try {
    theirs = refMap(git(dir, ['ls-remote', '--heads', '--tags', url || remote]));
  } catch (err) {
    return { problems: [{ kind: 'error', text: `${label}: ${remote} unreachable (${url ? accessHint(err.message) : err.message})` }] };
  }
  const ours = refMap(git(dir, ['for-each-ref', '--format=%(objectname) %(refname)', 'refs/heads', 'refs/tags']));
  const behind = [], ahead = [], differs = [];
  for (const [ref, hash] of theirs) {
    const mine = ours.get(ref);
    if (mine === hash) continue;
    const name = shortRef(ref);
    if (!mine) behind.push(name);
    else if (ref.startsWith('refs/tags/')) differs.push(`tag ${name}`);
    else if (!hasObject(dir, hash)) behind.push(name);
    else if (isAncestor(dir, mine, hash)) behind.push(name);
    else if (isAncestor(dir, hash, mine)) ahead.push(name);
    else differs.push(name);
  }
  const problems = [];
  if (differs.length) problems.push({ kind: 'differs', text: `${label}: ${names(differs)} differ${differs.length === 1 ? 's' : ''} from ${remote}` });
  if (behind.length) problems.push({ kind: 'behind', key: label, text: `${label}: ${names(behind)} behind ${remote}` });
  if (ahead.length) problems.push({ kind: 'ahead', text: `${label}: ${names(ahead)} ahead of ${remote}` });
  return { problems };
}

function checkDeploy(dir, o) {
  if (!fs.existsSync(path.join(dir, '.git'))) throw new CheckError(`${dir} is not a git checkout`);
  const problems = [];
  const changed = git(dir, ['--no-optional-locks', 'status', '--porcelain']).split('\n').filter(Boolean).length;
  if (changed) problems.push({ kind: 'changed', text: `${changed} file${changed === 1 ? '' : 's'} changed on the server` });

  const remote = pickRemote(dir, o.remote);
  if (!remote && o.remote) throw new CheckError(`${dir} has no remote ${o.remote}`);
  const current = (git(dir, ['symbolic-ref', '--short', '-q', 'HEAD'], { allowFail: true }) || '').trim();
  const head = git(dir, ['rev-parse', 'HEAD']).trim();
  if (!remote) {
    // One copy only (pushed straight into this folder): nothing to compare
    // with, so only files changed by hand and the branch are checked.
    if (o.branch && current !== o.branch) problems.push({ kind: 'branch', text: `on ${current ? `branch ${current}` : 'a detached HEAD'}, expected ${o.branch}` });
    return { problems, branch: current || 'detached HEAD', head, local: true };
  }
  let branch = o.branch || current;
  if (!branch) {
    // Detached HEAD without --branch: the remote's default branch.
    try {
      const sym = /ref: refs\/heads\/(\S+)\s+HEAD/.exec(git(dir, ['ls-remote', '--symref', remote, 'HEAD']));
      branch = sym ? sym[1] : null;
    } catch (err) {
      return { problems: [...problems, { kind: 'error', text: `${remote} unreachable (${err.message})` }] };
    }
    if (!branch) throw new CheckError('detached HEAD: say which branch with --branch');
  }
  if (o.branch && current && current !== o.branch) problems.push({ kind: 'branch', text: `on branch ${current}, expected ${o.branch}` });

  let latest;
  try {
    latest = refMap(git(dir, ['ls-remote', '--heads', remote, branch])).get(`refs/heads/${branch}`);
  } catch (err) {
    return { problems: [...problems, { kind: 'error', text: `${remote} unreachable (${err.message})` }] };
  }
  if (!latest) problems.push({ kind: 'error', text: `${remote} has no branch ${branch}` });
  else if (latest !== head) {
    if (hasObject(dir, latest) && isAncestor(dir, latest, head)) {
      const n = parseInt(git(dir, ['rev-list', '--count', `${latest}..${head}`]), 10) || 0;
      problems.push({ kind: 'ahead', text: `${n} commit${n === 1 ? '' : 's'} on the server that ${remote}/${branch} doesn't have` });
    } else {
      problems.push({ kind: 'behind', key: 'deploy', text: `not on the latest ${branch}: on ${short(head)}, ${remote}/${branch} is at ${short(latest)}` });
    }
  }
  return { problems, branch, head };
}

// --behind-after: "behind" only counts once it has lasted that long; the
// first time seen is kept per token and folder in ~/.cache/tockward/.
function applyBehindAfter(problems, seconds, token, folder) {
  if (!seconds) return { problems, waiting: [] };
  const dir = path.join(process.env.XDG_CACHE_HOME || path.join(os.homedir(), '.cache'), 'tockward');
  const file = path.join(dir, `git-${crypto.createHash('sha256').update(`${token}\n${folder}`).digest('hex').slice(0, 16)}.json`);
  let seen = {};
  try { seen = JSON.parse(fs.readFileSync(file, 'utf8')); } catch {}
  const now = Math.floor(Date.now() / 1000);
  const next = {}, kept = [], waiting = [];
  for (const p of problems) {
    if (p.kind !== 'behind') { kept.push(p); continue; }
    const since = seen[p.key] || now;
    next[p.key] = since;
    if (now - since >= seconds) kept.push(p);
    else waiting.push(p);
  }
  try { fs.mkdirSync(dir, { recursive: true }); fs.writeFileSync(file, JSON.stringify(next)); } catch {}
  return { problems: kept, waiting };
}

const ORDER = ['error', 'differs', 'changed', 'branch', 'ahead', 'behind'];
const fmtDuration = s => (s % 86400 === 0 ? `${s / 86400} d` : s % 3600 === 0 ? `${s / 3600} h` : s >= 60 ? `${Math.round(s / 60)} min` : `${s} s`);

// Runs the check. Returns { code, message, lines }: code 0 ok, 1 problems,
// 2 the check itself failed; message is what goes to Tockward.
function run(token, args) {
  let o;
  try {
    o = parseArgs(args);
    const folder = path.resolve(o.folder);
    if (!fs.existsSync(folder)) throw new CheckError(`${o.folder} does not exist`);
    let problems = [], summary;
    if (o.deploy) {
      const r = checkDeploy(folder, o);
      problems = r.problems;
      summary = r.head ? `on ${r.branch} at ${short(r.head)}${r.local ? ' (no remote: checked for changes only)' : ''}` : 'checked';
    } else {
      const repos = findRepos(folder, o.depth);
      if (!repos.length) throw new CheckError(`no git repos found in ${o.folder}`);
      if (o.compareWith) gitEnv = tokenConfig(o);
      let skipped = 0;
      for (const dir of repos) {
        const label = path.relative(folder, dir) || path.basename(dir);
        const name = repoName(dir);
        if (o.compareWith && o.skip.includes(name.toLowerCase())) { skipped++; continue; }
        const url = o.compareWith ? (o.map[name.toLowerCase()] || o.compareWith.split('{repo}').join(name)) : null;
        const r = checkMirror(dir, label, o.remote, url);
        if (r.skipped) skipped++;
        else problems.push(...r.problems);
      }
      const checked = repos.length - skipped;
      // Nothing compared is no proof of anything: fail rather than say ok.
      if (!checked) {
        throw new CheckError(o.compareWith
          ? `all ${repos.length} repos in ${o.folder} are skipped: nothing was compared`
          : `none of the ${repos.length} repos in ${o.folder} has a remote to compare with; for a server that is only pushed to, use --compare-with`);
      }
      const against = o.compareWith ? ` against ${urlName(o.compareWith)}` : '';
      summary = `${checked} repo${checked === 1 ? '' : 's'} checked${against}${skipped ? `, ${skipped} ${o.compareWith ? 'skipped' : 'without a remote skipped'}` : ''}`;
    }
    const { problems: kept, waiting } = applyBehindAfter(problems, o.behindAfter, token, folder);
    kept.sort((a, b) => ORDER.indexOf(a.kind) - ORDER.indexOf(b.kind));
    if (!kept.length) {
      const ok = waiting.length
        ? `ok for now: ${summary}; behind for less than ${fmtDuration(o.behindAfter)}`
        : `${o.deploy ? 'up to date' : 'in sync'}: ${summary}`;
      return { code: 0, message: ok, lines: [ok, ...waiting.map(p => `  waiting: ${p.text}`)] };
    }
    const lines = kept.map(p => p.text);
    return { code: 1, message: lines.join('\n'), lines };
  } catch (err) {
    if (!(err instanceof CheckError)) throw err;
    return { code: 2, message: `git check failed: ${err.message}`, lines: [`git check failed: ${err.message}`] };
  }
}

module.exports = { run, parseDuration, parseArgs, findRepos };
