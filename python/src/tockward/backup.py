"""`tockward backup`: checks the backup files in a folder and returns what's
wrong, for a heartbeat. Only looks at names, sizes and times; never opens or
changes a file. The customer says what kind of backup it is:

  full (default)  every file is a full backup
  incremental     fulls (--full pattern or --full-day) plus increments
  differential    the same, but each differential grows until the next full

`--detect` prints what the folder looks like and suggests options; it sends
nothing. Same behaviour and output as the npm client (backup.js).
"""

import datetime
import hashlib
import json
import math
import os
import re
import time

DAYS = ["sun", "mon", "tue", "wed", "thu", "fri", "sat"]
DAY_NAMES = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"]
SETTLE_SECONDS = 60  # younger files may still be being written
PARTIAL = re.compile(r"\.(tmp|part|partial|inprogress|lock)$", re.I)


class CheckError(Exception):
    pass


def _round(x):
    """Rounds halves up, like JavaScript's Math.round."""
    return math.floor(x + 0.5)


def parse_duration(text):
    """'30m', '26h', '8d' -> seconds (a bare number is minutes, like tockward git)."""
    m = re.match(r"^(\d+)\s*([smhd]?)$", str(text or "").strip(), re.I)
    if not m:
        raise CheckError(f'can\'t read the duration "{text}" (use e.g. 26h, 8d)')
    return int(m.group(1)) * {"": 60, "s": 1, "m": 60, "h": 3600, "d": 86400}[m.group(2).lower()]


def parse_size(text):
    """'50MB', '2G', '500k', '1.5GB', '1000' -> bytes (powers of 1024)."""
    m = re.match(r"^(\d+(?:\.\d+)?)\s*([kmgt]?)i?b?$", str(text or "").strip(), re.I)
    if not m:
        raise CheckError(f'can\'t read the size "{text}" (use e.g. 50MB, 2GB)')
    return _round(float(m.group(1)) * 1024 ** " kmgt".index(m.group(2).lower() or " "))


def parse_percent(text):
    m = re.match(r"^(\d+)\s*%?$", str(text or "").strip())
    if not m or int(m.group(1)) > 99:
        raise CheckError(f'can\'t read the percentage "{text}" (use e.g. 50%, or 0 for off)')
    return int(m.group(1))


def glob_re(pattern):
    """A shell-style pattern (* and ?) on the file name, ignoring case."""
    body = "".join(".*" if c == "*" else "." if c == "?" else re.escape(c) for c in str(pattern))
    return re.compile(f"^{body}$", re.I | re.S)


def parse_args(args):
    o = {"folder": None, "detect": False, "kind": "full", "match": None, "full": None, "full_day": None,
         "max_age": 26 * 3600, "full_max_age": 8 * 86400, "min_size": 1, "drop": 50, "depth": 0}
    i = 0
    while i < len(args):
        a = args[i]

        def value():
            nonlocal i
            if i + 1 >= len(args):
                raise CheckError(f"{a} needs a value")
            i += 1
            return args[i]

        if a == "--detect":
            o["detect"] = True
        elif a == "--kind":
            o["kind"] = value().lower()
            if o["kind"] not in ("full", "incremental", "differential"):
                raise CheckError(f"--kind is full, incremental or differential, not {o['kind']}")
        elif a == "--match":
            o["match"] = value()
        elif a == "--full":
            o["full"] = value()
        elif a == "--full-day":
            d = value().lower()[:3]
            if d not in DAYS:
                raise CheckError("--full-day is a weekday: mon, tue, wed, thu, fri, sat or sun")
            o["full_day"] = DAYS.index(d)
        elif a == "--max-age":
            o["max_age"] = parse_duration(value())
        elif a == "--full-max-age":
            o["full_max_age"] = parse_duration(value())
        elif a == "--min-size":
            o["min_size"] = max(1, parse_size(value()))
        elif a == "--drop":
            o["drop"] = parse_percent(value())
        elif a == "--depth":
            try:
                o["depth"] = max(0, int(value()))
            except ValueError:
                o["depth"] = 0
        elif a.startswith("--"):
            raise CheckError(f"unknown option {a}")
        elif o["folder"] is None:
            o["folder"] = a
        else:
            raise CheckError(f"one folder only (got {o['folder']} and {a})")
        i += 1
    if o["folder"] is None:
        raise CheckError("which folder? e.g. tockward backup <token> /backups/db")
    if o["kind"] != "full" and o["full"] is None and o["full_day"] is None:
        raise CheckError(f"--kind {o['kind']} needs --full <pattern> or --full-day <day> to tell the full backups apart")
    return o


