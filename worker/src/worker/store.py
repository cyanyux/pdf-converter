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
        schema_sql = schema_path.read_text(encoding="utf-8")
        self.conn.executescript(schema_sql)
        self._migrate(schema_sql)

    def _migrate(self, schema_sql: str) -> None:
        """Idempotent schema catch-up for a pre-existing DB, derived from schema.sql itself.

        executescript applies schema.sql, but its CREATE TABLE IF NOT EXISTS is a no-op on an
        already-created table — so a column added to schema.sql after the DB was first built
        never lands. Rather than hand-maintaining per-column ALTERs here AND in the TS server's
        JobStore.migrate (a pair that can silently drift), apply schema.sql to a throwaway
        in-memory DB and ALTER in whatever columns the live tables are missing. schema.sql stays
        the single source of truth; a new NOT NULL column just needs a DEFAULT there (SQLite
        can't backfill one without it — that fails loudly HERE at startup, not later at some
        INSERT). The TS server runs the identical algorithm.
        """
        ref = sqlite3.connect(":memory:")
        try:
            ref.row_factory = sqlite3.Row
            ref.executescript(schema_sql)
            tables = [r["name"] for r in ref.execute("SELECT name FROM sqlite_master WHERE type='table'")]
            for table in tables:
                have = {r["name"] for r in self.conn.execute(f"PRAGMA table_info({table})")}
                for col in ref.execute(f"PRAGMA table_info({table})"):
                    if col["name"] in have:
                        continue
                    ddl = f"ALTER TABLE {table} ADD COLUMN {col['name']} {col['type']}"
                    if col["notnull"]:
                        ddl += " NOT NULL"
                    if col["dflt_value"] is not None:
                        ddl += f" DEFAULT {col['dflt_value']}"
                    self.conn.execute(ddl)
        finally:
            ref.close()

    def close(self) -> None:
        self.conn.close()

    # ---- supervisor ----

    def reap(self, stale_s: int, max_attempts: int) -> None:
        """Recover jobs a dead worker left in 'processing'; fail poison pills.

        Also a systemic backstop for stranded cancels: a 'cancel_requested' job whose heartbeat
        is stale has no child running it (crash/restart), and nothing else terminalizes that
        non-terminal state — so resolve it (and its whole group, mirroring
        resolve_group_cancel_requested) to 'cancelled'.
        """
        cutoff = time.time() - stale_s
        self._reap_stale_cancel_requested(cutoff)
        rows = self.conn.execute(
            "SELECT id, attempts FROM jobs WHERE status='processing' AND (heartbeat_at IS NULL OR heartbeat_at < ?)",
            (cutoff,),
        ).fetchall()
        now = time.time()
        for row in rows:
            # This recovery counts as an attempt (attempts + 1). Poison once a job has been
            # attempted max_attempts times: with the default 3 a job runs at most 3 times
            # (attempts 0,1,2) before it is failed. `>=`, not `>` — `>` would grant one extra
            # run than MAX_ATTEMPTS names (see test_store.py's attempts=2 boundary tests).
            if row["attempts"] + 1 >= max_attempts:
                # Poison pill: two statements (jobs -> error, progress row), so wrap
                # each row's change in its own txn — a TS reader never sees a torn state.
                self.conn.execute("BEGIN IMMEDIATE")
                try:
                    self.conn.execute(
                        "UPDATE jobs SET status='error', error=?, updated_at=? WHERE id=?",
                        ("interrupted too many times", now, row["id"]),
                    )
                    self._progress_stmts(row["id"], 0, 0, "error", "interrupted too many times")
                    self.conn.execute("COMMIT")
                except Exception:
                    self.conn.execute("ROLLBACK")
                    raise
            else:
                self.conn.execute(
                    "UPDATE jobs SET status='queued', attempts=attempts+1, heartbeat_at=NULL, updated_at=? WHERE id=?",
                    (now, row["id"]),
                )

    def _reap_stale_cancel_requested(self, cutoff: float) -> None:
        """Resolve stranded 'cancel_requested' jobs (stale heartbeat) to 'cancelled'.

        Same staleness rule as the 'processing' reap: a NULL or older-than-cutoff heartbeat means
        no child is running the job. After a crash/restart nothing else terminalizes it, so
        finish the cancel here — resolving the WHOLE group (like resolve_group_cancel_requested)
        so a dual export's sibling can't leak. Each row's status + progress commit atomically.
        """
        rows = self.conn.execute(
            "SELECT id, group_id FROM jobs "
            "WHERE status='cancel_requested' AND (heartbeat_at IS NULL OR heartbeat_at < ?)",
            (cutoff,),
        ).fetchall()
        seen: set[str] = set()
        for row in rows:
            gid = row["group_id"]
            if gid:
                if gid in seen:
                    continue
                seen.add(gid)
                self.resolve_group_cancel_requested(gid, "cancelled")
            else:
                if self.status_of(row["id"]) == "cancel_requested":
                    self.set_cancelled(row["id"], "cancelled")

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
        # Same boundary as reap(): this crash counts as an attempt; fail once the job has
        # been attempted max_attempts times (default 3 -> at most 3 runs). See reap().
        if row["attempts"] + 1 >= max_attempts:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                self.conn.execute(
                    "UPDATE jobs SET status='error', error=?, updated_at=? WHERE id=?",
                    (reason, now, job_id),
                )
                self._progress_stmts(job_id, 0, 0, "error", reason)
                self.conn.execute("COMMIT")
            except Exception:
                self.conn.execute("ROLLBACK")
                raise
            return False
        self.conn.execute(
            "UPDATE jobs SET status='queued', attempts=attempts+1, heartbeat_at=NULL, updated_at=? WHERE id=?",
            (now, job_id),
        )
        return True

    def requeue_for_shutdown(self, job_id: str) -> bool:
        """Return a still-running job to 'queued' on graceful shutdown, WITHOUT counting an
        attempt.

        A SIGTERM-driven drain is not a job failure — the job never got to finish — so unlike
        requeue() (child crash/timeout) it must not increment attempts, or repeated container
        restarts during one slow job would burn through MAX_ATTEMPTS and poison a healthy job.
        Guarded on the live, non-terminal states a job can hold mid-run
        ('queued'/'processing'/'saving') so it never clobbers a result the child already
        committed — and pointedly NOT 'cancel_requested': a user's pending cancel must resolve to
        'cancelled' on drain (via resolve_group_cancel_requested), not be silently requeued and
        run to completion after restart. Returns whether it re-queued."""
        now = time.time()
        cur = self.conn.execute(
            "UPDATE jobs SET status='queued', heartbeat_at=NULL, updated_at=? "
            "WHERE id=? AND status IN ('queued','processing','saving')",
            (now, job_id),
        )
        return bool(cur.rowcount)

    def set_processing(self, job_id: str) -> None:
        """Promote a still-queued group sibling to 'processing'.

        A VL dual export (markdown+word) recognizes the PDF ONCE and mirrors that progress onto
        both group members, but only the claimed job is flipped to 'processing' by claim(). The
        sibling would otherwise sit at status='queued' while its progress row shows live
        recognition — an inconsistency every consumer (SPA badge, REST, MCP) sees. Guarded on
        'queued' so it never resurrects a terminal job or clobbers a 'cancel_requested' one."""
        now = time.time()
        self.conn.execute(
            "UPDATE jobs SET status='processing', heartbeat_at=?, updated_at=? WHERE id=? AND status='queued'",
            (now, now, job_id),
        )

    def heartbeat(self, active_model: str | None, gpu: dict[str, Any]) -> None:
        self.conn.execute(
            "INSERT INTO worker_heartbeat(id, updated_at, active_model, gpu_json) VALUES(1, ?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET updated_at=excluded.updated_at, "
            "active_model=excluded.active_model, gpu_json=excluded.gpu_json",
            (time.time(), active_model, json.dumps(gpu)),
        )

    # ---- child / pipeline ----

    def _progress_stmts(self, job_id: str, current: int, total: int, status: str, message: str) -> None:
        """The progress upsert + heartbeat bump, WITHOUT a surrounding transaction.

        Call this from within an already-open BEGIN...COMMIT (set_result/set_error/
        set_cancelled/reap) so the jobs.status change and its progress row commit
        atomically. set_progress() wraps this as its own single-shot autocommit."""
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

    def set_progress(self, job_id: str, current: int, total: int, status: str, message: str) -> None:
        # Standalone per-page progress from the child: stays single-shot autocommit.
        self._progress_stmts(job_id, current, total, status, message)

    def set_result(self, job_id: str, download_id: str, result: dict[str, Any], done_msg: str) -> None:
        now = time.time()
        total = int(result.get("totalPages", 0)) or 1
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            self.conn.execute(
                "UPDATE jobs SET status='done', result_json=?, download_id=?, updated_at=? WHERE id=?",
                (json.dumps(result), download_id, now, job_id),
            )
            self._progress_stmts(job_id, total, total, "done", done_msg)
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise

    def set_error(self, job_id: str, message: str) -> None:
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            self.conn.execute(
                "UPDATE jobs SET status='error', error=?, updated_at=? WHERE id=?",
                (message, time.time(), job_id),
            )
            self._progress_stmts(job_id, 0, 0, "error", message)
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise

    def set_cancelled(self, job_id: str, message: str) -> None:
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            self.conn.execute("UPDATE jobs SET status='cancelled', updated_at=? WHERE id=?", (time.time(), job_id))
            self._progress_stmts(job_id, 0, 0, "cancelled", message)
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise

    def resolve_group_cancel_requested(self, group_id: str | None, message: str) -> list[str]:
        """Terminate EVERY still-'cancel_requested' member of a group to 'cancelled'.

        The supervisor's wedge-kill cancel path SIGKILLs the child before its own group-wide
        Cancelled handler (child.py) can run, so a dual export (markdown+word share one VL pass
        via group_id) leaves the un-claimed sibling stuck at 'cancel_requested' forever — a
        non-terminal state no reap/claim/gc ever resolves, leaking a phantom active job and
        pinning the shared upload. Resolve the whole group here, mirroring child.py's handler.

        Guarded on `status='cancel_requested'` (the terminal-guard idiom used across this
        module): never clobbers a sibling that already committed a 'done'/'error' result, and
        it is a no-op when the child self-cancelled it first. Returns the ids actually flipped.
        A NULL group_id (single-export job) matches nothing here — the caller's own
        set_cancelled covers that case. Each row's jobs.status + progress commit atomically."""
        if not group_id:
            return []
        rows = self.conn.execute(
            "SELECT id FROM jobs WHERE group_id=? AND status='cancel_requested'",
            (group_id,),
        ).fetchall()
        now = time.time()
        flipped: list[str] = []
        for row in rows:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                # Re-assert the guard inside the txn: a concurrent worker/child commit could have
                # moved this row terminal between the SELECT above and here.
                cur = self.conn.execute(
                    "UPDATE jobs SET status='cancelled', updated_at=? WHERE id=? AND status='cancel_requested'",
                    (now, row["id"]),
                )
                if cur.rowcount:
                    self._progress_stmts(row["id"], 0, 0, "cancelled", message)
                    flipped.append(row["id"])
                self.conn.execute("COMMIT")
            except Exception:
                self.conn.execute("ROLLBACK")
                raise
        return flipped

    def status_of(self, job_id: str) -> str | None:
        row = self.conn.execute("SELECT status FROM jobs WHERE id=?", (job_id,)).fetchone()
        return row["status"] if row else None

    def progress_status(self, job_id: str) -> str | None:
        """The progress-row status ('processing' during recognition, 'saving'/'converting'
        during the opaque post-recognition save phase). The watchdog reads it to choose a
        strict vs. lenient idle timeout."""
        row = self.conn.execute("SELECT status FROM progress WHERE job_id=?", (job_id,)).fetchone()
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
            # Sweep EVERY upload, not just *.pdf: an unsupported upload can briefly land as
            # <uuid>.bin before the server rejects it, and would otherwise leak forever.
            for f in uploads_dir.iterdir():
                if not f.is_file():
                    continue
                if str(f) not in referenced and (now - f.stat().st_mtime) > job_max_age_s:
                    f.unlink(missing_ok=True)
