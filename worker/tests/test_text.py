from worker.i18n import msg, normalize_locale
from worker.text_utils import fix_ocr_text, latex_to_unicode, strip_cjk_spaces, strip_cjk_spaces_across


def test_strip_cjk_spaces() -> None:
    # VL's stray Han<->Han spaces are dropped...
    assert strip_cjk_spaces("總務科 辦公室") == "總務科辦公室"
    assert strip_cjk_spaces("中 華 民 國") == "中華民國"
    assert strip_cjk_spaces("第一條  讓與標的") == "第一條讓與標的"  # collapses a run too
    # ...but a CJK<->Latin / CJK<->digit boundary is NEVER touched (real, intentional spacing)...
    assert strip_cjk_spaces("Onvif 攝影機") == "Onvif 攝影機"
    assert strip_cjk_spaces("通知後 24 小時") == "通知後 24 小時"
    assert strip_cjk_spaces("溫度 ±5% 範圍") == "溫度 ±5% 範圍"
    # ...and a markdown table pipe survives (the space sits next to "|", not between two Han).
    assert strip_cjk_spaces("| 甲 | 乙丙 |") == "| 甲 | 乙丙 |"


def test_strip_cjk_spaces_across_run_boundaries() -> None:
    # The cross-run variant sees a Han<->Han space split over adjacent runs and deletes exactly
    # those characters, mapped back to the run each lives in; anything else is untouched.
    assert strip_cjk_spaces_across(["繳交 ", "維護名冊"]) == ["繳交", "維護名冊"]
    assert strip_cjk_spaces_across(["繳交", "  維護"]) == ["繳交", "維護"]
    assert strip_cjk_spaces_across(["交 ", " 維"]) == ["交", "維"]  # space split across both runs
    assert strip_cjk_spaces_across(["以內 ", "24小時"]) is None  # CJK<->digit boundary survives
    assert strip_cjk_spaces_across(["Onvif ", "攝影機"]) is None  # Latin<->CJK survives
    assert strip_cjk_spaces_across(["甲", " ", "乙"]) == ["甲", "", "乙"]  # whitespace-only run


def test_latex_to_unicode() -> None:
    assert latex_to_unicode("溫度 $ ^{\\circ} $C 精度 $ \\pm0.3 $") == "溫度 °C 精度 ±0.3"
    assert latex_to_unicode("導線 $ 8mm^{2} $ 及 $ 22 mm^{2} $") == "導線 8mm² 及 22 mm²"
    assert latex_to_unicode("$ H_{2}O $ 與 $ CO_{2} $") == "H₂O 與 CO₂"
    assert latex_to_unicode("$x_{i}^{2}+y_{j}^{3}$") == "x_i²+y_j³"  # operator-safe multi-script
    assert latex_to_unicode("純文字沒有數學") == "純文字沒有數學"  # fast path, unchanged


def test_latex_handles_all_delimiters_and_never_silently_drops() -> None:
    # \( \) and \[ \] must convert too (VL uses them), and structural commands the old
    # hand-rolled table silently deleted (\frac -> "", \sqrt -> "", \sum -> drop ∑) must survive.
    assert latex_to_unicode("溫度 \\( ^{\\circ} \\)C") == "溫度 °C"
    assert latex_to_unicode("公式 \\[ x^{2} \\]") == "公式 x²"
    assert latex_to_unicode("$\\frac{1}{2}$") == "1/2"
    assert "√" in latex_to_unicode("$\\sqrt{2}$")
    assert "∑" in latex_to_unicode("$$ \\sum_{i=1}^{n} x_i $$")  # operator kept, not dropped
    assert latex_to_unicode("$\\leftarrow$") == "←"  # no \le prefix-collision corruption


def test_latex_preserves_currency_and_paths() -> None:
    # A lone `$` is currency, not a math delimiter: it must be left intact (unbalanced spans
    # are not math). Backslashes outside a math delimiter (Windows paths) must be untouched.
    assert latex_to_unicode("價格 $5 美元") == "價格 $5 美元"
    assert latex_to_unicode("約 US$5.5 億元") == "約 US$5.5 億元"
    assert latex_to_unicode("C:\\Users\\test") == "C:\\Users\\test"
    # two currency `$` on one line pair into a `$...$` span; prose CJK + no command -> not math
    assert latex_to_unicode("價格 $5 至 US$9 美元") == "價格 $5 至 US$9 美元"
    # ...and the same for NON-CJK (English) prose: "$5 to $10" is currency, not a math span, so the
    # `$` signs and inter-amount spacing must survive (converting fused it to "5 to10").
    assert latex_to_unicode("The price is $5 to $10 per unit.") == "The price is $5 to $10 per unit."
    assert latex_to_unicode("items cost $3 and $4") == "items cost $3 and $4"
    # VL wraps bare operators/values in SPACE-PADDED `$ ... $` when it styles a formula split
    # around CJK words. That IS math markup (currency never pads the digit away from its `$`):
    # strip the delimiters instead of shipping literal "$ ( $" in the docx/markdown.
    assert latex_to_unicode("公式= $ ( $線路長度 $ * $線路衰減值 $ ) $") == "公式= (線路長度 *線路衰減值 )"
    assert latex_to_unicode("解析度： $ 1920*1080 $（含）以上") == "解析度： 1920*1080（含）以上"
    # a real inline-math span with structure (^ / _ / \\) still converts even without any CJK.
    assert latex_to_unicode("area $x^{2}$ here") == "area x² here"
    # currency `$` FOLLOWED by a real `$...$` formula on one line: the left-to-right scan must not
    # let the currency `$` eat the formula's opening `$` (which would ship "x^{2}" as literal
    # LaTeX). Resuming just past the currency `$` lets "$x^{2}$" re-pair and convert.
    assert latex_to_unicode("cost $5, area $x^{2}$") == "cost $5, area x²"
    assert latex_to_unicode("$3 and area $y^{2}$ end") == "$3 and area y² end"


def test_latex_to_unicode_is_idempotent() -> None:
    # finalize_docx_text may apply it per-run; a second pass must be a no-op.
    for s in ["溫度 $ ^{\\circ} $C 精度 $ \\pm0.3 $", "價格 $5", "$\\frac{1}{2}$ 與 $CO_{2}$"]:
        once = latex_to_unicode(s)
        assert latex_to_unicode(once) == once


def test_s2t_only_applies_for_zh_tw() -> None:
    # OpenCC s2tw is context-aware: it fixes ambiguous chars the old static table skipped
    # (范围 -> 範圍) while leaving Simplified users' output untouched.
    assert fix_ocr_text("温度感测器侦测范围", "zh-TW") == "溫度感測器偵測範圍"
    assert fix_ocr_text("数据", "zh-TW") == "數據"
    assert fix_ocr_text("范围", "zh-CN") == "范围"  # Simplified users keep native output
    assert fix_ocr_text("范围", "en") == "范围"
    assert fix_ocr_text("范围", None) == "範圍"  # default locale is zh-TW


def test_s2t_is_idempotent_on_traditional() -> None:
    # Converting already-Traditional text must be a no-op (no over-conversion).
    trad = "溫度感測器偵測範圍：量測精度以下"
    assert fix_ocr_text(trad, "zh-TW") == trad


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
