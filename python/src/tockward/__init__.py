"""Tockward heartbeat client. No dependencies.

    import tockward
    tockward.ping("abc123")            # "I ran"
    tockward.fail("abc123")            # "I failed"
    tockward.ping("abc123", ms=4200)   # with the run's duration

A token can also be a full ping URL (https://.../v1/beat/<token>), so URLs
copied from the dashboard work as they are. TOCKWARD_URL overrides the base.
"""

import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request

__version__ = "0.1.0"
__all__ = ["ping", "fail", "beat_url", "TockwardError"]

DEFAULT_BASE = "https://ping.tockward.com"


class TockwardError(Exception):
    """The heartbeat could not be delivered."""


def beat_url(token, ok=True, ms=None):
    if not token or not isinstance(token, str):
        raise ValueError("missing token")
    if re.match(r"^https?://", token, re.I):
        url = re.sub(r"/fail$", "", token.rstrip("/"))
    else:
        base = os.environ.get("TOCKWARD_URL", DEFAULT_BASE).rstrip("/")
        url = f"{base}/v1/beat/{urllib.parse.quote(token, safe='')}"
    if not ok:
        url += "/fail"
    if ms is not None:
        url += f"?ms={max(0, round(ms))}"
    return url


def _send(token, ok, ms=None, timeout=10, retries=2):
    url = beat_url(token, ok, ms)
    last = None
    for attempt in range(retries + 1):
        if attempt:
            time.sleep(attempt)
        try:
            req = urllib.request.Request(url, data=b"", method="POST",
                                         headers={"User-Agent": f"tockward-python/{__version__}"})
            with urllib.request.urlopen(req, timeout=timeout):
                return True
        except urllib.error.HTTPError as err:
            if err.code == 404:
                raise TockwardError("unknown monitor (check the token)") from None
            last = TockwardError(f"HTTP {err.code}")
        except (urllib.error.URLError, OSError) as err:
            last = TockwardError(str(getattr(err, "reason", err)))
    raise last


def ping(token, ms=None, timeout=10, retries=2):
    """Report a successful run."""
    return _send(token, True, ms, timeout, retries)


def fail(token, ms=None, timeout=10, retries=2):
    """Report a failed run."""
    return _send(token, False, ms, timeout, retries)
