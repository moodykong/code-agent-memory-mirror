"""User configuration, store discovery, store.json and project settings.

Store resolution order: the --store flag, then the AGENT_MIRROR_STORE
environment variable, then store_path in the user config file
(Linux/macOS: ${XDG_CONFIG_HOME:-~/.config}/agent-mirror/config.json,
Windows: %APPDATA%\\agent-mirror\\config.json).
"""

import json
import os

from agent_mirror import DEFAULT_REF, STORE_SCHEMA, TOOL_REPO
from agent_mirror.platforms import Platform
from agent_mirror.runtime import AGENT_NAMES
from agent_mirror.util import err, expand, norm, tilde, warn, within, write_text

NO_STORE_HELP = (
    "no store configured. Make one with `agent-mirror init-store PATH` "
    "(or `agent-mirror init-store PATH --adopt` for an existing store folder), "
    "or pass --store PATH, or set AGENT_MIRROR_STORE"
)


def load_json_file(path, what="file"):
    """Parsed JSON, {} if the file is missing. Exits with a clear message on bad JSON."""
    if not os.path.isfile(path):
        return {}
    with open(path, encoding="utf-8") as fh:
        try:
            data = json.load(fh)
        except ValueError as exc:
            raise SystemExit("error: %s %s is not valid JSON: %s" % (what, path, exc))
    if not isinstance(data, dict):
        raise SystemExit("error: %s %s must hold a JSON object" % (what, path))
    return data


def dump_json(data):
    return json.dumps(data, indent=2) + "\n"


# user config ------------------------------------------------------------------


def load_user_config(plat):
    return load_json_file(plat.config_file(), "config file")


def save_user_config(plat, data):
    write_text(plat.config_file(), dump_json(data))


def config_projects(cfg):
    """[(name, [paths])] from the config's projects map (a path or a list of paths per name)."""
    found = []
    for name, value in sorted((cfg.get("projects") or {}).items()):
        paths = value if isinstance(value, list) else [value]
        found.append((name, [str(p) for p in paths if p]))
    return found


def remember_project(plat, name, path):
    """Add path to the config's projects map. Returns True if the file changed."""
    cfg = load_user_config(plat)
    projects = cfg.setdefault("projects", {})
    value = projects.get(name)
    paths = value if isinstance(value, list) else ([value] if value else [])
    if any(norm(expand(p)) == norm(path) for p in paths):
        return False
    paths.append(path)
    projects[name] = paths[0] if len(paths) == 1 else paths
    save_user_config(plat, cfg)
    return True


def config_agents(cfg):
    agents = [a for a in (cfg.get("agents") or []) if a in AGENT_NAMES]
    return agents or list(AGENT_NAMES)


# store ----------------------------------------------------------------------------


def resolve_store_path(flag, plat):
    """(path, where it came from) or (None, None)."""
    if flag:
        return expand(flag), "--store"
    value = (plat.env.get("AGENT_MIRROR_STORE") or "").strip()
    if value:
        return expand(value), "AGENT_MIRROR_STORE"
    cfg = load_user_config(plat)
    value = str(cfg.get("store_path") or "").strip()
    if value:
        return expand(value), "config " + tilde(plat.config_file())
    return None, None


class Store:
    """A store folder: AGENTS.md, skills/, memory/<project>/, projects/<project>/env.json, store.json."""

    def __init__(self, root, source=None):
        self.root = os.path.realpath(root)
        self.source = source

    def path(self, *parts):
        return os.path.join(self.root, *parts)

    def meta(self):
        return load_json_file(self.path("store.json"), "store file")

    def schema_problem(self):
        meta = self.meta()
        if not meta:
            return None
        schema = meta.get("schema")
        if isinstance(schema, int) and schema > STORE_SCHEMA:
            return (
                "store.json schema %d is newer than this agent-mirror understands (%d); "
                "update agent-mirror before relying on it" % (schema, STORE_SCHEMA)
            )
        return None

    def tool_repo(self):
        return str((self.meta().get("tool") or {}).get("repo") or TOOL_REPO)

    def cloud_push(self):
        """Where cloud sessions push: 'inbox' (the default, also when store.json does not say) or 'main'."""
        value = str(self.meta().get("cloud_push") or "inbox").strip().lower()
        return value if value in ("inbox", "main") else "inbox"

    def tool_ref(self):
        return str((self.meta().get("tool") or {}).get("ref") or DEFAULT_REF)

    def project_names(self):
        d = self.path("projects")
        if not os.path.isdir(d):
            return []
        return sorted(
            n for n in os.listdir(d) if not n.startswith(("_", ".")) and os.path.isfile(os.path.join(d, n, "env.json"))
        )

    def env_path(self, project):
        return self.path("projects", project, "env.json")

    def load_env(self, project):
        if not project:
            return {}
        return load_json_file(self.env_path(project), "project config")

    def memory_project(self, project):
        return self.load_env(project).get("memory_project") or project

    def memory_store(self, project):
        return self.path("memory", self.memory_project(project))

    def skill_names(self):
        d = self.path("skills")
        if not os.path.isdir(d):
            return []
        names = []
        for n in sorted(os.listdir(d)):
            p = os.path.join(d, n)
            if n.startswith(".") or n == "__pycache__":
                continue
            if os.path.isdir(p) or os.path.islink(p):
                names.append(n)
        return names


class Context:
    """What every command needs: the platform, the user config and (lazily) the store."""

    def __init__(self, store_flag=None, plat=None):
        self.plat = plat or Platform()
        self.store_flag = store_flag
        self._config = None
        self._store = None
        self._resolved = False

    @property
    def config(self):
        if self._config is None:
            self._config = load_user_config(self.plat)
        return self._config

    def reload_config(self):
        self._config = None

    def store_or_none(self, quiet=False):
        if not self._resolved:
            self._resolved = True
            path, source = resolve_store_path(self.store_flag, self.plat)
            if path and os.path.isdir(path):
                self._store = Store(path, source)
                problem = self._store.schema_problem()
                if problem and not quiet:
                    warn(problem)
            elif path and not quiet:
                err("store folder %s (from %s) does not exist; run `agent-mirror init-store`" % (path, source))
        return self._store

    def require_store(self):
        store = self.store_or_none()
        if store is None:
            path, _ = resolve_store_path(self.store_flag, self.plat)
            if not path:
                err(NO_STORE_HELP)
            raise SystemExit(2)
        return store

    def store_source_for_commands(self):
        """'flag' or 'env' when the store must be named explicitly in commands run later, else None."""
        _, source = resolve_store_path(self.store_flag, self.plat)
        if source == "--store":
            return "flag"
        if source == "AGENT_MIRROR_STORE":
            return "env"
        return None


# projects --------------------------------------------------------------------------


def load_env_file(path):
    return load_json_file(path, "project config")


def match_config_project(cfg, path):
    for name, paths in config_projects(cfg):
        for p in paths:
            if within(path, os.path.realpath(expand(p))):
                return name
    return None


def resolve_project(ctx, name=None, path=None):
    """(project, path) for commands that work on one project. See projects.py for the discovery rules."""
    from agent_mirror.projects import single_project

    return single_project(ctx, name, path)
