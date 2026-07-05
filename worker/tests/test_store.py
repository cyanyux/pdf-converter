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


def test_set_processing_promotes_queued_sibling_only(tmp_path: Path) -> None:
    # A VL dual export mirrors recognition progress onto the queued sibling; set_processing
    # flips it to 'processing' so its badge/status matches, but must NOT resurrect a terminal
    # job or clobber a 'cancel_requested' one (guarded on status='queued').
    s = _store(tmp_path)
    _insert_queued(s, "sib", mode="markdown")
    s.set_processing("sib")
    assert s.status_of("sib") == "processing"

    _insert_queued(s, "gone")
    s.set_cancelled("gone", "x")
    s.set_processing("gone")
    assert s.status_of("gone") == "cancelled"  # terminal not resurrected

    _insert_queued(s, "cxl")
    s.conn.execute("UPDATE jobs SET status='cancel_requested' WHERE id='cxl'")
    s.set_processing("cxl")
    assert s.status_of("cxl") == "cancel_requested"  # not clobbered


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


def _insert_group(s: Store, job_id: str, group_id: str, status: str, mode: str = "markdown") -> None:
    now = time.time()
    s.conn.execute(
        "INSERT INTO jobs(id,mode,filename,locale,status,group_id,upload_path,created_at,updated_at) "
        "VALUES(?,?,?,?,?,?,?,?,?)",
        (job_id, mode, "a.pdf", "en", status, group_id, "/tmp/shared.pdf", now, now),
    )


def test_resolve_group_cancel_requested_terminates_stuck_sibling(tmp_path: Path) -> None:
    # Dual export (markdown+word share one VL pass via group_id). The supervisor's wedge-kill
    # cancel path sets ONLY the claimed job cancelled; the sibling is left 'cancel_requested'
    # (the child's own group-wide handler is bypassed by the SIGKILL). resolve_group_cancel_requested
    # flips every still-'cancel_requested' member to terminal 'cancelled'.
    s = _store(tmp_path)
    _insert_group(s, "md", "grp", "cancelled", mode="markdown")  # claimed job already terminal
    _insert_group(s, "wd", "grp", "cancel_requested", mode="word")  # stuck sibling
    flipped = s.resolve_group_cancel_requested("grp", "x")
    assert flipped == ["wd"]
    assert s.status_of("wd") == "cancelled"
    assert s.status_of("md") == "cancelled"  # untouched (was already terminal)
    # progress row is written for the flipped sibling so consumers see a terminal status
    row = s.conn.execute("SELECT status FROM progress WHERE job_id='wd'").fetchone()
    assert row["status"] == "cancelled"
    # And the shared upload is now unreferenced (both members terminal), so cleanup can unlink it.
    assert s.upload_refcount_active("/tmp/shared.pdf", "md") == 0


def test_resolve_group_cancel_requested_guards_terminal_and_null(tmp_path: Path) -> None:
    # Never resurrect/clobber a member that reached a terminal result, and a NULL group_id
    # (single-export job) resolves nothing (the caller's own set_cancelled covers it).
    s = _store(tmp_path)
    _insert_group(s, "done", "g2", "done", mode="markdown")  # committed a real result
    _insert_group(s, "cxl", "g2", "cancel_requested", mode="word")
    flipped = s.resolve_group_cancel_requested("g2", "x")
    assert flipped == ["cxl"]
    assert s.status_of("done") == "done"  # terminal result NOT clobbered
    assert s.status_of("cxl") == "cancelled"
    assert s.resolve_group_cancel_requested(None, "x") == []


