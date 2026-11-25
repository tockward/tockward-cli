"""`tockward git`: checks repos on this machine against their remote and
returns what's wrong, for a heartbeat. Read-only: only git status,
rev-parse, for-each-ref, cat-file, merge-base and ls-remote; never fetch or
pull. Uses this machine's own git setup and access.

  mirror (default)  every repo under the folder (a Git server's repos): are
                    its branches and tags the same as the hosted remote
                    (GitHub, GitLab, Bitbucket, else origin)?
  deploy (--deploy) one checkout (a production server): nothing changed by
                    hand, on the expected branch, on the remote's latest
                    commit.

Same behaviour and output as the npm client (git.js).
"""

import hashlib
import json
import os
import re
import subprocess
import time

HOSTED = re.compile(r"(^|[@/.])(github\.com|gitlab\.com|bitbucket\.org)[:/]", re.I)
GIT_TIMEOUT = 60


class CheckError(Exception):
    pass


def _git(d, args, allow_fail=False):
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0", LC_ALL="C")
    try:
        r = subprocess.run(["git", "-C", d, *args], stdin=subprocess.DEVNULL, capture_output=True,
                           text=True, timeout=GIT_TIMEOUT, env=env)
    except FileNotFoundError:
        raise CheckError("git is not installed") from None
    except subprocess.TimeoutExpired:
        if allow_fail:
            return None
        raise CheckError(f"git {args[0]} timed out") from None
    if r.returncode != 0:
        if allow_fail:
            return None
        lines = [l.strip() for l in r.stderr.split("\n") if l.strip()]
        why = next((l for l in lines if re.match(r"^(fatal|error):", l)), lines[0] if lines else f"exit {r.returncode}")
        raise CheckError(re.sub(r"^(fatal|error): ", "", why))
    return r.stdout


def parse_duration(text):
    """'30m', '2h', '90s', '1d' -> seconds."""
    m = re.match(r"^(\d+)\s*([smhd]?)$", str(text or "").strip(), re.I)
    if not m:
        raise CheckError(f'can\'t read the duration "{text}" (use e.g. 30m, 2h, 1d)')
    return int(m.group(1)) * {"": 60, "s": 1, "m": 60, "h": 3600, "d": 86400}[m.group(2).lower()]


def parse_args(args):
    o = {"folder": ".", "deploy": False, "branch": None, "remote": None, "behind_after": 0, "depth": 3}
    i = 0
    while i < len(args):
        a = args[i]

        def value():
            nonlocal i
            if i + 1 >= len(args):
                raise CheckError(f"{a} needs a value")
            i += 1
            return args[i]

        if a == "--deploy":
            o["deploy"] = True
        elif a == "--branch":
            o["branch"] = value()
        elif a == "--remote":
            o["remote"] = value()
        elif a == "--behind-after":
            o["behind_after"] = parse_duration(value())
        elif a == "--depth":
            v = value()
            o["depth"] = max(0, int(v)) if v.isdigit() else 0
        elif a.startswith("--"):
            raise CheckError(f"unknown option {a}")
        else:
            o["folder"] = a
        i += 1
    return o


def _is_repo(d):
    j = os.path.join
    return os.path.exists(j(d, ".git")) or all(os.path.exists(j(d, x)) for x in ("HEAD", "objects", "refs"))


def find_repos(root, depth):
    """Repos at or under the folder, up to depth levels down; hidden folders
    and node_modules are skipped, and nothing inside a repo is searched."""
    found = []

    def walk(d, level):
        if _is_repo(d):
            found.append(d)
            return
        if level >= depth:
            return
        try:
            entries = sorted(os.scandir(d), key=lambda e: e.name)
        except OSError:
            return
        for e in entries:
            if e.is_dir() and not e.name.startswith(".") and e.name != "node_modules":
                walk(os.path.join(d, e.name), level + 1)

    walk(root, 0)
    return found


def _pick_remote(d, wanted):
    """The remote to compare with: --remote, else a hosted one, else origin."""
    names = [n for n in (_git(d, ["remote"]) or "").split("\n") if n]
    if wanted:
        return wanted if wanted in names else None
    for n in names:
        url = _git(d, ["remote", "get-url", n], allow_fail=True) or ""
        if HOSTED.search(url.strip()):
            return n
    return "origin" if "origin" in names else None


