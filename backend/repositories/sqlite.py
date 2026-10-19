"""Small SQLite persistence boundary for the first API version."""
from __future__ import annotations

import json
import sqlite3
import copy
from functools import wraps
from threading import RLock
from pathlib import Path
from typing import Any


def serialized(method):
    """Keep each operation and its transaction on the shared connection isolated."""
    @wraps(method)
    def wrapped(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)
    return wrapped


# Every table that stores rows belonging to one task, mirroring the ``CREATE
# TABLE`` list above.  ``tasks`` is keyed by ``id``; every other table is keyed
# by ``task_id``.  ``tests/test_task_batch.py`` asserts this set still equals the
# live schema, so a new task-scoped table cannot be forgotten here silently.
TASK_SCOPED_TABLES = (
    "analysis_leases",
    "candidates",
    "documents",
    "facts",
    "final_decisions",
    "procurement_records",
    "province_award_cache",
    "rag_chunks",
    "reports",
    "requirements",
    "search_runs",
    "source_decisions",
    "supplier_inquiries",
    "supplier_replies",
    "task_archives",
    "task_history",
    "tasks",
    "workflow_state",
)


def _chunks(values: list[str], size: int = 200):
    for start in range(0, len(values), size):
        yield values[start:start + size]


class SQLiteRepository:
    def __init__(self, path: str | Path = "data/app.sqlite3") -> None:
        self._lock = RLock()
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS procurement_records (id TEXT PRIMARY KEY, task_id TEXT NOT NULL, payload TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS analysis_leases (task_id TEXT PRIMARY KEY, owner TEXT NOT NULL, expires REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS tasks (id TEXT PRIMARY KEY, payload TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS requirements (task_id TEXT PRIMARY KEY, payload TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS documents (id TEXT PRIMARY KEY, task_id TEXT NOT NULL, payload TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS candidates (id TEXT PRIMARY KEY, task_id TEXT NOT NULL, payload TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS reports (id TEXT PRIMARY KEY, task_id TEXT NOT NULL, payload TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS search_runs (id TEXT PRIMARY KEY, task_id TEXT NOT NULL, payload TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS source_decisions (id TEXT PRIMARY KEY, task_id TEXT NOT NULL, payload TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS final_decisions (id TEXT PRIMARY KEY, task_id TEXT NOT NULL, payload TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS supplier_inquiries (id TEXT PRIMARY KEY, task_id TEXT NOT NULL, payload TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS supplier_replies (id TEXT PRIMARY KEY, task_id TEXT NOT NULL, payload TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS facts (id TEXT PRIMARY KEY, task_id TEXT NOT NULL, payload TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS workflow_state (task_id TEXT PRIMARY KEY, payload TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS task_history (task_id TEXT NOT NULL, version_number INTEGER NOT NULL, payload TEXT NOT NULL, PRIMARY KEY(task_id, version_number));
        CREATE TABLE IF NOT EXISTS task_archives (id TEXT PRIMARY KEY, task_id TEXT NOT NULL, created_at TEXT NOT NULL, payload TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS rag_chunks (task_id TEXT NOT NULL, chunk_id TEXT NOT NULL, payload TEXT NOT NULL, PRIMARY KEY(task_id, chunk_id));
        CREATE TABLE IF NOT EXISTS province_award_cache (id TEXT PRIMARY KEY, task_id TEXT NOT NULL, payload TEXT NOT NULL);
        """)
        self.db.commit()

    @serialized
    def put(self, table: str, key: str, payload: Any, *, task_id: str | None = None) -> None:
        if table not in {"tasks", "requirements", "documents", "candidates", "reports", "search_runs", "source_decisions", "final_decisions", "supplier_inquiries", "supplier_replies", "facts", "workflow_state", "procurement_records", "province_award_cache"}:
            raise ValueError("invalid table")
        data = json.dumps(payload, ensure_ascii=False, default=str)
        if table == "tasks":
            self.db.execute(f"INSERT OR REPLACE INTO {table}(id, payload) VALUES (?, ?)", (key, data))
        elif table == "requirements":
            self.db.execute("INSERT OR REPLACE INTO requirements(task_id, payload) VALUES (?, ?)", (key, data))
        elif table == "workflow_state":
            self.db.execute("INSERT OR REPLACE INTO workflow_state(task_id, payload) VALUES (?, ?)", (key, data))
        else:
            self.db.execute(f"INSERT OR REPLACE INTO {table}(id, task_id, payload) VALUES (?, ?, ?)", (key, task_id, data))
        self.db.commit()

    @serialized
    def claim_analysis(self, task_id):
        import time
        import uuid
        owner = uuid.uuid4().hex
        # A stale worker cannot release a newer lease.
        self.db.execute('BEGIN IMMEDIATE')
        try:
            self.db.execute('DELETE FROM analysis_leases WHERE task_id = ? AND expires < ?', (task_id, time.time()))
            result = self.db.execute('INSERT OR IGNORE INTO analysis_leases VALUES (?, ?, ?)', (task_id, owner, time.time() + 120))
            self.db.commit()
            return owner if result.rowcount else None
        except Exception:
            self.db.rollback()
            raise

    @serialized
    def active_analysis(self, task_id):
        import time
        row = self.db.execute('SELECT owner FROM analysis_leases WHERE task_id = ? AND expires > ?', (task_id, time.time())).fetchone()
        return row['owner'] if row else None

    @serialized
    def renew_analysis(self, task_id, owner):
        import time
        stamp = time.time()
        changed = self.db.execute('UPDATE analysis_leases SET expires = ? WHERE task_id = ? AND owner = ? AND expires > ?', (stamp + 120, task_id, owner, stamp))
        self.db.commit()
        return bool(changed.rowcount)

    @serialized
    def release_analysis(self, task_id, owner):
        self.db.execute('DELETE FROM analysis_leases WHERE task_id = ? AND owner = ?', (task_id, owner))
        self.db.commit()

    @serialized
    def interrupt_orphaned_research(self, run_id):
        """Reconcile an inactive worker without overwriting a new live owner."""
        import time
        from datetime import datetime, timezone
        self.db.execute('BEGIN IMMEDIATE')
        try:
            row = self.db.execute('SELECT payload FROM procurement_records WHERE id = ?', (run_id,)).fetchone()
            if not row:
                self.db.commit()
                return None
            run = json.loads(row['payload'])
            lease = self.db.execute('SELECT 1 FROM analysis_leases WHERE task_id = ? AND expires > ?',
                                    ('research:' + run_id, time.time())).fetchone()
            if not lease and run.get('kind') == 'research' and run.get('status') in {'queued', 'running'}:
                stamp = datetime.now(timezone.utc).isoformat()
                run.update(status='interrupted', message='研究执行已中断，已有结果已保存，可继续未完成研究。',
                           updated_at=stamp, finished_at=stamp)
                self.db.execute('UPDATE procurement_records SET payload = ? WHERE id = ?',
                                (json.dumps(run, ensure_ascii=False), run_id))
                task = self.db.execute('SELECT payload FROM tasks WHERE id = ?', (run['task_id'],)).fetchone()
                if task:
                    value = json.loads(task['payload'])
                    if value.get('baseline_version') == run['baseline']['version']:
                        value['procurement_status'] = 'interrupted'
                        self.db.execute('UPDATE tasks SET payload = ? WHERE id = ?',
                                        (json.dumps(value, ensure_ascii=False), run['task_id']))
            self.db.commit()
            return run
        except Exception:
            self.db.rollback()
            raise

    @serialized
    def create_procurement_run(self, baseline, run):
        """Atomically deduplicate confirmation across tabs and connections."""
        self.db.execute('BEGIN IMMEDIATE')
        try:
            rows = self.db.execute('SELECT payload FROM procurement_records WHERE task_id = ?', (baseline['task_id'],)).fetchall()
            records = [json.loads(row['payload']) for row in rows]
            baselines = [r for r in records if r.get('kind') == 'baseline']
            existing = next((b for b in baselines if b.get('digest') == baseline['digest']), None)
            if existing:
                previous = next(r for r in records if r.get('kind') == 'research' and r['baseline_id'] == existing['id'])
                self.db.commit()
                return previous, False
            baseline['version'] = max((b['version'] for b in baselines), default=0) + 1
            run['baseline'] = baseline
            for previous in records:
                if previous.get('kind') == 'research' and previous.get('status') in {'queued', 'running'}:
                    previous.update(status='cancelled', message='比较基准已修改，旧研究已停止。')
                    self.db.execute('UPDATE procurement_records SET payload = ? WHERE id = ?', (json.dumps(previous, ensure_ascii=False), previous['id']))
            for record in (baseline, run):
                self.db.execute('INSERT INTO procurement_records(id, task_id, payload) VALUES (?, ?, ?)', (record['id'], record['task_id'], json.dumps(record, ensure_ascii=False)))
            self.db.commit()
            return run, True
        except Exception:
            self.db.rollback()
            raise

    @serialized
    def get(self, table: str, key: str) -> dict[str, Any] | None:
        # The same adapter also implements M12's ``get(task_id, version)``.
        if table not in {"tasks", "requirements", "documents", "candidates", "reports", "search_runs", "source_decisions", "final_decisions", "supplier_inquiries", "supplier_replies", "facts", "workflow_state", "procurement_records", "province_award_cache"}:
            return self.get_version(table, int(key))
        column = "task_id" if table in {"requirements", "workflow_state"} else "id"
        row = self.db.execute(f"SELECT payload FROM {table} WHERE {column} = ?", (key,)).fetchone()
        return json.loads(row["payload"]) if row else None

    @serialized
    def list(self, table: str, task_id: str) -> list[dict[str, Any]]:
        if table == "workflow_state":
            value = self.get("workflow_state", task_id)
            return [value] if value else []
        rows = self.db.execute(f"SELECT payload FROM {table} WHERE task_id = ? ORDER BY id", (task_id,)).fetchall()
        return [json.loads(row["payload"]) for row in rows]

    @serialized
    def list_tasks(self) -> list[dict[str, Any]]:
        rows = self.db.execute("SELECT id, payload FROM tasks ORDER BY rowid DESC").fetchall()
        return [{"id": row["id"], **json.loads(row["payload"])} for row in rows]

    @serialized
    def delete_reports(self, ids: list[str]) -> None:
        """Delete only generated reports, atomically; preserve tasks and files."""
        with self.db:
            for report_id in set(ids):
                row = self.db.execute('SELECT payload FROM reports WHERE id = ?', (report_id,)).fetchone()
                if not row or not isinstance(json.loads(row['payload']).get('report'), dict):
                    raise ValueError('report no longer available')
                self.db.execute('DELETE FROM reports WHERE id = ?', (report_id,))

    @serialized
    def task_scoped_tables(self) -> list[str]:
        """Tables holding task rows, read from the live schema instead of memory."""
        names = [row['name'] for row in self.db.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
        scoped = []
        for name in names:
            columns = {row['name'] for row in self.db.execute(f'PRAGMA table_info({name})')}
            if name == 'tasks' or 'task_id' in columns:
                scoped.append(name)
        return scoped

    @serialized
    def delete_tasks(self, task_ids: list[str]) -> dict[str, int]:
        """Physically delete tasks and every row that carries their id.

        Counts are taken first and returned per table, so the caller can report
        exactly what disappeared.  Deleting the same id twice is not an error:
        the second call simply reports zeros.
        """
        ids = sorted({str(value) for value in task_ids if value})
        tables = self.task_scoped_tables()
        counts = {table: 0 for table in tables}
        if not ids:
            return counts
        with self.db:
            # ``analysis_leases`` stores research leases under ``research:<run id>``
            # besides the plain task id, so those keys have to be collected first.
            lease_keys = list(ids)
            for chunk in _chunks(ids):
                placeholders = ','.join('?' * len(chunk))
                lease_keys += [f"research:{row['id']}" for row in self.db.execute(
                    f'SELECT id FROM procurement_records WHERE task_id IN ({placeholders})', chunk)]
            # Measure before deleting: the same transaction must not report a
            # count taken after the rows are gone.
            keys_by_table = {}
            for table in tables:
                column = 'id' if table == 'tasks' else 'task_id'
                keys = lease_keys if table == 'analysis_leases' else ids
                keys_by_table[table] = (column, keys)
                total = 0
                for chunk in _chunks(keys):
                    placeholders = ','.join('?' * len(chunk))
                    total += self.db.execute(
                        f'SELECT COUNT(*) FROM {table} WHERE {column} IN ({placeholders})', chunk).fetchone()[0]
                counts[table] = total
            for table in tables:
                column, keys = keys_by_table[table]
                for chunk in _chunks(keys):
                    placeholders = ','.join('?' * len(chunk))
                    self.db.execute(f'DELETE FROM {table} WHERE {column} IN ({placeholders})', chunk)
        return counts

    @serialized
    def update_candidate(self, task_id: str, candidate_id: str, payload: Any) -> bool:
        """Atomically replace a candidate only inside the requested task."""
        data = json.dumps(payload, ensure_ascii=False, default=str)
        cursor = self.db.execute(
            "UPDATE candidates SET payload = ? WHERE id = ? AND task_id = ?",
            (data, candidate_id, task_id),
        )
        self.db.commit()
        return cursor.rowcount == 1

    # Implements backend.domain.history.TaskHistoryRepository without coupling
    # the generic JSON tables to M12's immutable version contract.
    @serialized
    def save(self, version, expected_parent_version_id=None):
        from backend.domain.history import TaskVersion
        if not isinstance(version, TaskVersion):
            raise TypeError("version must be TaskVersion")
        latest = self.latest(version.task_id)
        if latest is None:
            if version.version_number != 1 or version.parent_version_id is not None or expected_parent_version_id is not None:
                from backend.domain.errors import ModuleValidationError
                raise ModuleValidationError("first task version has invalid parent", code="version_conflict", field="parent_version_id")
        else:
            if latest.content_hash == version.content_hash:
                if expected_parent_version_id not in (None, latest.version_id):
                    from backend.domain.errors import ModuleValidationError
                    raise ModuleValidationError("parent version is stale", code="version_conflict", field="parent_version_id")
                return copy.deepcopy(latest)
            if expected_parent_version_id != latest.version_id or version.parent_version_id != latest.version_id or version.version_number != latest.version_number + 1:
                from backend.domain.errors import ModuleValidationError
                raise ModuleValidationError("parent version is stale", code="version_conflict", field="parent_version_id")
        self.db.execute("INSERT INTO task_history(task_id, version_number, payload) VALUES (?, ?, ?)", (version.task_id, version.version_number, json.dumps(version.to_dict(), ensure_ascii=False)))
        self.db.commit()
        return copy.deepcopy(version)

    @serialized
    def _history_rows(self, task_id: str):
        return self.db.execute("SELECT payload FROM task_history WHERE task_id = ? ORDER BY version_number", (task_id,)).fetchall()

    @serialized
    def get_version(self, task_id: str, version_number: int):
        from backend.domain.history import TaskVersion
        row = self.db.execute("SELECT payload FROM task_history WHERE task_id = ? AND version_number = ?", (task_id, int(version_number))).fetchone()
        return TaskVersion(**json.loads(row["payload"])) if row else None

    @serialized
    def list_versions(self, task_id: str):
        from backend.domain.history import TaskVersion
        return [TaskVersion(**json.loads(row["payload"])) for row in self._history_rows(task_id)]

    @serialized
    def latest(self, task_id: str):
        values = self.list_versions(task_id)
        return values[-1] if values else None

    @serialized
    def close(self) -> None:
        """Release the file handle; safe to call more than once."""
        db, self.db = self.db, None
        if db is not None:
            db.close()

    @serialized
    def save_archive(self, archive_id: str, task_id: str, payload: Any, created_at: str) -> None:
        self.db.execute("INSERT INTO task_archives(id, task_id, created_at, payload) VALUES (?, ?, ?, ?)",
                        (archive_id, task_id, created_at, json.dumps(payload, ensure_ascii=False, default=str)))
        self.db.commit()

    @serialized
    def get_archive(self, task_id: str, archive_id: str | None = None) -> dict[str, Any] | None:
        if archive_id:
            row = self.db.execute("SELECT payload FROM task_archives WHERE id = ? AND task_id = ?", (archive_id, task_id)).fetchone()
        else:
            row = self.db.execute("SELECT payload FROM task_archives WHERE task_id = ? ORDER BY created_at DESC, id DESC LIMIT 1", (task_id,)).fetchone()
        return json.loads(row["payload"]) if row else None

    @serialized
    def list_archives(self, task_id: str) -> list[dict[str, Any]]:
        rows = self.db.execute("SELECT payload FROM task_archives WHERE task_id = ? ORDER BY created_at DESC, id DESC", (task_id,)).fetchall()
        return [json.loads(row["payload"]) for row in rows]

    @serialized
    def replace_task_state(self, task_id: str, snapshot: dict[str, Any]) -> None:
        """Restore only persisted task-scoped JSON state; uploaded files are never copied."""
        task = snapshot.get("task")
        if task:
            self.put("tasks", task_id, task)
        if "requirement" in snapshot and snapshot["requirement"] is not None:
            self.put("requirements", task_id, snapshot["requirement"])
        if "workflow_state" in snapshot:
            if snapshot["workflow_state"] is None:
                self.db.execute("DELETE FROM workflow_state WHERE task_id = ?", (task_id,))
            else:
                self.put("workflow_state", task_id, snapshot["workflow_state"])
        for table in ("documents", "candidates", "reports", "search_runs", "source_decisions",
                      "final_decisions", "supplier_inquiries", "supplier_replies", "facts"):
            self.db.execute(f"DELETE FROM {table} WHERE task_id = ?", (task_id,))
            source_key = table
            items = snapshot.get(table, [])
            if table == "final_decisions" and not items:
                items = snapshot.get("final_confirmation", [])
            key_names = {"final_decisions": ("decision_id", "id"), "supplier_inquiries": ("inquiry_id", "id"),
                         "supplier_replies": ("reply_id", "id"), "facts": ("fact_id", "id")}.get(table, ("id", "url"))
            for item in items:
                key = next((item.get(name) for name in key_names if item.get(name)), None)
                if key:
                    self.put(table, key, item, task_id=task_id)
        self.db.execute("DELETE FROM task_history WHERE task_id = ?", (task_id,))
        for item in snapshot.get("history", []):
            version_number = int(item.get("version_number", 0))
            if version_number:
                self.db.execute("INSERT INTO task_history(task_id, version_number, payload) VALUES (?, ?, ?)",
                                (task_id, version_number, json.dumps(item, ensure_ascii=False, default=str)))
        self.db.commit()

    @serialized
    def replace_rag_chunks(self, task_id: str, chunks: list[dict[str, Any]]) -> None:
        self.db.execute("DELETE FROM rag_chunks WHERE task_id = ?", (task_id,))
        for chunk in chunks:
            self.db.execute("INSERT INTO rag_chunks(task_id, chunk_id, payload) VALUES (?, ?, ?)",
                            (task_id, chunk["chunk_id"], json.dumps(chunk, ensure_ascii=False, default=str)))
        self.db.commit()

    @serialized
    def list_rag_chunks(self, task_id: str) -> list[dict[str, Any]]:
        rows = self.db.execute("SELECT payload FROM rag_chunks WHERE task_id = ? ORDER BY chunk_id", (task_id,)).fetchall()
        return [json.loads(row["payload"]) for row in rows]
