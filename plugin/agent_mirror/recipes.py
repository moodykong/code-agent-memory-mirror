"""Secret recipes: how this machine produces each environment value a cloud environment needs.

The store says WHERE a value comes from, never the value. Only these recipe types
exist; anything else is rejected, and no free-form command from the store is ever run:

  rclone-remote   RCLONE_CONFIG_<NAME>_* from this machine's rclone config
  local-file      NAME=value lines from an owner-only file on this machine
  env             the current process environment
  literal         a non-secret constant (refused when it looks like a secret)
  azure-keyvault  `az keyvault secret show ... --query value -o tsv`
  ask-user        something only the user can create (a token, say)
"""

import os
import shutil

from agent_mirror.envfile import VALID_NAME, owner_only, parse_env_text, privacy_instruction
from agent_mirror.util import expand, run, short

RECIPE_TYPES = ("rclone-remote", "local-file", "env", "literal", "azure-keyvault", "ask-user")
STORE_TOKEN = "STORE_REPO_TOKEN"
STORE_TOKEN_PLACEHOLDER = "<STORE_REPO_TOKEN>"


class Item:
    """One environment value: its name, the recipe that covers it and whether it can be produced now."""

    def __init__(
        self, name, recipe, status="needs", value=None, instruction="", where="env", placeholder=None, automatic=False
    ):
        self.name = name
        self.recipe = recipe
        self.status = status  # ready, needs, ask
        self.value = value
        self.instruction = instruction
        self.where = where
        self.placeholder = placeholder
        self.automatic = automatic

    def describe(self):
        return {
            "name": self.name,
            "recipe": self.recipe,
            "where": self.where,
            "placeholder": self.placeholder,
            "automatic": self.automatic,
            "instructions": self.instruction or None,
        }


def secret_hit(text):
    from agent_mirror.sync import SECRET_PATTERNS

    return [name for name, rx in SECRET_PATTERNS if rx.search(str(text))]


def entries(env):
    value = env.get("secrets") or []
    return value if isinstance(value, list) else []


def problems(env):
    """Reasons the secrets list is not valid (empty when it is)."""
    found = []
    raw = env.get("secrets")
    if raw is None:
        return found
    if not isinstance(raw, list):
        return ["secrets must be a list of recipe entries"]
    for i, e in enumerate(raw):
        where = "secrets[%d]" % i
        if not isinstance(e, dict):
            found.append("%s must be an object" % where)
            continue
        kind = e.get("recipe")
        if kind not in RECIPE_TYPES:
            found.append(
                "%s: recipe %r is not one of %s (no other kind is ever run)" % (where, kind, ", ".join(RECIPE_TYPES))
            )
            continue
        names = []
        if kind == "rclone-remote":
            remote = e.get("remote")
            if not isinstance(remote, str) or not remote or not VALID_NAME.match(remote):
                found.append('%s: rclone-remote needs "remote": letters, digits and _ only' % where)
        elif kind in ("local-file", "env"):
            names = e.get("names")
            if not isinstance(names, list) or not names:
                found.append('%s: %s needs a non-empty "names" list' % (where, kind))
                names = []
            if kind == "local-file" and not isinstance(e.get("path"), str):
                found.append('%s: local-file needs "path"' % where)
        elif kind == "literal":
            names = [e.get("name")]
            if not isinstance(e.get("value"), str):
                found.append('%s: literal needs a text "value"' % where)
            elif secret_hit(e["value"]):
                found.append(
                    "%s: the literal value of %s looks like a secret (%s); use another recipe"
                    % (where, e.get("name"), ", ".join(secret_hit(e["value"])))
                )
        elif kind == "azure-keyvault":
            vault, mapping = e.get("vault"), e.get("secrets")
            if not isinstance(vault, str) or not vault:
                found.append('%s: azure-keyvault needs "vault"' % where)
            if not isinstance(mapping, dict) or not mapping:
                found.append('%s: azure-keyvault needs a "secrets" map of VARIABLE to secret name' % where)
                mapping = {}
            names = list(mapping)
        elif kind == "ask-user":
            names = [e.get("name")]
            if e.get("where", "env") not in ("env", "setup-script"):
                found.append('%s: ask-user "where" must be env or setup-script' % where)
            if e.get("where") == "setup-script" and not str(e.get("placeholder") or "").startswith("<"):
                found.append('%s: ask-user with where setup-script needs a "placeholder" like <NAME>' % where)
        for n in names:
            if not isinstance(n, str) or not VALID_NAME.match(n):
                found.append("%s: %r is not a valid variable name" % (where, n))
    return found