def test_requeue_for_shutdown_does_not_count_attempt(tmp_path: Path) -> None:
    # A graceful-shutdown drain returns a running job to 'queued' WITHOUT incrementing attempts
    # (it is not a failure); it must never resurrect a terminal job either.
    s = _store(tmp_path)
    now = time.time()
    s.conn.execute(
        "INSERT INTO jobs(id,mode,filename,locale,status,attempts,heartbeat_at,created_at,updated_at) "
        "VALUES('d1','vl','a.pdf','en','processing',1,?,?,?)",
        (now, now, now),
    )
    assert s.requeue_for_shutdown("d1") is True
    row = s.conn.execute("SELECT status, attempts, heartbeat_at FROM jobs WHERE id='d1'").fetchone()
    assert row["status"] == "queued" and row["attempts"] == 1 and row["heartbeat_at"] is None
    # A 'saving' member (mid post-recognition save) is also re-queueable.
    s.conn.execute("UPDATE jobs SET status='saving' WHERE id='d1'")
    assert s.requeue_for_shutdown("d1") is True
    assert s.status_of("d1") == "queued"
    # But a 'cancel_requested' member must NOT be requeued: the user's pending cancel would be
    # silently discarded and the job would run to completion after restart. The drain path
    # resolves it to 'cancelled' via resolve_group_cancel_requested instead (see run.py DRAIN).
    s.conn.execute("UPDATE jobs SET status='cancel_requested' WHERE id='d1'")
    assert s.requeue_for_shutdown("d1") is False
    assert s.status_of("d1") == "cancel_requested"  # left for the cancel-resolve path
    # And a job that already committed a terminal result is left alone.
    s.conn.execute("UPDATE jobs SET status='processing' WHERE id='d1'")
    s.set_result("d1", "d1", {"totalPages": 1, "downloadId": "d1"}, "done")
    assert s.requeue_for_shutdown("d1") is False
    assert s.status_of("d1") == "done"


def test_reap_resolves_stale_cancel_requested(tmp_path: Path) -> None:
    # A 'cancel_requested' job whose heartbeat is stale has no child running it (crash/restart);
    # nothing else terminalizes that state, so reap must resolve it to 'cancelled' — same
    # staleness rule as the 'processing' reap.
    s = _store(tmp_path)
    old = time.time() - 10_000
    s.conn.execute(
        "INSERT INTO jobs(id,mode,filename,locale,status,heartbeat_at,created_at,updated_at) "
        "VALUES('c1','pdf','a.pdf','en','cancel_requested',?,?,?)",
        (old, old, old),
    )
    s.reap(stale_s=120, max_attempts=3)
    assert s.status_of("c1") == "cancelled"
    row = s.conn.execute("SELECT status FROM progress WHERE job_id='c1'").fetchone()
    assert row["status"] == "cancelled"


def test_reap_resolves_stale_cancel_requested_group(tmp_path: Path) -> None:
    # Group semantics: reap resolves the WHOLE group of a stale 'cancel_requested' member (mirrors
    # resolve_group_cancel_requested), so a dual export's sibling can't leak a phantom active job.
    s = _store(tmp_path)
    old = time.time() - 10_000
    for jid, mode in (("gm", "markdown"), ("gw", "word")):
        s.conn.execute(
            "INSERT INTO jobs(id,mode,filename,locale,status,group_id,heartbeat_at,upload_path,created_at,updated_at) "
            "VALUES(?,?,?,?,'cancel_requested','g9',?,?,?,?)",
            (jid, mode, "a.pdf", "en", old, "/tmp/shared.pdf", old, old),
        )
    s.reap(stale_s=120, max_attempts=3)
    assert s.status_of("gm") == "cancelled"
    assert s.status_of("gw") == "cancelled"


def test_reap_leaves_fresh_cancel_requested(tmp_path: Path) -> None:
    # A 'cancel_requested' job with a FRESH heartbeat still has a live child working it (which
    # will resolve the cancel itself between pages) — reap must not steal it.
    s = _store(tmp_path)
    now = time.time()
    s.conn.execute(
        "INSERT INTO jobs(id,mode,filename,locale,status,heartbeat_at,created_at,updated_at) "
        "VALUES('cf','pdf','a.pdf','en','cancel_requested',?,?,?)",
        (now, now, now),
    )
    s.reap(stale_s=120, max_attempts=3)
    assert s.status_of("cf") == "cancel_requested"


def test_requeue_for_shutdown_returns_false_on_cancel_requested(tmp_path: Path) -> None:
    # requeue_for_shutdown is guarded off 'cancel_requested' (a user's pending cancel must resolve
    # to 'cancelled', not be silently requeued and run to completion after restart): it must leave
    # the row untouched and return False so the DRAIN path can hand it to set_cancelled instead.
    s = _store(tmp_path)
    now = time.time()
    s.conn.execute(
        "INSERT INTO jobs(id,mode,filename,locale,status,attempts,created_at,updated_at) "
        "VALUES('cr','pdf','a.pdf','en','cancel_requested',1,?,?)",
        (now, now),
    )
    assert s.requeue_for_shutdown("cr") is False
    assert s.status_of("cr") == "cancel_requested"


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
