"""Run the agent-mirror tests in parallel worker processes (standard library only).

    python3 tests/run_parallel.py                     # every test file, one worker per CPU
    python3 tests/run_parallel.py --jobs 4            # at most 4 workers at a time
    python3 tests/run_parallel.py test_box_notes.py   # only these files (names or patterns)

Each test class runs in its own Python process (a unit; `--split file` makes one per file), started from this
file with `--worker`. A worker runs its tests with the standard unittest runner, so module and class fixtures,
skips and printed output behave as in a serial run. Every unit gets its own temp folder: TMPDIR, TEMP and TMP
point into it (so every sandbox the tests make with tempfile lives there), and so do AGENT_MIRROR_HOME and
AGENT_MIRROR_TEST_SANDBOX, so no two units share a home, a temp folder or a file name. Every worker imports
`helpers`, which runs the sandbox guard before any test, exactly as in a serial run; so does this process while
it collects the tests. The run's temp folder is removed at the end.

The output: one line per unit as it finishes (with what its tests print, such as the call-count tables, and
the full output of a unit that failed), one line per file, the skipped tests with their reasons, and last one
summary line. The exit code is 0 when every collected test ran and passed, 97 when the sandbox guard stopped a
worker, and 1 otherwise.
"""

import argparse
import fnmatch
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor, as_completed

TESTS = os.path.dirname(os.path.realpath(os.path.abspath(__file__)))
SANDBOX_EXIT = 97


def remove_tree(path):
    """Like helpers.remove_tree (git object files are read-only on Windows); helpers is not imported yet."""

    def retry(func, target, _exc):
        try:
            os.chmod(target, stat.S_IWRITE)
            func(target)
        except OSError:
            pass

    if not os.path.lexists(path):
        return
    if sys.version_info >= (3, 12):
        shutil.rmtree(path, onexc=retry)
    else:
        shutil.rmtree(path, onerror=retry)


def text_of(data):
    if data is None:
        return ""
    text = data.decode("utf-8", "replace") if isinstance(data, bytes) else data
    return text.replace("\r\n", "\n")  # the worker's output is read as bytes; printed again in text mode


# the worker ------------------------------------------------------------------------------------------------


def worker(result_path, names):
    """Run the named tests (module.Class, or file:test_x.py for a whole file) and write the counts as JSON."""
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")  # the parent reads UTF-8 on every platform
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    for name in names:
        if name.startswith("file:"):
            suite.addTests(loader.discover(TESTS, pattern=name[len("file:") :], top_level_dir=TESTS))
        else:
            suite.addTests(loader.loadTestsFromName(name))
    result = unittest.TextTestRunner(stream=sys.stderr, verbosity=1).run(suite)
    data = {
        "run": result.testsRun,
        "failures": len(result.failures),
        "errors": len(result.errors),
        "skipped": [[t.id(), reason] for t, reason in result.skipped],
        "expected_failures": len(result.expectedFailures),
        "unexpected_successes": len(result.unexpectedSuccesses),
        "ok": result.wasSuccessful(),
    }
    with open(result_path, "w", encoding="utf-8") as fh:
        json.dump(data, fh)
    return 0 if result.wasSuccessful() else 1


# collecting the units -------------------------------------------------------------------------------------


def test_files(patterns):
    names = sorted(f for f in os.listdir(TESTS) if f.startswith("test_") and f.endswith(".py"))
    if not patterns:
        return names
    chosen = []
    for p in patterns:
        p = os.path.basename(p)
        hits = [n for n in names if fnmatch.fnmatchcase(n, p) or n == p + ".py"]
        if not hits:
            raise SystemExit("error: no test file matches %s" % p)
        chosen += [n for n in hits if n not in chosen]
    return chosen


def iter_tests(suite):
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            for t in iter_tests(item):
                yield t
        else:
            yield item


class Unit:
    def __init__(self, name, names, count, file, starts_program):
        self.name, self.names, self.count, self.file = name, names, count, file
        self.starts_program = starts_program  # its tests start the program (ToolTest): slower per test


