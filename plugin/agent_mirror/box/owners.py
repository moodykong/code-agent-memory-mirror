"""Who pushes which folder: <shared>/_owners.json, one owning machine per folder unit.

    {"schema": 1,
     "folders": {"<folder>": {"machine_id": "...", "machine_name": "...", "since": "...",
                              "fingerprint": "...", "updated": "...",
                              "note": "...", "note_updated": "..."}}}

note and note_updated (0.5.3, optional) are the folder's one-line note, set by its owning machine with
`box note FOLDER TEXT` (notes.py). A push or take-over keeps every key of an entry it does not set itself,
so the note stays with the folder; a release removes the entry and its note.

A folder is a folder unit (units.py): an archived folder or an include folder. `box push` claims a
folder nobody owns (recorded after its upload succeeded), uploads the folders this machine owns, and
skips folders another machine owns, saying whether the local copy is the same as the owner's.
`--take-over FOLDER` moves one folder to this machine; `box release FOLDER` gives one up.

The fingerprint is the archive fingerprint: sha256 over every carried file's relative path, size and
modification time (whole seconds). Two copies count as the same only when all three match.

The remote has no locking. Every change reads the file again right before writing, changes only this
machine's entries, writes, reads it back and checks them; on a mismatch it tries again, then fails.

Older versions kept one writer per box in <shared>/_writer.json. When only that marker exists, its
machine owns every folder currently in the box. The first _owners.json written for such a box carries
those folders too (otherwise they would look unowned); _writer.json itself is never deleted.

Since 0.5.6 a push also remembers, in the clone (<project>/.agent-mirror/pushed.json, git-excluded), the
fingerprint and time of each folder it uploaded, and `box release` forgets a released folder there. A
developer session start uses it with the owners line of the committed index, offline, to say which folders
this machine owns that changed since its last push (session_start_push_check).
"""

import datetime
import json
import os
import shlex
import shutil
import time

from agent_mirror import __version__, machine
from agent_mirror.box import archive, units
from agent_mirror.box.local import STATE_DIR, ensure_git_exclude
from agent_mirror.box.notes import NOTE_KEYS, shown
from agent_mirror.box.sides import side_for
from agent_mirror.config import load_user_config
from agent_mirror.platforms import current
from agent_mirror.runtime import CLOUD_RUNTIMES
from agent_mirror.util import err, out, scrub, short, warn, write_text

OWNERS = "_owners.json"
LEGACY = "_writer.json"
MARKERS = (OWNERS, LEGACY)
SCHEMA = 1
TRIES = 3
TAKE_ALL = "\0all"  # the value of a bare --take-over
TOOL_FOR_BACKEND = {"azure-blob": "az", "rclone": "rclone"}
ENTRY_KEYS = ("machine_id", "machine_name", "since", "fingerprint", "updated")
PUSHED = "pushed.json"  # in <project>/.agent-mirror/: what this machine last uploaded of each folder
PUSH_CHECK_TIMEOUT = 5.0


def utc_now():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def where(side, rel=OWNERS):
    return scrub(side.where(rel))


def read_json(side, rel, timeout):
    """(dict or None, problem or None). A missing file is (None, None)."""
    tmp = current().mkdtemp("agent-mirror-owners-")
    try:
        path = os.path.join(tmp, "f.json")
        got = side.get(rel, path, timeout)
        if got == "missing":
            return None, None
        if got != "ok":
            return None, got
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            return None, "%s is not valid JSON" % where(side, rel)
        return (data if isinstance(data, dict) else None), None
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def put_json(side, rel, data, timeout):
    tmp = current().mkdtemp("agent-mirror-owners-")
    try:
        path = os.path.join(tmp, os.path.basename(rel))
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=1, sort_keys=True)
            fh.write("\n")
        return side.put(path, rel, timeout)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def clean_folders(data):
    """The folders map of an _owners.json document: well-formed entries, kept as they are (keys this
    version does not know included, so a write never drops what another machine recorded)."""
    folders = data.get("folders") if isinstance(data, dict) else None
    found = {}
    for name, entry in (folders or {}).items():
        if isinstance(name, str) and isinstance(entry, dict) and entry.get("machine_id"):
            found[name] = dict(entry)
    return found