def _list_files(root, o, now):
    """Backup files in the folder (and depth levels of subfolders), oldest
    first. Hidden files, partial uploads and files still being written are
    skipped."""
    match = glob_re(o["match"]) if o["match"] else None
    files = []

    def walk(d, level):
        try:
            names = os.listdir(d)
        except PermissionError:
            raise CheckError(f"can't read {d}: permission denied") from None
        except OSError as err:
            raise CheckError(f"can't read {d}: {err.strerror or err}") from None
        for name in names:
            if name.startswith("."):
                continue
            full = os.path.join(d, name)
            try:
                st = os.stat(full)  # follows symlinks: a linked backup counts like any other
            except OSError:
                continue
            if os.path.isdir(full):
                if level < o["depth"]:
                    walk(full, level + 1)
                continue
            if not os.path.isfile(full) or PARTIAL.search(name) or (match and not match.match(name)):
                continue
            mtime = int(st.st_mtime)
            if now - mtime < SETTLE_SECONDS:
                continue
            files.append({"name": os.path.relpath(full, root), "size": st.st_size, "mtime": mtime})

    walk(root, 0)
    files.sort(key=lambda f: (f["mtime"], f["name"]))
    return files


def _weekday(ts):
    """0 = Sunday, in local time."""
    return (datetime.datetime.fromtimestamp(ts).weekday() + 1) % 7


def _is_full(f, o):
    if o["kind"] == "full":
        return True
    if o["full"] is not None:
        return bool(glob_re(o["full"]).match(os.path.basename(f["name"])))
    return _weekday(f["mtime"]) == o["full_day"]


def _num(v):
    return str(int(v)) if v == int(v) else str(v)


def fmt_size(b):
    if b < 1024:
        return f"{b} B"
    units = ["KB", "MB", "GB", "TB"]
    v, i = b / 1024, 0
    while v >= 1024 and i < len(units) - 1:
        v /= 1024
        i += 1
    return f"{_num(_round(v) if v >= 100 else _round(v * 10) / 10)} {units[i]}"


def fmt_age(s):
    if s < 3600:
        return f"{max(1, _round(s / 60))} min"
    if s < 2 * 86400:
        return f"{s // 3600} h"
    d, h = s // 86400, (s % 86400) // 3600
    return f"{d} d {h} h" if h else f"{d} d"


def _fmt_limit(s):
    if s % 86400 == 0:
        return f"{s // 86400} d"
    if s % 3600 == 0:
        return f"{s // 3600} h"
    return f"{_round(s / 60)} min"


def _state_file(token, folder):
    """The newest full from the last run, per token and folder, so a backup
    that is overwritten in place (one file) can still be compared with last
    time."""
    d = os.path.join(os.environ.get("XDG_CACHE_HOME") or os.path.join(os.path.expanduser("~"), ".cache"), "tockward")
    h = hashlib.sha256(f"{token}\n{folder}".encode()).hexdigest()[:16]
    return d, os.path.join(d, f"backup-{h}.json")


def _read_state(token, folder):
    try:
        with open(_state_file(token, folder)[1]) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def _write_state(token, folder, f):
    d, path = _state_file(token, folder)
    try:
        os.makedirs(d, exist_ok=True)
        with open(path, "w") as fh:
            json.dump({"name": f["name"], "mtime": f["mtime"], "size": f["size"]}, fh)
    except OSError:
        pass


def _drop_text(f, before, drop, what):
    """Is f more than drop% smaller than before? Returns the text or None."""
    if not drop or not before or not before.get("size") or f["size"] >= before["size"] * (1 - drop / 100):
        return None
    pct = _round((1 - f["size"] / before["size"]) * 100)
    return f"{f['name']} is {fmt_size(f['size'])}, {pct}% smaller than {what} ({fmt_size(before['size'])})"


