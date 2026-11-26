import os
import signal
import subprocess
import sys
import threading
import time

from . import OutputTail, TockwardError, __version__, fail, failure_message, ping, success_summary

USAGE = """Tockward: know when your cron jobs, scripts and devices go silent.

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
  --compare-with <url> compare each repo with this URL instead of a remote,
                       for a server that is only pushed to (push to both);
                       {repo} = the repo's folder without .git, e.g.
                       https://github.com/acme/{repo}
  --map <repo>=<url>   another URL for one repo (with --compare-with)
  --skip <repo>        leave a repo out (with --compare-with)
  --token-file <file>  a read-only token for private repos (with
                       --compare-with), or lines "<url prefix> <token>",
                       one per owner; or set TOCKWARD_GIT_TOKEN
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
Set TOCKWARD_URL to use another server.  https://tockward.com"""


def warn(msg):
    print(f"tockward: {msg}", file=sys.stderr)


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] in ("-h", "--help", "help"):
        print(USAGE)
        return 0
    if args[0] in ("-v", "--version"):
        print(__version__)
        return 0
    cmd, rest = args[0], args[1:]
    if cmd == "backup" and "--detect" in rest:
        from . import backup
        # --detect needs no token; one given before the folder is skipped.
        code, _, lines, _ = backup.run(None, rest if rest[0] == "--detect" else rest[1:])
        for line in lines:
            print(line, file=sys.stderr if code else sys.stdout)
        return code
    if not rest:
        warn("missing token")
        print(USAGE, file=sys.stderr)
        return 2
    token, rest = rest[0], rest[1:]

    if cmd in ("ping", "fail"):
        try:
            if cmd == "ping":
                value = None
                if "--value" in rest:
                    i = rest.index("--value")
                    try:
                        value = float(rest[i + 1])
                        if value != value or value in (float("inf"), float("-inf")):
                            raise ValueError
                    except (IndexError, ValueError):
                        warn("--value needs a number")
                        return 2
                ping(token, value=value, unit="bytes" if "--bytes" in rest else None)
            else:
                fail(token, message=" ".join(rest).strip() or None)
            return 0
        except (TockwardError, ValueError) as err:
            warn(f"could not report: {err}")
            return 1

    if cmd in ("git", "backup"):
        if cmd == "git":
            from . import git as check
        else:
            from . import backup as check
        code, message, lines, *more = check.run(token, rest)
        for line in lines:
            print(line, file=sys.stderr if code else sys.stdout)
        # backup sends the newest full backup's size along, for the chart.
        size = more[0] if more else None
        extra = {"value": size, "unit": "bytes"} if size is not None else {}
        try:
            (ping if code == 0 else fail)(token, message=message, **extra)
        except (TockwardError, ValueError) as err:
            warn(f"could not report: {err}")
            return code or 1
        return code

    if cmd == "run":
        no_summary = rest[:1] == ["--no-summary"]
        args = rest[1:] if no_summary else rest
        command = args[1:] if args[:1] == ["--"] else args
        if not command:
            warn("nothing to run; use: tockward run <token> -- <command>")
            return 2
        started = time.monotonic()
        # Output still goes to the terminal or cron log as it comes; the
        # last lines are kept: the last one is the summary of a successful
        # run, all of them go with a failure.
        tail = OutputTail()
        lock = threading.Lock()

        def pump(src, dst):
            for chunk in iter(lambda: os.read(src.fileno(), 65536), b""):
                dst.buffer.write(chunk)
                dst.buffer.flush()
                with lock:
                    tail.write(chunk)

        try:
            proc = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        except OSError as err:
            headline = f"could not start {command[0]}: {err.strerror or err}"
            warn(headline)
            code = 127
        else:
            threads = [threading.Thread(target=pump, args=(proc.stdout, sys.stdout), daemon=True),
                       threading.Thread(target=pump, args=(proc.stderr, sys.stderr), daemon=True)]
            for t in threads:
                t.start()
            code = proc.wait()
            for t in threads:
                t.join()
            if code < 0:
                try:
                    name = signal.Signals(-code).name
                except ValueError:
                    name = f"signal {-code}"
                headline = f"killed by {name}"
                code = 128 - code
            else:
                headline = f"exited with code {code}"
        ms = (time.monotonic() - started) * 1000
        try:
            if code == 0:
                ping(token, ms=ms, message=None if no_summary else (success_summary(tail.last) or None))
            else:
                fail(token, ms=ms, message=failure_message(headline, tail.last))
        except (TockwardError, ValueError) as err:
            warn(f"could not report: {err}")
        return code

    warn(f"unknown command: {cmd}")
    print(USAGE, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
