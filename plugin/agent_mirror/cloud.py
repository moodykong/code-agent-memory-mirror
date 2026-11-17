"""cloud render: paste-ready settings for Claude cloud and Codex cloud, written into the store.

Output goes to <store>/projects/<project>/cloud/<vendor>/. Nothing secret is
ever rendered: the store token is a placeholder (Claude) or a setup-only
secret variable (Codex legacy container), environment variables are names
only, and any credentials in the store's remote URL are dropped.
"""

import json
import os
import re

from agent_mirror import PLUGIN_ROOT, companions, recipes, runbook
from agent_mirror.envfile import placement_problem, privacy_words, write_secret_file
from agent_mirror.util import err, expand, out, read_text, run, tilde, warn, write_text

TEMPLATES = os.path.join(PLUGIN_ROOT, "templates", "cloud")
SAFE_REF = re.compile(r"^[A-Za-z0-9._/-]+$")
SAFE_PYTHON = re.compile(r"^[0-9][0-9A-Za-z.+-]*$")
SAFE_URL_PATH = re.compile(r"^[A-Za-z0-9._~-]+(/[A-Za-z0-9._~-]+)+$")
SAFE_HOST = re.compile(r"^[A-Za-z0-9.-]+(:[0-9]+)?$")
PYTHON_HOSTS = [
    "pypi.org",
    "files.pythonhosted.org",
    "astral.sh",
    "objects.githubusercontent.com",
    "release-assets.githubusercontent.com",
]
AZURE_HOSTS = ["login.microsoftonline.com", "management.azure.com"]
SP_VARS = ("AZURE_CLIENT_ID", "AZURE_CLIENT_SECRET", "AZURE_TENANT_ID")


def repo_host_path(url):
    """(host, 'owner/name') from a git remote URL, with any credentials dropped; None if unknown."""
    url = (url or "").strip()
    m = re.match(r"^(?:https?|ssh|git)://(?:[^@/]*@)?([^/:]+)(?::\d+)?/(.+?)(?:\.git)?/?$", url)
    if not m:
        m = re.match(r"^[^@/\s]+@([^:/\s]+):(.+?)(?:\.git)?/?$", url)
    if not m:
        return None
    host, path = m.group(1), m.group(2).strip("/")
    if not SAFE_HOST.match(host) or not SAFE_URL_PATH.match(path):
        return None
    return host, path


def store_remote(ctx, store):
    rc, url, _ = run(["git", "-C", store.root, "remote", "get-url", "origin"], timeout=10)
    if rc == 0 and url.strip():
        return url.strip()
    return str(ctx.config.get("store_remote") or "")


def template(name):
    return read_text(os.path.join(TEMPLATES, name))


def fill(text, values):
    for key, value in values.items():
        text = text.replace("@@%s@@" % key, value)
    left = re.findall(r"@@[A-Z_]+@@", text)
    if left:
        raise ValueError("unfilled template placeholders: %s" % ", ".join(sorted(set(left))))
    return text


def setup_commands_step(project, commands):
    if not commands:
        return "# no setup_commands in projects/%s/env.json" % project
    lines = ["# the owner's setup_commands from projects/%s/env.json" % project]
    n = len(commands)
    for i, cmd in enumerate(commands, 1):
        lines.append("( %s ) || true" % cmd)
        lines.append('note "setup command %d of %d done"' % (i, n))
    return "\n".join(lines)


def hosts_for(env, store_host, box):
    hosts = []

    def add(h):
        if h and h not in hosts:
            hosts.append(h)

    for h in env.get("allowed_hosts") or []:
        add(str(h).strip())
    add(store_host or "github.com")
    add("github.com")  # the public agent-mirror repo
    for h in companions.hosts(env):
        add(h)
    if env.get("python"):
        for h in PYTHON_HOSTS:
            add(h)
    if str(box.get("backend") or "").lower() == "azure-blob":
        for h in AZURE_HOSTS:
            add(h)
        account = str(box.get("storage_account") or "").strip()
        add("%s.blob.core.windows.net" % account if account else "*.blob.core.windows.net")
    return hosts


def env_names_for(env, vendor, box):
    lines = ["AGENT_RUNTIME=codex-cloud"] if vendor == "codex" else []
    seen = {"AGENT_RUNTIME"}
    wanted = [str(n).strip() for n in env.get("env_var_names") or []]
    if str(box.get("backend") or "").lower() == "azure-blob":
        wanted += list(SP_VARS)
    for n in wanted:
        if n and n not in seen:
            seen.add(n)
            lines.append(n + "=")
    return lines