def _check(token, folder, o, now):
    files = _list_files(folder, o, now)
    if not files:
        return [f"no backups found in {o['folder']}" + (f" matching {o['match']}" if o["match"] else "")], None, None
    problems = []
    newest = files[-1]
    fulls = [f for f in files if _is_full(f, o)]
    full = fulls[-1] if fulls else None

    if now - newest["mtime"] > o["max_age"]:
        problems.append(f"newest backup is {fmt_age(now - newest['mtime'])} old (limit {_fmt_limit(o['max_age'])}): {newest['name']}")
    if newest["size"] == 0 and newest is not full:
        problems.append(f"{newest['name']} is empty")

    if full is None:
        how = f"fulls look like {o['full']}" if o["full"] is not None else f"fulls are made on {DAY_NAMES[o['full_day']]}"
        problems.append(f"no full backup found ({how}): increments are useless without one")
    else:
        if o["kind"] != "full" and now - full["mtime"] > o["full_max_age"]:
            problems.append(f"newest full backup is {fmt_age(now - full['mtime'])} old (limit {_fmt_limit(o['full_max_age'])}): {full['name']}")
        if full["size"] == 0:
            problems.append(f"{full['name']} is empty")
        elif full["size"] < o["min_size"]:
            problems.append(f"{full['name']} is {fmt_size(full['size'])}, below the minimum of {fmt_size(o['min_size'])}")
        else:
            # Compared with the full before it, or with last run's if it was
            # overwritten in place.
            state = _read_state(token, folder)
            if len(fulls) > 1:
                before = fulls[-2]
            elif state and state.get("mtime", 0) < full["mtime"]:
                before = state
            else:
                before = None
            t = _drop_text(full, before, o["drop"], "the full backup before it")
            if t:
                problems.append(t)
        _write_state(token, folder, full)

        if o["kind"] == "differential":
            diffs = [f for f in files if f["mtime"] > full["mtime"] and not _is_full(f, o)]
            if len(diffs) > 1:
                t = _drop_text(diffs[-1], diffs[-2], o["drop"],
                               "the differential before it, while differentials should grow until the next full")
                if t:
                    problems.append(t)

    n = f"{len(files)} backup{'' if len(files) == 1 else 's'}"

    def describe(f):
        return f"{f['name']}, {fmt_size(f['size'])}, {fmt_age(now - f['mtime'])} old"

    if o["kind"] == "full" or full is None or newest is full:
        summary = f"{describe(newest)} ({n})"
    else:
        summary = f"newest {describe(newest)}; full {describe(full)} ({n})"
    # The size that goes along for the chart: the newest full, the backup
    # whose size means something from night to night.
    return problems, summary, (full or newest)["size"]


# --- --detect: a suggestion, not a decision ---------------------------------

TOKENS = ["full", "incr", "inc", "diff", "weekly", "daily", "base"]


