"""Model-child subprocess: loads exactly one model family and processes jobs.

Invoked by the supervisor as `python -m worker.child <ppocr|vl>`. Reads job
requests (one JSON per line) on stdin, runs the pipeline (writing progress /
result / error to the DB via Store, checking cancel between pages), and prints
`DONE <id>` / `ERR <id>` per job. Exits on EOF. Process exit reclaims all VRAM,
which is how the supervisor guarantees a clean family switch.
"""

from __future__ import annotations

import json
import logging
import signal
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from . import config, docparse, models, probe
from .engines import ENGINE_DOCLING, ENGINE_PPOCR, ENGINE_VL
from .i18n import msg
from .searchable_pdf import Cancelled, create_searchable_pdf
from .store import Store

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] child: %(message)s")
log = logging.getLogger("child")


def _emit(line: str) -> None:
    sys.stdout.write(line + "\n")
    sys.stdout.flush()


def _progress_cb(store: Store, job_ids: list[str]) -> Callable[[int, int, str, str], None]:
    def cb(current: int, total: int, status: str, message: str) -> None:
        for jid in job_ids:
            store.set_progress(jid, current, total, status, message)

    return cb


def _cancel_cb(store: Store, job_ids: list[str]) -> Callable[[], bool]:
    # Dual export: cancel the VL run only if ALL group jobs are cancelled.
    def cb() -> bool:
        return all(store.is_cancel_requested(jid) for jid in job_ids)

    return cb


def run_ppocr(store: Store, ocr: Any, job: dict[str, Any]) -> None:
    jid, locale = job["id"], job["locale"]
    out_dir = config.OUTPUTS_DIR / jid
    out_dir.mkdir(parents=True, exist_ok=True)
    out_pdf = out_dir / f"{jid}.pdf"
    result = create_searchable_pdf(
        ocr,
        job["upload_path"],
        str(out_pdf),
        _progress_cb(store, [jid]),
        _cancel_cb(store, [jid]),
        locale=locale,
        dpi=config.SEARCHABLE_DPI,
        # Clean /Title from the real filename so an inline-viewed PDF shows the right tab caption
        # in Chrome instead of the source's often-mojibaked title.
        title=Path(job["filename"]).stem,
    )
    res: dict[str, Any] = {
        "totalPages": result["total_pages"],
        "downloadId": jid,
        "originalName": job["filename"],
        "engine": ENGINE_PPOCR,
    }
    if "warning" in result:
        res["warning"] = result["warning"]
    store.set_result(jid, jid, res, msg("done", locale))


def run_docling_job(store: Store, job: dict[str, Any]) -> None:
    """Digital-markdown path (Docling, CPU): produce ONLY the claimed markdown job.

    Unlike run_vl_job, this NEVER co-produces the group — a markdown+word dual export whose
    markdown member routed to Docling has a word member that must still get its own full VL pass
    when it is claimed. So this saves exactly the one claimed job; the queued word sibling is
    left for a later VL claim. Docling is imported lazily here (docparse_digital) so the
    supervisor, the GPU children, and the tests never load it."""
    from . import docparse_digital

    jid, locale = job["id"], job["locale"]
    r = docparse_digital.run_digital_markdown(
        job["upload_path"],
        config.OUTPUTS_DIR / jid,
        jid,
        _progress_cb(store, [jid]),
        _cancel_cb(store, [jid]),
        locale,
    )
    store.set_result(
        jid,
        jid,
        {
            "totalPages": r["total_pages"],
            "downloadId": jid,
            "imagesCount": len(r["images"]),
            "originalName": job["filename"],
            "engine": ENGINE_DOCLING,
        },
        msg("done", locale),
    )


def _co_produce_markdown_sibling(sibling: dict[str, Any], upload_path: str) -> bool:
    """Should this VL word pass co-produce the group's markdown sibling, or leave it for its own
    claim?

    The sibling's OWN requested engine (default 'auto') decides, and takes precedence over the
    probe:
      - 'vl'      -> co-produce (the sibling is pinned to this very family).
      - 'docling' -> decline: the sibling's own claim runs the docling child (or fails ineligible).
      - 'auto'    -> probe.route_markdown the shared upload: co-produce iff it routes 'vl'. A probe
                     failure declines — defer to the sibling's own claim, never co-produce with a
                     guessed engine (pick_family is then the single fallback authority there).
    """
    engine = sibling.get("engine") or "auto"
    if engine == "vl":
        return True
    if engine == "docling":
        return False
    try:
        return probe.route_markdown(upload_path) == "vl"
    except Exception:
        return False


