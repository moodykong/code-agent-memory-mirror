# Decision catalogue

Each decision lists what to measure first, the usual options, and what to recommend when nothing else decides it. Ask only the ones that apply. Bring a recommendation every time.

## 1. Where cloud sessions run

- **Measure:** does the project need a GPU, a database that speaks a non-web protocol, more disk than the vendor machine has, or data that may not leave the user's network? Look up the vendor's current machine size and network model.
- **Options:** vendor-hosted only; self-hosted environment on the user's own infrastructure; hosted first with self-hosted later for the parts that do not fit.
- **Default:** hosted first. It needs nothing to maintain. Name the specific things that will not work there.

## 2. Which agents and which targets

- **Options:** one agent or several; local only, cloud only, or both.
- **Default:** design for every agent the user actually runs, and test all targets in the pilot. A second vendor means any credential and any data a session reads is exposed to that vendor too. Say this once, clearly.

## 3. Where the store lives

- **Measure:** does memory hold employer detail (system names, identities, incident notes)?
- **Options:** private repo in a personal account; private repo in an organisation; one store or one per employer.
- **Default:** one private store per person. If it holds employer detail, say that a personal account places that detail outside the employer's control, then follow the user's choice.

## 4. Where project environment files live

- **Options:** all in the store; tracked in the project repo; split.
- **Default:** all in the store. A cloud session clones one branch, so anything tracked in the project exists only on branches that contain it, and a teammate should not see references to a personal store. Dependencies still follow the branch, because the session reads the branch's own lock file.

## 5. Memory across agents

- **Options:** one shared store that every agent reads and writes; shared but read-only for some agents; separate stores.
- **Default:** one shared store. An agent without a compatible built-in memory follows written instructions to read the index and add entries in the same format.

## 6. What cloud sessions may reach

Ask as a multiple choice over the systems found in the inventory. For each selected system, ask or decide the level:

| Level | Meaning |
|---|---|
| Read | List and read only |
| Operate | Start work that costs money or changes non-production state |
| Change production | Deployments, endpoints, production data |

- **Default:** read, plus operate where the user explicitly wants it. Production changes stay local unless the user asks, and then they need an approval rule.

## 7. How a risky right is guarded

- **Options:** a technical limit (a narrower role, or a separate environment that holds the stronger credential so that choosing it is the authorisation); a written approval rule; both.
- **Default:** a technical limit when one exists. If the user prefers a written rule, write it into the store's `AGENTS.md` and state that it is the only guard.

## 8. One credential set or one per vendor

- **Default:** one per vendor, so logs tell agents apart and a leak means revoking one set. One shared set is acceptable when the user prefers less to rotate.

## 9. Who creates identities

- **Measure:** the user's effective permissions, read-only. Often the user can do more than they expect.
- **Output:** a runnable script for what the user can do, and a short exact request for an administrator for the rest.

## 10. Where bulk untracked files go (the box)

- **Measure:** total size and file count of unique work; whether it is employer data; which storage the planned identity can already reach.
- **Options:** object storage in the employer's cloud account; a consumer drive through rclone (which account); none.
- **Default:** object storage that the cloud identity can already read with a narrow role. A consumer drive needs a sign-in token that usually opens the whole drive, and handles many small files slowly. If the user has no such storage, rclone to a drive is a fine start.
- **Always two sides:** an inbound side that developer machines write and agents read, and an outbound side that agents write and the developer pulls. On the inbound side every folder has one owning machine; a push skips folders owned elsewhere, so several machines can contribute without an old copy overwriting newer work. Cloud sessions never write the inbound side.

## 11. What goes into the box

| Kind | Fate |
|---|---|
| Rebuildable (environments, caches, indexes, worktrees) | Never stored |
| Copy of a registered, versioned asset | Pointer with a size or checksum check |
| Unique work | Mirrored |
| Secrets | Never synced |
| Unclear | Ask the user |

- **Default scope:** all unique work in the main checkout. Worktrees only when the user asks.

## 12. Sync cadence

- **Default:** automatic. Pull at session start; push right after a memory write, at session end, and from a timer on the developer machine. Bulk files on a slower cadence or by hand.

## 13. Rollout

- **Default:** pilot on one project, then copy the project folder pattern to the others. The user-level part needs no change for the next project.
