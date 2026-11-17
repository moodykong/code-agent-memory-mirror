"""companion_repos: public repos a cloud session needs next to the project but cannot attach.

In env.json: "companion_repos": [{"repo": "https://github.com/owner/name", "ref": "v1.2", "dir": "name"}].
`cloud render` adds a setup-script step that clones each one into ~/companions/<dir> at that ref
without a token (git init, fetch --depth 1 origin <ref>, checkout FETCH_HEAD; never fails the
script). On a cloud runtime `session-start` links ~/companions/<dir> next to the project clone as
../<dir> when nothing is there yet.
"""

import os
import re

from agent_mirror.links import is_linklike, link_target, make_link
from agent_mirror.util import norm, out, tilde, warn

REPO_RX = re.compile(
    r"^https://(?P<host>[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?)+)"
    r"/(?P<path>[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)+)/?$"
)
REF_RX = re.compile(r"^[A-Za-z0-9._/-]+$")
DIR_RX = re.compile(r"^[A-Za-z0-9._-]+$")


def entry_problem(item):
    """A reason this companion_repos entry is refused, or None."""
    if not isinstance(item, dict):
        return "each companion_repos entry must be an object with repo, ref and dir"
    repo, ref, folder = (str(item.get(k) or "").strip() for k in ("repo", "ref", "dir"))
    m = REPO_RX.match(repo)
    if not m or any(part in (".", "..") for part in m.group("path").split("/")):
        return (
            "companion repo %r must be a public https URL of the form https://host/owner/name "
            "(no credentials, port, query or other scheme)" % repo
        )
    if not ref or not REF_RX.match(ref) or ref.startswith("-") or ".." in ref:
        return "companion %r: ref %r must be a branch, tag or commit (letters, digits, . _ / -)" % (repo, ref)
    if not folder or not DIR_RX.match(folder) or set(folder) == {"."} or folder.startswith("-"):
        return "companion %r: dir %r must be one folder name (no path separators, not dots only)" % (repo, folder)
    return None


def problems(env):
    value = env.get("companion_repos")
    if value is None:
        return []
    if not isinstance(value, list):
        return ["companion_repos must be a list of {repo, ref, dir} objects"]
    found = [p for p in (entry_problem(item) for item in value) if p]
    dirs = [str(item.get("dir")) for item in value if isinstance(item, dict)]
    for d in sorted(set(d for d in dirs if dirs.count(d) > 1)):
        found.append("companion_repos: dir %r is used twice" % d)
    return found


def entries(env):
    """The valid entries as (repo, ref, dir)."""
    found = []
    for item in env.get("companion_repos") or []:
        if entry_problem(item) is None:
            found.append(tuple(str(item[k]).strip() for k in ("repo", "ref", "dir")))
    return found


def hosts(env):
    return [REPO_RX.match(repo).group("host") for repo, _, _ in entries(env)]


def setup_step(project, env):
    """Bash lines for the setup scripts (values are validated, so they are safe inside double quotes)."""
    items = entries(env)
    if not items:
        return "# no companion_repos in projects/%s/env.json" % project
    lines = [
        "# public companion repos from projects/%s/env.json, cloned without a token into ~/companions/<dir>;" % project,
        "# session-start links each one next to the project clone as ../<dir>",
        'mkdir -p "$HOME/companions" || true',
    ]
    for repo, ref, folder in items:
        lines.append(
            '( d="$HOME/companions/%s"; mkdir -p "$d" && git -C "$d" init -q && '
            '{ git -C "$d" remote add origin "%s" 2>/dev/null || git -C "$d" remote set-url origin "%s"; } && '
            'timeout 120 git -C "$d" fetch -q --depth 1 origin "%s" && git -C "$d" checkout -q --force FETCH_HEAD ) || true'
            % (folder, repo, repo, ref)
        )
        lines.append(
            'note "companion %s at %s: $(git -C "$HOME/companions/%s" rev-parse --short HEAD 2>/dev/null || echo missing)"'
            % (folder, ref, folder)
        )
    return "\n".join(lines)


def link_companions(plat, project, clone, env, dry_run):
    """Link ~/companions/<dir> to <clone>/../<dir> for each companion repo (cloud session-start)."""
    for problem in problems(env):
        warn("%s (projects/%s/env.json); that entry is skipped" % (problem, project))
    for repo, ref, folder in entries(env):
        src = os.path.join(plat.companions_dir(), folder)
        dest = os.path.join(os.path.dirname(os.path.abspath(clone)), folder)
        shown = "../%s next to %s" % (folder, tilde(clone))
        if not os.path.isdir(src):
            out("companion %s: %s is missing (the setup script clones it); not linked" % (folder, tilde(src)))
            continue
        if os.path.lexists(dest):
            if is_linklike(dest) and norm(link_target(dest) or "") == norm(src):
                out("companion %s: %s already links to %s" % (folder, shown, tilde(src)))
            else:
                out("companion %s: something is already at %s; left alone" % (folder, tilde(dest)))
            continue
        if dry_run:
            out("would link companion %s: %s -> %s" % (folder, shown, tilde(src)))
            continue
        try:
            make_link(dest, src, True, plat.windows)
            out("companion %s: linked %s -> %s (%s at %s)" % (folder, shown, tilde(src), repo, ref))
        except OSError as exc:
            warn("companion %s: could not link %s: %s" % (folder, tilde(dest), exc))