def run_vl_job(store: Store, vl: Any, job: dict[str, Any]) -> None:
    group_id = job.get("group_id")
    group = store.group_jobs(group_id) if group_id else [job]
    # active members of the group (dual export runs VL once for md + word)
    modes: dict[str, dict[str, Any]] = {}
    for g in group:
        if g["id"] != job["id"] and g["status"] in ("done", "error", "cancelled"):
            continue
        # A markdown sibling is co-produced by this VL pass ONLY when it is itself destined for VL —
        # its own requested engine decides (engine='vl' -> yes; 'docling' -> no; 'auto' -> probe the
        # shared upload, co-produce iff it routes 'vl', probe failure declines). A declined sibling
        # gets its own docling child (or a fresh VL pass) when claimed. (The CLAIMED job is always
        # kept: if it were markdown, pick_family would have sent it to the docling family, not here,
        # so keeping it on 'vl' below is the correct engine for it.) Invariant: never co-produce a
        # markdown sibling with a guessed engine — defer to its own claim, where pick_family is the
        # single fallback authority.
        if g["mode"] == "markdown" and g["id"] != job["id"] and not _co_produce_markdown_sibling(
            g, job["upload_path"]
        ):
            continue  # engine='docling' OR routes-to-docling OR probe failed -> leave for its own claim
        modes[g["mode"]] = g
    job_ids = [g["id"] for g in modes.values()]
    locale = job["locale"]

    # The recognition pass below is shared across the group and its progress is mirrored onto
    # every member, so promote the queued sibling(s) to 'processing' now — otherwise a sibling
    # shows live recognition progress while still badged 'queued' (status='processing' is the
    # honest state and keeps its heartbeat fresh for the group-aware watchdog).
    for jid in job_ids:
        store.set_processing(jid)

    try:
        restructured, total = docparse.run_vl(
            vl,
            job["upload_path"],
            _progress_cb(store, job_ids),
            _cancel_cb(store, job_ids),
            locale,
            total_steps=0,
        )
    except Cancelled:
        for jid in job_ids:
            store.set_cancelled(jid, msg("cancelled", locale))
        return

    # Each export commits (or fails) INDEPENDENTLY. A failure must not bubble out of
    # run_vl_job: main()'s generic handler would set_error() on the CLAIMED job id and
    # clobber a sibling export that already reached 'done' (data loss). Per-job try
    # keeps the failure scoped to the job whose save actually failed.
    if "markdown" in modes:
        mj = modes["markdown"]
        if store.is_cancel_requested(mj["id"]):
            store.set_cancelled(mj["id"], msg("cancelled", locale))
        else:
            try:
                r = docparse.save_markdown(restructured, config.OUTPUTS_DIR / mj["id"], mj["id"], locale)
                store.set_result(
                    mj["id"],
                    mj["id"],
                    {
                        "totalPages": total,
                        "downloadId": mj["id"],
                        "imagesCount": len(r["images"]),
                        "originalName": mj["filename"],
                        "engine": ENGINE_VL,
                    },
                    msg("done", locale),
                )
            except Exception as e:
                log.exception("markdown export failed for %s", mj["id"])
                store.set_error(mj["id"], str(e))

    if "word" in modes:
        wj = modes["word"]
        if store.is_cancel_requested(wj["id"]):
            store.set_cancelled(wj["id"], msg("cancelled", locale))
        else:
            try:
                store.set_progress(wj["id"], total, total, "converting", msg("converting_word", locale))
                r = docparse.save_word(restructured, config.OUTPUTS_DIR / wj["id"], wj["id"], locale)
                store.set_result(
                    wj["id"],
                    wj["id"],
                    {
                        "totalPages": total,
                        "downloadId": wj["id"],
                        "imagesCount": r["images_count"],
                        "originalName": wj["filename"],
                        "engine": ENGINE_VL,
                    },
                    msg("done", locale),
                )
            except Exception as e:
                log.exception("word export failed for %s", wj["id"])
                store.set_error(wj["id"], str(e))


def main() -> None:
    # The SUPERVISOR owns this child's lifecycle (kill/reap/drain). If a group-directed SIGTERM
    # (docker stop variants, a shell killing the process group) also reaches the child, it dies
    # BEFORE the supervisor's graceful DRAIN branch can run — the shutdown then takes the DEAD
    # path (attempts++, poison-pill counting) instead of the attempt-free drain requeue. Ignore
    # it here; the supervisor kills the child explicitly when it wants it gone.
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    family = sys.argv[1] if len(sys.argv) > 1 else "ppocr"
    store = Store(config.DB_PATH, config.SCHEMA_PATH)
    model: Any = None
    if family == "docling":
        # CPU-only digital-markdown path: no GPU device select, no model preload. The supervisor
        # boots this child with CUDA_VISIBLE_DEVICES="" so it can never touch VRAM. Docling itself
        # is imported lazily on first job (run_docling_job) — READY is immediate.
        log.info("loading family=docling (cpu, gpu hidden)")
        _emit("@@READY")
    else:
        on_gpu = models.set_device()
        log.info("loading family=%s (gpu=%s)", family, on_gpu)
        if family == "ppocr":
            model = models.build_ocr()
            models.warmup_ocr(model)
        elif family == "vl":
            model = models.build_vl()
        else:
            _emit(f"@@FATAL unknown family {family}")
            return
        _emit("@@READY")

    for line in sys.stdin:
        line = line.strip()
        if not line or line == "QUIT":
            break
        job = json.loads(line)
        jid = job["id"]
        try:
            if family == "ppocr":
                run_ppocr(store, model, job)
            elif family == "docling":
                run_docling_job(store, job)
            else:
                run_vl_job(store, model, job)
            _emit(f"@@DONE {jid}")
        except Cancelled:
            store.set_cancelled(jid, msg("cancelled", job.get("locale")))
            _emit(f"@@DONE {jid}")
        except Exception as e:
            store.set_error(jid, str(e))
            log.exception("job %s failed", jid)
            _emit(f"@@ERR {jid}")

    store.close()


if __name__ == "__main__":
    main()
