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
import sys
from collections.abc import Callable
from typing import Any

from . import config, docparse, models
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
    )
    res: dict[str, Any] = {
        "totalPages": result["total_pages"],
        "downloadId": jid,
        "originalName": job["filename"],
    }
    if "warning" in result:
        res["warning"] = result["warning"]
    store.set_result(jid, jid, res, msg("done", locale))


def run_vl_job(store: Store, vl: Any, job: dict[str, Any]) -> None:
    group_id = job.get("group_id")
    group = store.group_jobs(group_id) if group_id else [job]
    # active members of the group (dual export runs VL once for md + word)
    modes: dict[str, dict[str, Any]] = {}
    for g in group:
        if g["id"] == job["id"] or g["status"] not in ("done", "error", "cancelled"):
            modes[g["mode"]] = g
    job_ids = [g["id"] for g in modes.values()]
    locale = job["locale"]

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
                    },
                    msg("done", locale),
                )
            except Exception as e:
                log.exception("word export failed for %s", wj["id"])
                store.set_error(wj["id"], str(e))


def main() -> None:
    family = sys.argv[1] if len(sys.argv) > 1 else "ppocr"
    on_gpu = models.set_device()
    store = Store(config.DB_PATH, config.SCHEMA_PATH)
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
