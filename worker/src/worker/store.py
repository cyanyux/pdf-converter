"""SQLite job-store accessors for the worker side of the cross-language contract.

Mirrors db/schema.sql exactly; the TS server (node:sqlite) is the other party.
Used by both the supervisor (reap/claim/heartbeat/gc) and the model child
(progress/result/cancel checks). WAL + busy_timeout make the multi-writer,
low-rate access safe.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import time
from pathlib import Path
from typing import Any

TERMINAL = ("done", "error", "cancelled")


class Store:
    def __init__(self, db_path: Path, schema_path: Path) -> None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(db_path, timeout=5.0, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA busy_timeout=5000")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.executescript(schema_path.read_text(encoding="utf-8"))

    def close(self) -> None:
        self.conn.close()

    # ---- supervisor ----

    def reap(self, stale_s: int, max_attempts: int) -> None:
        """Recover jobs a dead worker left in 'processing'; fail poison pills."""
        cutoff = time.time() - stale_s
        rows = self.conn.execute(
            "SELECT id, attempts FROM jobs WHERE status='processing' AND (heartbeat_at IS NULL OR heartbeat_at < ?)",
            (cutoff,),
        ).fetchall()
        now = time.time()
        for row in rows:
            if row["attempts"] + 1 > max_attempts:
                self.conn.execute(
                    "UPDATE jobs SET status='error', error=?, updated_at=? WHERE id=?",
                    ("interrupted too many times", now, row["id"]),
                )
                self.set_progress(row["id"], 0, 0, "error", "interrupted too many times")
            else:
                self.conn.execute(
                    "UPDATE jobs SET status='queued', attempts=attempts+1, heartbeat_at=NULL, updated_at=? WHERE id=?",
                    (now, row["id"]),
                )

    def claim(self) -> dict[str, Any] | None:
        """Atomically claim the oldest queued job (IMMEDIATE txn)."""
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            row = self.conn.execute("SELECT * FROM jobs WHERE status='queued' ORDER BY created_at LIMIT 1").fetchone()
            if row is None:
                self.conn.execute("COMMIT")
                return None
            now = time.time()
            self.conn.execute(
                "UPDATE jobs SET status='processing', heartbeat_at=?, updated_at=? WHERE id=?",
                (now, now, row["id"]),
            )
            self.conn.execute("COMMIT")
            claimed = dict(row)
            claimed["status"] = "processing"
            claimed["heartbeat_at"] = now
            return claimed
        except Exception:
            self.conn.execute("ROLLBACK")
            raise

    def requeue(self, job_id: str, max_attempts: int, reason: str) -> bool:
        """After a child crash: requeue if under the attempt cap, else fail. Returns requeued?"""
        row = self.conn.execute("SELECT attempts, status FROM jobs WHERE id=?", (job_id,)).fetchone()
        if row is None:
            return False
        # Never resurrect a job that already reached a terminal state: a child can
        # write its result(s) and then die before emitting @@DONE, in which case the
        # supervisor sees DEAD and calls requeue on an already-'done' job. Clobbering
        # it back to 'queued'/'error' would lose a downloadable result.
        if row["status"] in ("done", "error", "cancelled"):
            return False
        now = time.time()
        if row["attempts"] + 1 > max_attempts:
            self.conn.execute(
                "UPDATE jobs SET status='error', error=?, updated_at=? WHERE id=?",
                (reason, now, job_id),
            )
            self.set_progress(job_id, 0, 0, "error", reason)
            return False
        self.conn.execute(
            "UPDATE jobs SET status='queued', attempts=attempts+1, heartbeat_at=NULL, updated_at=? WHERE id=?",
            (now, job_id),
        )
        return True

    def heartbeat(self, active_model: str | None, gpu: dict[str, Any]) -> None:
        self.conn.execute(
            "INSERT INTO worker_heartbeat(id, updated_at, active_model, gpu_json) VALUES(1, ?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET updated_at=excluded.updated_at, "
            "active_model=excluded.active_model, gpu_json=excluded.gpu_json",
            (time.time(), active_model, json.dumps(gpu)),
        )

    # ---- child / pipeline ----

    def set_progress(self, job_id: str, current: int, total: int, status: str, message: str) -> None:
        percent = min(100, int(current / total * 100)) if total else 0
        now = time.time()
        self.conn.execute(
            "INSERT INTO progress(job_id,current,total,percent,status,message,updated_at) "
            "VALUES(?,?,?,?,?,?,?) ON CONFLICT(job_id) DO UPDATE SET current=excluded.current,"
            "total=excluded.total,percent=excluded.percent,status=excluded.status,"
            "message=excluded.message,updated_at=excluded.updated_at",
            (job_id, current, total, percent, status, message, now),
        )
        self.conn.execute("UPDATE jobs SET heartbeat_at=?, updated_at=? WHERE id=?", (now, now, job_id))

    def set_result(self, job_id: str, download_id: str, result: dict[str, Any], done_msg: str) -> None:
        now = time.time()
        self.conn.execute(
            "UPDATE jobs SET status='done', result_json=?, download_id=?, updated_at=? WHERE id=?",
            (json.dumps(result), download_id, now, job_id),
        )
        total = int(result.get("totalPages", 0)) or 1
        self.set_progress(job_id, total, total, "done", done_msg)

    def set_error(self, job_id: str, message: str) -> None:
        self.conn.execute(
            "UPDATE jobs SET status='error', error=?, updated_at=? WHERE id=?",
            (message, time.time(), job_id),
        )
        self.set_progress(job_id, 0, 0, "error", message)

    def set_cancelled(self, job_id: str, message: str) -> None:
        self.conn.execute("UPDATE jobs SET status='cancelled', updated_at=? WHERE id=?", (time.time(), job_id))
        self.set_progress(job_id, 0, 0, "cancelled", message)

    def status_of(self, job_id: str) -> str | None:
        row = self.conn.execute("SELECT status FROM jobs WHERE id=?", (job_id,)).fetchone()
        return row["status"] if row else None

    def job_heartbeat_at(self, job_id: str) -> float | None:
        """Last time the child advanced this job (claim + every set_progress). The
        supervisor watchdog compares it against JOB_IDLE_TIMEOUT_S to spot a wedged child."""
        row = self.conn.execute("SELECT heartbeat_at FROM jobs WHERE id=?", (job_id,)).fetchone()
        return row["heartbeat_at"] if row else None

    def is_cancel_requested(self, job_id: str) -> bool:
        return self.status_of(job_id) in ("cancel_requested", "cancelled")

    # ---- grouping / uploads ----

    def group_jobs(self, group_id: str) -> list[dict[str, Any]]:
        rows = self.conn.execute("SELECT * FROM jobs WHERE group_id=?", (group_id,)).fetchall()
        return [dict(r) for r in rows]

    def upload_refcount_active(self, upload_path: str, exclude_job: str) -> int:
        """Non-terminal jobs (other than exclude_job) still needing this upload."""
        row = self.conn.execute(
            "SELECT count(*) AS c FROM jobs WHERE upload_path=? AND id!=? "
            "AND status NOT IN ('done','error','cancelled')",
            (upload_path, exclude_job),
        ).fetchone()
        return int(row["c"])

    # ---- retention GC ----

    def gc(self, job_max_age_s: int, outputs_dir: Path, uploads_dir: Path) -> None:
        now = time.time()
        # Delete old terminal jobs and their outputs.
        old = self.conn.execute(
            "SELECT id, download_id FROM jobs WHERE status IN ('done','error','cancelled') AND updated_at < ?",
            (now - job_max_age_s,),
        ).fetchall()
        for row in old:
            if row["download_id"]:
                shutil.rmtree(outputs_dir / row["download_id"], ignore_errors=True)
            self.conn.execute("DELETE FROM jobs WHERE id=?", (row["id"],))
        # Delete orphan uploads no live job references.
        referenced = {
            r["upload_path"]
            for r in self.conn.execute("SELECT DISTINCT upload_path FROM jobs WHERE upload_path IS NOT NULL").fetchall()
        }
        if uploads_dir.is_dir():
            # Sweep EVERY upload, not just *.pdf: Office uploads land as <uuid>.docx/
            # .xlsx/.pptx (and unknown types as .bin), and would otherwise leak forever.
            for f in uploads_dir.iterdir():
                if not f.is_file():
                    continue
                if str(f) not in referenced and (now - f.stat().st_mtime) > job_max_age_s:
                    f.unlink(missing_ok=True)
