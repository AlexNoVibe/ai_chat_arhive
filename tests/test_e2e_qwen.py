import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "ai_chat_archive.py"
SRC = ROOT / "example" / "qwen_example.json"
EXP_CHATS = 1
EXP_MSGS = 2
STATS = "1 chats &middot; 2 messages"
DOM_NEEDLES = [
    "Example Title for Article",
    "Example question",
    "I am carefully examining",
    "Example File Name.docx",
]
INDEX_NEEDLES = [
    '"t": "example question"',
    "i am carefully examining",
    '"fmt": "qwen"',
]
STDOUT_NEEDLES = ["[qwen]", "1 chat(s), 2 msg(s)"]


class TestQwenE2E(unittest.TestCase):
    def test_generate_archive(self):
        tmp = tempfile.mkdtemp(prefix="aia_e2e_qwen_")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        shutil.copyfile(SRC, os.path.join(tmp, SRC.name))
        out = os.path.join(tmp, "out.html")

        proc = subprocess.run(
            [sys.executable, str(SCRIPT), tmp, "-o", out, "-v"],
            capture_output=True, text=True, timeout=120)
        self.assertEqual(proc.returncode, 0, proc.stderr[-2000:])
        self.assertTrue(os.path.isfile(out), "out.html was not created")

        # Check external search_index.json was created
        search_index_path = os.path.splitext(out)[0] + "_search_index.js"
        self.assertTrue(os.path.isfile(search_index_path), "search_index.js was not created")

        with open(out, encoding="utf-8") as fh:
            html = fh.read()

        m = re.search(r'id="sidebar-stats">([^<]*)<', html)
        self.assertIsNotNone(m, "sidebar-stats line not found")
        self.assertEqual(m.group(1), STATS, "sidebar stats mismatch")
        self.assertEqual(html.count("<section"), EXP_CHATS, "section count")
        self.assertEqual(html.count('class="sidebar-item"'), EXP_CHATS, "sidebar item count")
        self.assertEqual(html.count('class="message msg-'), EXP_MSGS, "message div count")
        for needle in DOM_NEEDLES:
            self.assertIn(needle, html, f"DOM lacks {needle!r}")

        # Verify external search_index.json
        with open(search_index_path, encoding="utf-8") as fh:
            search_text = fh.read()
            self.assertTrue(search_text.startswith("window.SEARCH_INDEX = "), "search_index.js should start with window.SEARCH_INDEX =")
            search_data = json.loads(search_text.replace("window.SEARCH_INDEX = ", "").rstrip(";"))
            
        self.assertEqual(len(search_data), EXP_CHATS, "search index entry count")
        search_blob = json.dumps(search_data)
        for needle in INDEX_NEEDLES:
            self.assertIn(needle, search_blob, f"index lacks {needle!r}")

        # Verify inline SEARCH_INDEX is not present (or is null)
        self.assertNotIn('var SEARCH_INDEX = [', html, "inline SEARCH_INDEX array data should not be in HTML")

        for needle in STDOUT_NEEDLES:
            self.assertIn(needle, proc.stdout, f"stdout lacks {needle!r}")


if __name__ == "__main__":
    unittest.main()
