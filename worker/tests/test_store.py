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


def test_reaper_poisons_at_attempts_boundary(tmp_path: Path) -> None:
    # attempts=2 with max=3: this recovery would be the 3rd run, so it must poison, not
    # requeue. Locks the `>=` boundary — the old `>` requeued here (a silent 4th attempt).
    s = _store(tmp_path)
    old = time.time() - 10_000
    s.conn.execute(
        "INSERT INTO jobs(id,mode,filename,locale,status,heartbeat_at,attempts,created_at,updated_at) "
        "VALUES('j4','vl','d.pdf','en','processing',?,2,?,?)",
        (old, old, old),
    )
    s.reap(stale_s=120, max_attempts=3)
    assert s.status_of("j4") == "error"


def test_reaper_requeues_below_boundary(tmp_path: Path) -> None:
    # attempts=1 with max=3: still under the cap -> requeue (attempts becomes 2).
    s = _store(tmp_path)
    old = time.time() - 10_000
    s.conn.execute(
        "INSERT INTO jobs(id,mode,filename,locale,status,heartbeat_at,attempts,created_at,updated_at) "
        "VALUES('j5','vl','e.pdf','en','processing',?,1,?,?)",
        (old, old, old),
    )
    s.reap(stale_s=120, max_attempts=3)
    row = s.conn.execute("SELECT status, attempts FROM jobs WHERE id='j5'").fetchone()
    assert row["status"] == "queued" and row["attempts"] == 2


def test_requeue_attempts_boundary(tmp_path: Path) -> None:
    # requeue() shares reap()'s poison boundary: attempts=1 -> requeued (True); =2 -> fail.
    s = _store(tmp_path)
    now = time.time()
    s.conn.execute(
        "INSERT INTO jobs(id,mode,filename,locale,status,attempts,created_at,updated_at) "
        "VALUES('r1','pdf','a.pdf','en','processing',1,?,?)",
        (now, now),
    )
    assert s.requeue("r1", max_attempts=3, reason="crash") is True
    row = s.conn.execute("SELECT status, attempts FROM jobs WHERE id='r1'").fetchone()
    assert row["status"] == "queued" and row["attempts"] == 2

    s.conn.execute(
        "INSERT INTO jobs(id,mode,filename,locale,status,attempts,created_at,updated_at) "
        "VALUES('r2','pdf','b.pdf','en','processing',2,?,?)",
        (now, now),
    )
    assert s.requeue("r2", max_attempts=3, reason="crash") is False
    assert s.status_of("r2") == "error"


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
