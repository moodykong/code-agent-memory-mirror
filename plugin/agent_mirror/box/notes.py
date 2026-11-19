"""Short notes that travel with the box's files: what they are, why they were made, what to do next.

A later session sees names and sizes in the box, not purpose. Notes are plain text kept next to the
records that already exist, never in a new file or a git record:

- a session's notes are in its manifest (sessions.py): `box send --note TEXT` sets the session's `note`
  (the latest one given) and adds `{"at", "note", "paths"}` to its `notes` list (the newest NOTES_CAP;
  consecutive sends with the same text share one entry). A cloud runtime refuses a send without a note.
- a shared folder's note is in its entry in <shared>/_owners.json (`note`, `note_updated`): set by
  `box note FOLDER TEXT` on the owning machine. `box push` keeps it; `box index` copies the entry into the
  committed index, where `box ls` and `box owners` read it offline.
- a cloud `session-start` prints, per project, the newest sessions with their notes and the shared
  folders that have notes (start_block): one listing and one batched manifest read, as `box sessions`.
- a session's `skipped` list (since 0.5.6; `box skip PATH... --reason TEXT`, unsent.py) says which paths it
  left behind on purpose and why; the reason follows the text rules of a note.

A note is one line: control characters are dropped, whitespace is collapsed, at most NOTE_MAX characters,
and text that looks like a secret is refused (the pattern names are printed, never the text).
"""

import argparse
import contextlib
import datetime
import io
import json
import os
import shlex
import shutil
import time

from agent_mirror.runtime import CLOUD_RUNTIMES
from agent_mirror.util import err, out, scrub, short, warn, write_text

NOTE_MAX = 500
SHOW = 200  # characters of a note shown in a list
NOTES_CAP = 20  # entries of a session's notes list
PATHS_CAP = 50  # paths of one notes entry
START_SESSIONS = 5  # sessions shown at a cloud session start
START_TIMEOUT = 20
NOTE_KEYS = ("note", "note_updated")  # in a folder's _owners.json entry
NO_NOTE = '--note "what these files are and why"'


# the text ---------------------------------------------------------------------------------------------


def secret_hits(text):
    """Names of the secret patterns found in text (the ones sync's scan uses), never the text."""
    from agent_mirror.sync import SECRET_PATTERNS
    from agent_mirror.util import secret_values

    hits = [name for name, rx in SECRET_PATTERNS if rx.search(text)]
    if any(v in text for v in secret_values()):
        hits.append("the value of an environment variable whose name looks secret")
    return hits


def clean_note(text, what="note"):
    """(note or None, problem or None). One line: control characters dropped, whitespace collapsed.

    what names the text in the messages ("note", or "reason" for `box skip`)."""
    if text is None:
        return None, None
    kept = "".join(c if (c.isprintable() or c.isspace()) else " " for c in str(text))
    note = " ".join(kept.split())
    if not note:
        return None, None
    if len(note) > NOTE_MAX:
        return None, (
            "a %s is at most %d characters (this one has %d once spaces are collapsed); keep it to one to "
            "three sentences" % (what, NOTE_MAX, len(note))
        )
    hits = secret_hits(note)
    if hits:
        return None, (
            "refusing the %s: it looks like it holds a secret (%s); nothing was changed. Describe the files "
            "without keys, tokens or passwords" % (what, ", ".join(hits))
        )
    return note, None


def shown(note, limit=SHOW):
    note = " ".join(str(note or "").split())
    return note if len(note) <= limit else note[: limit - 3].rstrip() + "..."


def missing_note(runtime):
    return (
        "refusing box send on %s without a note: pass %s (one to three sentences: what the files are, why they "
        "were made, what to do next; later sessions read it); nothing was uploaded" % (runtime, NO_NOTE)
    )


# a session's notes (the manifest) ----------------------------------------------------------------------


def add_note(doc, note, paths, at):
    """Record one send's note in a manifest dict: the session's latest note and its notes list."""
    if not note:
        return
    doc["note"] = note
    notes = [n for n in doc.get("notes") or [] if isinstance(n, dict) and n.get("note")]
    if notes and notes[-1].get("note") == note:
        last = dict(notes[-1])
        merged = [p for p in last.get("paths") or [] if isinstance(p, str)]
        for p in paths:
            if p in merged:
                merged.remove(p)
            merged.append(p)
        last["paths"] = merged[-PATHS_CAP:]
        last["at"] = at
        notes[-1] = last
    else:
        notes.append({"at": at, "note": note, "paths": list(paths)[-PATHS_CAP:]})
    doc["notes"] = notes[-NOTES_CAP:]