def document(folders):
    return {"schema": SCHEMA, "folders": dict(sorted(folders.items()))}


def legacy_entry(marker, fingerprint=None):
    return {
        "machine_id": marker.get("machine_id"),
        "machine_name": marker.get("machine_name"),
        "since": marker.get("date"),
        "fingerprint": fingerprint,
        "updated": marker.get("date"),
    }


def describe_owner(entry):
    return "%s since %s" % (entry.get("machine_name") or str(entry.get("machine_id") or "?")[:12], entry.get("since") or "?")


class State:
    """What the box says about ownership: from _owners.json, or from the old _writer.json marker."""

    def __init__(self, folders, exists, legacy=None):
        self.folders = folders
        self.exists = exists  # _owners.json is there
        self.legacy = legacy  # the _writer.json marker, when _owners.json is not there

    def owner(self, name):
        return self.folders.get(name)


def box_folders(side, s, timeout, list_timeout=None):
    """({folder: fingerprint or None} for every folder currently in the box, problem or None).

    Archived folders come from their sidecars, include folders from the files listed under them.
    Only needed for a box that still has the old single-writer marker.
    """
    listed = side.list("", list_timeout or timeout)
    if listed is None:
        return None, "cannot list %s" % where(side, "")
    layout_folders = units.folder_units(s.get("include") or [], s.get("archives") or [])
    found = {}
    candidates = []
    for it in listed:
        rel = it.get("path") or ""
        if rel in MARKERS:
            continue
        if rel.startswith(archive.ARCHIVE_DIR + "/"):
            name = archive.sidecar_name(rel)
            if name:
                candidates.append(name)
            continue
        unit = units.unit_of_file(rel, layout_folders, s.get("archives") or [], s.get("exclude") or [])
        if unit and unit not in found:
            found[unit] = None
    for name in sorted(set(candidates)):
        sc = archive.parse_json(side.cat(archive.sidecar_rel(name), timeout))
        if sc and sc.get("name") == name:
            found[name] = sc.get("fingerprint")
    return found, None


def read_both(side, timeout):
    """(owners data, marker data, problem): one transfer call on rclone, one or two on azure-blob."""
    if side.backend != "rclone":
        data, problem = read_json(side, OWNERS, timeout)
        if problem or data is not None:
            return data, None, problem
        marker, problem = read_json(side, LEGACY, timeout)
        return None, marker, problem
    tmp = current().mkdtemp("agent-mirror-owners-")
    try:
        found, problem = side.get_present("", list(MARKERS), tmp, timeout)
        if problem:
            return None, None, problem
        result = []
        for rel in MARKERS:
            data = None
            if rel in found:
                try:
                    with open(os.path.join(tmp, rel), encoding="utf-8") as fh:
                        data = json.load(fh)
                except (OSError, ValueError):
                    return None, None, "%s is not valid JSON" % where(side, rel)
            result.append(data if isinstance(data, dict) else None)
        return result[0], result[1], None
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def read_state(side, s, timeout, list_timeout=None):
    """(State, problem or None). list_timeout bounds the whole-box listing an old marker needs."""
    data, marker, problem = read_both(side, timeout)
    if problem:
        return None, "cannot read %s: %s" % (where(side), problem)
    if problem:
        return None, "cannot read %s: %s" % (where(side), problem)
    if data is not None:
        return State(clean_folders(data), True), None
    if not marker or not marker.get("machine_id"):
        return State({}, False), None
    in_box, problem = box_folders(side, s, timeout, list_timeout)
    if problem:
        return None, problem
    return State(dict((n, legacy_entry(marker, fp)) for n, fp in in_box.items()), False, marker), None


def fresh_state(side, s, timeout, legacy_base=None, list_timeout=None):
    """The state right before a write. While the box has no _owners.json, the folders of the old marker
    are the ones found before this command uploaded anything (legacy_base), not a new listing."""
    if legacy_base is None:
        return read_state(side, s, timeout, list_timeout)
    data, problem = read_json(side, OWNERS, timeout)
    if problem:
        return None, "cannot read %s: %s" % (where(side), problem)
    if data is not None:
        return State(clean_folders(data), True), None
    return State(dict(legacy_base.folders), False, legacy_base.legacy), None


