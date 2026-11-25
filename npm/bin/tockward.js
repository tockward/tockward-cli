#!/usr/bin/env node
'use strict';
const { spawn } = require('child_process');
const os = require('os');
const { ping, fail, OutputTail, failureMessage } = require('..');
const { version } = require('../package.json');

const USAGE = `Tockward: know when your cron jobs, scripts and devices go silent.

Usage:
  tockward ping <token>               report a successful run
  tockward fail <token> [message]     report a failed run, optionally why
  tockward run <token> -- <command>   run a command, then report success or
                                      failure (with its duration, and on
                                      failure its last output lines); exits
                                      with the command's exit code
  tockward git <token> [folder] [options]
                                      check git repos and report: ok, or
                                      fail with what's wrong (read-only)

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

<token> is the monitor's token or its full ping URL.
Set TOCKWARD_URL to use another server.  https://tockward.com`;

const warn = msg => process.stderr.write(`tockward: ${msg}\n`);

async function main(argv) {
  const [cmd, token, ...rest] = argv;
  if (!cmd || cmd === '-h' || cmd === '--help' || cmd === 'help') { console.log(USAGE); return 0; }
  if (cmd === '-v' || cmd === '--version') { console.log(version); return 0; }
  if (!token) { warn('missing token'); console.error(USAGE); return 2; }

  if (cmd === 'ping' || cmd === 'fail') {
    const message = cmd === 'fail' ? rest.join(' ').trim() : '';
    try { await (cmd === 'ping' ? ping : fail)(token, { message }); return 0; }
    catch (err) { warn(`could not report: ${err.message}`); return 1; }
  }

  if (cmd === 'git') {
    const result = require('../git').run(token, rest);
    for (const line of result.lines) (result.code ? process.stderr : process.stdout).write(`${line}\n`);
    try { await (result.code === 0 ? ping : fail)(token, { message: result.message }); }
    catch (err) { warn(`could not report: ${err.message}`); return result.code || 1; }
    return result.code;
  }

  if (cmd === 'run') {
    const command = rest[0] === '--' ? rest.slice(1) : rest;
    if (!command.length) { warn('nothing to run; use: tockward run <token> -- <command>'); return 2; }
    const started = Date.now();
    // Output still goes to the terminal or cron log as it comes; the last
    // lines are kept for the failure message.
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
    try { await (code === 0 ? ping(token, { ms }) : fail(token, { ms, message: failureMessage(headline, tail.last) })); }
    catch (err) { warn(`could not report: ${err.message}`); }
    return code;
  }

  warn(`unknown command: ${cmd}`); console.error(USAGE); return 2;
}

main(process.argv.slice(2)).then(code => process.exit(code));