def collect(files, split):
    """The units, in file order. Importing the test modules imports helpers, whose sandbox guard runs first."""
    loader = unittest.TestLoader()
    units = []
    for f in files:
        tests = list(iter_tests(loader.discover(TESTS, pattern=f, top_level_dir=TESTS)))
        tool_test = getattr(sys.modules.get("helpers"), "ToolTest", ())
        broken = any(type(t).__module__ == "unittest.loader" for t in tests)  # an import error, say
        if split == "file" or broken:
            starts = any(isinstance(t, tool_test) for t in tests) if tool_test else True
            units.append(Unit(f[:-3], ["file:" + f], len(tests), f, starts))
            continue
        by_class = {}
        for t in tests:
            key = "%s.%s" % (type(t).__module__, type(t).__name__)
            by_class.setdefault(key, []).append(t)
        for key, members in by_class.items():
            starts = isinstance(members[0], tool_test) if tool_test else True
            units.append(Unit(key, [key], len(members), f, starts))
    return units


# running the units ----------------------------------------------------------------------------------------


def unit_env(unit_dir):
    env = dict(os.environ)
    for name in ("CLAUDE_CONFIG_DIR", "CODEX_HOME"):
        env.pop(name, None)
    env.update(
        {
            "TMPDIR": unit_dir,
            "TEMP": unit_dir,
            "TMP": unit_dir,
            "AGENT_MIRROR_HOME": os.path.join(unit_dir, "home"),
            "AGENT_MIRROR_TEST_SANDBOX": unit_dir,
        }
    )
    return env


def run_unit(index, unit, root, timeout, stopping):
    if stopping.is_set():
        return None
    unit_dir = os.path.join(root, str(index))  # short: on Windows the tests' own paths are long already
    os.makedirs(os.path.join(unit_dir, "home"))
    result_path = os.path.join(root, "%d.json" % index)
    began = time.time()
    try:
        proc = subprocess.run(
            [sys.executable, os.path.realpath(os.path.abspath(__file__)), "--worker", result_path] + unit.names,
            cwd=unit_dir,
            env=unit_env(unit_dir),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
        )
        rc, stdout, stderr, timed_out = proc.returncode, proc.stdout, proc.stderr, False
    except subprocess.TimeoutExpired as exc:
        rc, stdout, stderr, timed_out = None, exc.stdout, exc.stderr, True
    took = time.time() - began
    data = None
    if os.path.isfile(result_path) and not timed_out:
        with open(result_path, encoding="utf-8") as fh:
            data = json.load(fh)
    left = sorted(set(os.listdir(unit_dir)) - {"home"})
    remove_tree(unit_dir)
    return {
        "rc": rc,
        "stdout": text_of(stdout),
        "stderr": text_of(stderr),
        "data": data,
        "took": took,
        "left": left,
        "timed_out": timed_out,
    }


def describe(r, timeout):
    d = r["data"]
    if r["timed_out"]:
        return "FAIL", "stopped after the --timeout of %d s" % timeout
    if r["rc"] == SANDBOX_EXIT:
        return "FAIL", "the sandbox guard stopped the worker (exit code %d)" % SANDBOX_EXIT
    if d is None:
        return "FAIL", "the worker ended with exit code %s and no result" % r["rc"]
    parts = []
    for key in ("failures", "errors", "unexpected_successes"):
        if d[key]:
            parts.append("%s=%d" % (key.replace("_", " "), d[key]))
    if d["skipped"]:
        parts.append("skipped=%d" % len(d["skipped"]))
    if d["expected_failures"]:
        parts.append("expected failures=%d" % d["expected_failures"])
    if r["left"]:
        parts.append("left in its temp folder: %s" % ", ".join(r["left"]))
    return ("ok" if d["ok"] and r["rc"] == 0 else "FAIL"), ", ".join(parts)


def indent(text):
    return "".join(("    " + ln) if ln.strip() else ln for ln in text.splitlines(True))


