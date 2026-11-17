"""The runbook: a declarative description of one cloud environment, and its human text.

`cloud render` writes it as projects/<project>/cloud/<vendor>/runbook.json and generates the
README steps from the same data, so the two never drift apart. Step kinds come from a fixed set.
"""

import re

from agent_mirror import __version__
from agent_mirror.recipes import plan

SCHEMA = 1
STEP_KINDS = ("network-hosts", "env-values", "setup-script", "first-session", "cleanup")
PRODUCTS = {
    "claude": "Claude Code on the web (claude.ai/code)",
    "codex": "Codex cloud (newer VM product and legacy container product)",
}
PROBE_PROMPT = (
    "Run agent-mirror probe --full from the session folder. Reply with its complete output in one code "
    "block, unchanged. Do not do anything else."
)
PLACEHOLDER = re.compile(r"<[A-Z][A-Z0-9_]*>")


def placeholders_in(text):
    found = []
    for p in PLACEHOLDER.findall(text):
        if p not in found:
            found.append(p)
    return found


def build(project, vendor, env, scripts, hosts, info):
    """The runbook dict. scripts: {file name: rendered text}; info: project_repo, store_repo, store_name, tool_ref."""
    rows = plan(env, vendor)
    asks = dict((r["placeholder"], r) for r in rows if r["recipe"] == "ask-user" and r["where"] == "setup-script")
    env_rows = [r for r in rows if not (r["recipe"] == "ask-user" and r["where"] == "setup-script")]
    secrets_cmd = "agent-mirror cloud secrets --project %s --vendor %s" % (project, vendor)

    def script_step(name, title, text, product=None, extra=None):
        holders = []
        for p in placeholders_in(scripts.get(name, "")):
            r = asks.get(p)
            holders.append(
                {
                    "placeholder": p,
                    "secret": r["name"] if r else None,
                    "instructions": (r or {}).get("instructions") or "no ask-user recipe names this placeholder",
                }
            )
        step = {"kind": "setup-script", "title": title, "file": name, "placeholders": holders, "text": text}
        if product:
            step["product"] = product
        step.update(extra or {})
        return step

    steps = [
        {
            "kind": "network-hosts",
            "title": "Network: allowed hosts",
            "file": "allowed-hosts.txt",
            "hosts": list(hosts),
            "text": "Allow every host in allowed-hosts.txt on the environment's network settings.",
        },
        {
            "kind": "env-values",
            "title": "Environment variables",
            "file": "env-vars.txt",
            "variables": env_rows,
            "check_command": secrets_cmd + " --check",
            "values_command": secrets_cmd + " --out <FILE outside any repo>",
            "text": "Add every variable in env-vars.txt. Run the check command to see which values this machine "
            "can produce, then the values command to write them to an owner-only file; paste them on the "
            "settings page (agent-mirror clip copy FILE --secret puts them on the clipboard without "
            "printing them).",
        },
    ]
    if vendor == "claude":
        steps.append(
            script_step(
                "setup.sh",
                "Setup script",
                "Open the environment for %s, paste setup.sh into its setup script box, then replace each placeholder "
                "on the settings page (never in the store)." % info["project_repo"],
            )
        )
    else:
        steps.append(
            script_step(
                "vm-install.sh",
                "Setup script (newer VM product)",
                "Select both repos in the environment: %s and the store %s (Codex checks the store out with its own "
                "access). Paste vm-install.sh as the install script and add vm-start-skill.md as the start skill "
                "(UNVERIFIED how the product triggers a start skill). If the store checkout is not found by its folder "
                "name %s, set AGENT_MIRROR_STORE to it."
                % (info["project_repo"], info["store_repo"], info["store_name"]),
                product="vm",
                extra={"also": ["vm-start-skill.md"]},
            )
        )
        steps.append(
            script_step(
                "container-setup.sh",
                "Setup script (legacy container product)",
                "Select only %s. Store the token as a setup-only secret named STORE_REPO_TOKEN, paste container-setup.sh "
                "as the setup script and container-maintenance.sh as the maintenance script." % info["project_repo"],
                product="container",
                extra={"also": ["container-maintenance.sh"]},
            )
        )
    steps.append(
        {
            "kind": "first-session",
            "title": "First session",
            "prompt": PROBE_PROMPT,
            "text": "Start a session and send the prompt below; read the output against the README notes.",
        }
    )
    steps.append(
        {
            "kind": "cleanup",
            "title": "Clean up",
            "items": [
                "delete the values file written by `%s --out` once the values are pasted" % secrets_cmd,
                "clear the clipboard if you used it: agent-mirror clip clear",
                "revoke any token you created and no longer need",
            ],
            "text": "Remove local copies of secret values.",
        }
    )
    return {
        "schema": SCHEMA,
        "tool": "agent-mirror %s" % __version__,
        "project": project,
        "vendor": vendor,
        "product": PRODUCTS[vendor],
        "environment_name": project,
        "project_repo": info["project_repo"],
        "store_repo": info["store_repo"],
        "tool_ref": info["tool_ref"],
        "steps": steps,
    }


def steps_markdown(runbook):
    lines = ["## Steps", ""]
    for n, step in enumerate(runbook["steps"], 1):
        lines.append("%d. **%s.** %s" % (n, step["title"], step["text"]))
        kind = step["kind"]
        if kind == "network-hosts":
            lines.append("   Hosts (%d): %s" % (len(step["hosts"]), ", ".join("`%s`" % h for h in step["hosts"])))
        elif kind == "env-values":
            lines.append("   Check: `%s`. Values: `%s`." % (step["check_command"], step["values_command"]))
            for v in step["variables"]:
                note = " (automatic)" if v.get("automatic") else ""
                extra = ": %s" % v["instructions"] if v.get("instructions") else ""
                lines.append("   - `%s`: recipe `%s`%s%s" % (v["name"], v["recipe"], note, extra))
        elif kind == "setup-script":
            files = ", ".join("`%s`" % f for f in [step["file"]] + step.get("also", []))
            lines.append("   Files: %s." % files)
            for p in step["placeholders"]:
                lines.append(
                    "   - replace `%s` with %s: %s"
                    % (p["placeholder"], "`%s`" % p["secret"] if p["secret"] else "a value", p["instructions"])
                )
        elif kind == "first-session":
            lines.append('   Prompt: "%s"' % step["prompt"])
        elif kind == "cleanup":
            for item in step["items"]:
                lines.append("   - %s" % item)
    return "\n".join(lines)


def steps_text(runbook):
    """Plain text for `agent-mirror cloud runbook`."""
    md = steps_markdown(runbook)
    head = "%s: %s, project %s (store %s, agent-mirror ref %s)" % (
        runbook["vendor"],
        runbook["product"],
        runbook["project"],
        runbook["store_repo"],
        runbook["tool_ref"],
    )
    return head + "\n\n" + md.replace("**", "").replace("## Steps\n\n", "")
