from worker.i18n import msg, normalize_locale
from worker.s2t import S2T_ONE_TO_ONE
from worker.text_utils import fix_ocr_text


def test_s2t_only_applies_for_zh_tw() -> None:
    simp, trad = next(iter(S2T_ONE_TO_ONE.items()))
    assert fix_ocr_text(simp, "zh-TW") == trad
    assert fix_ocr_text(simp, "zh-CN") == simp
    assert fix_ocr_text(simp, "en") == simp
    assert fix_ocr_text(simp, None) == trad  # default locale is zh-TW


def test_normalize_locale() -> None:
    assert normalize_locale("zh-Hant") == "zh-TW"
    assert normalize_locale("zh-Hans") == "zh-CN"
    assert normalize_locale("zh-CN") == "zh-CN"
    assert normalize_locale("en-US") == "en"
    assert normalize_locale(None) == "zh-TW"
    assert normalize_locale("fr") == "zh-TW"  # unknown -> default


def test_msg_translation_and_formatting() -> None:
    assert msg("done", "en") == "Done!"
    assert "頁" in msg("recognizing_page", "zh-TW", current=1, total=5)
    assert msg("unknown_key", "en") == "unknown_key"  # missing key returns the key