def update(side, s, mid, changes, timeout, convert=False, legacy_base=None, list_timeout=None):
    """Apply this machine's changes to _owners.json without losing anyone else's.

    changes: {folder: ("set", entry, mode)} with mode "claim", "update" or "take-over",
    {folder: ("remove", None, "release")}, or {folder: ("note", text or None, "note")}. A claim, update
    or note is not applied when the folder now belongs to another machine (it is returned as a conflict,
    with None as the owner when nobody owns it now); a take-over always is. A set keeps the keys of the
    current entry it does not set itself (the folder's note, and keys a newer version wrote).
    legacy_base: the State read before this command changed anything, when it came from _writer.json.
    Returns (ok, problem or None, applied {folder: entry or None}, conflicts [(folder, owner entry)]).
    """
    problem = None
    applied, conflicts = {}, []
    for attempt in range(TRIES):
        state, problem = fresh_state(side, s, timeout, legacy_base, list_timeout)
        if problem:
            return False, problem, {}, []
        folders = dict(state.folders)
        applied, conflicts = {}, []
        for name, (op, entry, mode) in sorted(changes.items()):
            cur = folders.get(name)
            mine = bool(cur and cur.get("machine_id") == mid)
            if op == "remove":
                if mine:
                    del folders[name]
                    applied[name] = None
                elif cur:
                    conflicts.append((name, cur))
                continue
            if op == "note":
                if not mine:
                    conflicts.append((name, cur))
                    continue
                new = dict(cur)
                for key in NOTE_KEYS:
                    new.pop(key, None)
                if entry:
                    new.update({"note": entry, "note_updated": utc_now()})
                if (cur.get("note") or None) != (entry or None):
                    folders[name] = new
                    applied[name] = new
                continue
            if mode == "take-over" or cur is None or mine:
                new = dict(entry)
                if mine and cur.get("since"):
                    new["since"] = cur["since"]  # already this machine's: it owns it since then
                for key, value in (cur or {}).items():
                    new.setdefault(key, value)  # the note, and anything a newer version recorded
                if cur != new:
                    folders[name] = new
                    applied[name] = new
            else:
                conflicts.append((name, cur))
        if not applied and not (convert and not state.exists):
            return True, None, applied, conflicts
        ok, detail = put_json(side, OWNERS, document(folders), timeout)
        if not ok:
            return False, "cannot write %s: %s" % (where(side), detail), {}, conflicts
        back, problem = read_json(side, OWNERS, timeout)
        got = clean_folders(back or {})
        if not problem and all(
            (got.get(n) is None or got[n].get("machine_id") != mid) if e is None else got.get(n) == e
            for n, e in applied.items()
        ):
            return True, None, applied, conflicts
        if attempt + 1 < TRIES:
            warn("%s changed while this machine wrote it (another machine wrote at the same time); trying again" % where(side))
    return (
        False,
        "could not record this machine's folders in %s after %d tries: another machine keeps writing it. "
        "The uploads themselves finished; run the same command again in a minute" % (where(side), TRIES),
        applied,
        conflicts,
    )


def report_conflicts(conflicts, verb):
    for name, cur in conflicts:
        err(
            "%s: not %s: it now belongs to %s (another machine changed %s while this ran). "
            "If both machines uploaded it, the box holds whichever upload finished last; "
            "see `agent-mirror box owners`, then push from its owner or take it over" % (name, verb, describe_owner(cur), OWNERS)
        )


# box push ------------------------------------------------------------------------------------


def role_refusal(project, role):
    err(
        "refusing box push for %s: this machine's recorded role is %s (a %s never pushes). "
        'To push from here, run `agent-mirror apply --choices FILE` with {"box:%s": "writer"}'
        % (project, role, role, project)
    )
    return 2


