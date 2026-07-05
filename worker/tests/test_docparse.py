from pathlib import Path
from typing import Any

from worker.docparse import save_markdown


class _FakeMdResult:
    """Minimal stand-in for a PaddleOCR-VL result: save_to_markdown writes one .md file."""

    def __init__(self, text: str) -> None:
        self.text = text

    def save_to_markdown(self, save_path: str) -> None:
        (Path(save_path) / "page.md").write_text(self.text, encoding="utf-8")


def test_save_markdown_wipes_stale_artifacts_from_prior_attempt(tmp_path: Path) -> None:
    # A requeued job re-enters save_markdown with a dir a prior (crashed) attempt polluted.
    # Without the wipe, the leftover .md is globbed and merged into the output. The dir must
    # be recreated first so only the current attempt's content survives.
    out = tmp_path / "job1"
    out.mkdir()
    (out / "stale.md").write_text("STALE-CONTENT", encoding="utf-8")
    (out / "job1.md").write_text("STALE-FINAL", encoding="utf-8")  # prior attempt's final file

    results: list[Any] = [_FakeMdResult("FRESH-CONTENT")]
    save_markdown(results, out, "job1", "en")

    final = (out / "job1.md").read_text(encoding="utf-8")
    assert "FRESH-CONTENT" in final
    assert "STALE" not in final
