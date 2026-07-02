"""Worker supervisor entrypoint (Phase 1 skeleton).

Owns the SQLite job queue: applies the schema, runs a startup reaper, then polls
for queued jobs and processes them. Real PaddleOCR inference + subprocess-per-model
teardown land in Phase 3; this skeleton exercises the cross-language SQLite contract
(TS enqueues → worker claims → writes progress/result → TS reads) end to end.

Env:
  PDF_OCR_DB        SQLite file (default ./data/pdf-ocr.db)
  PDF_OCR_SCHEMA    schema.sql path (default ./db/schema.sql)
  PDF_OCR_POLL_MS   poll interval ms (default 200)
  PDF_OCR_STALE_S   heartbeat staleness for the reaper (default 120)
  PDF_OCR_MAX_ATTEMPTS  poison-pill cap (default 3)
  PDF_OCR_STUB      when '1', mark jobs done with a stub result (Phase-1 loop test)
"""

from __future__ import annotations

import json
import logging
import os
import signal
import sqlite3
import time
from pathlib import Path

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s [%(levelname)s] worker: %(message)s"
)
log = logging.getLogger("worker")

DB_PATH = Path(os.environ.get("PDF_OCR_DB", "./data/pdf-ocr.db"))
SCHEMA_PATH = Path(os.environ.get("PDF_OCR_SCHEMA", "./db/schema.sql"))
POLL_S = int(os.environ.get("PDF_OCR_POLL_MS", "200")) / 1000
STALE_S = int(os.environ.get("PDF_OCR_STALE_S", "120"))
MAX_ATTEMPTS = int(os.environ.get("PDF_OCR_MAX_ATTEMPTS", "3"))
STUB = os.environ.get("PDF_OCR_STUB") == "1"

_stop = False
_current_job: str | None = None


def _handle_signal(signum: int, _frame: object) -> None:
    global _stop
    log.info("received signal %s — draining", signum)
    _stop = True


def connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=5.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def apply_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))


def heartbeat(conn: sqlite3.Connection, active_model: str | None) -> None:
    conn.execute(
        "INSERT INTO worker_heartbeat(id, updated_at, active_model, gpu_json) "
        "VALUES(1, ?, ?, ?) ON CONFLICT(id) DO UPDATE SET "
        "updated_at=excluded.updated_at, active_model=excluded.active_model, gpu_json=excluded.gpu_json",
        (time.time(), active_model, json.dumps(_gpu_info())),
    )


def _gpu_info() -> dict[str, object]:
    # Real device/VRAM telemetry is filled in Phase 3 (paddle.device / nvidia-smi).
    return {"device": os.environ.get("PDF_OCR_DEVICE", "auto")}


def reap(conn: sqlite3.Connection) -> None:
    """Recover jobs a dead worker left mid-flight; fail poison pills."""
    cutoff = time.time() - STALE_S
    stale = conn.execute(
        "SELECT id, attempts FROM jobs WHERE status='processing' "
        "AND (heartbeat_at IS NULL OR heartbeat_at < ?)",
        (cutoff,),
    ).fetchall()
    for row in stale:
        if row["attempts"] + 1 > MAX_ATTEMPTS:
            conn.execute(
                "UPDATE jobs SET status='error', error=?, updated_at=? WHERE id=?",
                ("exceeded max attempts after interruption", time.time(), row["id"]),
            )
            log.warning("reaper failed poison-pill job %s", row["id"])
        else:
            conn.execute(
                "UPDATE jobs SET status='queued', attempts=attempts+1, heartbeat_at=NULL, updated_at=? WHERE id=?",
                (time.time(), row["id"]),
            )
            log.info("reaper requeued interrupted job %s", row["id"])


def claim(conn: sqlite3.Connection) -> sqlite3.Row | None:
    """Atomically claim the oldest queued job (IMMEDIATE txn)."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute(
            "SELECT * FROM jobs WHERE status='queued' ORDER BY created_at LIMIT 1"
        ).fetchone()
        if row is None:
            conn.execute("COMMIT")
            return None
        now = time.time()
        conn.execute(
            "UPDATE jobs SET status='processing', heartbeat_at=?, updated_at=? WHERE id=?",
            (now, now, row["id"]),
        )
        conn.execute("COMMIT")
        return row
    except Exception:
        conn.execute("ROLLBACK")
        raise


def set_progress(
    conn: sqlite3.Connection, job_id: str, current: int, total: int, status: str, message: str
) -> None:
    percent = min(100, int(current / total * 100)) if total else 0
    now = time.time()
    conn.execute(
        "INSERT INTO progress(job_id,current,total,percent,status,message,updated_at) "
        "VALUES(?,?,?,?,?,?,?) ON CONFLICT(job_id) DO UPDATE SET "
        "current=excluded.current,total=excluded.total,percent=excluded.percent,"
        "status=excluded.status,message=excluded.message,updated_at=excluded.updated_at",
        (job_id, current, total, percent, status, message, now),
    )
    conn.execute("UPDATE jobs SET heartbeat_at=?, updated_at=? WHERE id=?", (now, now, job_id))


def process(conn: sqlite3.Connection, job: sqlite3.Row) -> None:
    """Phase-1 stub. Phase 3 routes by mode to the model-child subprocess."""
    job_id = job["id"]
    if not STUB:
        raise RuntimeError("inference not implemented yet (Phase 3)")
    total = 1
    set_progress(conn, job_id, 1, total, "processing", "stub processing")
    result = {"totalPages": 0, "downloadId": job_id, "originalName": job["filename"]}
    conn.execute(
        "UPDATE jobs SET status='done', result_json=?, download_id=?, updated_at=? WHERE id=?",
        (json.dumps(result), job_id, time.time(), job_id),
    )
    set_progress(conn, job_id, total, total, "done", "done (stub)")


def main() -> None:
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)
    conn = connect()
    apply_schema(conn)
    reap(conn)
    log.info("worker started (db=%s stub=%s)", DB_PATH, STUB)
    global _current_job
    last_hb = 0.0
    while not _stop:
        now = time.time()
        if now - last_hb > 5:
            heartbeat(conn, None)
            last_hb = now
        job = claim(conn)
        if job is None:
            time.sleep(POLL_S)
            continue
        _current_job = job["id"]
        log.info("claimed job %s (%s, %s)", job["id"], job["mode"], job["filename"])
        try:
            process(conn, job)
            log.info("finished job %s", job["id"])
        except Exception as exc:  # noqa: BLE001 — worker must survive any job failure
            conn.execute(
                "UPDATE jobs SET status='error', error=?, updated_at=? WHERE id=?",
                (str(exc), time.time(), job["id"]),
            )
            set_progress(conn, job["id"], 0, 0, "error", str(exc))
            log.exception("job %s failed", job["id"])
        finally:
            _current_job = None

    # Graceful drain: hand any in-flight job back to the queue.
    if _current_job:
        conn.execute(
            "UPDATE jobs SET status='queued', heartbeat_at=NULL, updated_at=? WHERE id=? AND status='processing'",
            (time.time(), _current_job),
        )
    conn.close()
    log.info("worker stopped")


if __name__ == "__main__":
    main()