def push(args, s, runtime, backend, plat):
    """box push with folder ownership. backend is the backend module (azure or rclone)."""
    if runtime in CLOUD_RUNTIMES:
        err("refusing box push on %s: only a developer machine writes the shared side" % runtime)
        return 2
    project_path = s["project_path"]
    if not os.path.isdir(project_path):
        err("project folder %s does not exist" % project_path)
        return 1
    cfg = load_user_config(plat)
    role = machine.box_role(cfg, s["project"])
    if role in ("reader", "none"):
        return role_refusal(s["project"], role)
    if args.dry_run:
        out("dry run: nothing will change")
    layout = units.Layout(s)
    for w in layout.warnings:
        warn(w)
    take = list(getattr(args, "take_over", None) or [])
    take_all = TAKE_ALL in take
    take_names = [t.strip().strip("/") for t in take if t != TAKE_ALL]
    only = [o.strip().strip("/") for o in (getattr(args, "only", None) or [])]
    for name in take_names + only:
        if name not in layout.by_name:
            err(
                "%s is not one of the box folders on this machine; folders here: %s"
                % (name, ", ".join(u["name"] for u in layout.units) or "none (check box.include and box.archives)")
            )
            return 2
    local = []
    for u in layout.units:
        summary = layout.summary(u)
        if summary["files"]:
            local.append((u, summary))
    here = dict((u["name"], sm) for u, sm in local)
    for name in take_names:
        if name not in here:
            err("cannot take over %s: it has no files here, and a take-over uploads this machine's copy" % name)
            return 2
    if not local:
        out("nothing to push: no folder of this box has files in %s" % project_path)
        return 0
    side = side_for(s, "shared")
    tool = TOOL_FOR_BACKEND[s["backend"]]
    if not backend.ready(args.dry_run):
        return 1
    mid, mname = machine.ensure(plat, dry_run=args.dry_run, quiet=args.dry_run)
    if shutil.which(tool):
        state, problem = read_state(side, s, args.timeout, args.transfer_timeout)
        if problem:
            err(problem)
            return 1
    else:
        out("would read %s (%s is not available here, so ownership is not checked)" % (where(side), tool))
        state = State({}, False)
    if take_all and state.exists:
        err(
            "this box records an owner per folder (%s): name the folders to take over, "
            "for example --take-over %s" % (where(side), local[0][0]["name"])
        )
        return 2
    if state.legacy:
        out(
            "this box still has the single-writer marker %s (writer %s): read as that machine owning every "
            "folder now in the box; %s is written on this push when anything is recorded"
            % (where(side, LEGACY), describe_owner(legacy_entry(state.legacy)), OWNERS)
        )
    selected = (set(only) | set(take_names)) if (only or take_names) else None
    plan = []  # (unit, summary, action, note)
    for u, sm in local:
        name = u["name"]
        if selected is not None and name not in selected:
            continue
        cur = state.owner(name)
        mine = bool(cur and mid and cur.get("machine_id") == mid)
        if name in take_names or take_all:
            if cur and not mine:
                plan.append((u, sm, "take-over", "owned by %s; this machine takes it over" % describe_owner(cur)))
            elif mine:
                plan.append((u, sm, "upload", "owned by this machine since %s" % (cur.get("since") or "?")))
            else:
                plan.append((u, sm, "claim", "no owner yet"))
        elif cur is None:
            plan.append((u, sm, "claim", "no owner yet"))
        elif mine:
            plan.append((u, sm, "upload", "owned by this machine since %s" % (cur.get("since") or "?")))
        else:
            how = layout.compare(u, sm, cur.get("fingerprint"))
            words = {
                "unknown": "not compared (the owner recorded no fingerprint)",
                "fetched": "fetched from the box",
            }.get(how, how)
            plan.append((u, sm, "skip", "owned by %s; local copy %s" % (describe_owner(cur), words)))
    if not plan:
        out("nothing selected to push (see --only)")
        return 0
    verbs = {
        "claim": "claim and upload",
        "upload": "upload when changed",
        "take-over": "take over and upload",
        "skip": "skip",
    }
    out("folders of %s here (%d):" % (s["project"], len(plan)))
    for u, sm, action, note in plan:
        out(
            "  %s%-20s %s (%d files)  %s"
            % ("would " if args.dry_run and action != "skip" else "", verbs[action] + ":", u["name"], sm["files"], note)
        )
    todo = [(u, sm, action) for u, sm, action, _ in plan if action != "skip"]
    if not todo:
        out("nothing to upload: every folder here is owned by another machine (skipping is not an error)")
        return 0
    if args.delete:
        warn("--delete: remote files of the folders pushed now that are gone locally will be deleted")
    done = set()
    fingerprints = {}
    folder_todo = [u for u, sm, a in todo if u["kind"] == "folder"]
    failures = 0
    if folder_todo:
        ok_names, failed = backend.push_folders(args, s, layout, folder_todo)
        done |= set(ok_names)
        failures += failed
    sidecars = {}
    if not args.dry_run:
        sidecars = archive.read_sidecars(side, [u["name"] for u, sm, a in todo if u["kind"] == "archive"], args.timeout)
    for u, sm, action in todo:
        if u["kind"] == "folder":
            fingerprints[u["name"]] = sm["fingerprint"]
            continue
        result = archive.push_one(
            args, s, side, u["name"], scanned=(sm["entries"], sm["skipped"]), remote_sidecar=sidecars.get(u["name"], False)
        )
        if result["rc"]:
            failures += 1
        else:
            done.add(u["name"])
            fingerprints[u["name"]] = result["fingerprint"]
    now = utc_now()
    changes = {}
    for u, sm, action in todo:
        name = u["name"]
        if name not in done:
            continue
        entry = {
            "machine_id": mid,
            "machine_name": mname,
            "since": now,
            "fingerprint": fingerprints.get(name),
            "updated": now,
        }
        if action == "upload":
            cur = state.owner(name) or {}
            if cur.get("fingerprint") == entry["fingerprint"] and cur.get("machine_name") == mname:
                continue  # nothing changed in the record
        mode = "take-over" if (take_all or name in take_names) else {"claim": "claim", "upload": "update"}[action]
        changes[name] = ("set", entry, mode)
    if take_all and state.legacy:
        # as before 0.4.0: this machine becomes the writer of every folder in the box
        for name, cur in state.folders.items():
            if name not in changes and cur.get("machine_id") != mid:
                changes[name] = (
                    "set",
                    dict(cur, machine_id=mid, machine_name=mname, since=now, updated=now),
                    "take-over",
                )
    convert = bool(state.legacy) and (bool(changes) or state.legacy.get("machine_id") == mid)
    if args.dry_run:
        if changes or convert:
            out(
                "would record in %s: %s (read again right before writing; only this machine's entries change)"
                % (where(side), ", ".join("%s %s" % (c[2], n) for n, c in sorted(changes.items())) or "the folders of the old marker")
            )
        return 0
    rc = 1 if failures else 0
    if not changes and not convert:
        record_push(project_path, mid, done, fingerprints, now)
        return rc
    ok, problem, applied, conflicts = update(
        side,
        s,
        mid,
        changes,
        args.timeout,
        convert=convert,
        legacy_base=state if state.legacy else None,
        list_timeout=args.transfer_timeout,
    )
    record_push(project_path, mid, set(done) - set(n for n, _ in conflicts), fingerprints, now)
    if conflicts:
        report_conflicts(conflicts, "recorded for this machine")
        rc = 1
    if not ok:
        err(problem)
        return 1
    if applied:
        words = []
        for name in sorted(applied):
            words.append("%s %s" % (changes[name][2], name))
        out("recorded in %s: %s" % (where(side), ", ".join(words)))
    elif convert:
        out("wrote %s (the folders of the old single-writer marker)" % where(side))
    if take_all and state.legacy:
        ok, detail = put_json(
            side,
            LEGACY,
            {"machine_id": mid, "machine_name": mname, "date": now, "tool": "agent-mirror %s" % __version__},
            args.timeout,
        )
        if ok:
            out("rewrote the old marker %s for versions before 0.4.0" % where(side, LEGACY))
        else:
            warn("could not rewrite %s: %s" % (where(side, LEGACY), detail))
    return rc


