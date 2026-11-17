"""This repo is public: no private names, real hosts, email addresses or token-shaped strings anywhere.

The forbidden words are kept here only as SHA-256 digests (with their length),
so this file does not contain them. Every lower-cased window of each length in
every text file is hashed and compared. To check more words locally without
committing them, list them one per line in a file and point the environment
variable AGENT_MIRROR_EXTRA_FORBIDDEN at it.
"""

import hashlib
import os
import re
import unittest

from helpers import setUpModule  # noqa: F401 (the sandbox guard runs first)
from helpers import REPO

# (length, sha256 of the lower-case word): words the owner listed as private
FORBIDDEN_DIGESTS = (
    (9, "9154cb98979477ce4ee50d15dbe34668ced777998d66f1aeb2360b6589c723e3"),
    (5, "242a3573076b96f3bbe5184afea589fa8de48644b6ac341f5394428954a3b1db"),
    (5, "52a39733bfab90ad7c9b874cf0b8254a5c2857549126341f198c39df2a343c0d"),
    (11, "2249fdeace9ca42e3cf64e00ca6a115771c4c4ca926643764f58bfc4e47a8591"),
    (6, "bc67fa529f5cf6f4f794ce80ca7a3628e33e68ededdeea84b9536bdf1dbf8eae"),
    (8, "6eb1d2f73a2122c289cbd525deeae6c91ad414c6399d09865dadce45020f4dd3"),
    (17, "5f1bae264986ca01d483d969371b81307a5823dbdb14f740d37b93cefd3b7848"),
    (7, "6b5fdfc7609c16bb36b41b92bbab7972e2c21dc98e9803767f6f9a8d3261cf21"),
    (7, "d0efd5a1a4b5be745caa143e894692dccbb21f7a5ed57ffefd95065ddfafffee"),
    (5, "8641741b52ebabd5aa259ed42aa7bead2a7663bba4330ff09aa01d01f2ffd48f"),
)
EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")
EMAIL_ALLOWED = re.compile(r"^(git@github\.com|[^@]+@example\.(com|org|net))$")
TOKENS = [
    re.compile(r"sk-[A-Za-z0-9]{20,}"),
    re.compile(r"eyJ[A-Za-z0-9_-]{20,}"),
    re.compile(r"ghp_[A-Za-z0-9]{30,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{30,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
]
SKIP_DIRS = {".git", "__pycache__"}


def digests():
    found = {}
    for n, d in FORBIDDEN_DIGESTS:
        found.setdefault(n, set()).add(d)
    extra = os.environ.get("AGENT_MIRROR_EXTRA_FORBIDDEN")
    if extra and os.path.isfile(extra):
        with open(extra, encoding="utf-8") as fh:
            for word in fh.read().split():
                w = word.strip().lower()
                found.setdefault(len(w), set()).add(hashlib.sha256(w.encode("utf-8")).hexdigest())
    return found


def forbidden_hits(text, wanted):
    """Lengths of forbidden words found in text (case-insensitive)."""
    low = text.lower().encode("utf-8")
    hits = []
    for n, ds in wanted.items():
        for i in range(0, max(0, len(low) - n + 1)):
            if hashlib.sha256(low[i : i + n]).hexdigest() in ds:
                hits.append((n, i))
                break
    return hits


def repo_files():
    for root, dirs, files in os.walk(REPO):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for f in files:
            path = os.path.join(root, f)
            if os.path.islink(path):
                continue
            try:
                with open(path, encoding="utf-8") as fh:
                    yield os.path.relpath(path, REPO), fh.read()
            except (UnicodeDecodeError, OSError):
                continue


class PublicSafetyTest(unittest.TestCase):
    def test_hash_scan_finds_a_planted_word(self):
        word = "planted-" + "word"
        wanted = {len(word): {hashlib.sha256(word.encode()).hexdigest()}}
        self.assertEqual(forbidden_hits("abc PLANTED-WORD xyz", wanted), [(len(word), 4)])
        self.assertEqual(forbidden_hits("nothing here", wanted), [])

    def test_no_forbidden_strings(self):
        wanted = digests()
        hits = []
        for rel, text in repo_files():
            for n, pos in forbidden_hits(rel + "\n" + text, wanted):
                hits.append("%s: a forbidden word (%d characters) near offset %d" % (rel, n, pos - len(rel) - 1))
            for m in EMAIL.finditer(text):
                if not EMAIL_ALLOWED.match(m.group(0)):
                    hits.append("%s: email-like %s" % (rel, m.group(0)))
            for rx in TOKENS:
                if rx.search(text):
                    hits.append("%s: token-shaped string (%s)" % (rel, rx.pattern))
        self.assertEqual(hits, [])

    def test_no_em_dash(self):
        dash = chr(0x2014)
        hits = [rel for rel, text in repo_files() if dash in text]
        self.assertEqual(hits, [])


if __name__ == "__main__":
    unittest.main()