def main(argv=None):
    parser = argparse.ArgumentParser(description="Run the agent-mirror tests in parallel worker processes.")
    parser.add_argument("files", nargs="*", help="test files to run (names or patterns; default: every test_*.py)")
    parser.add_argument(
        "-j", "--jobs", type=int, default=os.cpu_count() or 1, help="workers at a time (default: the CPU count)"
    )
    parser.add_argument(
        "--split", choices=("class", "file"), default="class", help="one unit per test class (default) or per file"
    )
    parser.add_argument(
        "--timeout", type=int, default=900, help="seconds one unit may take before it is stopped (default 900)"
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="print the full output of every unit")
    parser.add_argument("--worker", metavar="RESULT", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.worker:
        return worker(args.worker, args.files)
    if args.jobs < 1:
        parser.error("--jobs must be 1 or more")

    began = time.time()
    root = os.path.realpath(tempfile.mkdtemp(prefix="amr-"))
    stopping = threading.Event()
    out = sys.stdout
    try:
        # the temp folders helpers makes in this process go into the run's folder too
        tempfile.tempdir = os.path.join(root, "collect")
        os.makedirs(tempfile.tempdir)
        if TESTS not in sys.path:
            sys.path.insert(0, TESTS)
        units = collect(test_files(args.files), args.split)
        expected = sum(u.count for u in units)
        # a rough proxy for duration, so that no long unit starts last: units whose tests start the program
        # first, the larger first
        order = sorted(range(len(units)), key=lambda i: (not units[i].starts_program, -units[i].count))
        out.write(
            "agent-mirror tests: %d tests in %d units, --jobs %d, temp folder %s\n"
            % (expected, len(units), args.jobs, root)
        )
        out.flush()
        results = {}
        with ThreadPoolExecutor(max_workers=args.jobs) as pool:
            futures = {pool.submit(run_unit, i, units[i], root, args.timeout, stopping): i for i in order}
            try:
                for fut in as_completed(futures):
                    i = futures[fut]
                    r = fut.result()
                    if r is None:
                        continue
                    results[i] = r
                    status, note = describe(r, args.timeout)
                    run = r["data"]["run"] if r["data"] else 0
                    out.write(
                        "%-4s %-50s %3d tests %6.1f s%s\n"
                        % (status, units[i].name, run, r["took"], ("  (%s)" % note) if note else "")
                    )
                    if r["stdout"].strip():
                        out.write(indent(r["stdout"].strip("\n") + "\n"))
                    if args.verbose or status != "ok":
                        out.write(indent(r["stderr"]))
                    if r["rc"] == SANDBOX_EXIT:
                        stopping.set()  # the guard refused: start nothing more
                    out.flush()
            except KeyboardInterrupt:
                stopping.set()
                raise

        totals = {"run": 0, "failures": 0, "errors": 0, "expected_failures": 0, "unexpected_successes": 0}
        skipped, broken = [], []
        by_file = {}
        for i, u in enumerate(units):
            f = by_file.setdefault(u.file, {"run": 0, "skipped": 0, "took": 0.0, "bad": False})
            r = results.get(i)
            if r is None:
                broken.append(u.name + " (not run)")
                f["bad"] = True
                continue
            status, _ = describe(r, args.timeout)
            f["took"] += r["took"]
            f["bad"] = f["bad"] or status != "ok"
            d = r["data"]
            if d is None:
                broken.append(u.name)
                continue
            for k in totals:
                totals[k] += d[k]
            skipped += d["skipped"]
            f["run"] += d["run"]
            f["skipped"] += len(d["skipped"])

        out.write("\nper file:\n")
        for name in sorted(by_file):
            f = by_file[name]
            out.write(
                "%-4s %-50s %3d tests %6.1f s%s\n"
                % (
                    "FAIL" if f["bad"] else "ok",
                    name,
                    f["run"],
                    f["took"],
                    ("  (skipped=%d)" % f["skipped"]) if f["skipped"] else "",
                )
            )
        if skipped:
            out.write("\nskipped:\n")
            for test_id, reason in sorted(skipped):
                out.write("    %s: %s\n" % (test_id, reason))

        problems = []
        for k in ("failures", "errors", "unexpected_successes"):
            if totals[k]:
                problems.append("%s=%d" % (k.replace("_", " "), totals[k]))
        if broken:
            problems.append("units without a result=%d (%s)" % (len(broken), ", ".join(broken)))
        elif totals["run"] != expected:
            problems.append("ran %d of the %d collected tests" % (totals["run"], expected))
        extras = []
        if skipped:
            extras.append("skipped=%d" % len(skipped))
        if totals["expected_failures"]:
            extras.append("expected failures=%d" % totals["expected_failures"])
        if problems:
            verdict = "FAILED (%s)" % ", ".join(problems + extras)
        else:
            verdict = "OK (%s)" % ", ".join(extras) if extras else "OK"
        out.write(
            "\nRan %d tests in %.1f s with --jobs %d: %s\n" % (totals["run"], time.time() - began, args.jobs, verdict)
        )
        out.flush()
        if any(r["rc"] == SANDBOX_EXIT for r in results.values()):
            return SANDBOX_EXIT
        return 1 if problems else 0
    finally:
        stopping.set()
        tempfile.tempdir = None
        remove_tree(root)


if __name__ == "__main__":
    sys.exit(main())