ON_DEMAND = (
    "\nCloud machines have little disk, so nothing is fetched up front. The agent browses the box offline\n"
    "with `agent-mirror box ls` (from the committed `projects/<project>/box_index.jsonl` and `box_toc/`),\n"
    "fetches what the task needs with `agent-mirror box fetch <path>`, checks `agent-mirror box status`, and\n"
    "frees space with `agent-mirror box evict <path>`. Fetches refuse to run when they would leave less than\n"
    "`box.min_free_bytes` (default 2 GiB) free. Archived folders fetch their bundle only; large files are\n"
    "listed with the exact command to fetch each one.\n"
)


def box_note(box):
    backend = str(box.get("backend") or "none").lower()
    if backend == "azure-blob":
        return (
            "\n## Box (azure-blob)\n\n"
            "`agent-mirror box index`, `box fetch` and `box send` need the Azure CLI (`az`). Install it with a\n"
            "`setup_commands` line in env.json (for example from PyPI with uv, or Microsoft's apt package) and\n"
            "allow the hosts that install needs. agent-mirror signs `az` in as a service principal from\n"
            "`AZURE_CLIENT_ID`, `AZURE_CLIENT_SECRET` and `AZURE_TENANT_ID` when `az account show` fails; the\n"
            "secret goes to `az` on stdin and never appears in output. Give that identity read access to the\n"
            "inbound container and write access to the outbound container only.\n" + ON_DEMAND
        )
    if backend == "rclone":
        return (
            "\n## Box (rclone)\n\n"
            "`agent-mirror box` needs `rclone` on the machine (install it with a `setup_commands` line).\n"
            "A cloud machine has no rclone config file, so the remote is defined by environment variables:\n"
            "rclone reads a remote named `<remote>` from variables called `RCLONE_CONFIG_<REMOTE>_<KEY>`\n"
            "(for example `RCLONE_CONFIG_MYDRIVE_TYPE`, `_TOKEN`, `_DRIVE_ID`, `_DRIVE_TYPE`). Such a remote is\n"
            "named in lower case (`mydrive:`), so use lower-case remote names in box.inbound_remote and\n"
            "box.outbound_remote. On the developer machine, `agent-mirror box rclone-env --remote mydrive` prints\n"
            "the variable NAMES (add them to env_var_names, then re-render), and `--out FILE` writes the values to\n"
            "an owner-only file to copy onto the settings page. An OAuth token stored this way cannot be refreshed\n"
            "on a throwaway machine: replace it on the settings page before it expires.\n"
            "Allow the hosts the remote uses (for OneDrive, for example, the Microsoft Graph and login hosts).\n"
            "`agent-mirror box mount` needs FUSE, which cloud machines may not offer (UNVERIFIED); `probe --full`\n"
            "reports whether mounting looks possible.\n" + ON_DEMAND
        )
    return ""


