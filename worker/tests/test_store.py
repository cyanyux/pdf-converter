import json
import time
from pathlib import Path

from worker.store import Store

SCHEMA = Path(__file__).resolve().parents[2] / "db" / "schema.sql"


def _store(tmp_path: Path) -> Store:
    return Store(tmp_path / "jobs.db", SCHEMA)


def _insert_queued(s: Store, job_id: str, mode: str = "pdf") -> None:
    now = time.time()
    s.conn.execute(
        "INSERT INTO jobs(id,mode,filename,locale,status,upload_path,created_at,updated_at) "
        "VALUES(?,?,?,?,'queued',?,?,?)",
        (job_id, mode, "a.pdf", "en", "/tmp/a.pdf", now, now),
    )


def test_claim_progress_result(tmp_path: Path) -> None:
    s = _store(tmp_path)
    _insert_queued(s, "j1")
    job = s.claim()
    assert job is not None and job["id"] == "j1" and job["status"] == "processing"
    assert s.claim() is None  # nothing else queued

    s.set_progress("j1", 1, 2, "processing", "half")
    s.set_result("j1", "j1", {"totalPages": 2, "downloadId": "j1"}, "done")
    row = s.conn.execute("SELECT status, result_json FROM jobs WHERE id='j1'").fetchone()
    assert row["status"] == "done"
    assert json.loads(row["result_json"])["totalPages"] == 2


def test_reaper_requeues_stale_processing(tmp_path: Path) -> None:
    s = _store(tmp_path)
    old = time.time() - 10_000
    s.conn.execute(
        "INSERT INTO jobs(id,mode,filename,locale,status,heartbeat_at,attempts,created_at,updated_at) "
        "VALUES('j2','vl','b.pdf','en','processing',?,0,?,?)",
        (old, old, old),
    )
    s.reap(stale_s=120, max_attempts=3)
    assert s.status_of("j2") == "queued"


def test_reaper_fails_poison_pill(tmp_path: Path) -> None:
    s = _store(tmp_path)
    old = time.time() - 10_000
    s.conn.execute(
        "INSERT INTO jobs(id,mode,filename,locale,status,heartbeat_at,attempts,created_at,updated_at) "
        "VALUES('j3','vl','c.pdf','en','processing',?,3,?,?)",
        (old, old, old),
    )
    s.reap(stale_s=120, max_attempts=3)
    assert s.status_of("j3") == "error"


def test_upload_refcount(tmp_path: Path) -> None:
    s = _store(tmp_path)
    now = time.time()
    for jid in ("a", "b"):
        s.conn.execute(
            "INSERT INTO jobs(id,mode,filename,locale,status,upload_path,created_at,updated_at) "
            "VALUES(?,?,?,?,'queued','/tmp/shared.pdf',?,?)",
            (jid, "markdown", "s.pdf", "en", now, now),
        )
    # 'b' still references the shared upload while excluding 'a'
    assert s.upload_refcount_active("/tmp/shared.pdf", "a") == 1
    s.conn.execute("UPDATE jobs SET status='done' WHERE id='b'")
    assert s.upload_refcount_active("/tmp/shared.pdf", "a") == 0
