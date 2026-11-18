"""Step timings of session-start: names, times and exit status only, never command output.

Each run of session-start appends one JSON line to <cache>/session-start.jsonl:

    {"schema": 1, "command": "session-start", "tool": "agent-mirror 0.5.2", "runtime": "claude-cloud",
     "started": "2026-11-18T10:00:00Z", "ended": "...", "seconds": 12.3, "self_updated": false,
     "steps": [{"name": "store pull", "started": "...", "ended": "...", "seconds": 0.4, "status": "ok"}, ...]}

When the cloud self-update runs session-start again with the new code, the steps of the first run are
handed to the second one in an environment variable, so one session start is still one record.
`agent-mirror probe` prints the last record. The file keeps the newest records only.
"""

import contextlib
import datetime
import json
import os
import time

from agent_mirror import __version__
from agent_mirror.util import short

FILE_NAME = "session-start.jsonl"
HANDOFF_VAR = "AGENT_MIRROR_SESSION_STEPS"
SCHEMA = 1
KEEP = 50


def iso(ts):
    return datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def log_path(plat):
    return os.path.join(plat.cache_dir(), FILE_NAME)


class Step:
    def __init__(self, name):
        self.name = short(name, 120)
        self.start = time.time()
        self.end = None
        self.status = "ok"

    def as_dict(self, end=None):
        end = self.end if self.end is not None else (end if end is not None else time.time())
        return {
            "name": self.name,
            "started": iso(self.start),
            "ended": iso(end),
            "seconds": round(end - self.start, 2),
            "status": self.status,
        }


class StepLog:
    """Collects the steps of one command run; write() appends the record. Never raises."""

    def __init__(self, plat, command, runtime, env=None):
        self.plat = plat
        self.command = command
        self.runtime = runtime
        self.start = time.time()
        self.steps = []
        self.open = []
        env = os.environ if env is None else env
        # a re-run with the updated tool (selfupdate.MARKER); an older tool hands over no steps
        self.self_updated = bool(env.get("AGENT_MIRROR_SELF_UPDATED"))
        inherited = env.get(HANDOFF_VAR)
        if inherited:
            try:
                data = json.loads(inherited)
                self.start = float(data.get("start") or self.start)
                self.steps = [s for s in data.get("steps") or [] if isinstance(s, dict)]
                self.self_updated = True
            except (ValueError, TypeError, AttributeError):
                pass

    @contextlib.contextmanager
    def step(self, name):
        """Time one step; the caller may set .status ('failed (exit 1)', 'skipped', ...)."""
        st = Step(name)
        self.open.append(st)
        try:
            yield st
        except BaseException:
            st.status = "error"
            raise
        finally:
            st.end = time.time()
            self.open.remove(st)
            self.steps.append(st.as_dict())

    def add(self, name, start, end, status):
        st = Step(name)
        st.start, st.end, st.status = start, end, status
        self.steps.append(st.as_dict())

    def handoff(self):
        """{HANDOFF_VAR: json} for a re-run with new code: the steps so far, open ones ended now."""
        now = time.time()
        steps = list(self.steps) + [s.as_dict(end=now) for s in self.open]
        return {HANDOFF_VAR: json.dumps({"start": self.start, "steps": steps})}

    def record(self):
        end = time.time()
        failed = [s["name"] for s in self.steps if not str(s.get("status", "")).startswith(("ok", "skipped"))]
        return {
            "schema": SCHEMA,
            "command": self.command,
            "tool": "agent-mirror %s" % __version__,
            "runtime": self.runtime,
            "started": iso(self.start),
            "ended": iso(end),
            "seconds": round(end - self.start, 2),
            "self_updated": self.self_updated,
            "failed": failed,
            "steps": list(self.steps),
        }

    def summary(self):
        rec = self.record()
        parts = [
            "%s %.1f s%s" % (s["name"], s["seconds"], "" if s["status"] == "ok" else " (%s)" % s["status"])
            for s in rec["steps"]
        ]
        return "%s took %.1f s: %s" % (self.command, rec["seconds"], "; ".join(parts) or "no steps")

    def write(self):
        """Append the record (and trim the file to the newest records). Returns the path, or None."""
        path = log_path(self.plat)
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            lines = []
            if os.path.isfile(path):
                with open(path, encoding="utf-8", errors="replace") as fh:
                    lines = [ln for ln in fh.read().splitlines() if ln.strip()]
            lines.append(json.dumps(self.record(), sort_keys=True))
            tmp = path + ".tmp-agent-mirror"
            with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
                fh.write("\n".join(lines[-KEEP:]) + "\n")
            os.replace(tmp, path)
            return path
        except OSError:
            return None


def last_record(plat):
    """The newest record of the log, or None."""
    path = log_path(plat)
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            lines = [ln for ln in fh.read().splitlines() if ln.strip()]
    except OSError:
        return None
    for line in reversed(lines):
        try:
            data = json.loads(line)
        except ValueError:
            continue
        if isinstance(data, dict):
            return data
    return None


def record_lines(rec):
    """Short lines for probe: when, total, then one line per step (name, seconds, status)."""
    if not rec:
        return ["no session-start record on this machine yet"]
    lines = [
        "last session-start: %s, %s, %.1f s in all%s"
        % (
            rec.get("started", "?"),
            rec.get("runtime", "?"),
            float(rec.get("seconds") or 0),
            ", ran again with the updated tool" if rec.get("self_updated") else "",
        )
    ]
    for s in rec.get("steps") or []:
        lines.append(
            "  %-44s %7.1f s  %s" % (short(s.get("name", "?"), 44), float(s.get("seconds") or 0), s.get("status", "?"))
        )
    if not rec.get("steps"):
        lines.append("  (no steps recorded)")
    return lines