# box release ---------------------------------------------------------------------------------


def release(args, s, runtime, backend, plat):
    if runtime in CLOUD_RUNTIMES:
        err("refusing box release on %s: only a developer machine changes ownership" % runtime)
        return 2
    side = side_for(s, "shared")
    if not backend.ready(False):
        return 1
    mid, _ = machine.identity(load_user_config(plat))
    state, problem = read_state(side, s, args.timeout, args.transfer_timeout)
    if problem:
        err(problem)
        return 1
    names = [f.strip().strip("/") for f in args.folders]
    for name in names:
        cur = state.owner(name)
        if cur is None:
            err("%s has no owner in %s; nothing to release" % (name, where(side)))
            return 2
        if not mid or cur.get("machine_id") != mid:
            err("%s is owned by %s, not this machine; only its owner releases it" % (name, describe_owner(cur)))
            return 2
    if args.dry_run:
        out("dry run: nothing will change")
        out(
            "would remove %s from %s; no data is deleted, and another machine's next push claims it"
            % (", ".join(names), where(side))
        )
        return 0
    changes = dict((n, ("remove", None, "release")) for n in names)
    ok, problem, applied, conflicts = update(
        side,
        s,
        mid,
        changes,
        args.timeout,
        convert=bool(state.legacy),
        legacy_base=state if state.legacy else None,
        list_timeout=args.transfer_timeout,
    )
    if conflicts:
        report_conflicts(conflicts, "released")
    if not ok:
        err(problem)
        return 1
    forget_push(s["project_path"], applied)
    out(
        "released %s in %s: no data was deleted; the next push from another machine that has it claims it"
        % (", ".join(sorted(applied)) or "nothing", where(side))
    )
    return 1 if conflicts else 0