def _ref_map(text):
    out = {}
    for line in (text or "").split("\n"):
        parts = line.split()
        if len(parts) >= 2 and not parts[1].endswith("^{}"):
            out[parts[1]] = parts[0]
    return out


def _has_object(d, h):
    return _git(d, ["cat-file", "-e", f"{h}^{{commit}}"], allow_fail=True) is not None


def _is_ancestor(d, a, b):
    return _git(d, ["merge-base", "--is-ancestor", a, b], allow_fail=True) is not None


def _short(h):
    return h[:7]


def _short_ref(r):
    return re.sub(r"^refs/(heads|tags)/", "", r)


def _names(items):
    shown = ", ".join(items[:3])
    return f"{shown} and {len(items) - 3} more" if len(items) > 3 else shown


def _check_mirror(d, label, remote_wanted):
    remote = _pick_remote(d, remote_wanted)
    if not remote and remote_wanted:
        return {"problems": [{"kind": "error", "text": f"{label}: no remote {remote_wanted}"}]}
    if not remote:
        return {"skipped": True}
    try:
        theirs = _ref_map(_git(d, ["ls-remote", "--heads", "--tags", remote]))
    except CheckError as err:
        return {"problems": [{"kind": "error", "text": f"{label}: {remote} unreachable ({err})"}]}
    ours = _ref_map(_git(d, ["for-each-ref", "--format=%(objectname) %(refname)", "refs/heads", "refs/tags"]))
    behind, ahead, differs = [], [], []
    for ref, h in theirs.items():
        mine = ours.get(ref)
        if mine == h:
            continue
        name = _short_ref(ref)
        if not mine:
            behind.append(name)
        elif ref.startswith("refs/tags/"):
            differs.append(f"tag {name}")
        elif not _has_object(d, h):
            behind.append(name)
        elif _is_ancestor(d, mine, h):
            behind.append(name)
        elif _is_ancestor(d, h, mine):
            ahead.append(name)
        else:
            differs.append(name)
    problems = []
    if differs:
        problems.append({"kind": "differs", "text": f"{label}: {_names(differs)} differ{'s' if len(differs) == 1 else ''} from {remote}"})
    if behind:
        problems.append({"kind": "behind", "key": label, "text": f"{label}: {_names(behind)} behind {remote}"})
    if ahead:
        problems.append({"kind": "ahead", "text": f"{label}: {_names(ahead)} ahead of {remote}"})
    return {"problems": problems}


def _check_deploy(d, o):
    if not os.path.exists(os.path.join(d, ".git")):
        raise CheckError(f"{d} is not a git checkout")
    problems = []
    changed = len([l for l in _git(d, ["--no-optional-locks", "status", "--porcelain"]).split("\n") if l])
    if changed:
        problems.append({"kind": "changed", "text": f"{changed} file{'' if changed == 1 else 's'} changed on the server"})

    remote = _pick_remote(d, o["remote"])
    if not remote and o["remote"]:
        raise CheckError(f"{d} has no remote {o['remote']}")
    current = (_git(d, ["symbolic-ref", "--short", "-q", "HEAD"], allow_fail=True) or "").strip()
    head = _git(d, ["rev-parse", "HEAD"]).strip()
    if not remote:
        # One copy only (pushed straight into this folder): nothing to compare
        # with, so only files changed by hand and the branch are checked.
        if o["branch"] and current != o["branch"]:
            where = f"branch {current}" if current else "a detached HEAD"
            problems.append({"kind": "branch", "text": f"on {where}, expected {o['branch']}"})
        return {"problems": problems, "branch": current or "detached HEAD", "head": head, "local": True}
    branch = o["branch"] or current
    if not branch:
        # Detached HEAD without --branch: the remote's default branch.
        try:
            m = re.search(r"ref: refs/heads/(\S+)\s+HEAD", _git(d, ["ls-remote", "--symref", remote, "HEAD"]))
            branch = m.group(1) if m else None
        except CheckError as err:
            return {"problems": problems + [{"kind": "error", "text": f"{remote} unreachable ({err})"}]}
        if not branch:
            raise CheckError("detached HEAD: say which branch with --branch")
    if o["branch"] and current and current != o["branch"]:
        problems.append({"kind": "branch", "text": f"on branch {current}, expected {o['branch']}"})

    try:
        latest = _ref_map(_git(d, ["ls-remote", "--heads", remote, branch])).get(f"refs/heads/{branch}")
    except CheckError as err:
        return {"problems": problems + [{"kind": "error", "text": f"{remote} unreachable ({err})"}]}
    if not latest:
        problems.append({"kind": "error", "text": f"{remote} has no branch {branch}"})
    elif latest != head:
        if _has_object(d, latest) and _is_ancestor(d, latest, head):
            n = int((_git(d, ["rev-list", "--count", f"{latest}..{head}"]) or "0").strip() or 0)
            problems.append({"kind": "ahead", "text": f"{n} commit{'' if n == 1 else 's'} on the server that {remote}/{branch} doesn't have"})
        else:
            problems.append({"kind": "behind", "key": "deploy",
                             "text": f"not on the latest {branch}: on {_short(head)}, {remote}/{branch} is at {_short(latest)}"})
    return {"problems": problems, "branch": branch, "head": head}


