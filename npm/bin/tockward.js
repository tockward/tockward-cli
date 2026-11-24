#!/usr/bin/env node
'use strict';
const { spawn } = require('child_process');
const { ping, fail } = require('..');
const { version } = require('../package.json');

const USAGE = `Tockward: know when your cron jobs, scripts and devices go silent.

Usage:
  tockward ping <token>               report a successful run
  tockward fail <token>               report a failed run
  tockward run <token> -- <command>   run a command, then report success or
                                      failure (with its duration); exits with
                                      the command's exit code

<token> is the monitor's token or its full ping URL.
Set TOCKWARD_URL to use another server.  https://tockward.com`;

const warn = msg => process.stderr.write(`tockward: ${msg}\n`);

async function main(argv) {
  const [cmd, token, ...rest] = argv;
  if (!cmd || cmd === '-h' || cmd === '--help' || cmd === 'help') { console.log(USAGE); return 0; }
  if (cmd === '-v' || cmd === '--version') { console.log(version); return 0; }
  if (!token) { warn('missing token'); console.error(USAGE); return 2; }

  if (cmd === 'ping' || cmd === 'fail') {
    try { await (cmd === 'ping' ? ping : fail)(token); return 0; }
    catch (err) { warn(`could not report: ${err.message}`); return 1; }
  }

  if (cmd === 'run') {
    const command = rest[0] === '--' ? rest.slice(1) : rest;
    if (!command.length) { warn('nothing to run; use: tockward run <token> -- <command>'); return 2; }
    const started = Date.now();
    const code = await new Promise(resolve => {
      const child = spawn(command[0], command.slice(1), { stdio: 'inherit' });
      child.on('error', err => { warn(`could not start ${command[0]}: ${err.message}`); resolve(127); });
      child.on('exit', (c, signal) => resolve(signal ? 128 : c));
    });
    try { await (code === 0 ? ping : fail)(token, { ms: Date.now() - started }); }
    catch (err) { warn(`could not report: ${err.message}`); }
    return code;
  }

  warn(`unknown command: ${cmd}`); console.error(USAGE); return 2;
}

main(process.argv.slice(2)).then(code => process.exit(code));
