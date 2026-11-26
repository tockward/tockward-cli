"""Tockward heartbeat client. No dependencies.

    import tockward
    tockward.ping("abc123")            # "I ran"
    tockward.fail("abc123")            # "I failed"
    tockward.ping("abc123", ms=4200)   # with the run's duration
    tockward.ping("abc123", value=12400)   # a number to chart
    tockward.ping("abc123", value=145278836, unit="bytes")   # a size
    tockward.fail("abc123", message="disk full")  # say why

A token can also be a full ping URL (https://.../v1/beat/<token>), so URLs
copied from the dashboard work as they are. TOCKWARD_URL overrides the base.
"""

import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request

__version__ = "0.6.0"
__all__ = ["ping", "fail", "beat_url", "TockwardError", "OutputTail", "failure_message", "success_summary"]

DEFAULT_BASE = "https://ping.tockward.com"
# The server keeps up to 500 characters; the first line is the alert's reason.
MESSAGE_MAX = 500


class TockwardError(Exception):
    """The heartbeat could not be delivered."""


def beat_url(token, ok=True, ms=None, value=None, unit=None):
    if not token or not isinstance(token, str):
        raise ValueError("missing token")
    if re.match(r"^https?://", token, re.I):
        url = re.sub(r"/fail$", "", token.rstrip("/"))
    else:
        base = os.environ.get("TOCKWARD_URL", DEFAULT_BASE).rstrip("/")
        url = f"{base}/v1/beat/{urllib.parse.quote(token, safe='')}"
    if not ok:
        url += "/fail"
    query = []
    if ms is not None:
        query.append(f"ms={max(0, round(ms))}")
    if value is not None:
        query.append(f"value={_num(value)}")
        if unit == "bytes":
            query.append("unit=bytes")
    return f"{url}?{'&'.join(query)}" if query else url


def _num(v):
    """A number as JavaScript writes it: 12400, not 12400.0."""
    return str(int(v)) if float(v) == int(v) else repr(float(v))


def _send(token, ok, ms=None, timeout=10, retries=2, message=None, value=None, unit=None):
    url = beat_url(token, ok, ms, value, unit)
    data = str(message)[:MESSAGE_MAX].encode("utf-8") if message else b""
    headers = {"User-Agent": f"tockward-python/{__version__}"}
    if data:
        headers["Content-Type"] = "text/plain; charset=utf-8"
    last = None
    for attempt in range(retries + 1):
        if attempt:
            time.sleep(attempt)
        try:
            req = urllib.request.Request(url, data=data, method="POST", headers=headers)
            with urllib.request.urlopen(req, timeout=timeout):
                return True
        except urllib.error.HTTPError as err:
            if err.code == 404:
                raise TockwardError("unknown monitor (check the token)") from None
            last = TockwardError(f"HTTP {err.code}")
        except (urllib.error.URLError, OSError) as err:
            last = TockwardError(str(getattr(err, "reason", err)))
    raise last


def ping(token, ms=None, timeout=10, retries=2, message=None, value=None, unit=None):
    """Report a successful run; value is an optional number to chart
    (unit="bytes" for a size)."""
    return _send(token, True, ms, timeout, retries, message, value, unit)


def fail(token, ms=None, timeout=10, retries=2, message=None, value=None, unit=None):
    """Report a failed run; message says why (shown in the alert)."""
    return _send(token, False, ms, timeout, retries, message, value, unit)


_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")


class OutputTail:
    """Remembers the last lines a command printed, for the failure message.
    Colour codes and progress-bar redraws (\\r) are left out."""

    def __init__(self, max_lines=20):
        self.max = max_lines
        self.lines = []
        self.partial = ""

    def write(self, chunk):
        if isinstance(chunk, bytes):
            chunk = chunk.decode("utf-8", "replace")
        parts = _ANSI.sub("", self.partial + chunk).split("\n")
        self.partial = parts.pop()[-1000:]
        for p in parts:
            self._push(p)

    def _push(self, line):
        pieces = [x for x in line.split("\r") if x]
        clean = pieces[-1] if pieces else ""
        if not clean.strip():
            return
        self.lines.append(clean.rstrip()[:300])
        if len(self.lines) > self.max:
            self.lines.pop(0)

    @property
    def last(self):
        if self.partial:
            self._push(self.partial)
            self.partial = ""
        return self.lines


SUMMARY_MAX = 200


def success_summary(lines):
    """After a successful run: its last output line, as the summary the
    dashboard shows ("5 sensors ok"). Empty when it printed nothing."""
    return lines[-1].strip()[:SUMMARY_MAX] if lines else ""


def failure_message(headline, lines):
    """"exited with code 3: <last line>", then as much of the last output as fits."""
    first = (f"{headline}: {lines[-1].strip()}" if lines else headline)[:200]
    # A single line is already in the headline: no "Last output" then.
    if len(lines) == 1 and first.endswith(lines[0].strip()):
        return first
    kept = []
    size = len(first) + len("\n\nLast output:")
    for line in reversed(lines):
        if size + len(line) + 1 > MESSAGE_MAX:
            break
        size += len(line) + 1
        kept.insert(0, line)
    return f"{first}\n\nLast output:\n" + "\n".join(kept) if kept else first
