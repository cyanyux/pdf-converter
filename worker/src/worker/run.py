"""Worker supervisor: owns the SQLite queue and one model-child subprocess.

Loop: reap interrupted jobs -> poll/claim -> pick model family -> ensure the
right child is up (tearing the other down first, so the OS reclaims VRAM) ->
hand the job over -> record completion. A child crash (e.g. OOM) requeues the
job (bounded by attempts) and rebuilds the child. Heartbeat + retention GC run
on a timer; SIGTERM drains cleanly.
"""

from __future__ import annotations

import json
import logging
import os
import queue
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from . import config, models
from .i18n import msg
from .store import TERMINAL, Store

# Sentinel distinguishing "no line within the tick" from a real line or EOF (None).
_TICK_EMPTY = object()

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] worker: %(message)s")
log = logging.getLogger("worker")

FAMILY_BY_MODE = {"pdf": "ppocr", "markdown": "vl", "word": "vl"}
# Progress statuses meaning "past recognition, in the opaque CPU save/consolidation phase";
# the watchdog gives these a looser idle bound (config.SAVE_IDLE_TIMEOUT_S).
_SAVE_PHASES = ("saving", "converting")
_stop = False


def _handle_signal(signum: int, _frame: Any) -> None:
    global _stop
    log.info("signal %s received; draining", signum)
    _stop = True


class ModelChild:
    """A single-family inference subprocess, spoken to over stdin/stdout markers.

    A dedicated reader thread drains the child's stdout into a queue so the supervisor
    NEVER blocks indefinitely on a wedged child: it waits on the queue with a timeout,
    running its watchdog/heartbeat between ticks. Draining in a thread also means the OS
    stdout pipe can't back up regardless of how chatty the child is.
    """

    def __init__(self, family: str, load_timeout_s: float, on_wait: Callable[[], None] | None = None) -> None:
        self.family = family
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "worker.child", family],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
            bufsize=1,
            env=os.environ.copy(),
        )
        self._lines: queue.Queue[str | None] = queue.Queue()
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()
        if not self._await("@@READY", load_timeout_s, on_wait):
            self.kill()
            raise RuntimeError(f"{family} child failed to reach READY")
        log.info("child %s ready (pid %s)", family, self.proc.pid)

    def _read_loop(self) -> None:
        assert self.proc.stdout is not None
        try:
            while True:
                line = self.proc.stdout.readline()
                if line == "":
                    break
                self._lines.put(line)
        finally:
            self._lines.put(None)  # EOF sentinel

    def _next(self, timeout: float) -> Any:
        """Next line, None on EOF, or _TICK_EMPTY if nothing arrived within `timeout`."""
        try:
            return self._lines.get(timeout=timeout)
        except queue.Empty:
            return _TICK_EMPTY

    def _await(self, marker: str, timeout_s: float, on_wait: Callable[[], None] | None = None) -> bool:
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            item = self._next(1.0)
            if item is _TICK_EMPTY:
                if self.proc.poll() is not None:
                    return False  # child exited without emitting the marker
                if on_wait is not None:
                    on_wait()  # keep the worker heartbeat fresh during a long (~2 GB) model load
                continue
            if item is None:
                return False
            line = item.strip()
            if line == marker:
                return True
            if line.startswith("@@FATAL"):
                log.error("child fatal: %s", line)
                return False
        return False

    def process(self, job: dict[str, Any], on_tick: Callable[[], str | None], tick_s: float) -> str:
        """Send a job; block until completion, calling on_tick() whenever a tick elapses
        with no child output. Returns DONE | ERR | DEAD, or whatever truthy verdict on_tick
        returns (TIMEOUT | CANCELLED) so the supervisor can kill + recover a wedged child."""
        assert self.proc.stdin is not None
        try:
            self.proc.stdin.write(json.dumps(job) + "\n")
            self.proc.stdin.flush()
        except (BrokenPipeError, ValueError):
            return "DEAD"
        while True:
            item = self._next(tick_s)
            if item is _TICK_EMPTY:
                verdict = on_tick()
                if verdict:
                    return verdict
                continue
            if item is None:
                return "DEAD"
            line = item.strip()
            if line.startswith("@@DONE"):
                return "DONE"
            if line.startswith("@@ERR"):
                return "ERR"
            if line.startswith("@@FATAL"):
                # Child hit an unrecoverable error post-READY but is still alive; don't
                # wait out the idle watchdog — treat it as dead so the supervisor
                # closes + requeues immediately.
                log.error("child fatal: %s", line)
                return "DEAD"

    def alive(self) -> bool:
        return self.proc.poll() is None

    def kill(self) -> None:
        """Hard-kill a wedged/timed-out child. A child stuck in a native GPU op won't
        honour QUIT/SIGTERM promptly, so go straight to SIGKILL; process exit reclaims VRAM."""
        try:
            self.proc.kill()
            self.proc.wait(timeout=5)
        except Exception:
            pass
        log.info("child %s killed", self.family)

    def close(self) -> None:
        try:
            if self.proc.stdin and not self.proc.stdin.closed:
                self.proc.stdin.write("QUIT\n")
                self.proc.stdin.flush()
                self.proc.stdin.close()
            self.proc.wait(timeout=10)
        except Exception:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except Exception:
                self.proc.kill()
        log.info("child %s closed", self.family)


