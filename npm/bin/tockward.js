#!/usr/bin/env node
'use strict';
const { spawn } = require('child_process');
const os = require('os');
const { ping, fail, OutputTail, failureMessage, successSummary } = require('..');
const { version } = require('../package.json');

const USAGE = `Tockward: know when your cron jobs, scripts and devices go silent.

Usage:
  tockward ping <token> [--value <n>] [--bytes]
                                      report a successful run, optionally
                                      with a number to chart (a size with
                                      --bytes)
  tockward fail <token> [message]     report a failed run, optionally why
  tockward run <token> [--no-summary] -- <command>
                                      run a command, then report success or
                                      failure with its duration and output:
                                      on success its last line (the summary
                                      on the dashboard; --no-summary leaves
                                      it out), on failure its last lines;
                                      exits with the command's exit code
  tockward git <token> [folder] [options]
                                      check git repos and report: ok, or
                                      fail with what's wrong (read-only)
  tockward backup <token> <folder> [options]
                                      check the backup files in a folder and
                                      report: ok, or fail with what's wrong
  tockward backup --detect <folder>   look at a backup folder and suggest
                                      options (sends nothing)

git options:
  (default)            every repo under folder (a Git server) must have the
                       same branches and tags as its hosted remote
  --deploy             folder is a checkout (production): nothing changed by
                       hand, on the remote's latest commit
  --branch <name>      the branch it should be on (deploy)
  --remote <name>      compare with this remote (default: GitHub, GitLab or
                       Bitbucket, else origin)
  --behind-after <t>   only alert when behind for longer than t (30m, 2h)
  --depth <n>          how deep to look for repos (default 3)

backup options (names, sizes and times only; files are never opened):
  --kind <kind>        full (default): every file is a full backup
                       incremental: fulls plus increments
                       differential: fulls plus differentials that grow
  --full <pattern>     which files are fulls, e.g. '*full*' (incremental,
                       differential)
  --full-day <day>     or: fulls are the ones made on this day (sun)
  --match <pattern>    only files like this, e.g. '*.sql.gz'
  --max-age <t>        the newest backup may be this old (default 26h)
  --full-max-age <t>   the newest full may be this old (default 8d)
  --min-size <size>    a full must be at least this big, e.g. 50MB
  --drop <pct>         alert when a full is this much smaller than the one
                       before (default 50%; 0 = off)
  --depth <n>          also look n levels of subfolders deep (default 0)

<token> is the monitor's token or its full ping URL.
Set TOCKWARD_URL to use another server.  https://tockward.com`;

const warn = msg => process.stderr.write(`tockward: ${msg}\n`);

async function main(argv) {
  const [cmd, token, ...rest] = argv;
  if (!cmd || cmd === '-h' || cmd === '--help' || cmd === 'help') { console.log(USAGE); return 0; }
  if (cmd === '-v' || cmd === '--version') { console.log(version); return 0; }
  if (cmd === 'backup' && (token === '--detect' || rest.includes('--detect'))) {
    const result = require('../backup').run(null, token === '--detect' ? [token, ...rest] : rest);
    for (const line of result.lines) (result.code ? process.stderr : process.stdout).write(`${line}\n`);
    return result.code;
  }
  if (!token) { warn('missing token'); console.error(USAGE); return 2; }

  if (cmd === 'ping' || cmd === 'fail') {
    let value, unit, words = rest;
    if (cmd === 'ping') {
      const i = rest.indexOf('--value');
      if (i >= 0) {
        value = Number(rest[i + 1]);
        if (rest[i + 1] === undefined || !Number.isFinite(value)) { warn('--value needs a number'); return 2; }
      }
      if (rest.includes('--bytes')) unit = 'bytes';
      words = [];
    }
    const message = cmd === 'fail' ? words.join(' ').trim() : '';
    try { await (cmd === 'ping' ? ping : fail)(token, { message, value, unit }); return 0; }
    catch (err) { warn(`could not report: ${err.message}`); return 1; }
  }

  if (cmd === 'git' || cmd === 'backup') {
    const result = require(cmd === 'git' ? '../git' : '../backup').run(token, rest);
    for (const line of result.lines) (result.code ? process.stderr : process.stdout).write(`${line}\n`);
    // backup sends the newest full backup's size along, for the chart.
    const extra = Number.isFinite(result.value) ? { value: result.value, unit: 'bytes' } : {};
    try { await (result.code === 0 ? ping : fail)(token, { message: result.message, ...extra }); }
    catch (err) { warn(`could not report: ${err.message}`); return result.code || 1; }
    return result.code;
  }

  if (cmd === 'run') {
    const noSummary = rest[0] === '--no-summary';
    const args = noSummary ? rest.slice(1) : rest;
    const command = args[0] === '--' ? args.slice(1) : args;
    if (!command.length) { warn('nothing to run; use: tockward run <token> -- <command>'); return 2; }
    const started = Date.now();
    // Output still goes to the terminal or cron log as it comes; the last
    // lines are kept: the last one is the summary of a successful run, all
    // of them go with a failure.
    const tail = new OutputTail();
    let headline = '';
    const code = await new Promise(resolve => {
      const child = spawn(command[0], command.slice(1), { stdio: ['inherit', 'pipe', 'pipe'] });
      child.stdout.on('data', d => { process.stdout.write(d); tail.write(d); });
      child.stderr.on('data', d => { process.stderr.write(d); tail.write(d); });
      child.on('error', err => {
        headline = `could not start ${command[0]}: ${err.message}`;
        warn(headline);
        resolve(127);
      });
      child.on('close', (c, signal) => {
        if (headline) return;
        headline = signal ? `killed by ${signal}` : `exited with code ${c}`;
        resolve(signal ? 128 + (os.constants.signals[signal] || 0) : c);
      });
    });
    const ms = Date.now() - started;
    const summary = noSummary ? '' : successSummary(tail.last);
    try { await (code === 0 ? ping(token, { ms, message: summary }) : fail(token, { ms, message: failureMessage(headline, tail.last) })); }
    catch (err) { warn(`could not report: ${err.message}`); }
    return code;
  }

  warn(`unknown command: ${cmd}`); console.error(USAGE); return 2;
}

main(process.argv.slice(2)).then(code => process.exit(code));