def render(ctx, store, project, vendors):
    """{relative path under cloud/: text} for the chosen vendors."""
    env = store.load_env(project)
    box = env.get("box") or {}
    ref = store.tool_ref()
    tool_repo = store.tool_repo()
    if not SAFE_REF.match(ref):
        raise ValueError("store.json tool.ref %r has characters a setup script cannot hold safely" % ref)
    if not re.match(r"^https://[A-Za-z0-9.-]+/[A-Za-z0-9._~/-]+$", tool_repo):
        raise ValueError("store.json tool.repo %r must be a plain https URL" % tool_repo)
    python = str(env.get("python") or "").strip()
    if python and not SAFE_PYTHON.match(python):
        raise ValueError("python %r in env.json is not a plain version" % python)
    commands = env.get("setup_commands") or []
    if not isinstance(commands, list) or not all(isinstance(c, str) for c in commands):
        raise ValueError("setup_commands in env.json must be a list of shell lines")
    bad = companions.problems(env)
    if bad:
        raise ValueError("; ".join(bad) + " (in projects/%s/env.json)" % project)
    remote = store_remote(ctx, store)
    hp = repo_host_path(remote)
    if hp:
        host, path = hp
    else:
        warn(
            "the store has no usable origin remote (git remote add origin ...); "
            "the scripts use the placeholder <OWNER>/<STORE_REPO>"
        )
        host, path = "github.com", "<OWNER>/<STORE_REPO>"
    store_name = path.rsplit("/", 1)[-1]
    values = {
        "PROJECT": project,
        "PROJECT_REPO": str(env.get("repo") or "<owner>/<project repo>"),
        "STORE_REPO_PATH": "%s/%s" % (host, path),
        "STORE_NAME": store_name,
        "STORE_TOKEN_URL": "https://x-access-token:<STORE_REPO_TOKEN>@%s/%s.git" % (host, path),
        "STORE_ENV_TOKEN_URL": "https://x-access-token:${STORE_REPO_TOKEN}@%s/%s.git" % (host, path),
        "STORE_PLAIN_URL": "https://%s/%s.git" % (host, path),
        "TOOL_REPO": tool_repo,
        "TOOL_REF": ref,
        "PYTHON": python,
    }
    values["PRELUDE"] = template("_prelude.sh").rstrip("\n")
    values["TOOL_STEP"] = fill(template("_tool.sh"), values).rstrip("\n")
    values["PYTHON_STEP"] = (
        fill(template("_python.sh"), values).rstrip("\n") if python else "# no python version in the project config"
    )
    values["SETUP_COMMANDS_STEP"] = setup_commands_step(project, commands)
    values["COMPANIONS_STEP"] = companions.setup_step(project, env)
    found = companions.entries(env)
    values["COMPANIONS_LINE"] = (
        "- Clones %d public companion repo(s) from `companion_repos` into `~/companions/` without a token; "
        "`session-start` links each one next to the project clone as `../<dir>`." % len(found)
        if found
        else "- Clones no companion repos (env.json has no `companion_repos`)."
    )
    values["PYTHON_LINE"] = (
        "- Installs Python %s with uv (and uv itself when it is missing)." % python
        if python
        else "- Installs no Python version (the project config sets none)."
    )
    values["SETUP_LINE"] = (
        "- Runs the %d `setup_commands` from env.json, each allowed to fail." % len(commands)
        if commands
        else "- Runs no extra setup commands (env.json has no `setup_commands`)."
    )
    values["BOX_NOTE"] = box_note(box)
    values["INBOX_NOTE"] = inbox_note(store)
    hosts = hosts_for(env, host, box)
    info = {
        "project_repo": values["PROJECT_REPO"],
        "store_repo": values["STORE_REPO_PATH"],
        "store_name": store_name,
        "tool_ref": ref,
    }
    files, books = {}, {}
    if "claude" in vendors:
        scripts = {"setup.sh": fill(template("claude-setup.sh"), values)}
        book = runbook.build(project, "claude", env, scripts, hosts, info)
        books["claude"] = book
        files["claude/setup.sh"] = scripts["setup.sh"]
        files["claude/allowed-hosts.txt"] = "\n".join(hosts) + "\n"
        files["claude/env-vars.txt"] = "\n".join(env_names_for(env, "claude", box)) + "\n"
        files["claude/runbook.json"] = json.dumps(book, indent=2) + "\n"
        files["claude/README.md"] = fill(template("README-claude.md"), dict(values, STEPS=runbook.steps_markdown(book)))
    if "codex" in vendors:
        scripts = {
            "vm-install.sh": fill(template("codex-vm-install.sh"), values),
            "container-setup.sh": fill(template("codex-container-setup.sh"), values),
        }
        book = runbook.build(project, "codex", env, scripts, hosts, info)
        books["codex"] = book
        files["codex/vm-install.sh"] = scripts["vm-install.sh"]
        files["codex/vm-start-skill.md"] = fill(template("codex-vm-start-skill.md"), values)
        files["codex/container-setup.sh"] = scripts["container-setup.sh"]
        files["codex/container-maintenance.sh"] = fill(template("codex-container-maintenance.sh"), values)
        files["codex/allowed-hosts.txt"] = "\n".join(hosts) + "\n"
        files["codex/env-vars.txt"] = "\n".join(env_names_for(env, "codex", box)) + "\n"
        files["codex/runbook.json"] = json.dumps(book, indent=2) + "\n"
        files["codex/README.md"] = fill(template("README-codex.md"), dict(values, STEPS=runbook.steps_markdown(book)))
    return files, books


def inbox_note(store):
    mode = store.cloud_push()
    if mode == "main":
        return (
            "\n## Pushes from cloud sessions\n\n"
            'store.json has `"cloud_push": "main"`: `agent-mirror sync` in a cloud session pushes straight to the\n'
            'store\'s main branch. Consider `"inbox"`, so that a cloud session cannot change the instructions or\n'
            "runbooks that a local agent with real credentials will follow.\n"
        )
    return (
        "\n## Inbox (why the token can push, but never to the main branch)\n\n"
        'With `"cloud_push": "inbox"` in store.json (the default), `agent-mirror sync` in a cloud session pushes\n'
        "its commits to a branch `inbox/<runtime>-<machine id>` of the store, never to the main branch. The next\n"
        "`agent-mirror sync` on a developer machine applies the memory changes from those branches and holds\n"
        "everything else (instructions, skills, project settings) for `agent-mirror review`. A cloud session\n"
        "must not be able to change the instructions or runbooks that a local agent with real credentials will\n"
        "follow. The cost: memory written in one cloud session reaches other machines only after a developer\n"
        "machine syncs.\n"
    )


def project_env(ctx, project):
    store = ctx.require_store()
    if not os.path.isfile(store.env_path(project)):
        err(
            "no %s; make projects/%s/env.json first (copy projects/_example/env.json)"
            % (store.env_path(project), project)
        )
        return store, None
    env = store.load_env(project)
    probs = recipes.problems(env)
    if probs:
        for p in probs:
            err("%s (in projects/%s/env.json)" % (p, project))
        return store, None
    return store, env


