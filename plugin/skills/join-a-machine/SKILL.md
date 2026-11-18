---
name: join-a-machine
description: Connect this machine to the user's existing agent store, item by item, without losing what the machine already has. Use when the user says "set up agent-mirror on this machine", "link this computer to my store", "install the mirror here", "share my memory and skills with this machine", or when `agent-mirror install` reports units waiting for a choice. Handles a machine that already has its own instructions, skills, memory or bulk files, as well as an empty one. For designing a new store or a cloud environment, use curate-agent-environment; for entering a curated cloud environment on a vendor page, use set-up-cloud-environment.
---

# Join a machine to the store

A machine and the store each either have content for an item or do not. The program compares them item by item and names the case. Your job is to show the user the few items that need a decision, recommend, and apply only what they chose.

| This machine | Store | Case | What happens |
|---|---|---|---|
| Has content | No record | first-setup | Adopt into the store, then link. Publishes content, so the user confirms. |
| Empty | Has content | join | Linked from the store. No question. |
| Has content | Has content, different | reconcile | The user chooses. |
| Has content | Identical content | identical | Linked silently. |
| Empty | No record | nothing | Nothing to do, except for a project's memory: the empty folder is linked so the first memory written here lands in the store. |
| Already linked | | linked | Nothing, unless the link has drifted. |

Items are: the instruction file for each agent, each skill, each project's memory, and each project's bulk-file box. One machine is usually in several cases at once.

## Rules

1. **Plan before anything.** `agent-mirror plan` is read-only. Never run `apply` or `install` on a machine that has content before the user has seen the plan.
2. **Ask only about `reconcile` and `first-setup` items**, and group them. Do not walk the user through items that need no decision; give counts.
3. **Recommend each time**, in plain words, with what the user gains and loses.
4. **Nothing is replaced without a backup**, and say how to undo (`agent-mirror uninstall`).
5. **Adopting publishes.** Content adopted from this machine goes into the store and from there to every other machine and cloud session. Say so when the machine and the store belong to different worlds, for example a work machine and a personal store.
6. **Applying never moves bulk files.** Choosing a box role only records it. Uploading is a separate step the user starts.
7. **Never print secrets**, and stop if the secret scan blocks an adoption: tell the user which file, not what it contains.

## Steps

### 1. See where the machine stands

```
agent-mirror doctor
agent-mirror plan --project <name> --project-path <path>     (repeat --project for each repo on this machine)
```

Use `--json` when you need the details. If there is no store on this machine yet, clone it and run `agent-mirror init-store <path> --adopt` first.

If the plan says the machine is fresh, run `agent-mirror install` and skip to step 5.

### 2. Summarise

One short table for the user: how many items in each case, then the items that need them. For each of those, say what the machine has and what the store has (count, size, last change), not file dumps.

### 3. Decide, in rounds

Typical recommendations:

| Item and case | Recommend | Because |
|---|---|---|
| Skill only on this machine | Adopt if the user wants it everywhere; keep local if it only makes sense here | A skill tied to software on one machine is noise elsewhere |
| Store skill not on this machine | Link, unless it is irrelevant here; then skip | Skipped skills stay available to link later |
| Skill on both, different | Look at which is newer and ask; if unsure, keep the machine copy unlinked and decide later | Taking the store's replaces local edits |
| Instructions on both, different | Merge by hand into the store's file with the user, then take the store's | One instruction file should rule; silent replacement loses rules |
| Memory on both | Merge | Nothing is overwritten; a file changed on both sides is kept twice for the user to resolve |
| Memory only here | Adopt, after the secret scan | This is the first setup of that project |
| Box, machine has the files, box empty | Writer, then choose the folders | See step 4 |
| Box, box has files, machine has none | Reader | Fetch on demand |
| Box, both have files | Writer, if this machine has folders of its own to add. Run `agent-mirror box owners` and go through only the folders that exist here with different content from their owner's: for each, the owner keeps it, or this machine takes it over | Each folder has one owning machine. Folders owned elsewhere are skipped on push, so two machines can both be writers without overwriting each other |

Write the choices to a small JSON file outside every repo: `{"<unit id>": "<action>"}`.

### 4. Bulk files, when this machine will be a writer

```
agent-mirror box candidates --project <name> --project-path <path>
```

Show the candidates with sizes. Recommend: unique work goes in; rebuildable folders, secrets and worktrees stay out; copies of registered assets become pointers; folders with very many small files go in as archives. Remind the user if the content includes other people's material that the destination's terms matter. Write the chosen folders into the project config in the store (`box.include`, `box.archives`), and record this machine as the writer through the choices file. Do not upload yet.

### 5. Apply

```
agent-mirror apply --choices <file> --dry-run
agent-mirror apply --choices <file>
```

Show the dry run first. After applying:

- `agent-mirror doctor` should show the links as correct and list anything still waiting.
- Prove memory works: have the agent write one memory through its normal path and confirm the file appears in the store.
- Offer `agent-mirror hooks install` and `agent-mirror timer install`, and say what each does: pull at session start, push after a memory write, push at session end, and a periodic sync.
- `agent-mirror sync` publishes the adopted content and the machine profile.

### 6. Bulk upload, only on the user's word

Show the folders, sizes and file counts once more. Then `agent-mirror box push`, started so that it survives the session, followed by `agent-mirror box index` and a commit of the index in the store.

### 7. Record

Write a short memory: which machine joined, which items were adopted, kept local or skipped, the machine's role for each box, and anything left waiting.

## If something looks wrong

- A link reported as drifted: show where it points and ask before repairing.
- An item the user does not recognise: leave it alone.
- The program fails on this platform: stop, say what failed, and do not link by hand. A half-linked machine is worse than an unlinked one.
