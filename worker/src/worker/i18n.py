# Backend i18n translations for progress/error messages.
# Supported locales: zh-TW (default), zh-CN, en

MESSAGES = {
    # Progress messages
    "converting_start": {
        "zh-TW": "開始轉換 {pages} 頁...",
        "zh-CN": "开始转换 {pages} 页...",
        "en": "Converting {pages} pages...",
    },
    "converting_doc": {
        "zh-TW": "轉換文件中…",
        "zh-CN": "转换文件中…",
        "en": "Converting document...",
    },
    "converting_page": {
        "zh-TW": "轉換第 {current}/{total} 頁...",
        "zh-CN": "转换第 {current}/{total} 页...",
        "en": "Converting page {current}/{total}...",
    },
    "processing_start": {
        "zh-TW": "開始處理 {pages} 頁...",
        "zh-CN": "开始处理 {pages} 页...",
        "en": "Processing {pages} pages...",
    },
    "recognizing_page": {
        "zh-TW": "辨識第 {current}/{total} 頁...",
        "zh-CN": "识别第 {current}/{total} 页...",
        "en": "Recognizing page {current}/{total}...",
    },
    "recognizing_pages": {
        "zh-TW": "辨識第 {start}-{end}/{total} 頁...",
        "zh-CN": "识别第 {start}-{end}/{total} 页...",
        "en": "Recognizing pages {start}-{end}/{total}...",
    },
    "consolidating": {
        "zh-TW": "整合頁面內容...",
        "zh-CN": "整合页面内容...",
        "en": "Consolidating page content...",
    },
    "saving_pdf": {
        "zh-TW": "儲存 PDF 中...",
        "zh-CN": "保存 PDF 中...",
        "en": "Saving PDF...",
    },
    "converting_word": {
        "zh-TW": "轉換為 Word...",
        "zh-CN": "转换为 Word...",
        "en": "Converting to Word...",
    },
    "done": {
        "zh-TW": "完成!",
        "zh-CN": "完成!",
        "en": "Done!",
    },
    "cancelled": {
        "zh-TW": "已取消",
        "zh-CN": "已取消",
        "en": "Cancelled",
    },
    "waiting": {
        "zh-TW": "等待中...",
        "zh-CN": "等待中...",
        "en": "Waiting...",
    },
    "timeout": {
        "zh-TW": "連線逾時",
        "zh-CN": "连接超时",
        "en": "Connection timeout",
    },
    # Error messages
    "err_encrypted_pdf": {
        "zh-TW": "無法處理加密的 PDF 檔案",
        "zh-CN": "无法处理加密的 PDF 文件",
        "en": "Cannot process encrypted PDF file",
    },
    "err_empty_pdf": {
        "zh-TW": "PDF 檔案沒有頁面",
        "zh-CN": "PDF 文件没有页面",
        "en": "PDF file has no pages",
    },
    "err_no_markdown": {
        "zh-TW": "VL 模型未產生 Markdown 檔案",
        "zh-CN": "VL 模型未产生 Markdown 文件",
        "en": "VL model did not produce Markdown file",
    },
    "err_no_word": {
        "zh-TW": "VL 模型未產生 Word 檔案",
        "zh-CN": "VL 模型未产生 Word 文件",
        "en": "VL model did not produce a Word file",
    },
    "err_partial_pages": {
        "zh-TW": "部分頁面處理失敗: {pages}",
        "zh-CN": "部分页面处理失败: {pages}",
        "en": "Some pages failed to process: {pages}",
    },
}

DEFAULT_LOCALE = "zh-TW"
SUPPORTED_LOCALES = ("zh-TW", "zh-CN", "en")


def normalize_locale(locale: str | None) -> str:
    """Normalize locale string to one of the supported locales."""
    if not locale:
        return DEFAULT_LOCALE
    locale = locale.strip()
    # Exact match
    if locale in SUPPORTED_LOCALES:
        return locale
    # Case-insensitive match
    lower = locale.lower().replace("_", "-")
    for supported in SUPPORTED_LOCALES:
        if lower == supported.lower():
            return supported
    # Prefix match (e.g. 'zh-Hans' -> 'zh-CN', 'zh-Hant' -> 'zh-TW', 'en-US' -> 'en')
    if lower.startswith("zh"):
        if "hans" in lower or "cn" in lower or "sg" in lower:
            return "zh-CN"
        return "zh-TW"
    if lower.startswith("en"):
        return "en"
    return DEFAULT_LOCALE


def msg(key: str, locale: str | None = None, **kwargs: object) -> str:
    """Get a translated message by key.

    Args:
        key: Message key from MESSAGES dict
        locale: Target locale (zh-TW, zh-CN, en). Defaults to zh-TW.
        **kwargs: Format arguments for the message template

    Returns:
        Translated and formatted message string
    """
    locale = normalize_locale(locale)
    translations = MESSAGES.get(key)
    if not translations:
        return key
    template = translations.get(locale, translations.get(DEFAULT_LOCALE, key))
    if kwargs:
        try:
            return template.format(**kwargs)
        except (KeyError, IndexError):
            return template
    return template