def cleanup_upload(store: Store, job: dict[str, Any]) -> None:
    up = job.get("upload_path")
    if up and store.upload_refcount_active(up, job["id"]) == 0:
        Path(up).unlink(missing_ok=True)


def main() -> None:
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)
    config.OUTPUTS_DIR.mkdir(parents=True, exist_ok=True)
    config.UPLOADS_DIR.mkdir(parents=True, exist_ok=True)

    store = Store(config.DB_PATH, config.SCHEMA_PATH)
    store.reap(config.STALE_S, config.MAX_ATTEMPTS)
    log.info("supervisor started (device=%s)", config.DEVICE)

    child: ModelChild | None = None
    last_hb = 0.0
    last_gc = time.time()
    last_reap = time.time()

    def ensure_child(family: str) -> ModelChild:
        nonlocal child
        if child is not None and (child.family != family or not child.alive()):
            child.close()
            child = None
        if child is None:
            # on_wait=maintenance keeps the worker heartbeat fresh during the model load
            # (first run downloads ~2 GB), so /health and the container HEALTHCHECK don't
            # flag a still-loading worker as dead.
            child = ModelChild(family, config.MODEL_LOAD_TIMEOUT_S, on_wait=maintenance)
        return child

    def maintenance() -> None:
        """Heartbeat + retention GC. Runs both between jobs AND, via the watchdog tick,
        while a child is busy — so /health and GC never stall during a long parse."""
        nonlocal last_hb, last_gc
        now = time.time()
        if now - last_hb > 5:
            try:
                fam = child.family if child else None
                store.heartbeat(fam, models.gpu_info(fam))
            except Exception as e:
                log.warning("heartbeat error: %s", e)
            last_hb = now
        if now - last_gc > config.GC_INTERVAL_S:
            try:
                store.gc(config.JOB_MAX_AGE_S, config.OUTPUTS_DIR, config.UPLOADS_DIR)
            except Exception as e:
                log.warning("gc error: %s", e)
            last_gc = now

    def watchdog(job: dict[str, Any]) -> Callable[[], str | None]:
        """Per-claim tick callback. Keeps maintenance running, and returns a verdict that
        makes process() give up on a stuck child: TIMEOUT (no progress for JOB_IDLE_TIMEOUT_S
        — heartbeat_at advances per page) or CANCELLED (cancel requested and the child didn't
        self-cancel between pages within CANCEL_GRACE_S).

        Group-aware: a VL dual export (markdown+word) is driven by ONE claimed job, but the
        child advances and saves EACH group member under its own id. So the watchdog must look
        at the whole group, not just the claimed job — otherwise, once the claimed member (say
        markdown) is set 'done' and its heartbeat freezes, a slow word save (which heartbeats
        only the word job) is false-killed on the frozen 'done' heartbeat, the exact wedge the
        save-phase leniency was meant to avoid. Group membership is fixed at claim time."""
        gid = job.get("group_id")
        ids = [g["id"] for g in store.group_jobs(gid)] if gid else [job["id"]]
        if job["id"] not in ids:
            ids.append(job["id"])
        state: dict[str, float | None] = {"cancel_at": None}

        def on_tick() -> str | None:
            maintenance()
            now = time.time()
            # Escalate to a hard kill only when every member the child is ACTUALLY working is
            # cancelled — the claimed job plus any still-non-terminal sibling. This mirrors the
            # child's _cancel_cb (child.py), which builds job_ids from the same active set: a
            # partial cancel is handled per-export by the child, which still produces the
            # sibling the user did not cancel. Including an already-'done' sibling here would
            # wedge all() False and defer a genuine whole-group cancel from CANCEL_GRACE_S to
            # the slow TIMEOUT path. (job["id"] is always kept, so active_ids is never empty.)
            active_ids = [i for i in ids if i == job["id"] or store.status_of(i) not in TERMINAL]
            if all(store.is_cancel_requested(i) for i in active_ids):
                if state["cancel_at"] is None:
                    state["cancel_at"] = now
                elif now - state["cancel_at"] > config.CANCEL_GRACE_S:
                    return "CANCELLED"
            else:
                state["cancel_at"] = None
            # Freshest heartbeat across the group: recognition bumps all members, each save
            # bumps only its own job, so the max is the child's true last-progress time.
            hbs = [h for h in (store.job_heartbeat_at(i) for i in ids) if h is not None]
            if hbs:
                # Recognition (progress status 'processing') keeps the strict CUDA-hang
                # timeout; if ANY member is in the opaque save/consolidation phase, use the
                # looser bound so a slow-but-alive save isn't mistaken for a wedge.
                in_save = any(store.progress_status(i) in _SAVE_PHASES for i in ids)
                limit = config.SAVE_IDLE_TIMEOUT_S if in_save else config.JOB_IDLE_TIMEOUT_S
                if now - max(hbs) > limit:
                    return "TIMEOUT"
            return None

        return on_tick

    try:
        while not _stop:
            now = time.time()
            maintenance()
            if now - last_reap > config.REAP_INTERVAL_S:
                # Recover jobs an earlier crashed worker left in 'processing'. Safe here
                # (between claims): the live child's job is guarded by the watchdog, not reap.
                try:
                    store.reap(config.STALE_S, config.MAX_ATTEMPTS)
                except Exception as e:
                    log.warning("reap error: %s", e)
                last_reap = now

            job = store.claim()
            if job is None:
                time.sleep(config.POLL_S)
                continue

            family = FAMILY_BY_MODE.get(job["mode"], "vl")
            log.info("claimed %s (%s -> %s)", job["id"], job["mode"], family)
            try:
                current = ensure_child(family)
            except Exception as e:
                store.requeue(job["id"], config.MAX_ATTEMPTS, f"model load failed: {e}")
                log.exception("failed to start %s child", family)
                child = None
                time.sleep(1)
                continue

            outcome = current.process(job, watchdog(job), config.WATCHDOG_TICK_S)
            if outcome in ("DONE", "ERR"):
                cleanup_upload(store, job)
                log.info("job %s -> %s", job["id"], outcome)
            elif outcome == "CANCELLED":
                # Child wedged mid-page past the cancel grace: kill it and record the
                # cancellation (unless it already committed a terminal result).
                current.kill()
                child = None
                if store.status_of(job["id"]) not in TERMINAL:
                    store.set_cancelled(job["id"], msg("cancelled", job.get("locale")))
                cleanup_upload(store, job)
                log.warning("job %s killed on cancel request", job["id"])
            else:  # DEAD (child exited) or TIMEOUT (wedged -> kill)
                if outcome == "TIMEOUT":
                    current.kill()
                else:
                    current.close()
                child = None
                reason = "inference timed out" if outcome == "TIMEOUT" else "inference process crashed"
                requeued = store.requeue(job["id"], config.MAX_ATTEMPTS, reason)
                log.warning("child %s on %s (requeued=%s)", outcome, job["id"], requeued)
    finally:
        if child is not None:
            child.close()
        store.close()
        log.info("supervisor stopped")


if __name__ == "__main__":
    main()
