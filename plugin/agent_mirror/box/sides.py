"""One side of the box (shared or sessions) as a small set of file operations.

Archives, receive and the archive index need the same few operations on both
backends: put one file, get one file, read a small file, list everything under
a path. Paths here are relative to the side's root (azure: <container>/<prefix>/,
rclone: the configured remote path).
"""

import json
import os
import re
import shutil

from agent_mirror.box.common import run_transfer
from agent_mirror.platforms import current
from agent_mirror.util import short

MISSING_RC = (3, 4)  # rclone: directory not found, file not found
BATCH_EXTRA_BYTES = 16 * 1024**2  # a batched azure download may fetch this much of other files, no more


def _esc(text):
    """Escape fnmatch wildcards (az batch commands match blob names with fnmatch)."""
    return re.sub(r"([*?\[])", r"[\1]", text)


def _common_suffix(texts):
    return os.path.commonprefix([t[::-1] for t in texts])[::-1]


def _not_found(text):
    t = (text or "").lower()
    return "not found" in t or "blobnotfound" in t or "does not exist" in t or "specified blob" in t


class AzureSide:
    def __init__(self, s, side):
        self.s = s
        self.container = s[side]
        self.prefix = s["prefix"]
        self.backend = "azure-blob"

    def blob(self, rel):
        return "%s/%s" % (self.prefix, rel) if rel else self.prefix

    def where(self, rel=""):
        return "%s/%s" % (self.container, self.blob(rel))

    def _storage(self):
        return ["--account-name", self.s["account"], "--auth-mode", "login"]

    def put_cmd(self, local, rel, overwrite=True):
        cmd = ["az", "storage", "blob", "upload"] + self._storage()
        cmd += ["--container-name", self.container, "--name", self.blob(rel), "--file", local]
        return cmd + [
            "--overwrite",
            "true" if overwrite else "false",
            "--no-progress",
            "--only-show-errors",
            "-o",
            "none",
        ]

    def get_cmd(self, rel, local):
        cmd = ["az", "storage", "blob", "download"] + self._storage()
        cmd += ["--container-name", self.container, "--name", self.blob(rel), "--file", local, "--overwrite", "true"]
        return cmd + ["--no-progress", "--only-show-errors", "-o", "none"]

    def list_cmd(self, rel=""):
        cmd = ["az", "storage", "blob", "list"] + self._storage()
        cmd += ["--container-name", self.container, "--prefix", self.blob(rel) + "/", "--num-results", "*"]
        query = "[].{name:name, size:properties.contentLength, modtime:properties.lastModified}"
        return cmd + ["--query", query, "--only-show-errors", "-o", "json"]

    def put(self, local, rel, timeout, overwrite=True):
        rc, stdout, stderr = run_transfer(self.put_cmd(local, rel, overwrite), timeout)
        return rc == 0, short(stderr or stdout or "rc %s" % rc, 400)

    def get(self, rel, local, timeout):
        """'ok', 'missing', or an error message."""
        os.makedirs(os.path.dirname(os.path.abspath(local)), exist_ok=True)
        rc, stdout, stderr = run_transfer(self.get_cmd(rel, local), timeout)
        if rc == 0:
            return "ok"
        return "missing" if _not_found(stderr or stdout) else short(stderr or stdout or "rc %s" % rc, 400)

    def list(self, rel, timeout):
        """[{'path', 'size', 'modtime'}] under rel (paths relative to rel), or None on an error."""
        rc, stdout, _ = run_transfer(self.list_cmd(rel), timeout)
        if rc != 0:
            return None
        try:
            items = json.loads(stdout or "[]")
        except ValueError:
            return None
        cut = len(self.blob(rel)) + 1
        return [
            {"path": it["name"][cut:], "size": it.get("size"), "modtime": it.get("modtime")}
            for it in items
            if isinstance(it, dict) and it.get("name")
        ]

    def copy_down(self, dest, timeout, keep_existing=True):
        return None  # no single command for this backend; the caller gets files one by one

    def dirs_cmd(self, rel=""):
        cmd = ["az", "storage", "blob", "list"] + self._storage()
        cmd += ["--container-name", self.container, "--prefix", self.blob(rel) + "/", "--delimiter", "/"]
        return cmd + ["--num-results", "*", "--query", "[].name", "--only-show-errors", "-o", "json"]

    def dirs(self, rel, timeout):
        """The folder names one level below rel, or None on an error.

        Asks for a '/' delimiter; when the listing comes back flat anyway, the first path parts are used.
        """
        rc, stdout, _ = run_transfer(self.dirs_cmd(rel), timeout)
        if rc != 0:
            return None
        try:
            names = json.loads(stdout or "[]")
        except ValueError:
            return None
        cut = len(self.blob(rel)) + 1
        found = set()
        for n in names:
            n = n.get("name") if isinstance(n, dict) else n
            if not isinstance(n, str) or len(n) <= cut:
                continue
            rest = n[cut:]
            if "/" in rest:
                found.add(rest.split("/", 1)[0])
        return sorted(found)

    def get_many(self, base, rels, stage, timeout, listed=None):
        """Download base/<rel> for each rel to stage/<rel>. 'ok' or an error message."""
        found, problem = self.get_present(base, rels, stage, timeout, listed)
        if problem:
            return problem
        missing = [r for r in rels if r not in found]
        return "ok" if not missing else "%d file(s) did not arrive, for example %s" % (len(missing), missing[0])

    def batch_pattern(self, base, rels, listed):
        """The download-batch pattern that fetches rels from base, or None when every pattern tried would also
        fetch more than BATCH_EXTRA_BYTES of other files. Tries the files' common ending and common start."""
        sizes = dict((f["path"], int(f.get("size") or 0)) for f in listed if f.get("path"))
        wanted = set(rels)
        tried = []
        suffix = _common_suffix(rels)
        if suffix:
            tried.append(("*" + _esc(suffix), lambda p: p.endswith(suffix)))
        start = os.path.commonprefix(rels)
        tried.append((_esc(start) + "*", lambda p: p.startswith(start)))
        best = None
        for tail, test in tried:
            extra = sum(n for p, n in sizes.items() if test(p) and p not in wanted)
            if extra <= BATCH_EXTRA_BYTES and (best is None or extra < best[1]):
                best = (tail, extra)
        return None if best is None else _esc(self.blob(base)) + "/" + best[0]

    def get_present(self, base, rels, stage, timeout, listed=None):
        """Download the files of rels that exist under base into stage. (set of rels that arrived, problem).

        Several files go in one `az storage blob download-batch` (after one listing, unless listed, the
        files under base, is given); one file, or a pattern that would fetch too much else, goes file by file.
        """
        rels = list(rels)
        if len(rels) > 1 and (listed is not None or len(rels) > 2):
            if listed is None:
                listed = self.list(base, timeout)
            if listed is not None:
                names = set(f["path"] for f in listed if f.get("path"))
                present = [r for r in rels if r in names]
                if not present:
                    return set(), None
                pattern = self.batch_pattern(base, present, listed) if len(present) > 1 else None
                if pattern:
                    return self.download_batch(base, present, pattern, stage, timeout)
                rels = present
        found = set()
        for r in rels:
            got = self.get("%s/%s" % (base, r) if base else r, os.path.join(stage, *r.split("/")), timeout)
            if got == "ok":
                found.add(r)
            elif got != "missing":
                return found, got
        return found, None

    def download_batch_cmd(self, pattern, dest):
        cmd = ["az", "storage", "blob", "download-batch"] + self._storage()
        cmd += ["--source", self.container, "--destination", dest, "--pattern", pattern]
        return cmd + ["--no-progress", "--only-show-errors", "-o", "none"]

    def download_batch(self, base, rels, pattern, stage, timeout):
        dl = os.path.join(stage, ".agent-mirror-batch")
        os.makedirs(dl, exist_ok=True)
        try:
            rc, stdout, stderr = run_transfer(self.download_batch_cmd(pattern, dl), timeout)
            if rc != 0:
                return set(), short(stderr or stdout or "rc %s" % rc, 400)
            found = set()
            for r in rels:
                src = os.path.join(dl, *(self.blob(base) + "/" + r).split("/"))
                if os.path.isfile(src):
                    dest = os.path.join(stage, *r.split("/"))
                    os.makedirs(os.path.dirname(dest), exist_ok=True)
                    os.replace(src, dest)
                    found.add(r)
            return found, None
        finally:
            shutil.rmtree(dl, ignore_errors=True)

    def delete_trees(self, rels, timeout):
        """Delete each namespace: one delete-batch per namespace, because one fnmatch pattern cannot name
        several prefixes without also matching others. (ok, problem)."""
        for rel in rels:
            ok, detail = self.delete_tree(rel, timeout)
            if not ok:
                return False, "%s: %s" % (rel, detail)
        return True, None

    def delete_tree_cmd(self, rel):
        pattern = _esc(self.blob(rel)) + "/*"
        cmd = ["az", "storage", "blob", "delete-batch"] + self._storage()
        return cmd + ["--source", self.container, "--pattern", pattern, "--only-show-errors", "-o", "none"]

    def delete_tree(self, rel, timeout):
        rc, stdout, stderr = run_transfer(self.delete_tree_cmd(rel), timeout)
        return rc == 0, short(stderr or stdout or "rc %s" % rc, 400)

    def cat(self, rel, timeout):
        """The text of a small remote file, or None when it does not exist or cannot be read."""
        tmp = current().mkdtemp("agent-mirror-cat-")
        try:
            path = os.path.join(tmp, "f")
            if self.get(rel, path, timeout) != "ok" or not os.path.isfile(path):
                return None
            with open(path, encoding="utf-8", errors="replace") as fh:
                return fh.read()
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class RcloneSide:
    def __init__(self, s, side):
        self.s = s
        self.root = s[side]
        self.backend = "rclone"

    def where(self, rel=""):
        if not rel:
            return self.root
        if self.root.endswith((":", "/")):
            return self.root + rel
        return self.root + "/" + rel

    def put_cmd(self, local, rel, overwrite=True):
        return ["rclone", "copyto", local, self.where(rel)] + ([] if overwrite else ["--ignore-existing"])

    def get_cmd(self, rel, local):
        return ["rclone", "copyto", self.where(rel), local]

    def list_cmd(self, rel=""):
        return ["rclone", "lsjson", "-R", "--files-only", "--no-mimetype", self.where(rel)]

    def put(self, local, rel, timeout, overwrite=True):
        rc, stdout, stderr = run_transfer(self.put_cmd(local, rel, overwrite), timeout)
        return rc == 0, short(stderr or stdout or "rc %s" % rc, 400)

    def get(self, rel, local, timeout):
        os.makedirs(os.path.dirname(os.path.abspath(local)), exist_ok=True)
        rc, stdout, stderr = run_transfer(self.get_cmd(rel, local), timeout)
        if rc == 0 and os.path.isfile(local):
            return "ok"
        if rc in MISSING_RC or _not_found(stderr or stdout) or rc == 0:
            return "missing"
        return short(stderr or stdout or "rc %s" % rc, 400)

    def list(self, rel, timeout):
        rc, stdout, stderr = run_transfer(self.list_cmd(rel), timeout)
        if rc in MISSING_RC:
            return []
        if rc != 0:
            return None
        try:
            items = json.loads(stdout or "[]")
        except ValueError:
            return None
        return [
            {"path": it.get("Path"), "size": it.get("Size"), "modtime": it.get("ModTime")}
            for it in items
            if isinstance(it, dict) and it.get("Path") and not it.get("IsDir")
        ]

    def dirs_cmd(self, rel=""):
        return ["rclone", "lsjson", "--dirs-only", "--no-mimetype", self.where(rel)]

    def dirs(self, rel, timeout):
        rc, stdout, _ = run_transfer(self.dirs_cmd(rel), timeout)
        if rc in MISSING_RC:
            return []
        if rc != 0:
            return None
        try:
            items = json.loads(stdout or "[]")
        except ValueError:
            return None
        return sorted(it.get("Path") for it in items if isinstance(it, dict) and it.get("Path") and it.get("IsDir"))

    def copy_listed(self, base, rels, stage, timeout):
        """`rclone copy --files-from` of rels under base into stage (rclone skips missing ones). Problem or None."""
        os.makedirs(stage, exist_ok=True)
        fd, listfile = current().mkstemp("agent-mirror-files-", ".txt")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write("\n".join(rels) + "\n")
            cmd = ["rclone", "copy", self.where(base), stage, "--files-from", listfile]
            rc, stdout, stderr = run_transfer(cmd, timeout)
        finally:
            try:
                os.unlink(listfile)
            except OSError:
                pass
        if rc == 0 or rc in MISSING_RC:
            return None  # a folder that does not exist yet holds none of them
        return short(stderr or stdout or "rc %s" % rc, 400)

    def get_many(self, base, rels, stage, timeout, listed=None):
        """One `rclone copy --files-from` for every rel under base, into stage. 'ok' or an error message."""
        if not rels:
            return "ok"
        problem = self.copy_listed(base, rels, stage, timeout)
        if problem:
            return problem
        missing = [r for r in rels if not os.path.isfile(os.path.join(stage, *r.split("/")))]
        return "ok" if not missing else "%d file(s) did not arrive, for example %s" % (len(missing), missing[0])

    def get_present(self, base, rels, stage, timeout, listed=None):
        """One `rclone copy --files-from` for rels under base (missing ones are skipped). (arrived, problem)."""
        if not rels:
            return set(), None
        problem = self.copy_listed(base, rels, stage, timeout)
        return set(r for r in rels if os.path.isfile(os.path.join(stage, *r.split("/")))), problem

    @staticmethod
    def tree_filters(rels):
        """--filter flags naming exactly these top-level folders: anchored at the root, with the slash."""
        flags = []
        for rel in rels:
            flags += ["--filter", "+ /%s/**" % rel]
        return flags + ["--filter", "- **"]

    def delete_trees_cmd(self, rels):
        return ["rclone", "delete", self.where("")] + self.tree_filters(rels)

    def rmdirs_cmd(self, rels):
        return ["rclone", "rmdirs", self.where(""), "--leave-root"] + self.tree_filters(rels)

    def delete_trees(self, rels, timeout):
        """Delete these namespaces in one call, then remove the folders that are left empty, with the same
        filters (no file is removed by that second call). (ok, problem)."""
        if not rels:
            return True, None
        rc, stdout, stderr = run_transfer(self.delete_trees_cmd(rels), timeout)
        if rc != 0 and rc not in MISSING_RC:
            return False, short(stderr or stdout or "rc %s" % rc, 400)
        rc, stdout, stderr = run_transfer(self.rmdirs_cmd(rels), timeout)
        if rc != 0 and rc not in MISSING_RC:
            return False, "the files are deleted, but removing empty folders failed: %s" % short(stderr or stdout, 300)
        return True, None

    def delete_tree_cmd(self, rel):
        return ["rclone", "purge", self.where(rel)]

    def delete_tree(self, rel, timeout):
        rc, stdout, stderr = run_transfer(self.delete_tree_cmd(rel), timeout)
        return rc == 0, short(stderr or stdout or "rc %s" % rc, 400)

    def copy_down_cmd(self, dest, keep_existing=True):
        return ["rclone", "copy", self.root, dest] + (["--ignore-existing"] if keep_existing else [])

    def copy_down(self, dest, timeout, keep_existing=True):
        rc, stdout, stderr = run_transfer(self.copy_down_cmd(dest, keep_existing), timeout)
        return rc == 0, short(stderr or stdout or "rc %s" % rc, 400)

    def cat(self, rel, timeout):
        rc, stdout, _ = run_transfer(["rclone", "cat", self.where(rel)], timeout)
        return stdout if rc == 0 else None


def side_for(s, side):
    return AzureSide(s, side) if s["backend"] == "azure-blob" else RcloneSide(s, side)