def _median(xs):
    s = sorted(xs)
    return s[len(s) // 2] if s else 0


def _suggest_max_age(gap):
    """A suggested --max-age for files about gap seconds apart."""
    s = gap + max(gap * 0.08, 3600)
    return f"{math.ceil(s / 86400)}d" if s >= 2 * 86400 else f"{math.ceil(s / 3600)}h"


def _detect(folder, o, now):
    all_files = _list_files(folder, o, now)
    files = [f for f in all_files if now - f["mtime"] <= 35 * 86400][-120:]
    lines = []
    if not files:
        if all_files:
            return [f"No backups in the last 35 days in {o['folder']} (newest is {fmt_age(now - all_files[-1]['mtime'])} old)."]
        return [f"No files found in {o['folder']}."]
    gaps = [g for g in (b["mtime"] - a["mtime"] for a, b in zip(files, files[1:])) if g > 0]
    gap = _median(gaps) or 86400
    max_age = _suggest_max_age(gap)
    biggest = max(f["size"] for f in files)
    large = [f for f in files if f["size"] >= biggest * 0.5]
    small = [f for f in files if f["size"] < biggest * 0.5]
    large_ids = {id(f) for f in large}

    def size_range(fs):
        lo, hi = fmt_size(min(f["size"] for f in fs)), fmt_size(max(f["size"] for f in fs))
        return lo if lo == hi else f"{lo}-{hi}"

    base = f"tockward backup <token> {o['folder']}" + (f" --match '{o['match']}'" if o["match"] else "") + (f" --depth {o['depth']}" if o["depth"] else "")

    lines.append(f"Found {len(files)} file{'' if len(files) == 1 else 's'} in the last 35 days, about one every {fmt_age(gap)}, newest {fmt_age(now - files[-1]['mtime'])} old.")

    # All about the same size (or too few to tell): a full backup each time.
    if len(files) < 3 or not small or _median([f["size"] for f in small]) > _median([f["size"] for f in large]) * 0.3:
        lines.append(f"  {'one' if len(files) == 1 else 'all'} {size_range(files)}: looks like a full backup each time")
        lines.append(f"Suggestion: {base} --max-age {max_age}")
        return lines

    # Only the newest is large: the newest is always full (reverse incremental).
    if len(large) == 1 and large[0] is files[-1]:
        ext = os.path.splitext(large[0]["name"])[1]
        lines.append(f"  newest {large[0]['name']}, {fmt_size(large[0]['size'])}: looks like the full backup, kept up to date")
        lines.append(f"  {len(small)} smaller ({size_range(small)}): look like the history kept beside it")
        plain = re.sub(r" --match '[^']*'", "", base, count=1)
        lines.append(f"Suggestion: {plain} --match '*{ext or os.path.basename(large[0]['name'])}' --max-age {max_age}")
        return lines

    def lc(f):
        return os.path.basename(f["name"]).lower()

    token = next((t for t in TOKENS if all(t in lc(f) for f in large) and all(t not in lc(f) for f in small)), None)
    large_ext = {os.path.splitext(f["name"])[1].lower() for f in large}
    ext = None
    if len(large_ext) == 1:
        only = next(iter(large_ext))
        if only and all(os.path.splitext(f["name"])[1].lower() != only for f in small):
            ext = only
    counts = {}
    for f in large:
        counts[_weekday(f["mtime"])] = counts.get(_weekday(f["mtime"]), 0) + 1
    top_day = next((d for d in sorted(counts) if counts[d] == len(large)), None)

    # Differential when the small ones clearly grow between fulls.
    steps = grows = 0
    for a, b in zip(files, files[1:]):
        if id(a) in large_ids or id(b) in large_ids:
            continue
        steps += 1
        if b["size"] > a["size"] * 1.02:
            grows += 1
    kind = "differential" if steps >= 2 and grows / steps >= 0.8 else "incremental"

    when = f", on {DAY_NAMES[top_day]}{'s' if len(large) > 1 else ''}" if top_day is not None else ""
    lines.append(f"  {len(large)} large ({size_range(large)}{when}): look like full backups")
    kind_text = "differentials (they grow until the next full)" if kind == "differential" else "incrementals"
    lines.append(f"  {len(small)} smaller ({size_range(small)}): look like {kind_text}")
    full_gap = _median([b["mtime"] - a["mtime"] for a, b in zip(large, large[1:])]) if len(large) > 1 else 7 * 86400
    if token:
        full_opt = f"--full '*{token}*'"
    elif ext:
        full_opt = f"--full '*{ext}'"
    elif top_day is not None:
        full_opt = f"--full-day {DAYS[top_day]}"
    else:
        full_opt = None
    if not full_opt:
        lines.append("  The names don't show which files are fulls; give a pattern with --full, e.g. --full '*full*'.")
        lines.append(f"Suggestion: {base} --kind {kind} --full '<pattern>' --max-age {max_age} --full-max-age {_suggest_max_age(full_gap)}")
        return lines
    lines.append(f"Suggestion: {base} --kind {kind} {full_opt} --max-age {max_age} --full-max-age {_suggest_max_age(full_gap)}")
    return lines


def run(token, args, now=None):
    """Runs the check (or --detect). Returns (code, message, lines, size):
    code 0 ok, 1 problems, 2 the check itself failed; message is what goes
    to Tockward (None for --detect, which sends nothing); size is the
    newest full backup's, sent along for the chart (None when unknown)."""
    now = int(time.time()) if now is None else now
    try:
        o = parse_args(args)
        folder = os.path.abspath(o["folder"])
        if not os.path.exists(folder):
            raise CheckError(f"{o['folder']} does not exist (not mounted?)")
        if not os.path.isdir(folder):
            raise CheckError(f"{o['folder']} is not a folder")
        if o["detect"]:
            return 0, None, _detect(folder, o, now), None
        problems, summary, size = _check(token, folder, o, now)
        if not problems:
            ok = f"ok: {summary}"
            return 0, ok, [ok], size
        return 1, "\n".join(problems), problems, size
    except CheckError as err:
        msg = f"backup check failed: {err}"
        return 2, msg, [msg], None
