"""`tockward git`: checks repos on this machine against their remote and
returns what's wrong, for a heartbeat. Read-only: only git status,
rev-parse, for-each-ref, cat-file, merge-base and ls-remote; never fetch or
pull. Uses this machine's own git setup and access.

  mirror (default)  every repo under the folder (a Git server's repos): are
                    its branches and tags the same as the hosted remote
                    (GitHub, GitLab, Bitbucket, else origin)? With
                    --compare-with: the same, against a URL per repo, for
                    servers that are only pushed to (push to both) and so
                    have no remote.
  deploy (--deploy) one checkout (a production server): nothing changed by
                    hand, on the expected branch, on the remote's latest
                    commit.

Same behaviour and output as the npm client (git.js).
"""

import base64
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


# Extra git config for every git call (the tokens for --compare-with), as
# GIT_CONFIG_* environment variables: never on a command line, where other
# users could see it in ps, and never in a URL that can end up in a message.
_git_env = {}


def _git(d, args, allow_fail=False):
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0", LC_ALL="C", **_git_env)
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
    o = {"folder": ".", "deploy": False, "branch": None, "remote": None, "behind_after": 0, "depth": 3,
         "compare_with": None, "map": {}, "skip": [], "token_file": None}
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
        elif a == "--compare-with":
            o["compare_with"] = value()
        elif a == "--map":
            m = re.match(r"^([^=]+)=(.+)$", value())
            if not m:
                raise CheckError("--map needs repo=url, e.g. --map app=https://github.com/acme/app")
            o["map"][_repo_name(m.group(1).strip()).lower()] = m.group(2).strip()
        elif a == "--skip":
            o["skip"] += [_repo_name(s.strip()).lower() for s in value().split(",") if s.strip()]
        elif a == "--token-file":
            o["token_file"] = value()
        elif a.startswith("--"):
            raise CheckError(f"unknown option {a}")
        else:
            o["folder"] = a
        i += 1
    if o["compare_with"] or o["map"] or o["skip"] or o["token_file"]:
        if o["deploy"]:
            raise CheckError("--compare-with, --map, --skip and --token-file are for a folder of repos, not --deploy")
        if o["remote"]:
            raise CheckError("use --remote or --compare-with, not both")
        if not o["compare_with"]:
            raise CheckError("--map, --skip and --token-file go with --compare-with")
        if "{repo}" not in o["compare_with"]:
            raise CheckError("--compare-with needs {repo} in it, e.g. https://github.com/acme/{repo}")
    return o


def _repo_name(p):
    """A repo's name: its folder without .git ("app.git" -> "app")."""
    return re.sub(r"\.git$", "", os.path.basename(p.rstrip("/")), flags=re.I)


def _url_name(url):
    """"GitHub" for a github.com URL, and so on; else the host."""
    m = re.match(r"^[a-z+]+://(?:[^@/]*@)?([^/:]+)", url, re.I) or re.match(r"^[^@]+@([^:]+):", url)
    host = m.group(1) if m else url
    return {"github.com": "GitHub", "gitlab.com": "GitLab", "bitbucket.org": "Bitbucket"}.get(host.lower(), host)


def _token_config(o):
    """--token-file: one token for the --compare-with host, or lines
    "<url prefix> <token>" (a GitHub fine-grained token covers one owner, so
    two owners need two). Sent as HTTP basic auth, only to matching URLs."""
    text = os.environ.get("TOCKWARD_GIT_TOKEN", "")
    if o["token_file"]:
        try:
            with open(o["token_file"]) as fh:
                text = fh.read()
        except OSError as err:
            why = (err.strerror or str(err)).lower()
            raise CheckError(f"can't read the token file {o['token_file']} ({'no such file' if why == 'no such file or directory' else why})") from None
    pairs = []
    for raw in text.split("\n"):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) == 1:
            m = re.match(r"^(https?://[^/]+/)", o["compare_with"], re.I)
            if not m:
                raise CheckError("a token works with an https:// --compare-with URL only")
            pairs.append((m.group(1), parts[0]))
        else:
            pairs.append((parts[0] if parts[0].endswith("/") else parts[0] + "/", parts[1]))
    env = {}
    for i, (prefix, token) in enumerate(pairs):
        env[f"GIT_CONFIG_KEY_{i}"] = f"http.{prefix}.extraHeader"
        env[f"GIT_CONFIG_VALUE_{i}"] = "Authorization: Basic " + base64.b64encode(f"x-access-token:{token}".encode()).decode()
    if pairs:
        env["GIT_CONFIG_COUNT"] = str(len(pairs))
    return env


def _access_hint(message):
    """git's own words for "no access" made into what to do about it."""
    if re.search(r"could not read Username|terminal prompts disabled|Authentication failed|403", message, re.I):
        if _git_env.get("GIT_CONFIG_COUNT"):
            return "no access: the token was refused, or it can't read this repo"
        return "no access: a private repo needs a token (--token-file)"
    if re.search(r"not found", message, re.I):
        return "not found, or the token can't read it"
    return message


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
            entries = sorted(os.scandir(d), key=lambda e: (e.name.lower(), e.name))  # as the npm client
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


def _check_mirror(d, label, remote_wanted, url=None):
    """One repo against its remote, or against a URL (--compare-with)."""
    remote = _url_name(url) if url else _pick_remote(d, remote_wanted)
    if not remote and remote_wanted:
        return {"problems": [{"kind": "error", "text": f"{label}: no remote {remote_wanted}"}]}
    if not remote:
        return {"skipped": True}
    try:
        theirs = _ref_map(_git(d, ["ls-remote", "--heads", "--tags", url or remote]))
    except CheckError as err:
        why = _access_hint(str(err)) if url else err
        return {"problems": [{"kind": "error", "text": f"{label}: {remote} unreachable ({why})"}]}
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
            if o["compare_with"]:
                _git_env.clear()
                _git_env.update(_token_config(o))
            skipped = 0
            for d in repos:
                label = os.path.relpath(d, folder)
                if label == ".":
                    label = os.path.basename(d)
                name = _repo_name(d)
                if o["compare_with"] and name.lower() in o["skip"]:
                    skipped += 1
                    continue
                url = (o["map"].get(name.lower()) or o["compare_with"].replace("{repo}", name)) if o["compare_with"] else None
                r = _check_mirror(d, label, o["remote"], url)
                if r.get("skipped"):
                    skipped += 1
                else:
                    problems.extend(r["problems"])
            checked = len(repos) - skipped
            # Nothing compared is no proof of anything: fail rather than say ok.
            if not checked:
                if o["compare_with"]:
                    raise CheckError(f"all {len(repos)} repos in {o['folder']} are skipped: nothing was compared")
                raise CheckError(f"none of the {len(repos)} repos in {o['folder']} has a remote to compare with; "
                                 "for a server that is only pushed to, use --compare-with")
            against = f" against {_url_name(o['compare_with'])}" if o["compare_with"] else ""
            summary = f"{_plural(checked, 'repo')} checked{against}" + (
                f", {skipped} {'skipped' if o['compare_with'] else 'without a remote skipped'}" if skipped else "")
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