def add_skip(doc, entry):
    """Record one `box skip` in a manifest dict: its optional `skipped` list of {"at", "reason", "paths"} (the
    newest NOTES_CAP entries, each with at most PATHS_CAP paths and the count of the others in `more`)."""
    if not entry or not entry.get("reason"):
        return
    paths = [p for p in entry.get("paths") or [] if isinstance(p, str)]
    kept = {"at": entry.get("at"), "reason": entry["reason"], "paths": paths[:PATHS_CAP]}
    if len(paths) > PATHS_CAP:
        kept["more"] = len(paths) - PATHS_CAP
    found = [e for e in doc.get("skipped") or [] if isinstance(e, dict) and e.get("reason")]
    doc["skipped"] = (found + [kept])[-NOTES_CAP:]


def manifest_skips(manifest):
    """[(at, reason, paths)] of a manifest's `skipped` list, oldest first (paths counts the ones left out too)."""
    found = []
    for e in (manifest or {}).get("skipped") or []:
        if isinstance(e, dict) and isinstance(e.get("reason"), str) and e["reason"].strip():
            paths = [p for p in e.get("paths") or [] if isinstance(p, str)]
            more = e.get("more") if isinstance(e.get("more"), int) and e.get("more") > 0 else 0
            found.append((e.get("at"), e["reason"], paths + ["..."] * more))
    return found


def skipped_summary(count, reason):
    """The short line `box sessions` prints under a session that left paths behind."""
    return "(%d path%s left behind: %s)" % (count, "" if count == 1 else "s", shown(reason, 120))


def skip_lines(manifest):
    """Lines that show what a session declared disposable, for box fetch --session (none when nothing)."""
    lines = []
    for at, reason, paths in manifest_skips(manifest):
        named = [p for p in paths if p != "..."]
        where = ", ".join(named[:3]) + (" and %d more" % (len(paths) - 3) if len(paths) > 3 else "")
        lines.append("left behind (%s): %s; reason: %s" % (at or "?", where or "?", shown(reason, 300)))
    return lines


def manifest_notes(manifest):
    """[(at, paths, note)] of a manifest, oldest first (a session note without a list counts as one)."""
    m = manifest or {}
    found = []
    for n in m.get("notes") or []:
        if isinstance(n, dict) and isinstance(n.get("note"), str) and n["note"].strip():
            paths = [p for p in n.get("paths") or [] if isinstance(p, str)]
            found.append((n.get("at"), paths, n["note"]))
    if not found and isinstance(m.get("note"), str) and m["note"].strip():
        found.append((m.get("updated"), [], m["note"]))
    return found


def note_lines(manifest):
    """Lines that show every note of a session, for box fetch --session."""
    lines = []
    for at, paths, note in manifest_notes(manifest):
        where = ", ".join(paths[:3]) + (" and %d more" % (len(paths) - 3) if len(paths) > 3 else "")
        lines.append("note (%s%s): %s" % (at or "?", ", on " + where if where else "", shown(note, 600)))
    if not lines:
        lines.append("note: none (this session was sent without one)")
    return lines


# a shared folder's note (_owners.json) ---------------------------------------------------------------------


def folder_notes(folders):
    """{folder: note} from an owners map."""
    found = {}
    for name, entry in (folders or {}).items():
        note = entry.get("note") if isinstance(entry, dict) else None
        if isinstance(note, str) and note.strip():
            found[name] = note
    return found


def index_folder_notes(index_path):
    """{folder: note} from the owners line of a committed box index (offline)."""
    from agent_mirror.box.owners import from_index

    folders, _ = from_index(index_path)
    return folder_notes(folders)


def patch_index(index_path, name, entry):
    """Put one folder's owners entry into the owners line of the local box index, so `box ls` and
    `box owners` show its note offline. True when the index has an owners line and now holds the entry."""
    if not os.path.isfile(index_path) or not entry:
        return False
    with open(index_path, encoding="utf-8") as fh:
        lines = fh.read().splitlines()
    for i, line in enumerate(lines):
        try:
            r = json.loads(line)
        except ValueError:
            continue
        if isinstance(r, dict) and isinstance(r.get("owners"), dict):
            folders = dict(r["owners"])
            folders[name] = entry
            r["owners"] = dict(sorted(folders.items()))
            lines[i] = json.dumps(r)
            write_text(index_path, "\n".join(lines) + "\n")
            return True
    return False


