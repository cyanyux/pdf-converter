"""Text post-processing shared by the OCR and doc-parse pipelines."""

from __future__ import annotations

from .i18n import normalize_locale
from .s2t import S2T_ONE_TO_ONE

_S2T_TABLE = str.maketrans(S2T_ONE_TO_ONE)


def fix_ocr_text(text: str, locale: str | None = None) -> str:
    """Correct mis-recognized Simplified characters to Traditional.

    Only applied for zh-TW (the default). zh-CN and en are returned as-is, so
    Simplified-Chinese users keep native output.
    """
    if normalize_locale(locale) != "zh-TW":
        return text
    return text.translate(_S2T_TABLE)
