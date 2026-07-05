"""run_digital_markdown progress streaming: per-page events must reach on_progress live,
on the caller's thread (the child's sqlite connection is thread-bound), even though the
Docling convert() runs on a worker thread. Docling itself is faked — these tests exercise
the queue pump, not the engine."""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

import pymupdf as fitz
import pytest

from worker import docparse_digital


def _make_pdf(path: Path, pages: int) -> None:
    doc = fitz.open()
    for _ in range(pages):
        doc.new_page()
    doc.save(str(path))
    doc.close()


class _FakeDoc:
    def __init__(self, pages: int) -> None:
        self._pages = pages

    def num_pages(self) -> int:
        return self._pages

    def save_as_markdown(self, filename: Any, artifacts_dir: Any = None, image_mode: Any = None) -> None:
        Path(filename).write_text("# fake\n", encoding="utf-8")


class _FakeResult:
    def __init__(self, pages: int) -> None:
        self.document = _FakeDoc(pages)


def _fake_build_converter(pages: int, fail: Exception | None = None) -> Any:
    """Stand-in for _build_converter: convert() emits one page event per page from its own
    thread (run_digital_markdown runs convert on a worker thread, mirroring docling's
    assemble-stage-thread callbacks), then returns a result exposing .document."""

    def build(on_page_done: Any) -> Any:
        class _Converter:
            def convert(self, _path: str) -> _FakeResult:
                for page_no in range(1, pages + 1):
                    on_page_done(page_no)
                if fail is not None:
                    raise fail
                return _FakeResult(pages)

        return _Converter()

    return build


def test_streams_per_page_progress_on_caller_thread(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pages = 3
    pdf = tmp_path / "in.pdf"
    _make_pdf(pdf, pages)
    monkeypatch.setattr(docparse_digital, "_build_converter", _fake_build_converter(pages))

    events: list[tuple[int, int, str, str]] = []
    threads: set[str] = set()

    def on_progress(current: int, total: int, status: str, message: str) -> None:
        threads.add(threading.current_thread().name)
        events.append((current, total, status, message))

    r = docparse_digital.run_digital_markdown(
        str(pdf), tmp_path / "out", "dl1", on_progress, lambda: False, "en"
    )

    assert r["total_pages"] == pages
    # One event per completed page, monotonically increasing, all in the save phase
    # (looser watchdog bound — model load and the assemble/save tail have no heartbeat).
    page_events = [e for e in events if "Converting page" in e[3]]
    assert [(e[0], e[1]) for e in page_events] == [(1, 3), (2, 3), (3, 3)]
    assert all(e[2] == "saving" for e in events)
    # Every progress write happened on the caller's thread: the child Store's sqlite
    # connection is check_same_thread=True, so a callback from the convert thread would break.
    assert threads == {threading.main_thread().name}
    assert (tmp_path / "out" / "dl1.md").read_text(encoding="utf-8") == "# fake\n"


def test_convert_failure_propagates_after_draining_events(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pdf = tmp_path / "in.pdf"
    _make_pdf(pdf, 2)
    boom = RuntimeError("docling exploded")
    monkeypatch.setattr(docparse_digital, "_build_converter", _fake_build_converter(2, fail=boom))

    events: list[int] = []
    with pytest.raises(RuntimeError, match="docling exploded"):
        docparse_digital.run_digital_markdown(
            str(pdf), tmp_path / "out", "dl2", lambda c, t, s, m: events.append(c), lambda: False, "en"
        )
    # Pages completed before the failure were still reported; the loop drained to the sentinel.
    assert 2 in events
