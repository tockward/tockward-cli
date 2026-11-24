import subprocess
import sys
import time

from . import TockwardError, __version__, fail, ping

USAGE = """Tockward: know when your cron jobs, scripts and devices go silent.

Usage:
  tockward ping <token>               report a successful run
  tockward fail <token>               report a failed run
  tockward run <token> -- <command>   run a command, then report success or
                                      failure (with its duration); exits with
                                      the command's exit code

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
    if not rest:
        warn("missing token")
        print(USAGE, file=sys.stderr)
        return 2
    token, rest = rest[0], rest[1:]

    if cmd in ("ping", "fail"):
        try:
            (ping if cmd == "ping" else fail)(token)
            return 0
        except (TockwardError, ValueError) as err:
            warn(f"could not report: {err}")
            return 1

    if cmd == "run":
        command = rest[1:] if rest[:1] == ["--"] else rest
        if not command:
            warn("nothing to run; use: tockward run <token> -- <command>")
            return 2
        started = time.monotonic()
        try:
            code = subprocess.call(command)
        except OSError as err:
            warn(f"could not start {command[0]}: {err}")
            code = 127
        if code < 0:
            code = 128 - code
        ms = (time.monotonic() - started) * 1000
        try:
            (ping if code == 0 else fail)(token, ms=ms)
        except (TockwardError, ValueError) as err:
            warn(f"could not report: {err}")
        return code

    warn(f"unknown command: {cmd}")
    print(USAGE, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
