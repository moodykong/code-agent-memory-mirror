"""One side of the box (inbound or outbound) as a small set of file operations.

Archives, receive and the archive index need the same few operations on both
backends: put one file, get one file, read a small file, list everything under
a path. Paths here are relative to the side's root (azure: <container>/<prefix>/,
rclone: the configured remote path).
"""

import json
import os
import shutil

from agent_mirror.box.common import run_transfer
from agent_mirror.platforms import current
from agent_mirror.util import short

MISSING_RC = (3, 4)  # rclone: directory not found, file not found


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
