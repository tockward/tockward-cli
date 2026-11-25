# tockward

Heartbeat client for **[Tockward](https://tockward.com)**: know when your cron jobs, scripts and devices go silent.

Your job reports in each time it runs. If a report doesn't arrive on time, Tockward alerts you by email or Telegram.

## Install

```sh
npm install -g tockward      # Node 18+
pip install tockward         # Python 3.8+
```

Both give you the same `tockward` command. No dependencies.

## Command line

```sh
tockward ping <token>                  # report a successful run
tockward fail <token> "disk full"       # report a failed run, optionally why
tockward run <token> -- ./backup.sh    # run a command, then report success or failure
tockward git <token> /var/www/app --deploy   # check git repos (see below)
tockward backup <token> /backups/db    # check backup files (see below)
```

`tockward run` passes the command's output through, sends its duration along, and exits
with the command's own exit code, so it drops straight into a crontab. When the command
fails, the last lines of its output go along too, so the alert says why
(`exited with code 3: error: disk full`):

```cron
0 3 * * *  tockward run abc123 -- /usr/local/bin/backup.sh
```

`<token>` is the monitor's token, or its full ping URL copied from the dashboard.

## Git repos

`tockward git` checks the git repos on a server and reports ok, or a failure saying what's
wrong. It only reads (no fetch, no pull), uses the server's own git access, and sends just
a short summary: repo, branch and commit names, never file names or code.

```cron
# Git server: every repo under /srv/git has the same branches and tags as GitHub/GitLab
0 * * * *  tockward git <token> /srv/git --behind-after 12h

# Production: nothing changed by hand, on the latest commit of main
*/15 * * * *  tockward git <token> /var/www/app --deploy --branch main --behind-after 30m
```

| Option | Meaning |
|---|---|
| `--deploy` | the folder is one checkout (production) instead of a folder of repos |
| `--branch <name>` | the branch it should be on (deploy; default: the current one) |
| `--remote <name>` | compare with this remote (default: the GitHub, GitLab or Bitbucket one, else `origin`) |
| `--behind-after <t>` | only alert when it has been behind for longer than `t` (`30m`, `2h`, `1d`), so a deploy or mirror sync in progress doesn't alert |
| `--depth <n>` | how deep to look for repos under the folder (default 3) |

A checkout without a remote (code pushed straight into the folder it runs from) has nothing to compare
with: then `--deploy` only checks for files changed by hand and the branch.

Exit code 0 when all is well, 1 when something's wrong, 2 when the check itself couldn't run.

## Backups

`tockward backup` checks the backup files in a folder: the newest one is recent, not empty,
and not suddenly much smaller than the one before. It only looks at names, sizes and times
(files are never opened) and sends a one-line summary.

```cron
# A full backup every night
30 6 * * *  tockward backup <token> /backups/db --match '*.sql.gz'

# A weekly full plus nightly increments
30 6 * * *  tockward backup <token> /backups/files --kind incremental --full '*full*'
```

Not sure which options fit? Let it look and suggest (it sends nothing):

```
$ tockward backup --detect /backups/files
Found 31 files in the last 35 days, about one every 24 h, newest 5 h old.
  4 large (2.1 GB, on Sundays): look like full backups
  27 smaller (40 MB-310 MB): look like incrementals
Suggestion: tockward backup <token> /backups/files --kind incremental --full '*full*' --max-age 26h --full-max-age 8d
```

| Option | Meaning |
|---|---|
| `--kind <kind>` | `full` (default): every file is a full backup. `incremental`: fulls plus increments. `differential`: fulls plus differentials that grow until the next full |
| `--full <pattern>` | which files are fulls, e.g. `'*full*'` or `'*.vbk'` (incremental, differential) |
| `--full-day <day>` | or: the fulls are the ones made on this day (`sun`) |
| `--match <pattern>` | only files like this, e.g. `'*.sql.gz'` |
| `--max-age <t>` | the newest backup may be this old (default `26h`) |
| `--full-max-age <t>` | the newest full may be this old (default `8d`): increments are useless without it |
| `--min-size <size>` | a full must be at least this big, e.g. `50MB` (default: not empty) |
| `--drop <pct>` | alert when a full is this much smaller than the one before (default `50%`, `0` = off). A backup overwritten in place is compared with last run's |
| `--depth <n>` | also look this many levels of subfolders deep (default 0) |

Hidden files, partial uploads (`.tmp`, `.part`) and files changed in the last minute are skipped.
For a reverse incremental backup (the newest file is always the full, like Veeam's `.vbk`),
use the default kind with `--match '*.vbk'`. Exit codes as for `tockward git`.

## In code

```js
const tockward = require('tockward');
await tockward.ping('abc123');
await tockward.fail('abc123', { message: 'disk full' });
```

```python
import tockward
tockward.ping("abc123")
tockward.fail("abc123", message="disk full")
```

Delivery is retried twice with a 10-second timeout per attempt; an unknown token fails at once.

## Without this package

It's just an HTTP request, so `curl` works too:

```sh
curl -fsS https://ping.tockward.com/v1/beat/<token>        # success
curl -fsS https://ping.tockward.com/v1/beat/<token>/fail   # failure
curl -fsS -d "disk full" https://ping.tockward.com/v1/beat/<token>/fail   # failure, with why
```

## Settings

| Variable | Default | Meaning |
|---|---|---|
| `TOCKWARD_URL` | `https://ping.tockward.com` | server to report to |

## License

MIT. Questions: support@tockward.com
