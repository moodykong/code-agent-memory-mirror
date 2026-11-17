"""box ls, box status and box evict: browse the box offline, see what was fetched, free disk."""

import datetime
import json
import os
import shlex

from agent_mirror.box.archive import read_local_toc
from agent_mirror.box.local import FetchRecord, free_bytes, human, min_free
from agent_mirror.box.rules import clean_rel
from agent_mirror.config import resolve_project
from agent_mirror.runtime import CLOUD_RUNTIMES, detect_runtime
from agent_mirror.util import err, out, warn, within

PRESENCE_LIMIT = 200000


def load_index(index_path):
    files, archives = [], []
    with open(index_path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if not isinstance(r, dict) or not r.get("path") or "writer" in r:
                continue  # the writer marker line, or anything that is not an entry
            (archives if r.get("archive") else files).append(r)
    return files, archives


def presence(project, path, size):
    p = os.path.join(project, *path.split("/"))
    try:
        st = os.stat(p)
    except OSError:
        return "-"
    return "here" if size is None or st.st_size == size else "differs"


def cmd_ls(args, s):
    index_path = s["index_path"]
    index_dir = os.path.dirname(index_path)
    project = s["project_path"]
    if not os.path.isfile(index_path):
        err(
            "no box index at %s; run `agent-mirror box index` (it reads the box), "
            "or pull the store if another machine committed one" % index_path
        )
        return 1
    rel = ""
    if args.path:
        rel = clean_rel(args.path)
        if rel is None:
            err("box ls takes a path relative to the project folder, without '..' (got %r)" % args.path)
            return 2
    files, archives = load_index(index_path)
    arch = dict((a["path"], a) for a in archives)
    top = rel.split("/")[0] if rel else ""
    when = datetime.datetime.fromtimestamp(os.path.getmtime(index_path)).strftime("%Y-%m-%d %H:%M")
    out("box index %s (written %s): %d files, %d archived folders" % (index_path, when, len(files), len(archives)))
    if top in arch:
        toc = read_local_toc(index_dir, arch[top])
        if toc is None:
            err("the index has no table of contents for the archived folder %s; run `agent-mirror box index`" % top)
            return 1
        items = [(top + "/" + e["path"], e.get("size") or 0, e.get("in", "bundle")) for e in toc]
    else:
        items = [(f["path"], f.get("size") or 0, "file") for f in files]
    check = len(items) <= PRESENCE_LIMIT
    prefix = rel + "/" if rel else ""
    exact = [it for it in items if it[0] == rel]
    if rel and exact and not any(it[0].startswith(prefix) for it in items):
        path, size, kind = exact[0]
        note = "  large: fetch with agent-mirror box fetch %s" % shlex.quote(path) if kind == "large" else ""
        out("  %-9s %10s  %s%s" % (presence(project, path, size), human(size), path, note))
        return 0
    rows = {}
    for path, size, kind in items:
        if not path.startswith(prefix):
            continue
        head, _, tail = path[len(prefix) :].partition("/")
        r = rows.setdefault(
            head, {"dir": False, "files": 0, "bytes": 0, "here": 0, "large": 0, "kind": kind, "path": path}
        )
        r["dir"] = r["dir"] or bool(tail)
        r["files"] += 1
        r["bytes"] += size
        r["large"] += kind == "large"
        if check and presence(project, path, size) == "here":
            r["here"] += 1
    if not rel:
        for name, a in arch.items():
            r = rows.setdefault(name, {"dir": True, "files": 0, "bytes": 0, "here": 0, "large": 0, "kind": "archive"})
            r.update(
                {
                    "dir": True,
                    "files": a.get("files") or 0,
                    "bytes": a.get("bytes") or 0,
                    "kind": "archive",
                    "large": a.get("large_files") or 0,
                }
            )
            toc = read_local_toc(index_dir, a) if check else None
            r["here"] = sum(1 for e in toc or [] if presence(project, name + "/" + e["path"], e.get("size")) == "here")
    if not rows:
        err("nothing at %s in the box index" % (rel or "the top"))
        return 1
    for name in sorted(rows):
        r = rows[name]
        shown = prefix + name
        if r["dir"]:
            status = "%d/%d" % (r["here"], r["files"]) if check else "?"
            note = "  archived folder: %d files" % r["files"] if r["kind"] == "archive" else "  %d files" % r["files"]
            if r["large"]:
                note += ", %d large (fetched only when named or with --all)" % r["large"]
            out("  %-9s %10s  %s/%s" % (status, human(r["bytes"]), shown, note))
        else:
            note = ""
            if r["kind"] == "large":
                note = "  large: agent-mirror box fetch %s" % shlex.quote(shown)
            out("  %-9s %10s  %s%s" % (presence(project, shown, r["bytes"]), human(r["bytes"]), shown, note))
    out("status: here = present locally with the same size; n/m = files of a folder present locally")
    return 0


def project_and_box(args, ctx):
    project, path = resolve_project(ctx, args.project, args.project_path)
    store = ctx.store_or_none(quiet=True)
    box = (store.load_env(project).get("box") or {}) if (store and project) else {}
    return project, path, box


def cmd_status(args, ctx):
    project, path, box = project_and_box(args, ctx)
    record = FetchRecord(path)
    out("box status for %s (%s)" % (project or "this folder", path))
    if not record.files:
        out("nothing fetched from the box on this machine yet (record: %s)" % record.path)
    else:
        groups = {}
        for rel, entry in record.files.items():
            g = groups.setdefault(rel.split("/")[0], {"files": 0, "bytes": 0, "here": 0, "changed": 0})
            g["files"] += 1
            g["bytes"] += int(entry.get("size") or 0)
            if os.path.lexists(os.path.join(path, *rel.split("/"))):
                g["here"] += 1
                g["changed"] += not record.unchanged(rel)
        total = sum(g["bytes"] for g in groups.values())
        out("fetched from the box: %d file(s), %s (record %s)" % (len(record.files), human(total), record.path))
        for name in sorted(groups):
            g = groups[name]
            note = ", %d changed since fetch (evict keeps them)" % g["changed"] if g["changed"] else ""
            out("  %10s  %s: %d file(s), %d still here%s" % (human(g["bytes"]), name, g["files"], g["here"], note))
    out(
        "free disk: %s at %s; reserve kept by fetch: %s (box.min_free_bytes)"
        % (human(free_bytes(path)), path, human(min_free(box)))
    )
    return 0


def remove_empty_parents(path, root):
    d = os.path.dirname(path)
    while d != root and within(d, root):
        try:
            os.rmdir(d)
        except OSError:
            return
        d = os.path.dirname(d)


def cmd_evict(args, ctx):
    rel = clean_rel(args.path)
    if rel is None:
        err("box evict takes a path relative to the project folder, without '..' (got %r)" % args.path)
        return 2
    runtime = detect_runtime()
    if runtime not in CLOUD_RUNTIMES and not args.force:
        err("box evict deletes files; it runs on cloud runtimes only unless you pass --force (this is %s)" % runtime)
        return 2
    _, path, _ = project_and_box(args, ctx)
    root = os.path.realpath(path)
    record = FetchRecord(path)
    targets = record.under(rel)
    if not targets:
        err("nothing under %s was fetched from the box (see `agent-mirror box status`); nothing deleted" % rel)
        return 1
    if args.dry_run:
        out("dry run: nothing will change")
    freed = deleted = changed = gone = 0
    for t in targets:
        safe = clean_rel(t)
        dest = os.path.join(root, *safe.split("/")) if safe else None
        if not dest or not within(os.path.realpath(dest), root):
            warn("record entry %r is not inside the project; skipped" % t)
            continue
        if not os.path.lexists(dest):
            gone += 1
            if not args.dry_run:
                record.files.pop(t, None)
            continue
        if os.path.islink(dest) or not record.unchanged(t):
            warn("%s changed since it was fetched; kept" % t)
            changed += 1
            continue
        size = os.path.getsize(dest)
        freed += size
        deleted += 1
        if args.dry_run:
            out("would delete %s (%s)" % (t, human(size)))
            continue
        os.remove(dest)
        record.files.pop(t, None)
        remove_empty_parents(dest, root)
    if not args.dry_run:
        record.save()
    out(
        "evict: %s %d file(s), %s %s; kept %d changed since fetch; %d were already gone"
        % (
            "would delete" if args.dry_run else "deleted",
            deleted,
            human(freed),
            "to free" if args.dry_run else "freed",
            changed,
            gone,
        )
    )
    return 0
