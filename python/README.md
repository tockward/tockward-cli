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
tockward fail <token>                  # report a failed run
tockward run <token> -- ./backup.sh    # run a command, then report success or failure
```

`tockward run` passes the command's output through, sends its duration along, and exits
with the command's own exit code, so it drops straight into a crontab:

```cron
0 3 * * *  tockward run abc123 -- /usr/local/bin/backup.sh
```

`<token>` is the monitor's token, or its full ping URL copied from the dashboard.

## In code

```js
const tockward = require('tockward');
await tockward.ping('abc123');
await tockward.fail('abc123');
```

```python
import tockward
tockward.ping("abc123")
tockward.fail("abc123")
```

Delivery is retried twice with a 10-second timeout per attempt; an unknown token fails at once.

## Without this package

It's just an HTTP request, so `curl` works too:

```sh
curl -fsS https://ping.tockward.com/v1/beat/<token>        # success
curl -fsS https://ping.tockward.com/v1/beat/<token>/fail   # failure
```

## Settings

| Variable | Default | Meaning |
|---|---|---|
| `TOCKWARD_URL` | `https://ping.tockward.com` | server to report to |

## License

MIT. Questions: support@tockward.com
