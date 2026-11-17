"""Windows checkouts use CRLF, the store keeps LF: the same skill must compare as identical.
A folder without SKILL.md in a skills folder is not a skill."""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from helpers import ToolTest, setUpModule, write  # noqa: E402,F401


class LineEndingTest(ToolTest):
    def put_skill(self, root, name, text, binary=None):
        folder = os.path.join(root, name)
        os.makedirs(folder, exist_ok=True)
        with open(os.path.join(folder, "SKILL.md"), "wb") as fh:
            fh.write(text)
        if binary is not None:
            with open(os.path.join(folder, "blob.bin"), "wb") as fh:
                fh.write(binary)

    def plan_case(self, name):
        import json

        rc, out = self.run_tool("plan", "--json", "--agents", "claude")
        self.assertEqual(rc, 0, out)
        data = json.loads(out[out.index("{"):])
        for unit in data["units"]:
            if unit["id"] == "skill:%s" % name:
                return unit["case"]
        return None

    def test_crlf_and_lf_text_compare_identical(self):
        body = b"---\nname: s\ndescription: d\n---\nline one\nline two\n"
        self.put_skill(self.s("skills"), "same", body)
        self.put_skill(self.h(".agents", "skills"), "same", body.replace(b"\n", b"\r\n"))
        self.assertEqual(self.plan_case("same"), "identical")

    def test_real_text_change_still_differs(self):
        self.put_skill(self.s("skills"), "changed", b"---\nname: s\ndescription: d\n---\nA\n")
        self.put_skill(self.h(".agents", "skills"), "changed", b"---\r\nname: s\r\ndescription: d\r\n---\r\nB\r\n")
        self.assertEqual(self.plan_case("changed"), "reconcile")

    def test_binary_files_are_compared_byte_for_byte(self):
        body = b"---\nname: s\ndescription: d\n---\nx\n"
        self.put_skill(self.s("skills"), "bin", body, binary=b"\x00\x01\r\n\x02")
        self.put_skill(self.h(".agents", "skills"), "bin", body, binary=b"\x00\x01\n\x02")
        self.assertEqual(self.plan_case("bin"), "reconcile")

    def test_folder_without_skill_md_is_not_a_skill(self):
        os.makedirs(self.h(".agents", "skills", "eval-workspace", "iteration-1"))
        write(self.h(".agents", "skills", "eval-workspace", "iteration-1", "notes.txt"), "x\n")
        self.assertIsNone(self.plan_case("eval-workspace"))


if __name__ == "__main__":
    unittest.main()
