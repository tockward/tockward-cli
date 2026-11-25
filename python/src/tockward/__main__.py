import os
import signal
import subprocess
import sys
import threading
import time

from . import OutputTail, TockwardError, __version__, fail, failure_message, ping

USAGE = """Tockward: know when your cron jobs, scripts and devices go silent.

Usage:
  tockward ping <token>               report a successful run
  tockward fail <token> [message]     report a failed run, optionally why
  tockward run <token> -- <command>   run a command, then report success or
                                      failure (with its duration, and on
                                      failure its last output lines); exits
                                      with the command's exit code

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
            if cmd == "ping":
                ping(token)
            else:
                fail(token, message=" ".join(rest).strip() or None)
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
        # Output still goes to the terminal or cron log as it comes; the
        # last lines are kept for the failure message.
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
                ping(token, ms=ms)
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
