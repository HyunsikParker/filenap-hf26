from __future__ import annotations

import ctypes
import errno
import hashlib
import json
import os
import re
import sqlite3
import stat
import sys
import threading
import time
import uuid
from pathlib import Path


class Problem(Exception):
    pass


def now():
    return time.time()


def move_exclusive(source_fd, source, target_fd, target):
    """Atomically move one directory entry, refusing an existing destination."""
    libc = ctypes.CDLL(None, use_errno=True)
    if sys.platform == "darwin":
        function, flag = libc.renameatx_np, 4
    elif sys.platform.startswith("linux") and hasattr(libc, "renameat2"):
        function, flag = libc.renameat2, 1
    else:
        raise Problem("This system does not provide an atomic move without replacement.")
    function.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    function.restype = ctypes.c_int
    if function(source_fd, os.fsencode(source), target_fd, os.fsencode(target), flag):
        code = ctypes.get_errno()
        if code == errno.EEXIST:
            raise Problem("The destination already exists. Both files were left in place.")
        raise OSError(code, os.strerror(code))


def safe_category(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9 _-]{0,39}", value):
        raise Problem("Folder names must start with a letter and use letters, numbers, spaces, - or _.")
    return value


class Engine:
    max_file_bytes = 8 * 1024 * 1024
    max_scan_bytes = 128 * 1024 * 1024
    max_batch = 16

    def __init__(self, folder, fault_hook=None):
        self.folder = Path(folder).resolve(strict=True)
        self.root_fd = os.open(self.folder, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        self.lock = threading.RLock()
        self.fault_hook = fault_hook
        self.state_fd = self._directory(self.root_fd, ".filenap")
        state = self.folder / ".filenap" / "state.sqlite3"
        if state.is_symlink():
            raise Problem("The state file must not be a symbolic link.")
        self.db = sqlite3.connect(state, check_same_thread=False)
        application_id = self.db.execute("PRAGMA application_id").fetchone()[0]
        tables = self.db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        if application_id not in {0, 0x464E4150} or (application_id == 0 and tables):
            self.db.close()
            os.close(self.state_fd)
            os.close(self.root_fd)
            raise Problem("The state file belongs to another application. It was not changed.")
        os.chmod(state, 0o600)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            PRAGMA application_id=1179533648;
            PRAGMA journal_mode=WAL;
            PRAGMA synchronous=FULL;
            CREATE TABLE IF NOT EXISTS plans (
                id TEXT PRIMARY KEY, created REAL, payload TEXT NOT NULL, status TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS batches (
                id TEXT PRIMARY KEY, plan_id TEXT, created REAL, status TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS moves (
                id TEXT PRIMARY KEY, batch_id TEXT, source TEXT, category TEXT,
                digest TEXT, status TEXT NOT NULL, error TEXT
            );
            CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY, created REAL, status TEXT, result TEXT, error TEXT
            );
        """)
        self.db.execute("UPDATE jobs SET status='interrupted', error=? WHERE status='running'",
                        ("Planning stopped when the app closed. You can start a new plan.",))
        self.db.commit()
        self.recover()

    def close(self):
        self.db.close()
        os.close(self.state_fd)
        os.close(self.root_fd)

    def _directory(self, parent, name):
        try:
            os.mkdir(name, 0o700, dir_fd=parent)
        except FileExistsError:
            pass
        return os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)

    def _target(self, category):
        safe_category(category)
        sorted_fd = self._directory(self.root_fd, "Sorted")
        try:
            return self._directory(sorted_fd, category)
        finally:
            os.close(sorted_fd)

    def _read(self, directory, name):
        if not name or "/" in name or "\0" in name or name in {".", ".."}:
            raise Problem("Invalid file name.")
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        try:
            before = os.fstat(fd)
            if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
                raise Problem("Special files and hard links need manual review.")
            if before.st_size > self.max_file_bytes:
                raise Problem("This file exceeds the 8 MB scan limit.")
            parts = []
            remaining = self.max_file_bytes + 1
            while remaining:
                part = os.read(fd, min(65536, remaining))
                if not part:
                    break
                parts.append(part)
                remaining -= len(part)
            content = b"".join(parts)
            after = os.fstat(fd)
            if len(content) > self.max_file_bytes:
                raise Problem("This file grew beyond the scan limit.")
            if (before.st_ino, before.st_size, before.st_mtime_ns) != (after.st_ino, after.st_size, after.st_mtime_ns):
                raise Problem("The file changed while it was being read.")
            digest = hashlib.sha256(content).hexdigest()
            try:
                preview = content.decode("utf-8")
                if "\0" in preview:
                    preview = ""
            except UnicodeDecodeError:
                preview = ""
            return {"id": hashlib.sha256(os.fsencode(name) + b"\0" + digest.encode()).hexdigest()[:20],
                    "name": name, "size": len(content), "sha256": digest,
                    "inode": before.st_ino, "device": before.st_dev,
                    "modified_ns": before.st_mtime_ns, "preview": preview[:1800],
                    "has_text": bool(preview.strip())}
        finally:
            os.close(fd)

    def scan(self):
        with self.lock:
            files, skipped, total = [], [], 0
            for name in sorted(os.listdir(self.root_fd), key=str.casefold):
                if name.startswith("."):
                    continue
                try:
                    metadata = os.stat(name, dir_fd=self.root_fd, follow_symlinks=False)
                    if stat.S_ISDIR(metadata.st_mode):
                        continue
                    if stat.S_ISLNK(metadata.st_mode):
                        skipped.append({"name": name, "reason": "Symbolic link; not followed."})
                        continue
                    if total + metadata.st_size > self.max_scan_bytes or len(files) >= 500:
                        skipped.append({"name": name, "reason": "Scan budget reached; try a smaller folder."})
                        continue
                    item = self._read(self.root_fd, name)
                    if total + item["size"] > self.max_scan_bytes or len(files) >= 500:
                        skipped.append({"name": name, "reason": "Scan budget reached; try a smaller folder."})
                        continue
                    total += item["size"]
                    files.append(item)
                except (OSError, Problem) as exc:
                    skipped.append({"name": name, "reason": str(exc)})
            return {"folder": self.folder.name, "files": files, "skipped": skipped,
                    "total_bytes": total, "batch_limit": self.max_batch}

    def store_plan(self, files, proposal, categories, model_record):
        with self.lock:
            categories = list(dict.fromkeys(safe_category(c) for c in categories))
            if not 1 <= len(categories) <= 8:
                raise Problem("Choose between one and eight destination folders.")
            by_id = {f["id"]: f for f in files}
            if not files or len(files) > self.max_batch or len(by_id) != len(files):
                raise Problem("Choose one to sixteen different files.")
            suggestions = proposal.get("items") if isinstance(proposal, dict) else None
            if not isinstance(suggestions, list) or len(suggestions) != len(files):
                raise Problem("The local model did not return one decision for every file. No files were moved.")
            seen, items = set(), []
            for suggestion in suggestions:
                if not isinstance(suggestion, dict):
                    raise Problem("The model returned a malformed file decision. No files were moved.")
                file_id = suggestion.get("id")
                decision = suggestion.get("decision")
                category = suggestion.get("category")
                if file_id not in by_id or file_id in seen or decision not in {"move", "keep", "review"}:
                    raise Problem("The model returned an unknown or repeated file decision. No files were moved.")
                if category not in categories:
                    raise Problem("The model chose a folder outside the allowed list. No files were moved.")
                seen.add(file_id)
                source = by_id[file_id]
                reason = str(suggestion.get("reason", ""))[:300]
                evidence = str(suggestion.get("evidence", ""))[:200]
                if decision == "move" and (not source["has_text"] or not evidence or evidence not in source["preview"][:1000]):
                    decision = "review"
                    reason = "The suggestion had no matching text evidence. Please review this file."
                items.append({**source, "decision": decision, "category": category,
                              "reason": reason, "evidence": evidence,
                              "destination": "Sorted/" + category + "/" + source["name"]})
            plan = {"id": uuid.uuid4().hex, "created": now(), "categories": categories,
                    "items": items, "model": model_record}
            self.db.execute("INSERT INTO plans VALUES (?,?,?,?)",
                            (plan["id"], plan["created"], json.dumps(plan), "ready"))
            self.db.commit()
            return plan

    def plan(self, plan_id):
        if not isinstance(plan_id, str) or not re.fullmatch(r"[a-f0-9]{32}", plan_id):
            raise Problem("Invalid plan identifier.")
        row = self.db.execute("SELECT * FROM plans WHERE id=?", (plan_id,)).fetchone()
        if row is None:
            raise Problem("This plan was not found.")
        return {**json.loads(row["payload"]), "status": row["status"]}

    def plans(self):
        return [{**json.loads(r["payload"]), "status": r["status"]}
                for r in self.db.execute("SELECT * FROM plans ORDER BY created DESC LIMIT 30")]

    def _verify_source(self, item):
        actual = self._read(self.root_fd, item["name"])
        if any(actual[k] != item[k] for k in ("sha256", "inode", "device", "size", "modified_ns")):
            raise Problem("File changed since planning: " + item["name"] + ". Make a new plan.")

    def apply(self, plan_id, selected):
        with self.lock:
            plan = self.plan(plan_id)
            if plan["status"] != "ready":
                raise Problem("This plan has already been used. Open its batch to resume or undo it.")
            if not isinstance(selected, list) or not selected or len(set(selected)) != len(selected):
                raise Problem("Select the proposed moves you want to apply.")
            allowed = {i["id"]: i for i in plan["items"] if i["decision"] == "move"}
            if any(i not in allowed for i in selected):
                raise Problem("Only proposed moves from this saved plan can be approved.")
            items = [allowed[i] for i in selected]
            for item in items:
                self._verify_source(item)
                target = self._target(item["category"])
                try:
                    try:
                        os.stat(item["name"], dir_fd=target, follow_symlinks=False)
                    except FileNotFoundError:
                        pass
                    else:
                        raise Problem("A destination already exists: " + item["destination"])
                finally:
                    os.close(target)
            batch_id = uuid.uuid4().hex
            self.db.execute("INSERT INTO batches VALUES (?,?,?,?)", (batch_id, plan_id, now(), "applying"))
            for item in items:
                self.db.execute("INSERT INTO moves VALUES (?,?,?,?,?,?,?)",
                                (uuid.uuid4().hex, batch_id, item["name"], item["category"],
                                 item["sha256"], "pending", None))
            self.db.execute("UPDATE plans SET status='applied' WHERE id=?", (plan_id,))
            self.db.commit()
            return self.resume(batch_id)

    def resume(self, batch_id):
        with self.lock:
            self.recover()
            batch = self.batch(batch_id)
            if batch["status"] in {"undone", "undo_partial"}:
                raise Problem("This batch has already been undone or partially undone.")
            for row in self.db.execute("SELECT * FROM moves WHERE batch_id=? AND status IN ('pending','blocked')", (batch_id,)).fetchall():
                target = None
                try:
                    source = self._read(self.root_fd, row["source"])
                    if source["sha256"] != row["digest"]:
                        raise Problem("Source changed after approval; it was left in place.")
                    target = self._target(row["category"])
                    self.db.execute("UPDATE moves SET status='intent',error=NULL WHERE id=?", (row["id"],))
                    self.db.commit()
                    move_exclusive(self.root_fd, row["source"], target, row["source"])
                    os.fsync(self.root_fd)
                    os.fsync(target)
                    if self.fault_hook:
                        self.fault_hook("after_move", dict(row))
                    self.db.execute("UPDATE moves SET status='moved',error=NULL WHERE id=?", (row["id"],))
                    self.db.commit()
                except (OSError, Problem) as exc:
                    self.db.execute("UPDATE moves SET status='blocked',error=? WHERE id=?", (str(exc), row["id"]))
                    self.db.commit()
                finally:
                    if target is not None:
                        os.close(target)
            pending = self.db.execute("SELECT COUNT(*) FROM moves WHERE batch_id=? AND status!='moved'", (batch_id,)).fetchone()[0]
            self.db.execute("UPDATE batches SET status=? WHERE id=?", ("partial" if pending else "complete", batch_id))
            self.db.commit()
            return self.batch(batch_id)

    def recover(self):
        with self.lock:
            affected = {}
            for row in self.db.execute("SELECT * FROM moves WHERE status IN ('intent','undo_intent')").fetchall():
                affected[row["batch_id"]] = row["status"]
                target = None
                try:
                    target = self._target(row["category"])
                    found = {}
                    for label, directory in (("source", self.root_fd), ("target", target)):
                        try:
                            found[label] = self._read(directory, row["source"])["sha256"]
                        except FileNotFoundError:
                            found[label] = None
                    if row["status"] == "intent" and found["target"] == row["digest"] and found["source"] is None:
                        status, error = "moved", None
                    elif row["status"] == "undo_intent" and found["source"] == row["digest"] and found["target"] is None:
                        status, error = "undone", None
                    elif row["status"] == "intent" and found["source"] == row["digest"] and found["target"] is None:
                        status, error = "pending", None
                    elif row["status"] == "undo_intent" and found["target"] == row["digest"]:
                        status = "moved"
                        error = "The original name is occupied. Both files were left in place." if found["source"] is not None else None
                    else:
                        status, error = "blocked", "Recovery found changed or conflicting files. Nothing was removed."
                except (OSError, Problem) as exc:
                    status, error = "blocked", str(exc)
                finally:
                    if target is not None:
                        os.close(target)
                self.db.execute("UPDATE moves SET status=?,error=? WHERE id=?", (status, error, row["id"]))
            for batch_id, action in affected.items():
                statuses = {r[0] for r in self.db.execute("SELECT status FROM moves WHERE batch_id=?", (batch_id,))}
                if action == "undo_intent":
                    status = "undone" if statuses <= {"undone", "pending"} else "undo_partial"
                else:
                    status = "complete" if statuses == {"moved"} else "partial"
                self.db.execute("UPDATE batches SET status=? WHERE id=?", (status, batch_id))
            self.db.commit()

    def undo(self, batch_id):
        with self.lock:
            self.recover()
            self.batch(batch_id)
            for row in self.db.execute("SELECT * FROM moves WHERE batch_id=? AND status='moved'", (batch_id,)).fetchall():
                target = None
                try:
                    target = self._target(row["category"])
                    actual = self._read(target, row["source"])
                    if actual["sha256"] != row["digest"]:
                        raise Problem("Moved file was edited. Undo left it in place.")
                    self.db.execute("UPDATE moves SET status='undo_intent' WHERE id=?", (row["id"],))
                    self.db.commit()
                    move_exclusive(target, row["source"], self.root_fd, row["source"])
                    os.fsync(target)
                    os.fsync(self.root_fd)
                    if self.fault_hook:
                        self.fault_hook("after_undo", dict(row))
                    self.db.execute("UPDATE moves SET status='undone',error=NULL WHERE id=?", (row["id"],))
                    self.db.commit()
                except (OSError, Problem) as exc:
                    self.db.execute("UPDATE moves SET error=? WHERE id=?", (str(exc), row["id"]))
                    self.db.commit()
                finally:
                    if target is not None:
                        os.close(target)
            self.recover()
            remaining = self.db.execute("SELECT COUNT(*) FROM moves WHERE batch_id=? AND status IN ('moved','intent','undo_intent','blocked')", (batch_id,)).fetchone()[0]
            self.db.execute("UPDATE batches SET status=? WHERE id=?", ("undo_partial" if remaining else "undone", batch_id))
            self.db.commit()
            return self.batch(batch_id)

    def batch(self, batch_id):
        if not isinstance(batch_id, str) or not re.fullmatch(r"[a-f0-9]{32}", batch_id):
            raise Problem("Invalid batch identifier.")
        row = self.db.execute("SELECT * FROM batches WHERE id=?", (batch_id,)).fetchone()
        if row is None:
            raise Problem("This batch was not found.")
        return {**dict(row), "moves": [dict(r) for r in self.db.execute("SELECT * FROM moves WHERE batch_id=?", (batch_id,))]}

    def batches(self):
        return [self.batch(r[0]) for r in self.db.execute("SELECT id FROM batches ORDER BY created DESC LIMIT 30")]