def automatic_entries(vendor):
    """Entries every environment of this vendor needs, whatever the config says."""
    if vendor == "claude":
        return [
            {
                "recipe": "ask-user",
                "name": STORE_TOKEN,
                "where": "setup-script",
                "placeholder": STORE_TOKEN_PLACEHOLDER,
                "instructions": "a fine-grained token that can READ ONLY the store repo (Contents: read-only). "
                "It is used once, to clone the store when the environment is built; inside a session the store "
                "is reached through the vendor's GitHub access for the repos you select, so select the store too",
            }
        ]
    if vendor == "codex":
        return [
            {"recipe": "literal", "name": "AGENT_RUNTIME", "value": "codex-cloud"},
            {
                "recipe": "ask-user",
                "name": STORE_TOKEN,
                "where": "env",
                "instructions": "legacy container product only: a setup-only secret named STORE_REPO_TOKEN holding "
                "a fine-grained token for the store repo (Contents: read-only)",
            },
        ]
    return []


def all_entries(env, vendor):
    configured = entries(env)
    names = set()
    for e in configured:
        if isinstance(e, dict):
            names.update(entry_names(e))
    extra = [e for e in automatic_entries(vendor) if not set(entry_names(e)) & names]
    if vendor == "claude":
        configured = [e for e in configured if not (isinstance(e, dict) and "AGENT_RUNTIME" in entry_names(e))]
    return configured + extra, set(id(e) for e in extra)


def entry_names(e):
    kind = e.get("recipe")
    if kind in ("local-file", "env"):
        return [n for n in e.get("names") or [] if isinstance(n, str)]
    if kind in ("literal", "ask-user"):
        return [e.get("name")] if isinstance(e.get("name"), str) else []
    if kind == "azure-keyvault":
        return list(e.get("secrets") or {})
    return []


def rclone_prefix(e):
    return "RCLONE_CONFIG_%s_" % str(e.get("remote") or "").upper()


def covering_entry(name, entry_list):
    for e in entry_list:
        if not isinstance(e, dict):
            continue
        if name in entry_names(e):
            return e
        if e.get("recipe") == "rclone-remote" and name.startswith(rclone_prefix(e)):
            return e
    return None


def wanted_names(env, vendor):
    """Variable names the environment needs: env_var_names plus the box sign-in names (as cloud render lists them)."""
    from agent_mirror.cloud import env_names_for

    return [line.split("=", 1)[0] for line in env_names_for(env, vendor, env.get("box") or {})]


def coverage(env, vendor="claude"):
    """(names with no recipe, recipe names that are not in env_var_names) for one vendor."""
    entry_list, _ = all_entries(env, vendor)
    wanted = wanted_names(env, vendor)
    uncovered = [n for n in wanted if covering_entry(n, entry_list) is None]
    extra = []
    for e in entry_list:
        if not isinstance(e, dict):
            continue
        if e.get("recipe") == "ask-user" and e.get("where") == "setup-script":
            continue
        if e.get("recipe") == "rclone-remote":
            if not any(n.startswith(rclone_prefix(e)) for n in wanted):
                extra.append(rclone_prefix(e) + "*")
            continue
        for n in entry_names(e):
            if n not in wanted and n != STORE_TOKEN:
                extra.append(n)
    return uncovered, extra


def coverage_warnings(env, vendor="claude"):
    uncovered, extra = coverage(env, vendor)
    found = []
    if uncovered:
        found.append('no recipe in "secrets" covers %s' % ", ".join(uncovered))
    if extra:
        found.append("recipes name %s, which env_var_names does not list" % ", ".join(extra))
    return found


# producing values ---------------------------------------------------------------------------


