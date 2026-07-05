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
import shutil
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pymupdf as fitz

from . import config, models, probe
from .engines import ENGINE_NONE
from .i18n import msg
from .store import TERMINAL, Store

# Sentinel distinguishing "no line within the tick" from a real line or EOF (None).
_TICK_EMPTY = object()

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] worker: %(message)s")
log = logging.getLogger("worker")

# Default family per mode; markdown is refined at claim time by probe.route_markdown (a digital
# PDF routes to the 'docling' family, a scanned one to 'vl'). pdf/word are fixed.
FAMILY_BY_MODE = {"pdf": "ppocr", "markdown": "vl", "word": "vl"}
# Progress statuses meaning "past recognition, in the opaque CPU save/consolidation phase";
# the watchdog gives these a looser idle bound (config.SAVE_IDLE_TIMEOUT_S).
_SAVE_PHASES = ("saving", "converting")
_stop = False

# The message a pinned engine='docling' job fails with when the upload is NOT born-digital.
# Docling runs do_ocr=False and would silently drop any raster-only content, so an ineligible
# input must fail loudly (never fall back) — the caller resubmits with engine=vl / engine=auto.
# The eligibility numbers are interpolated from probe's routing constants so the message can
# never claim a threshold the router no longer uses.
DOCLING_INELIGIBLE_MSG = (
    f"engine=docling requires a born-digital PDF (>={probe.DIGITAL_RATIO:.0%} of pages with a "
    "text layer, no text on raster-only pages) — this document routes to VL; resubmit with "
    "engine=vl or engine=auto"
)