def _apply_behind_after(problems, seconds, token, folder):
    """--behind-after: "behind" only counts once it has lasted that long; the
    first time seen is kept per token and folder in ~/.cache/tockward/."""
    if not seconds:
        return problems, []
    d = os.path.join(os.environ.get("XDG_CACHE_HOME") or os.path.join(os.path.expanduser("~"), ".cache"), "tockward")
    f = os.path.join(d, "git-" + hashlib.sha256(f"{token}\n{folder}".encode()).hexdigest()[:16] + ".json")
    try:
        with open(f) as fh:
            seen = json.load(fh)
    except (OSError, ValueError):
        seen = {}
    now = int(time.time())
    nxt, kept, waiting = {}, [], []
    for p in problems:
        if p["kind"] != "behind":
            kept.append(p)
            continue
        since = seen.get(p["key"]) or now
        nxt[p["key"]] = since
        (kept if now - since >= seconds else waiting).append(p)
    try:
        os.makedirs(d, exist_ok=True)
        with open(f, "w") as fh:
            json.dump(nxt, fh, separators=(",", ":"))
    except OSError:
        pass
    return kept, waiting


ORDER = ["error", "differs", "changed", "branch", "ahead", "behind"]


def _fmt_duration(s):
    if s % 86400 == 0:
        return f"{s // 86400} d"
    if s % 3600 == 0:
        return f"{s // 3600} h"
    return f"{round(s / 60)} min" if s >= 60 else f"{s} s"


def _plural(n, word):
    return f"{n} {word}{'' if n == 1 else 's'}"


def run(token, args):
    """Runs the check. Returns (code, message, lines): code 0 ok, 1 problems,
    2 the check itself failed; message is what goes to Tockward."""
    try:
        o = parse_args(args)
        folder = os.path.abspath(o["folder"])
        if not os.path.exists(folder):
            raise CheckError(f"{o['folder']} does not exist")
        problems = []
        if o["deploy"]:
            r = _check_deploy(folder, o)
            problems = r["problems"]
            summary = (f"on {r['branch']} at {_short(r['head'])}" + (" (no remote: checked for changes only)" if r.get("local") else "")) if r.get("head") else "checked"
        else:
            repos = find_repos(folder, o["depth"])
            if not repos:
                raise CheckError(f"no git repos found in {o['folder']}")
            skipped = 0
            for d in repos:
                label = os.path.relpath(d, folder)
                if label == ".":
                    label = os.path.basename(d)
                r = _check_mirror(d, label, o["remote"])
                if r.get("skipped"):
                    skipped += 1
                else:
                    problems.extend(r["problems"])
            checked = len(repos) - skipped
            summary = f"{_plural(checked, 'repo')} checked" + (f", {skipped} without a remote skipped" if skipped else "")
        kept, waiting = _apply_behind_after(problems, o["behind_after"], token, folder)
        kept.sort(key=lambda p: ORDER.index(p["kind"]))
        if not kept:
            if waiting:
                ok = f"ok for now: {summary}; behind for less than {_fmt_duration(o['behind_after'])}"
            else:
                ok = f"{'up to date' if o['deploy'] else 'in sync'}: {summary}"
            return 0, ok, [ok] + [f"  waiting: {p['text']}" for p in waiting]
        lines = [p["text"] for p in kept]
        return 1, "\n".join(lines), lines
    except CheckError as err:
        msg = f"git check failed: {err}"
        return 2, msg, [msg]