def produce(env, vendor, runner=run):
    """[Item] for every value the environment needs, produced on this machine where possible."""
    from agent_mirror.box.extras import rclone_remote_values

    entry_list, automatic = all_entries(env, vendor)
    wanted = wanted_names(env, vendor)
    items = []
    seen = set()

    def add(item):
        if item.name not in seen:
            seen.add(item.name)
            items.append(item)

    for e in entry_list:
        if not isinstance(e, dict):
            continue
        kind = e.get("recipe")
        auto = id(e) in automatic
        if kind == "rclone-remote":
            remote = e.get("remote")
            lines, _, problem = rclone_remote_values(remote)
            if problem:
                add(Item(rclone_prefix(e) + "*", kind, "needs", instruction=problem))
                continue
            produced = dict(lines)
            for n in [n for n in wanted if n.startswith(rclone_prefix(e))]:
                if n not in produced:
                    add(
                        Item(
                            n,
                            kind,
                            "needs",
                            instruction="the rclone remote %s on this machine has no %s setting"
                            % (remote, n[len(rclone_prefix(e)) :].lower()),
                        )
                    )
            for n, v in lines:
                add(Item(n, kind, "ready", v))
        elif kind == "local-file":
            path = expand(str(e.get("path")))
            values = None
            if not os.path.isfile(path):
                where = "inside your user profile" if os.name == "nt" else "owner-only"
                problem = "create %s (%s, NAME=value lines) holding %%s" % (path, where)
            elif not owner_only(path):
                problem = privacy_instruction(path).replace("%", "%%") + "; it holds %s"
            else:
                problem = None
                with open(path, encoding="utf-8") as fh:
                    values = parse_env_text(fh.read())
            for n in entry_names(e):
                if values is not None and n in values:
                    add(Item(n, kind, "ready", values[n]))
                elif values is not None:
                    add(Item(n, kind, "needs", instruction="add a line %s=... to %s" % (n, path)))
                else:
                    add(Item(n, kind, "needs", instruction=problem % n))
        elif kind == "env":
            for n in entry_names(e):
                if os.environ.get(n):
                    add(Item(n, kind, "ready", os.environ[n]))
                else:
                    add(Item(n, kind, "needs", instruction="set %s in this shell's environment, then run again" % n))
        elif kind == "literal":
            add(Item(e.get("name"), kind, "ready", e.get("value"), automatic=auto))
        elif kind == "azure-keyvault":
            vault = e.get("vault")
            for n, secret_name in (e.get("secrets") or {}).items():
                if not shutil.which("az"):
                    add(Item(n, kind, "needs", instruction="install the Azure CLI (az) and sign in, then run again"))
                    continue
                cmd = [
                    "az",
                    "keyvault",
                    "secret",
                    "show",
                    "--vault-name",
                    vault,
                    "--name",
                    str(secret_name),
                    "--query",
                    "value",
                    "-o",
                    "tsv",
                ]
                rc, stdout, stderr = runner(cmd, timeout=60)
                value = (stdout or "").rstrip("\r\n")
                if rc == 0 and value:
                    add(Item(n, kind, "ready", value))
                else:
                    add(
                        Item(
                            n,
                            kind,
                            "needs",
                            instruction="could not read secret %s from vault %s (sign in with "
                            "`az login` and check access): %s"
                            % (secret_name, vault, short(stderr or "rc %s" % rc, 160)),
                        )
                    )
        elif kind == "ask-user":
            add(
                Item(
                    e.get("name"),
                    kind,
                    "ask",
                    instruction=e.get("instructions") or "",
                    where=e.get("where", "env"),
                    placeholder=e.get("placeholder"),
                    automatic=auto,
                )
            )
    for n in wanted:
        if n not in seen and covering_entry(n, entry_list) is None:
            add(Item(n, "none", "needs", instruction='no recipe covers %s; add one to "secrets" in env.json' % n))
    return items


def plan(env, vendor):
    """Like produce, but without reading anything: [{name, recipe, where, placeholder, automatic, instructions}]."""
    entry_list, automatic = all_entries(env, vendor)
    rows, seen = [], set()
    for e in entry_list:
        if not isinstance(e, dict):
            continue
        kind = e.get("recipe")
        names = entry_names(e)
        if kind == "rclone-remote":
            names = [n for n in wanted_names(env, vendor) if n.startswith(rclone_prefix(e))] or [rclone_prefix(e) + "*"]
        for n in names:
            if n in seen:
                continue
            seen.add(n)
            rows.append(
                Item(
                    n,
                    kind,
                    "plan",
                    where=e.get("where", "env") if kind == "ask-user" else "env",
                    placeholder=e.get("placeholder"),
                    instruction=e.get("instructions") or "",
                    automatic=id(e) in automatic,
                ).describe()
            )
    for n in wanted_names(env, vendor):
        if n not in seen:
            seen.add(n)
            rows.append(Item(n, "none", "plan", instruction="no recipe covers it").describe())
    return rows
