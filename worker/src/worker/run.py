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
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from . import config, models
from .store import Store

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] worker: %(message)s")
log = logging.getLogger("worker")

FAMILY_BY_MODE = {"pdf": "ppocr", "markdown": "vl", "word": "vl"}
_stop = False


def _handle_signal(signum: int, _frame: Any) -> None:
    global _stop
    log.info("signal %s received; draining", signum)
    _stop = True


class ModelChild:
    """A single-family inference subprocess, spoken to over stdin/stdout markers."""

    def __init__(self, family: str) -> None:
        self.family = family
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "worker.child", family],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
            bufsize=1,
            env=os.environ.copy(),
        )
        if not self._await("@@READY"):
            raise RuntimeError(f"{family} child failed to reach READY")
        log.info("child %s ready (pid %s)", family, self.proc.pid)

    def _await(self, marker: str) -> bool:
        assert self.proc.stdout is not None
        while True:
            line = self.proc.stdout.readline()
            if line == "":
                return False
            line = line.strip()
            if line == marker:
                return True
            if line.startswith("@@FATAL"):
                log.error("child fatal: %s", line)
                return False

    def process(self, job: dict[str, Any]) -> str:
        """Send a job and block until completion. Returns DONE | ERR | DEAD."""
        assert self.proc.stdin is not None and self.proc.stdout is not None
        try:
            self.proc.stdin.write(json.dumps(job) + "\n")
            self.proc.stdin.flush()
        except (BrokenPipeError, ValueError):
            return "DEAD"
        while True:
            line = self.proc.stdout.readline()
            if line == "":
                return "DEAD"
            line = line.strip()
            if line.startswith("@@DONE"):
                return "DONE"
            if line.startswith("@@ERR"):
                return "ERR"

    def alive(self) -> bool:
        return self.proc.poll() is None

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

    def ensure_child(family: str) -> ModelChild:
        nonlocal child
        if child is not None and (child.family != family or not child.alive()):
            child.close()
            child = None
        if child is None:
            child = ModelChild(family)
        return child

    try:
        while not _stop:
            now = time.time()
            if now - last_hb > 5:
                fam = child.family if child else None
                store.heartbeat(fam, models.gpu_info(fam))
                last_hb = now
            if now - last_gc > config.GC_INTERVAL_S:
                try:
                    store.gc(config.JOB_MAX_AGE_S, config.OUTPUTS_DIR, config.UPLOADS_DIR)
                except Exception as e:
                    log.warning("gc error: %s", e)
                last_gc = now

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

            outcome = current.process(job)
            if outcome == "DEAD":
                requeued = store.requeue(job["id"], config.MAX_ATTEMPTS, "inference process crashed")
                log.warning("child died on %s (requeued=%s)", job["id"], requeued)
                current.close()
                child = None
            else:
                cleanup_upload(store, job)
                log.info("job %s -> %s", job["id"], outcome)
    finally:
        if child is not None:
            child.close()
        store.close()
        log.info("supervisor stopped")


if __name__ == "__main__":
    main()
