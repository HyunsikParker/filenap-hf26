import json
import os
import tempfile
import unittest
from pathlib import Path

from filenap.engine import Engine, Problem
from filenap.planner import Planner


class Interrupted(RuntimeError):
    pass


class FileOperations(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "inbox"
        self.root.mkdir()
        (self.root / "a.txt").write_text("Receipt for paper. Paid $12.")
        (self.root / "b.txt").write_text("Project note about the shelf.")
        self.engine = Engine(self.root)

    def tearDown(self):
        self.engine.close()
        self.temp.cleanup()

    def plan(self, engine=None):
        engine = engine or self.engine
        files = engine.scan()["files"]
        proposal = {"items": [{"id": f["id"], "decision": "move", "category": "Receipts",
                               "reason": "Test proposal, not a model run.", "evidence": f["preview"][:10]} for f in files]}
        return engine.store_plan(files, proposal, ["Receipts"], {"test_double": True})

    def test_only_approved_file_moves_and_undo_restores_exact_bytes(self):
        plan = self.plan()
        batch = self.engine.apply(plan["id"], [plan["items"][0]["id"]])
        self.assertEqual(batch["status"], "complete")
        self.assertFalse((self.root / "a.txt").exists())
        self.assertTrue((self.root / "b.txt").exists())
        self.assertEqual((self.root / "Sorted/Receipts/a.txt").read_bytes(), b"Receipt for paper. Paid $12.")
        self.assertEqual(self.engine.undo(batch["id"])["status"], "undone")
        self.assertEqual((self.root / "a.txt").read_bytes(), b"Receipt for paper. Paid $12.")

    def test_changed_source_refuses_entire_batch_before_moving(self):
        plan = self.plan()
        (self.root / "b.txt").write_text("A new version that was not approved.")
        with self.assertRaisesRegex(Problem, "changed since planning"):
            self.engine.apply(plan["id"], [f["id"] for f in plan["items"]])
        self.assertTrue((self.root / "a.txt").exists())
        self.assertEqual((self.root / "b.txt").read_text(), "A new version that was not approved.")
        self.assertEqual(self.engine.batches(), [])

    def test_destination_collision_preserves_both_files(self):
        plan = self.plan()
        dest = self.root / "Sorted/Receipts"
        dest.mkdir(parents=True)
        (dest / "a.txt").write_text("Already here.")
        with self.assertRaisesRegex(Problem, "already exists"):
            self.engine.apply(plan["id"], [plan["items"][0]["id"]])
        self.assertEqual((dest / "a.txt").read_text(), "Already here.")
        self.assertEqual((self.root / "a.txt").read_text(), "Receipt for paper. Paid $12.")

    def test_model_cannot_invent_file_ids_or_folders(self):
        files = self.engine.scan()["files"]
        for field, value in [("id", "not-a-file"), ("category", "../../outside")]:
            proposal = {"items": [{"id": f["id"], "decision": "move", "category": "Receipts",
                                   "reason": "Test", "evidence": "Receipt"} for f in files]}
            proposal["items"][0][field] = value
            with self.assertRaises(Problem):
                self.engine.store_plan(files, proposal, ["Receipts"], {})
        self.assertTrue((self.root / "a.txt").exists())

    def test_false_quote_is_held_for_review_and_cannot_be_applied(self):
        files = self.engine.scan()["files"][:1]
        plan = self.engine.store_plan(files, {"items": [{"id": files[0]["id"], "decision": "move", "category": "Receipts",
                                                        "reason": "Test", "evidence": "Invented quote"}]}, ["Receipts"], {})
        self.assertEqual(plan["items"][0]["decision"], "review")
        with self.assertRaises(Problem):
            self.engine.apply(plan["id"], [files[0]["id"]])

    def test_symlink_input_is_not_followed(self):
        outside = Path(self.temp.name) / "private.txt"
        outside.write_text("Outside folder.")
        (self.root / "link.txt").symlink_to(outside)
        scan = self.engine.scan()
        self.assertNotIn("link.txt", [f["name"] for f in scan["files"]])
        self.assertEqual(scan["skipped"][0]["name"], "link.txt")
        self.assertEqual(outside.read_text(), "Outside folder.")

    def test_quote_must_be_within_the_excerpt_actually_sent_to_model(self):
        (self.root / "long.txt").write_text("x" * 1100 + "Unseen unique marker.")
        item = next(f for f in self.engine.scan()["files"] if f["name"] == "long.txt")
        plan = self.engine.store_plan([item], {"items": [{"id": item["id"], "decision": "move",
                 "category": "Receipts", "reason": "Test double", "evidence": "Unseen unique marker."}]}, ["Receipts"], {})
        self.assertEqual(plan["items"][0]["decision"], "review")

    def test_symlink_destination_refused(self):
        plan = self.plan()
        outside = Path(self.temp.name) / "outside"
        outside.mkdir()
        (self.root / "Sorted").symlink_to(outside, target_is_directory=True)
        with self.assertRaises(OSError):
            self.engine.apply(plan["id"], [plan["items"][0]["id"]])
        self.assertEqual(list(outside.iterdir()), [])
        self.assertTrue((self.root / "a.txt").exists())

    def test_interrupted_move_is_reconciled_without_duplicate_execution(self):
        plan = self.plan()
        def fail(*_):
            raise Interrupted("Simulated stop after atomic move, before journal acknowledgement.")
        self.engine.fault_hook = fail
        with self.assertRaises(Interrupted):
            self.engine.apply(plan["id"], [f["id"] for f in plan["items"]])
        batch_id = self.engine.batches()[0]["id"]
        self.engine.close()
        self.engine = Engine(self.root)
        self.assertEqual(self.engine.batch(batch_id)["moves"][0]["status"], "moved")
        done = self.engine.resume(batch_id)
        self.assertEqual(done["status"], "complete")
        self.assertEqual(len(list((self.root / "Sorted/Receipts").iterdir())), 2)
        self.assertEqual(self.engine.resume(batch_id)["status"], "complete")
        self.assertEqual(self.engine.undo(batch_id)["status"], "undone")

    def test_edited_destination_is_not_undone_until_original_bytes_restored(self):
        plan = self.plan()
        batch = self.engine.apply(plan["id"], [plan["items"][0]["id"]])
        dest = self.root / "Sorted/Receipts/a.txt"
        dest.write_text("Edited after moving.")
        self.assertEqual(self.engine.undo(batch["id"])["status"], "undo_partial")
        self.assertEqual(dest.read_text(), "Edited after moving.")
        self.assertFalse((self.root / "a.txt").exists())
        dest.write_text("Receipt for paper. Paid $12.")
        self.assertEqual(self.engine.undo(batch["id"])["status"], "undone")

    def test_undo_collision_preserves_new_source_and_supports_retry(self):
        plan = self.plan()
        batch = self.engine.apply(plan["id"], [plan["items"][0]["id"]])
        (self.root / "a.txt").write_text("New file with the old name.")
        result = self.engine.undo(batch["id"])
        self.assertEqual(result["status"], "undo_partial")
        self.assertEqual((self.root / "a.txt").read_text(), "New file with the old name.")
        self.assertTrue((self.root / "Sorted/Receipts/a.txt").exists())
        (self.root / "a.txt").unlink()
        self.assertEqual(self.engine.undo(batch["id"])["status"], "undone")
        self.assertEqual((self.root / "a.txt").read_text(), "Receipt for paper. Paid $12.")

    def test_restart_after_undo_move_reconciles_restored_file(self):
        plan = self.plan()
        batch = self.engine.apply(plan["id"], [plan["items"][0]["id"]])
        def fail(*_):
            raise Interrupted("Simulated stop after undo move.")
        self.engine.fault_hook = fail
        with self.assertRaises(Interrupted):
            self.engine.undo(batch["id"])
        self.engine.close()
        self.engine = Engine(self.root)
        self.assertEqual(self.engine.batch(batch["id"])["moves"][0]["status"], "undone")
        self.assertEqual((self.root / "a.txt").read_text(), "Receipt for paper. Paid $12.")

    def test_cloud_endpoint_is_rejected(self):
        for url in ["https://api.example.com", "http://127.0.0.1@evil.example", "http://127.0.0.1/path", "http://localhost?x=1"]:
            with self.assertRaises(Problem):
                Planner(url)


if __name__ == "__main__":
    unittest.main()
