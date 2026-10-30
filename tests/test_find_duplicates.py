import os
import sys
import json
import shutil
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from find_duplicate_archives import (
    load_archive,
    analyze_duplicates,
    collect_archives,
    is_chat_covered,
)


class TestFindDuplicateArchives(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp(prefix="test_find_dup_")

    def tearDown(self):
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_complete_subset_duplicate(self):
        """Old archive has 2 chats, new archive has 3 chats (including the 2 old ones)."""
        chat1 = {
            "uuid": "chat-uuid-1",
            "name": "Chat 1",
            "chat_messages": [
                {"sender": "human", "text": "Hello 1"},
                {"sender": "assistant", "text": "Hi 1"}
            ]
        }
        chat2 = {
            "uuid": "chat-uuid-2",
            "name": "Chat 2",
            "chat_messages": [
                {"sender": "human", "text": "Hello 2"},
                {"sender": "assistant", "text": "Hi 2"}
            ]
        }
        chat3 = {
            "uuid": "chat-uuid-3",
            "name": "Chat 3",
            "chat_messages": [
                {"sender": "human", "text": "Hello 3"},
                {"sender": "assistant", "text": "Hi 3"}
            ]
        }

        old_file = os.path.join(self.tmp_dir, "old_archive.json")
        new_file = os.path.join(self.tmp_dir, "new_archive.json")

        with open(old_file, "w", encoding="utf-8") as f:
            json.dump([chat1, chat2], f)

        with open(new_file, "w", encoding="utf-8") as f:
            json.dump([chat1, chat2, chat3], f)

        archives = collect_archives(self.tmp_dir)
        self.assertEqual(len(archives), 2)

        full_dups, partials = analyze_duplicates(archives)

        self.assertEqual(len(full_dups), 1)
        self.assertEqual(full_dups[0].archive.abs_path, str(Path(old_file).resolve()))
        self.assertEqual(full_dups[0].keeper.abs_path, str(Path(new_file).resolve()))
        self.assertEqual(full_dups[0].matched_count, 2)
        self.assertEqual(full_dups[0].total_count, 2)
        self.assertFalse(full_dups[0].is_exact_copy)

    def test_identical_archives(self):
        """Two identical archives: exactly one must be marked for deletion, never both."""
        chat1 = {
            "uuid": "chat-uuid-1",
            "name": "Chat 1",
            "chat_messages": [{"sender": "human", "text": "Hello 1"}, {"sender": "assistant", "text": "Hi 1"}]
        }
        file1 = os.path.join(self.tmp_dir, "copy1.json")
        file2 = os.path.join(self.tmp_dir, "copy2.json")

        with open(file1, "w", encoding="utf-8") as f:
            json.dump([chat1], f)
        with open(file2, "w", encoding="utf-8") as f:
            json.dump([chat1], f)

        archives = collect_archives(self.tmp_dir)
        full_dups, _ = analyze_duplicates(archives)

        self.assertEqual(len(full_dups), 1, "Only one of the two identical files should be marked for deletion")
        self.assertTrue(full_dups[0].is_exact_copy)

    def test_partial_overlap_not_deleted(self):
        """Archives with partial overlap must NOT be marked for deletion."""
        chat1 = {"uuid": "chat-1", "name": "Chat 1", "chat_messages": [{"sender": "human", "text": "A"}]}
        chat2 = {"uuid": "chat-2", "name": "Chat 2", "chat_messages": [{"sender": "human", "text": "B"}]}
        chat3 = {"uuid": "chat-3", "name": "Chat 3", "chat_messages": [{"sender": "human", "text": "C"}]}

        file_a = os.path.join(self.tmp_dir, "a.json")
        file_b = os.path.join(self.tmp_dir, "b.json")

        with open(file_a, "w", encoding="utf-8") as f:
            json.dump([chat1, chat2], f)
        with open(file_b, "w", encoding="utf-8") as f:
            json.dump([chat2, chat3], f)

        archives = collect_archives(self.tmp_dir)
        full_dups, partials = analyze_duplicates(archives)

        self.assertEqual(len(full_dups), 0, "No file should be marked for deletion when each has unique records")
        self.assertGreater(len(partials), 0, "Partial overlap should be detected")

    def test_continued_conversation_coverage(self):
        """A newer archive has the same conversation with more messages; covers the older one."""
        old_chat = {
            "uuid": "conv-1",
            "name": "Discussion",
            "chat_messages": [
                {"sender": "human", "text": "Question 1"},
                {"sender": "assistant", "text": "Answer 1"}
            ]
        }
        new_chat = {
            "uuid": "conv-1",
            "name": "Discussion",
            "chat_messages": [
                {"sender": "human", "text": "Question 1"},
                {"sender": "assistant", "text": "Answer 1"},
                {"sender": "human", "text": "Question 2"},
                {"sender": "assistant", "text": "Answer 2"}
            ]
        }

        old_file = os.path.join(self.tmp_dir, "v1.json")
        new_file = os.path.join(self.tmp_dir, "v2.json")

        with open(old_file, "w", encoding="utf-8") as f:
            json.dump([old_chat], f)
        with open(new_file, "w", encoding="utf-8") as f:
            json.dump([new_chat], f)

        archives = collect_archives(self.tmp_dir)
        full_dups, _ = analyze_duplicates(archives)

        self.assertEqual(len(full_dups), 1)
        self.assertEqual(full_dups[0].archive.abs_path, str(Path(old_file).resolve()))
        self.assertEqual(full_dups[0].keeper.abs_path, str(Path(new_file).resolve()))

    def test_different_messages_same_id_not_covered(self):
        """If two chats share the same ID but have completely different messages, they MUST NOT match."""
        chat_a = {
            "uuid": "shared-uuid-123",
            "name": "Chat A",
            "chat_messages": [{"sender": "human", "text": "Original question"}]
        }
        chat_b = {
            "uuid": "shared-uuid-123",
            "name": "Chat B",
            "chat_messages": [{"sender": "human", "text": "Completely different question"}]
        }

        file_a = os.path.join(self.tmp_dir, "a.json")
        file_b = os.path.join(self.tmp_dir, "b.json")

        with open(file_a, "w", encoding="utf-8") as f:
            json.dump([chat_a], f)
        with open(file_b, "w", encoding="utf-8") as f:
            json.dump([chat_b], f)

        archives = collect_archives(self.tmp_dir)
        full_dups, _ = analyze_duplicates(archives)
        self.assertEqual(len(full_dups), 0, "Chats with different messages must not be marked as duplicates even with same ID")

    def test_cli_execution_with_backup_and_permanent(self):
        """Test CLI: dry-run report, safe backup move, cancellation on bad input, and permanent deletion."""
        import subprocess

        chat1 = {"uuid": "c-1", "name": "Topic 1", "chat_messages": [{"sender": "human", "text": "A"}]}
        chat2 = {"uuid": "c-2", "name": "Topic 2", "chat_messages": [{"sender": "human", "text": "B"}]}

        old_file = os.path.join(self.tmp_dir, "old.json")
        new_file = os.path.join(self.tmp_dir, "new.json")

        with open(old_file, "w", encoding="utf-8") as f:
            json.dump([chat1], f)
        with open(new_file, "w", encoding="utf-8") as f:
            json.dump([chat1, chat2], f)

        script_path = str(ROOT / "find_duplicate_archives.py")
        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"

        # 1. Dry run (default)
        proc = subprocess.run(
            [sys.executable, script_path, self.tmp_dir],
            capture_output=True,
            text=True,
            encoding="utf-8",
            env=env,
        )
        self.assertEqual(proc.returncode, 0)
        self.assertIn("МОЖЕТЕ УДАЛИТЬ ЭТО", proc.stdout)
        self.assertTrue(os.path.exists(old_file))

        # 2. Rejection when incorrect confirmation word is provided
        proc_cancel = subprocess.run(
            [sys.executable, script_path, self.tmp_dir, "--delete"],
            input="random_text\n",
            capture_output=True,
            text=True,
            encoding="utf-8",
            env=env,
        )
        self.assertEqual(proc_cancel.returncode, 0)
        self.assertIn("Операция отменена", proc_cancel.stdout)
        self.assertTrue(os.path.exists(old_file))

        # 3. Safe backup execution (moves to _duplicates_backup)
        proc_backup = subprocess.run(
            [sys.executable, script_path, self.tmp_dir, "--delete", "--yes"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            env=env,
        )
        self.assertEqual(proc_backup.returncode, 0)
        self.assertFalse(os.path.exists(old_file), "old_file should have been moved from original location")
        backup_file = os.path.join(self.tmp_dir, "_duplicates_backup", "old.json")
        self.assertTrue(os.path.exists(backup_file), "old_file should be preserved in _duplicates_backup")
        self.assertTrue(os.path.exists(new_file), "new_file must remain untouched")

        # 4. Permanent deletion with keyword confirmation
        # Restore old_file
        shutil.move(backup_file, old_file)
        proc_perm = subprocess.run(
            [sys.executable, script_path, self.tmp_dir, "--delete", "--permanent"],
            input="УДАЛИТЬ\n",
            capture_output=True,
            text=True,
            encoding="utf-8",
            env=env,
        )
        self.assertEqual(proc_perm.returncode, 0)
        self.assertFalse(os.path.exists(old_file), "old_file should be permanently deleted")
        self.assertTrue(os.path.exists(new_file), "new_file must remain untouched")


if __name__ == "__main__":
    unittest.main()
