---
name: curate-agent-environment
description: Set up or extend a user's agent context so Claude Code and Codex behave the same on every machine and in vendor-hosted cloud sessions. Use when the user wants cloud sessions to "work like local", wants instructions, skills or memory shared across machines or agents, wants untracked project files or credentials available to cloud agents, asks to "set up the cloud environment", "mirror my agent memory", "create a context store", or wants an existing setup checked, repaired or extended to another project.
---

# Curate an agent environment

A local agent works well because of things that are not in the project's git repo: user instructions, personal skills, memory, installed tools, credentials, and untracked files. A fresh machine or a cloud session has none of them. This skill walks the user through making them portable, using the `agent-mirror` program that ships with this plugin.

Three parts, kept separate:

| Part | What it is | Who owns it |
|---|---|---|
| Store | A private git repo with instructions, personal skills, memory and per-project config. Data only. | The user chooses the repo and account |
| Program | `agent-mirror`: links, syncs, moves bulk files, renders cloud settings, probes | This plugin |
| Box | Optional external storage for bulk untracked files (inbound and outbound) | The user chooses the backend |

Project repos are never changed and never refer to the store.

## How to work

1. **Measure before asking.** Run the inventory first. Ask the user only what the machine cannot tell you.
2. **Ask only the user's calls.** A question is worth asking when the answer changes what gets built and cannot be found on the machine. Every question comes with a short plain-language briefing (the problem, how serious, what each option means) and a recommendation. At most four questions per round.
3. **Recommend for the long term**, and say so when the cheap option has a later cost.
4. **Verify vendor behaviour, do not remember it.** Cloud products change. Look up the current vendor documentation for limits, network rules, secrets and settings pages, and treat anything undocumented as unknown until a probe on a real cloud machine shows it.
5. **Stop for the user's word** before anything that creates a credential, changes a setting on a shared or production system, uploads data, publishes anything, or replaces files in the user's home folders. Show a dry run first where one exists.
6. **Never print a secret.** Move secret values by script from file to file or to the clipboard. Show names and lengths only. This includes values found while scanning.
7. **Record the state** in the store's memory when pausing, so any agent can resume.

## Workflow

Do the phases in order. Skip a phase only when the user says it is out of scope. `references/decisions.md` lists every decision with its options and what to measure first. `references/safety-checks.md` lists checks that must not be skipped. `references/cloud-walkthrough.md` covers the vendor pages and the probe.

### Phase 0: Orient

- `agent-mirror detect` and `agent-mirror doctor`.
- Is there already a store (config file, or a repo the user names)? If so, this is a repair or an extension: read its `store.json`, `AGENTS.md` and `projects/` before proposing anything.
- Which machine is this, and which other machines and cloud products does the user want covered?

### Phase 1: Inventory (read-only)

Collect facts and show the user a short summary table. Do not change anything.

- **User level:** instruction files, personal skills and where they really live, per-project memory folders and their sizes, plugins, MCP servers and hooks that exist only on this machine.
- **Project level:** what is tracked, what is ignored, and the size of every ignored top-level folder. Sort ignored content into four kinds: rebuildable, re-downloadable from a registry or cloud, unique work, secrets.
- **Credentials:** where the code gets them (names of variables and files, never values), which cloud accounts and databases are used, and whether sign-in is interactive.
- **The user's own permissions** on those accounts, checked read-only. They decide whether the user can do each step or needs an administrator.
- **Hardcoded credentials** in tracked files and in anything that might be uploaded (file names and counts only).
- **Vendor facts**, looked up now: what a cloud session copies from the repo, what it never has, how secrets and network access work, machine size, caching.

### Phase 2: Decisions

Ask in rounds, most structural first. Typical order: where sessions run, which agents, where the store lives, what cloud sessions may reach, how each identity is limited, where bulk files go and what counts as a pointer, how wide the first rollout is. Record every answer in the project config or the store, not only in the conversation.

### Phase 3: Store and local install

- `agent-mirror init-store <path>` for a new store, or `--adopt` for an existing folder.
- Copy content in, scan it for secrets, and make the first commit and push only on the user's word.
- `agent-mirror install --dry-run ...`, show what would be linked and backed up, then install on the user's word. Immediately before, make sure the store copy of memory is current.
- Prove it: write one memory through the agent's normal path and confirm it lands in the store.
- `agent-mirror hooks install` and `agent-mirror timer install`, then prove the write hook fires.

### Phase 4: Identities and reach

For each system the user allowed:

- Create a dedicated non-personal identity. Never reuse the user's own login.
- Give it the smallest role that does the job, then work out what that role can do **indirectly** (see the safety checks) and tell the user.
- Check that mistakes are recoverable before granting write access.
- Create it with a script that writes the secret to an owner-only file outside every repo and never prints it.
- Spot-check as the new identity: one allowed action works, one forbidden action is refused.
- Write the approval rules into the store's `AGENTS.md`. Say plainly when a rule is the only guard.

### Phase 5: The box (bulk files)

- Choose the backend and write the `box` block in the project config.
- Registered assets become pointers with a checksum or size check. Rebuildable content is never stored. Secrets are never synced.
- Scan the upload set for credential-shaped strings and tell the user what was found.
- Show the exact folders, sizes and file counts. Upload only on the user's word, as a detached process that survives the session, then `agent-mirror box index` and commit the index.

### Phase 6: Cloud environments

- `agent-mirror cloud render --project <name>` writes the paste-ready setup script, host list and variable names into the store.
- Walk the user through the vendor page one field at a time. Put long text on the clipboard for them and confirm what the clipboard holds before they paste.
- First session: `agent-mirror probe --full`. Read the output against the checklist, fix what fails, repeat.

### Phase 7: Acceptance and handoff

- Spot checks on every target the user asked for: instructions and memory visible, a memory written in one place appears in another, the project's tests run, each allowed system answers, a forbidden action is refused.
- Write a status memory: what is done and verified, what is unverified, open items, expiry dates of secrets, and the exact next step.

## Never

- Put a store reference, a personal path or a secret into a project repo.
- Let links point out of the store, or nest the store inside a project.
- Sync `.env` files, key files or agent credential files anywhere.
- Treat an instruction as a technical limit. If a credential can do something, say so, even when a rule forbids it.
- Start an upload, create a credential or change a cloud setting on the strength of an earlier, more general "go ahead".
- State vendor limits or page layouts from memory as fact.
