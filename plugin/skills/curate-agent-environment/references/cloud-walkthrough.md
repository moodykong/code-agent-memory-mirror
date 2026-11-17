# Cloud environments: render, walk through, probe

Vendor pages and limits change. Before this phase, look up the vendor's current documentation and correct anything below that no longer holds. Mark what you could not confirm, and let the probe settle it.

## What usually differs from a local machine

Check each against the current documentation:

- Only committed files of the cloned branch are present. Ignored files, local settings and other branches are not.
- User-level instructions, personal skills, plugins, user-scope MCP servers, hooks and memory do not carry over.
- The machine is new for each session. Only a cached setup snapshot and what is pushed or uploaded survives.
- Interactive sign-in is impossible. Every credential must work unattended.
- Outbound traffic may go through a web proxy with a host allowlist. Non-web protocols (many databases) may not get through at all.
- A setup script may run before environment variables, secrets or the vendor's git access exist. It then needs its own narrow token to clone a private store.
- Secret features that inject a header for matching hosts cannot carry a cloud service principal, a key file or a database connection string. Those end up as plain variables the agent can read.
- A vendor may have more than one cloud product with different rules. Find out which one the account gets.

## Render

```
agent-mirror cloud render --project <name> --vendor all
```

This writes, under the store's `projects/<name>/cloud/`: the setup script, the allowed host list, the variable name list and a step list. Read them before handing them to the user. The store token placeholder stays a placeholder in the store; the user replaces it only on the vendor page.

The store token should be limited to the store repo alone, with the smallest permission that allows clone and push.

## Walk the user through

One field at a time. For each: say which box, put the text on the clipboard, read the clipboard back (first line and line count, or names and lengths for secrets), then let the user paste.

1. Create the store token.
2. Create the environment and set network access and the host list.
3. Paste the variable values from the owner-only file.
4. Paste the setup script, then replace the token placeholder in the clone line.
5. Save.

Ask the user to describe what the page shows when it does not match. You have documentation, not a view of their account.

Leave out credentials that the probe does not need yet. A reachability test for a database needs its host on the allowlist, not its login.

## Probe

First session prompt:

```
Run agent-mirror probe --full from the repository root. Reply with its complete output in one code block, unchanged. Do not do anything else.
```

Read the output for:

| Question | Where it shows |
|---|---|
| Did the store clone, and are instructions, skills and memory linked? | Setup note, doctor section |
| Does the agent honour settings written by the setup script? | Whether the session-start lines ran |
| Does the agent's built-in memory run here, and through the link? | Memory section |
| Are all expected variables present? | Variable names section |
| Do web hosts and raw TCP hosts answer? | Network section |
| Do the cloud identity's allowed and refused actions behave? | Box and custom command sections |
| Did setup finish inside the caching limit, and how long does the dependency install take? | Setup note, timing section |

Each failure has a usual fix:

- **Store not cloned:** token scope, or the host is not reachable at setup time.
- **Hook not honoured:** start sessions with an explicit first instruction to run `agent-mirror session-start`, or add one neutral line to the project's own tracked settings that runs a user script if it exists. That line names no personal store, but it exists only on branches that have it. Ask the user before touching a project repo.
- **Memory not linked in time:** the agent reads the index path that `session-start` prints.
- **Database unreachable:** it stays local, or goes through a private network link if the vendor offers one, or the user reconsiders a self-hosted environment. This is the user's decision; bring the actual error.
- **Setup too slow to cache:** move heavy steps to session start, or split them.

After the probe passes, add the credentials you left out, one system at a time, and repeat the relevant checks.

## Testing with a second machine or account

The store and the program are independent of the vendor account. A user can curate the environment on one machine and probe it from another machine or another account that has access to the project repo. The cloud environment itself lives on the vendor account that runs the session, so the settings must be entered on that account.