# box owners ----------------------------------------------------------------------------------


def from_index(index_path):
    """(owners folders or None, legacy writer or None) from a committed index."""
    folders, legacy = None, None
    try:
        with open(index_path, encoding="utf-8") as fh:
            for line in fh:
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                if isinstance(r, dict) and isinstance(r.get("owners"), dict):
                    folders = clean_folders({"folders": r["owners"]})
                elif isinstance(r, dict) and isinstance(r.get("writer"), dict):
                    legacy = r["writer"]
    except OSError:
        pass
    return folders, legacy


def cmd_owners(args, s, runtime, backend):
    side = side_for(s, "shared")
    tool = TOOL_FOR_BACKEND[s["backend"]]
    source = "the box (%s)" % where(side)
    state = None
    if shutil.which(tool):
        state, problem = read_state(side, s, args.timeout, args.transfer_timeout)
        if problem:
            warn("%s; using the committed index instead" % problem)
    else:
        warn("%s is not available here; using the committed index instead" % tool)
    if state is None:
        folders, legacy = from_index(s["index_path"])
        source = "the index %s (as of the last `agent-mirror box index`)" % s["index_path"]
        if folders is None and legacy:
            from agent_mirror.plan import read_box_index

            idx = read_box_index(s["index_path"], s) or {}
            folders = dict(
                (n, legacy_entry(legacy, (idx.get("folders") or {}).get(n, {}).get("fingerprint")))
                for n in (idx.get("folders") or {})
            )
        state = State(folders or {}, folders is not None, legacy)
    mid, _ = machine.identity(load_user_config(current()))
    layout = units.Layout(s)
    rows = []
    names = sorted(set(state.folders) | set(u["name"] for u in layout.units))
    for name in names:
        unit = layout.by_name.get(name)
        sm = layout.summary(unit) if unit else None
        here = bool(sm and sm["files"])
        cur = state.owner(name)
        if cur is None and not here:
            continue
        if cur is None:
            local = "unowned"
        elif not here:
            local = "absent"
        else:
            local = layout.compare(unit, sm, cur.get("fingerprint"))
        rows.append(
            {
                "folder": name,
                "owner": cur,
                "mine": bool(cur and mid and cur.get("machine_id") == mid),
                "here": here,
                "local": local,
                "files_here": sm["files"] if sm else 0,
                "bytes_here": sm["bytes"] if sm else 0,
                "note": (cur or {}).get("note") or None,
            }
        )
    if args.json:
        out(
            json.dumps(
                {
                    "project": s["project"],
                    "source": "box" if source.startswith("the box") else "index",
                    "legacy_writer": state.legacy,
                    "folders": rows,
                },
                indent=2,
            )
        )
        return 0
    out("owners of the box of %s, from %s:" % (s["project"], source))
    if state.legacy:
        out("  (old single-writer marker: %s owns every folder in the box)" % describe_owner(legacy_entry(state.legacy)))
    if not rows:
        out("  no folders yet")
    for r in rows:
        owner = (
            "%s%s" % (describe_owner(r["owner"]), " (this machine)" if r["mine"] else "") if r["owner"] else "-"
        )
        out("  %-32s %-44s here: %-3s local vs owner: %s" % (r["folder"], owner, "yes" if r["here"] else "no", r["local"]))
        if r["note"]:
            out("      note: %s" % shown(r["note"]))
    out(
        "local vs owner: same, different, absent (not here), fetched (here only as fetched from the box, "
        "unchanged), unknown (no fingerprint recorded), unowned (the next push from here claims it)"
    )
    return 0