def note_args_problem(args, runtime):
    """(exit code, message) when `box note` cannot run as given, else None. Checked before anything is read."""
    if runtime in CLOUD_RUNTIMES:
        return 2, (
            "refusing box note on %s: only the folder's owning machine (a developer machine) sets folder notes"
            % runtime
        )
    if args.clear and args.text:
        return 2, "box note takes the note text or --clear, not both"
    if not args.clear and not args.text:
        return 2, "give the note text (one line on what the folder holds), or --clear to remove it"
    if not args.clear:
        note, problem = clean_note(args.text)
        if problem:
            return 2, problem
        if not note:
            return 2, "the note is empty; to remove a note, pass --clear"
        args.text = note
    return None


def cmd_note(args, s, runtime, backend, plat):
    """box note FOLDER TEXT|--clear: the owning machine sets or clears a shared folder's note."""
    from agent_mirror import machine
    from agent_mirror.box import owners, units
    from agent_mirror.box.rules import clean_rel
    from agent_mirror.box.sides import side_for
    from agent_mirror.config import load_user_config

    name = str(args.folder or "").strip().strip("/")
    if name != units.TOP and (not name or clean_rel(name) != name):
        err("box note takes a folder of the box as `box owners` names it, without '..' (got %r)" % args.folder)
        return 2
    side = side_for(s, "shared")
    if not backend.ready(args.dry_run):
        return 1
    mid, _ = machine.identity(load_user_config(plat))
    state, problem = owners.read_state(side, s, args.timeout, args.transfer_timeout)
    if problem:
        err(problem)
        return 1
    cur = state.owner(name)
    if cur is None:
        if name in units.Layout(s).by_name:
            err(
                "%s has no owner in %s yet; only its owning machine sets its note. Push it from here first "
                "(`agent-mirror box push --only %s` claims it), then set the note" % (name, owners.where(side), name)
            )
        else:
            err("%s is not a folder of this box; `agent-mirror box owners` lists them" % name)
        return 2
    if not mid or cur.get("machine_id") != mid:
        err("%s is owned by %s, not this machine; only its owner sets its note" % (name, owners.describe_owner(cur)))
        return 2
    text = None if args.clear else args.text
    if (cur.get("note") or None) == text:
        out("nothing to change: %s %s" % (name, "has no note" if text is None else "already has this note"))
        return 0
    if args.dry_run:
        out("dry run: nothing will change")
        out(
            "would %s the note of %s in %s%s"
            % ("clear" if text is None else "set", name, owners.where(side), "" if text is None else ": " + text)
        )
        return 0
    ok, problem, applied, conflicts = owners.update(
        side,
        s,
        mid,
        {name: ("note", text, "note")},
        args.timeout,
        convert=bool(state.legacy),
        legacy_base=state if state.legacy else None,
        list_timeout=args.transfer_timeout,
    )
    for folder, owner in conflicts:
        err(
            "%s: note not recorded: %s while this ran"
            % (folder, "it now belongs to %s" % owners.describe_owner(owner) if owner else "it lost its owner")
        )
    if not ok:
        err(problem)
        return 1
    if conflicts:
        return 1
    entry = applied.get(name)
    out("%s the note of %s in %s" % ("cleared" if text is None else "set", name, owners.where(side)))
    if entry and patch_index(s["index_path"], name, entry):
        out(
            "also updated the owners line of the index %s (commit it, as after `agent-mirror box index`, so cloud "
            "sessions and other machines see the note)" % s["index_path"]
        )
    else:
        out(
            "the index has no owners line yet; run `agent-mirror box index` and commit it so other machines "
            "see the note"
        )
    return 0


# at a cloud session start ------------------------------------------------------------------------------------


