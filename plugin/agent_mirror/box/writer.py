"""One writer per box: <inbound>/_writer.json names the machine that pushes.

The first `box push` for a project writes the marker (machine id, machine name, date, tool version).
A later push from a machine with another id is refused and the message names the current writer;
`box push --take-over` rewrites the marker. A machine whose recorded role for the project is reader
or none is refused before any transfer. `box index` copies the marker into the index, so `plan` can
name the writer offline.
"""

import json
import os
import shutil

from agent_mirror import __version__, machine
from agent_mirror.box.sides import side_for
from agent_mirror.config import load_user_config
from agent_mirror.platforms import current
from agent_mirror.util import err, iso_now, out, scrub

MARKER = "_writer.json"
TOOL_FOR_BACKEND = {"azure-blob": "az", "rclone": "rclone"}


def read_marker(side, timeout):
    """(marker dict or None, problem or None). A missing marker is (None, None)."""
    tmp = current().mkdtemp("agent-mirror-writer-")
    try:
        path = os.path.join(tmp, "w.json")
        got = side.get(MARKER, path, timeout)
        if got == "missing":
            return None, None
        if got != "ok":
            return None, got
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            return None, "%s is not valid JSON" % where(side)
        return (data if isinstance(data, dict) else None), None
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def write_marker(side, mid, name, timeout):
    data = {"machine_id": mid, "machine_name": name, "date": iso_now(), "tool": "agent-mirror %s" % __version__}
    tmp = current().mkdtemp("agent-mirror-writer-")
    try:
        path = os.path.join(tmp, MARKER)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=1)
        ok, detail = side.put(path, MARKER, timeout)
        return ok, detail
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def where(side):
    """The marker's remote location for messages, with secret-looking values replaced by ***."""
    return scrub(side.where(MARKER))


def describe(marker):
    return "%s (machine id %s, writer since %s)" % (
        marker.get("machine_name") or "?",
        str(marker.get("machine_id") or "?")[:12],
        marker.get("date") or "?",
    )


def guard(args, s, ctx):
    """Before box push: the role check and the writer marker. None to go on, or an exit code."""
    project = s["project"]
    plat = ctx.plat
    cfg = load_user_config(plat)
    role = machine.box_role(cfg, project)
    if role in ("reader", "none"):
        err(
            "refusing box push for %s: this machine's recorded role is %s (one machine writes each box). "
            'To make it the writer, run `agent-mirror apply --choices FILE` with {"box:%s": "writer"}'
            % (project, role, project)
        )
        return 2
    side = side_for(s, "inbound")
    tool = TOOL_FOR_BACKEND[s["backend"]]
    if not shutil.which(tool):
        if args.dry_run:
            out("would read the writer marker %s (%s is not available here)" % (where(side), tool))
        return None  # the push itself reports the missing program
    mid, name = machine.identity(cfg)
    marker, problem = read_marker(side, args.timeout)
    if problem:
        err("cannot read the writer marker %s: %s" % (where(side), problem))
        return 1
    take_over = getattr(args, "take_over", False)
    if marker and mid and marker.get("machine_id") == mid:
        out("writer: this machine (%s) writes the box of %s" % (name, project))
        return None
    if marker and not take_over:
        err(
            "refusing box push for %s: the box's writer is %s, not this machine. One machine writes each box; "
            "run `agent-mirror box push --take-over` here to make this machine the writer" % (project, describe(marker))
        )
        return 2
    what = "take over as the writer from %s" % describe(marker) if marker else "become the writer"
    if args.dry_run:
        out("would write the writer marker %s: this machine would %s" % (where(side), what))
        return None
    mid, name = machine.ensure(plat)
    ok, detail = write_marker(side, mid, name, args.timeout)
    if not ok:
        err("cannot write the writer marker %s: %s" % (where(side), detail))
        return 1
    out(
        "wrote the writer marker %s: this machine (%s) %s"
        % (where(side), name, "took over" if marker else "is the writer")
    )
    return None


def index_line(side, timeout):
    """The index line that records the marker, or None when there is no readable marker."""
    marker, _ = read_marker(side, timeout)
    if not marker:
        return None
    keep = dict((k, marker.get(k)) for k in ("machine_id", "machine_name", "date", "tool"))
    return {"path": MARKER, "writer": keep}