# box index -----------------------------------------------------------------------------------


def index_lines(side, timeout, has_owners, has_legacy):
    """Index lines that record ownership: the _owners.json folders and, for older readers, the old marker."""
    lines = []
    if has_owners:
        data, _ = read_json(side, OWNERS, timeout)
        if data is not None:
            lines.append({"path": OWNERS, "schema": data.get("schema", SCHEMA), "owners": clean_folders(data)})
    if has_legacy:
        marker, _ = read_json(side, LEGACY, timeout)
        if marker:
            keep = dict((k, marker.get(k)) for k in ("machine_id", "machine_name", "date", "tool"))
            lines.append({"path": LEGACY, "writer": keep})
    return lines


# this machine's pushes, and the check at a developer session start ----------------------------------------------


def pushed_path(project):
    return os.path.join(project, STATE_DIR, PUSHED)


def load_pushed(project):
    """{folder: {"fingerprint", "at", "machine_id"}} of this clone's last pushes ({} when none)."""
    try:
        with open(pushed_path(project), encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    folders = data.get("folders") if isinstance(data, dict) else None
    return dict((n, e) for n, e in (folders or {}).items() if isinstance(n, str) and isinstance(e, dict))


def save_pushed(project, folders):
    write_text(pushed_path(project), json.dumps({"version": 1, "folders": folders}, indent=1, sort_keys=True) + "\n")
    ensure_git_exclude(project, os.path.join(project, STATE_DIR))


def record_push(project, mid, names, fingerprints, at):
    """Remember what this push uploaded of each folder. Local only; never raises."""
    if not names or not mid:
        return
    try:
        folders = load_pushed(project)
        for name in names:
            folders[name] = {"fingerprint": fingerprints.get(name), "at": at, "machine_id": mid}
        save_pushed(project, folders)
    except OSError:
        pass


def forget_push(project, names):
    try:
        folders = load_pushed(project)
        if any(n in folders for n in names or ()):
            save_pushed(project, dict((n, e) for n, e in folders.items() if n not in names))
    except OSError:
        pass


def owned_here(s, mid):
    """{folder: (fingerprint, time)} of the folders this machine owns, offline: the owners line of the committed
    index and this clone's record of its pushes. A folder the index gives to another machine is not this
    machine's; for a folder both name, the newer of the two (by time) is the reference."""
    folders, _ = from_index(s["index_path"])
    folders = folders or {}
    record = load_pushed(s["project_path"])
    owned = {}
    for name, e in folders.items():
        if e.get("machine_id") == mid:
            owned[name] = (e.get("fingerprint"), str(e.get("updated") or e.get("since") or ""))
    for name, e in record.items():
        if e.get("machine_id") != mid or (name in folders and name not in owned):
            continue
        mine = (e.get("fingerprint"), str(e.get("at") or ""))
        if name not in owned or mine[1] >= owned[name][1]:
            owned[name] = mine
    return owned


def changed_since_push(s, mid, deadline):
    """(owned folders here that changed since this machine's last push, number owned, True when cut short).

    A folder whose walk finished is compared by fingerprint (as `box owners` does; a copy only fetched from the
    box counts as unchanged). When the time runs out, a folder still counts as changed when one file seen so far
    is newer than its last push; otherwise it is not reported."""
    from agent_mirror.box.sessions import parse_utc

    owned = owned_here(s, mid)
    if not owned:
        return [], 0, False
    layout = units.Layout(s)
    changed, cut = [], False
    for name in sorted(owned):
        unit = layout.by_name.get(name)
        if unit is None:
            continue
        if time.monotonic() > deadline:
            cut = True
            break
        sm = layout.summary(unit, deadline=deadline)
        fingerprint, at = owned[name]
        if sm["fingerprint"] is not None:
            if sm["files"] and layout.compare(unit, sm, fingerprint) == "different":
                changed.append(name)
            continue
        cut = True
        when = parse_utc(at)
        if when is not None and any(e["mtime"] > when.timestamp() for e in sm["entries"]):
            changed.append(name)
    return changed, len(owned), cut


def push_check_line(ctx, project, path, deadline, timeout):
    """(line or None, step status) for one project at a developer session start."""
    from agent_mirror.box.notes import quiet_context

    cfg = load_user_config(ctx.plat)
    mid, _ = machine.identity(cfg)
    if not mid:
        return None, "skipped (no machine id here)"
    role = machine.box_role(cfg, project)
    if role in ("reader", "none"):
        return None, "skipped (box role %s)" % role
    if not os.path.isdir(path):
        return None, "skipped (no project folder)"
    s = quiet_context(ctx, project, path)
    if s is None:
        return None, "skipped (box not configured)"
    changed, owned, cut = changed_since_push(s, mid, deadline)
    if not owned:
        return None, "skipped (nothing owned here)"
    flag = " --project %s" % shlex.quote(project)
    if changed:
        n = len(changed)
        names = ", ".join(changed[:5]) + (" and %d more" % (n - 5) if n > 5 else "")
        line = (
            "box of %s: %d owned folder%s changed since the last push (%s); upload %s with: agent-mirror box push%s"
            % (
                project,
                n,
                "" if n == 1 else "s",
                names,
                "it" if n == 1 else "them",
                flag,
            )
        )
        if cut:
            line += " (the check stopped after %g s; others may have changed too)" % timeout
        return line, "ok (%d changed%s)" % (n, ", cut short" if cut else "")
    if cut:
        return (
            "box of %s: the check of this machine's box folders stopped after %g s; `agent-mirror box owners%s` "
            "compares them" % (project, timeout, flag),
            "ok (cut short)",
        )
    return None, "ok (none changed)"


def session_start_push_check(ctx, store, matched, args, log):
    """Developer session start: one line per project whose box folders owned by this machine changed since its last
    push. Offline (the committed index and local records), bounded by --push-check-timeout in all; never fails the
    session and prints nothing when nothing changed or nothing is owned here."""
    from agent_mirror.box.unsent import box_configured

    timeout = float(getattr(args, "push_check_timeout", PUSH_CHECK_TIMEOUT) or 0)
    deadline = time.monotonic() + max(0.0, timeout)
    for project, path in matched:
        try:
            if not box_configured(store.load_env(project).get("box")):
                continue
        except Exception:  # noqa: BLE001
            continue
        with log.step("box push check (%s)" % project) as st:
            if timeout <= 0:
                st.status = "skipped (--push-check-timeout 0)"
                continue
            if args.dry_run:
                st.status = "skipped (dry run)"
                continue
            try:
                line, st.status = push_check_line(ctx, project, path, deadline, timeout)
            except (Exception, SystemExit) as exc:  # noqa: BLE001 (never fail the session)
                line, st.status = None, "failed (%s)" % short(exc, 60)
            if line:
                out(line)