class DoclingIneligible(Exception):
    """Raised by pick_family when a pinned engine='docling' markdown job is not born-digital.

    Caught in the main claim loop, where it becomes a set_error + cleanup_upload for the job
    (mirroring the already-searchable short-circuit's error handling), NOT a silent VL fallback."""


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

    def __init__(
        self,
        family: str,
        load_timeout_s: float,
        on_wait: Callable[[], None] | None = None,
        env: dict[str, str] | None = None,
    ) -> None:
        self.family = family
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "worker.child", family],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
            bufsize=1,
            env=env if env is not None else os.environ.copy(),
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
        returns (TIMEOUT | CANCELLED | DRAIN) so the supervisor can kill + recover a wedged
        child, or promptly abort an in-flight job on graceful shutdown."""
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


def pick_family(job: dict[str, Any]) -> str:
    """Resolve the model family for a claimed job, honouring the requested markdown engine.

    pdf -> ppocr, word -> vl (fixed). markdown obeys the job's `engine` (default 'auto'):
      - 'vl': the VL family directly, no probe needed.
      - 'docling': eligibility is VERIFIED — probe.route_markdown must agree the PDF is born-digital
        ('docling'). Docling runs do_ocr=False, so an ineligible input would silently lose raster
        content; if it routes 'vl' OR the probe raises we raise DoclingIneligible (the loop fails the
        job) rather than ever falling back to VL.
      - 'auto' (or missing): route by probe.route_markdown against the upload — a born-digital PDF ->
        'docling' (CPU, text-faithful), a scanned one -> 'vl'. A probe failure (unreadable upload)
        falls back to 'vl', whose own pipeline surfaces the real error.
    """
    base = FAMILY_BY_MODE.get(job["mode"], "vl")
    if job["mode"] != "markdown":
        return base
    engine = job.get("engine") or "auto"
    if engine == "vl":
        return "vl"
    up = job.get("upload_path")
    if engine == "docling":
        # Pinned Docling: never guess. A missing upload and a failing probe both count as
        # ineligible (each logged distinctly), and the job fails — no silent VL fallback.
        if not up:
            log.warning("pinned docling job %s has no upload_path; failing", job["id"])
            raise DoclingIneligible(DOCLING_INELIGIBLE_MSG)
        try:
            route = probe.route_markdown(up)
        except Exception as e:
            log.warning("route_markdown probe failed for pinned docling job %s (%s); failing", job["id"], e)
            raise DoclingIneligible(DOCLING_INELIGIBLE_MSG) from e
        if route != "docling":
            raise DoclingIneligible(DOCLING_INELIGIBLE_MSG)
        return "docling"
    # engine == 'auto': probe-route as before, falling back to VL on an unreadable upload.
    if not up:
        return "vl"
    try:
        return probe.route_markdown(up)
    except Exception as e:
        log.warning("route_markdown probe failed for %s (%s); using vl", job["id"], e)
        return "vl"


def _docling_env() -> dict[str, str]:
    """Environment for the docling child: the GPU is HIDDEN so it can never touch VRAM.

    CUDA_VISIBLE_DEVICES="" is set ONLY in the child's copy of the environment — never the
    supervisor's or the VL/PP-OCR children's — so the digital-markdown path stays strictly CPU
    while the GPU families keep full device access.
    """
    env = os.environ.copy()
    env["CUDA_VISIBLE_DEVICES"] = ""
    return env


def complete_already_searchable(store: Store, job: dict[str, Any], total: int) -> None:
    """Short-circuit a mode=pdf job whose input already has a text layer on EVERY page.

    No OCR is needed, so no model child boots: copy the original upload to
    outputs/<download_id>/<download_id>.pdf and mark the job done. `total` is the page count from
    the caller's single classify_pages pass (no re-probe here). Mirrors run_ppocr's result shape
    plus engine='none' + notice='already_searchable' (the TS side reads both). The out dir is
    wiped+recreated first for requeue safety, like the save pipelines.
    """
    jid, locale = job["id"], job.get("locale")
    out_dir = config.OUTPUTS_DIR / jid
    shutil.rmtree(out_dir, ignore_errors=True)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_pdf = out_dir / f"{jid}.pdf"
    shutil.copyfile(job["upload_path"], out_pdf)
    # Clean /Title from the real filename, mirroring child.py run_ppocr, so an inline-viewed PDF
    # shows the right tab caption in Chrome instead of the source's often-mojibaked title. Best
    # effort: an incremental metadata save keeps the file near-byte-identical (only an appended
    # xref differs), and a failure here must still ship the copy.
    try:
        doc = fitz.open(out_pdf)
        try:
            meta = dict(doc.metadata or {})
            meta["title"] = Path(job["filename"]).stem
            doc.set_metadata(meta)
            doc.save(str(out_pdf), incremental=True, encryption=fitz.PDF_ENCRYPT_KEEP)  # type: ignore[attr-defined]
        finally:
            doc.close()
    except Exception as e:
        log.warning("already-searchable /Title update skipped for %s: %s", jid, e)
    res: dict[str, Any] = {
        "totalPages": total or 1,
        "downloadId": jid,
        "originalName": job["filename"],
        "engine": ENGINE_NONE,
        "notice": "already_searchable",
    }
    store.set_result(jid, jid, res, msg("done", locale))


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
            # The docling (digital-markdown) child runs GPU-hidden so it can never touch VRAM;
            # switching to it also tears down any live GPU child first (above), so families never
            # contend for the device. on_wait=maintenance keeps the worker heartbeat fresh during
            # the model load (first VL/PP-OCR run downloads ~2 GB) so /health and the container
            # HEALTHCHECK don't flag a still-loading worker as dead.
            env = _docling_env() if family == "docling" else None
            child = ModelChild(family, config.MODEL_LOAD_TIMEOUT_S, on_wait=maintenance, env=env)
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
            # A SIGTERM (container stop/restart) sets _stop, but the outer while-loop only
            # observes it BETWEEN claims — a long save-phase job would block here past
            # supervisord's stopwaitsecs, get SIGKILLed, and orphan the model child holding
            # VRAM. Surface the shutdown as a DRAIN verdict so process() unwinds at the next
            # idle tick (between pages / exports): the supervisor then kills the child promptly
            # and REQUEUES the job (not user-cancelled) so the reaper/attempts re-run it on the
            # next start. Checked first so shutdown always wins over cancel/timeout bookkeeping.
            if _stop:
                return "DRAIN"
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

            # Already-searchable short-circuit: a mode=pdf job whose input already has a text
            # layer on every page needs no OCR — complete it WITHOUT booting any child (the live
            # child, if any, stays up for the next real job). A probe failure falls through to the
            # normal PP-OCR path. Runs before family selection so no child is torn down/booted.
            if job["mode"] == "pdf" and job.get("upload_path"):
                # Walk the PDF ONCE here: classify, apply the pure predicate, and reuse the page
                # count for the result (complete_already_searchable no longer re-probes).
                try:
                    info = probe.classify_pages(job["upload_path"])
                    searchable = probe.already_searchable(info)
                except Exception as e:
                    log.warning("is_already_searchable probe failed for %s (%s); running OCR", job["id"], e)
                    info, searchable = None, False
                if searchable:
                    try:
                        assert info is not None
                        complete_already_searchable(store, job, info["total"])
                        cleanup_upload(store, job)
                        log.info("job %s -> DONE (already searchable, no child)", job["id"])
                    except Exception as e:
                        log.exception("already-searchable short-circuit failed for %s", job["id"])
                        store.requeue(job["id"], config.MAX_ATTEMPTS, f"already-searchable copy failed: {e}")
                    continue

            # A pinned engine='docling' markdown job whose upload isn't born-digital fails HERE,
            # before any child boots (like the already-searchable short-circuit above): record the
            # error and release the upload. Never a silent VL fallback for a pinned engine.
            try:
                family = pick_family(job)
            except DoclingIneligible as e:
                store.set_error(job["id"], str(e))
                cleanup_upload(store, job)
                log.info("job %s -> ERROR (engine=docling ineligible)", job["id"])
                continue
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
                cancel_msg = msg("cancelled", job.get("locale"))
                if store.status_of(job["id"]) not in TERMINAL:
                    store.set_cancelled(job["id"], cancel_msg)
                # The SIGKILL above pre-empts the child's own group-wide Cancelled handler
                # (child.py sets EVERY member cancelled), so a dual export (markdown+word share
                # one VL pass via group_id) would leave the UN-claimed sibling stuck forever at
                # 'cancel_requested' — a non-terminal state no reap/claim/gc resolves, leaking a
                # phantom active job and pinning the shared upload. Resolve the whole group.
                store.resolve_group_cancel_requested(job.get("group_id"), cancel_msg)
                cleanup_upload(store, job)
                log.warning("job %s killed on cancel request", job["id"])
            elif outcome == "DRAIN":
                # Graceful shutdown observed mid-job: kill the child now (VRAM reclaimed on exit)
                # so we don't overrun supervisord's stopwaitsecs, and re-queue the job WITHOUT
                # counting an attempt (a drain is not a failure). Re-queue every non-terminal
                # group member so a dual export's sibling is picked up on next start too. The
                # outer `while not _stop` then exits and the finally-block tears down cleanly.
                current.kill()
                child = None
                gid = job.get("group_id")
                # A user's pending cancel must survive the drain: resolve every 'cancel_requested'
                # member to terminal 'cancelled' FIRST, so the requeue below (guarded off
                # 'cancel_requested') can't resurrect it back to 'queued' and run it to completion
                # after restart. resolve_group_cancel_requested is a no-op for a NULL group_id and
                # for a single-export 'cancel_requested' job, so handle that case explicitly too.
                store.resolve_group_cancel_requested(gid, msg("cancelled", job.get("locale")))
                if not gid and store.is_cancel_requested(job["id"]):
                    store.set_cancelled(job["id"], msg("cancelled", job.get("locale")))
                ids = [g["id"] for g in store.group_jobs(gid)] if gid else [job["id"]]
                if job["id"] not in ids:
                    ids.append(job["id"])
                for jid in ids:
                    # requeue_for_shutdown is guarded off 'cancel_requested' and returns False when
                    # it didn't touch a row. A race can leave the non-group claimed job at
                    # 'cancel_requested' AFTER the resolve above (the user cancelled between them):
                    # the requeue no-ops, and nothing else terminalizes it. Detect that no-op and
                    # let the cancel win via the existing cancelled path, so it never strands.
                    if not store.requeue_for_shutdown(jid) and store.status_of(jid) == "cancel_requested":
                        store.set_cancelled(jid, msg("cancelled", job.get("locale")))
                log.warning("job %s requeued on shutdown drain", job["id"])
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