def ago(stamp, now=None):
    from agent_mirror.box.sessions import parse_utc

    when = parse_utc(stamp)
    if when is None:
        return "?"
    now = now or datetime.datetime.now(datetime.timezone.utc)
    secs = max(0, int((now - when).total_seconds()))
    if secs < 3600:
        return "%d min ago" % (secs // 60)
    if secs < 2 * 86400:
        return "%d h ago" % (secs // 3600)
    return "%d d ago" % (secs // 86400)


def session_line(r, now=None):
    files = int(r.get("files") or 0)
    head = "%s (%s)" % (r["label"], r["id"]) if r.get("label") else r["id"]
    note = "note: " + shown(r.get("note")) if r.get("note") else "no note"
    return "  %s, %s, %d file%s; %s" % (
        head,
        ago(r.get("updated") or r.get("started"), now),
        files,
        "" if files == 1 else "s",
        note,
    )


def quiet_context(ctx, project, path):
    """The box settings of a project, or None; whatever box_context prints is kept out of the session."""
    from agent_mirror.box.common import box_context

    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            s, _ = box_context(argparse.Namespace(project=project, project_path=path, env_file=None), ctx)
    except SystemExit:
        return None
    return s


def live_rows(s, runtime, timeout):
    """(rows of the newest sessions, total, None) from the box, or (None, None, problem). All calls share one
    deadline; nothing they print reaches the session."""
    from agent_mirror.box import azure
    from agent_mirror.box.sessions import View, summary_of
    from agent_mirror.box.sides import side_for

    deadline = time.time() + timeout

    def left():
        return max(1, int(deadline - time.time()))

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
        if s["backend"] == "azure-blob" and not azure.sign_in(runtime, left(), deadline=deadline):
            return None, None, "az could not sign in"
        if time.time() >= deadline:
            return None, None, "out of time after the sign-in"
        view = View(side_for(s, "sessions"), left())
        if view.listing() is None:
            return None, None, "the listing failed or took longer than %d s" % timeout
        names, _ = view.names()
        view.timeout = left()
        view.read(names[:START_SESSIONS])
        return [summary_of(view, n) for n in names[:START_SESSIONS]], len(names), None


def start_block(ctx, store, project, path, runtime, timeout):
    """(lines to print, step status) for one project's box at a cloud session start. Never raises."""
    from agent_mirror.bootstrap import sandboxed_away
    from agent_mirror.box.sessions import TOOL_FOR_BACKEND, from_index

    box = store.load_env(project).get("box") or {}
    backend = str(box.get("backend") or "none").strip().lower()
    if backend not in TOOL_FOR_BACKEND:
        return [], "skipped (no box)"
    s = quiet_context(ctx, project, path)
    if s is None:
        return [], "skipped (box not configured)"
    tool = TOOL_FOR_BACKEND[backend]
    found = shutil.which(tool)
    offline = None
    if not found:
        offline = "%s is not installed here" % tool
    elif sandboxed_away(ctx.plat, found):
        offline = "test sandbox: %s is outside it" % tool  # never a real transfer program in a test run
    fetch = "agent-mirror box fetch --session ID --project %s" % shlex.quote(project)
    lines = []
    if offline:
        line = from_index(s["index_path"])
        rows = [dict(r) for r in (line or {}).get("sessions") or [] if isinstance(r, dict) and r.get("id")]
        total = len(rows) + int((line or {}).get("more") or 0)
        rows = rows[:START_SESSIONS]
        status = "skipped (%s)" % offline
        source = "from the committed index as of the last `box index` (%s)" % offline
    else:
        rows, total, problem = live_rows(s, runtime, timeout)
        if problem:
            lines.append(
                "box of %s: could not list its sessions (%s); see them later with `agent-mirror box sessions "
                "--project %s`" % (project, problem, shlex.quote(project))
            )
            status = "failed (%s)" % short(problem, 60)
        else:
            status = "ok"
        source = "from the box"
    if rows:
        if total > len(rows):
            count = "the newest %d of %d sessions" % (len(rows), total)
        else:
            count = "%d session%s" % (len(rows), "" if len(rows) == 1 else "s")
        lines.append("box of %s: %s, %s (fetch one with `%s`):" % (project, count, source, fetch))
        now = datetime.datetime.now(datetime.timezone.utc)
        lines += [session_line(r, now) for r in rows]
    elif rows is not None:
        where = "in the committed index (%s)" % offline if offline else "in the box yet"
        lines.append("box of %s: no sessions %s" % (project, where))
    noted = index_folder_notes(s["index_path"])
    if noted:
        lines.append(
            "box of %s: shared folders with notes, from the committed index (`agent-mirror box ls` shows them too):"
            % project
        )
        lines += ["  %s: %s" % (name, shown(text)) for name, text in sorted(noted.items())]
    return [scrub(x) for x in lines], status


def session_start_notes(ctx, store, matched, runtime, args, log):
    """Cloud session start: print each project's newest sessions and folder notes. Never fails the session."""
    timeout = int(getattr(args, "box_timeout", START_TIMEOUT) or 0)
    for project, path in matched:
        with log.step("box notes (%s)" % project) as st:
            if timeout <= 0:
                st.status = "skipped (--box-timeout 0)"
                continue
            if args.dry_run:
                out("would list the newest sessions and folder notes of the box of %s" % project)
                st.status = "skipped (dry run)"
                continue
            try:
                lines, st.status = start_block(ctx, store, project, path, runtime, timeout)
            except (Exception, SystemExit) as exc:  # noqa: BLE001 (never fail the session)
                lines, st.status = [], "failed (%s)" % short(exc, 60)
                warn(
                    "box of %s: could not list its sessions and notes (%s); the session goes on"
                    % (project, short(exc, 120))
                )
            for line in lines:
                out(line)