def render_checked(ctx, store, project, vendors):
    files, books = render(ctx, store, project, vendors)
    env = store.load_env(project)
    for vendor in vendors:
        for w in recipes.coverage_warnings(env, vendor):
            warn("%s: %s" % (vendor, w))
        for step in books[vendor]["steps"]:
            for p in step.get("placeholders", []):
                if not p["secret"]:
                    warn(
                        "%s: %s holds the placeholder %s, but no ask-user recipe names it"
                        % (vendor, step["file"], p["placeholder"])
                    )
    return files, books


def cmd_cloud(args, ctx):
    action = getattr(args, "cloud_action", "render")
    if action == "secrets":
        return cmd_cloud_secrets(args, ctx)
    if action == "runbook":
        return cmd_cloud_runbook(args, ctx)
    project = args.project
    store, env = project_env(ctx, project)
    if env is None:
        return 2
    vendors = ["claude", "codex"] if args.vendor == "all" else [args.vendor]
    try:
        files, _ = render_checked(ctx, store, project, vendors)
    except ValueError as exc:
        err(str(exc))
        return 2
    root = store.path("projects", project, "cloud")
    if args.dry_run:
        out("dry run: nothing will change")
    for rel, text in sorted(files.items()):
        path = os.path.join(root, *rel.split("/"))
        if args.dry_run:
            out("would write %s (%d lines)" % (tilde(path), len(text.splitlines())))
            continue
        write_text(path, text)
        if rel.endswith(".sh"):
            try:
                os.chmod(path, 0o755)
            except OSError:
                pass
        out("wrote %s" % tilde(path))
    if not args.dry_run:
        out(
            "next: follow %s, then commit projects/%s/cloud in the store"
            % (tilde(os.path.join(root, vendors[0], "README.md")), project)
        )
    return 0


def cmd_cloud_runbook(args, ctx):
    store, env = project_env(ctx, args.project)
    if env is None:
        return 2
    vendors = ["claude", "codex"] if args.vendor == "all" else [args.vendor]
    try:
        _, books = render(ctx, store, args.project, vendors)
    except ValueError as exc:
        err(str(exc))
        return 2
    if args.json:
        data = books[vendors[0]] if len(vendors) == 1 else [books[v] for v in vendors]
        out(json.dumps(data, indent=2))
        return 0
    out("\n\n".join(runbook.steps_text(books[v]) for v in vendors))
    return 0


def cmd_cloud_secrets(args, ctx):
    store, env = project_env(ctx, args.project)
    if env is None:
        return 2
    vendor = args.vendor
    for w in recipes.coverage_warnings(env, vendor):
        warn(w)
    items = recipes.produce(env, vendor)
    ready = [it for it in items if it.status == "ready"]
    needs = [it for it in items if it.status == "needs"]
    asks = [it for it in items if it.status == "ask"]
    if args.check:
        out("values for %s (%s) as this machine can produce them now; values are never shown:" % (args.project, vendor))
        width = max([len(it.name) for it in items] + [10])
        for it in items:
            if it.status == "ready":
                state = "ready"
            elif it.status == "ask":
                where = (
                    ("replaces %s in the setup script" % it.placeholder) if it.where == "setup-script" else "env value"
                )
                state = "ask the user (%s)%s" % (where, ": " + it.instruction if it.instruction else "")
            else:
                state = "needs you: " + it.instruction
            out("  %-*s  %-15s %s" % (width, it.name, it.recipe, state))
        out("summary: %d ready, %d need you, %d for the user to create" % (len(ready), len(needs), len(asks)))
        return 3 if needs else 0
    path = expand(args.out)
    problem = placement_problem(path, store.root)
    if problem:
        err(problem)
        return 2
    pairs = [(it.name, it.value) for it in ready if not (it.recipe == "ask-user")]
    problem = write_secret_file(path, pairs, replace=args.replace)
    if problem:
        err(problem)
        return 2
    out("wrote %d value(s) to %s (%s); names and value lengths:" % (len(pairs), path, privacy_words(path)))
    for name, value in pairs:
        out("  %s (%d characters)" % (name, len(str(value))))
    for it in needs:
        out("still needs you: %s: %s" % (it.name, it.instruction))
    for it in asks:
        where = (
            ("replace %s in the setup script" % it.placeholder) if it.where == "setup-script" else "add as an env value"
        )
        out("for the user to create: %s (%s)%s" % (it.name, where, ": " + it.instruction if it.instruction else ""))
    out(
        "paste the values on the environment's settings page (agent-mirror clip copy %s --secret), then delete %s"
        % (path, path)
    )
    return 3 if needs else 0
