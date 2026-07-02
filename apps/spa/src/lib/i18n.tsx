import { type Locale, LOCALES } from "@pdf-ocr/shared";
import { createContext, type ReactNode, useCallback, useContext, useMemo, useState } from "react";

type Dict = Record<string, string>;

const STRINGS: Record<Locale, Dict> = {
  "zh-TW": {
    title: "文件識別",
    subtitle: "將掃描檔或圖片 PDF 轉換為可編輯格式",
    drop_zone_text: "拖曳 PDF 檔案至此",
    drop_zone_hint: "或點擊選擇檔案",
    output_format: "輸出格式",
    fmt_pdf: "可選取 PDF",
    fmt_pdf_desc: "保留原版面，可複製文字",
    fmt_md: "Markdown",
    fmt_md_desc: "結構化純文字",
    fmt_word: "Word",
    fmt_word_desc: "可編輯文件",
    btn_process: "開始轉換",
    results_title: "轉換結果",
    header_processing: "處理中...",
    completed_count: "{count} 項已完成",
    btn_download: "下載",
    btn_download_all: "全部下載",
    btn_clear_all: "清除全部",
    btn_remove: "移除",
    btn_cancel: "取消",
    btn_preview: "預覽",
    preview_title: "內容預覽",
    status_waiting: "等待中",
    status_processing: "處理中",
    status_done: "完成",
    status_error: "失敗",
    status_cancelled: "已取消",
    result_done: "轉換完成",
    page_info: "{pages} 頁",
    page_image_info: "{pages} 頁，{images} 張圖片",
    error_default: "處理時發生錯誤",
    toast_done: "轉換完成",
    toast_failed: "轉換失敗：{error}",
    toast_cancelled: "已取消處理",
    toast_downloading: "開始下載 {count} 個檔案",
    toast_error: "錯誤：{msg}",
    preview_load_error: "無法載入預覽",
    unnamed_file: "未命名檔案",
    reconnecting: "重新連線中...",
  },
  "zh-CN": {
    title: "文件识别",
    subtitle: "将扫描件或图片 PDF 转换为可编辑格式",
    drop_zone_text: "拖拽 PDF 文件至此",
    drop_zone_hint: "或点击选择文件",
    output_format: "输出格式",
    fmt_pdf: "可选取 PDF",
    fmt_pdf_desc: "保留原版面，可复制文字",
    fmt_md: "Markdown",
    fmt_md_desc: "结构化纯文本",
    fmt_word: "Word",
    fmt_word_desc: "可编辑文档",
    btn_process: "开始转换",
    results_title: "转换结果",
    header_processing: "处理中...",
    completed_count: "{count} 项已完成",
    btn_download: "下载",
    btn_download_all: "全部下载",
    btn_clear_all: "清除全部",
    btn_remove: "移除",
    btn_cancel: "取消",
    btn_preview: "预览",
    preview_title: "内容预览",
    status_waiting: "等待中",
    status_processing: "处理中",
    status_done: "完成",
    status_error: "失败",
    status_cancelled: "已取消",
    result_done: "转换完成",
    page_info: "{pages} 页",
    page_image_info: "{pages} 页，{images} 张图片",
    error_default: "处理时发生错误",
    toast_done: "转换完成",
    toast_failed: "转换失败：{error}",
    toast_cancelled: "已取消处理",
    toast_downloading: "开始下载 {count} 个文件",
    toast_error: "错误：{msg}",
    preview_load_error: "无法加载预览",
    unnamed_file: "未命名文件",
    reconnecting: "重新连接中...",
  },
  en: {
    title: "Document Recognition",
    subtitle: "Convert scanned or image PDFs to editable formats",
    drop_zone_text: "Drop PDF files here",
    drop_zone_hint: "or click to select files",
    output_format: "Output Format",
    fmt_pdf: "Searchable PDF",
    fmt_pdf_desc: "Preserves layout, selectable text",
    fmt_md: "Markdown",
    fmt_md_desc: "Structured plain text",
    fmt_word: "Word",
    fmt_word_desc: "Editable document",
    btn_process: "Start Conversion",
    results_title: "Results",
    header_processing: "Processing...",
    completed_count: "{count} completed",
    btn_download: "Download",
    btn_download_all: "Download All",
    btn_clear_all: "Clear All",
    btn_remove: "Remove",
    btn_cancel: "Cancel",
    btn_preview: "Preview",
    preview_title: "Content Preview",
    status_waiting: "Waiting",
    status_processing: "Processing",
    status_done: "Done",
    status_error: "Failed",
    status_cancelled: "Cancelled",
    result_done: "Conversion complete",
    page_info: "{pages} pages",
    page_image_info: "{pages} pages, {images} images",
    error_default: "An error occurred during processing",
    toast_done: "Conversion complete",
    toast_failed: "Conversion failed: {error}",
    toast_cancelled: "Processing cancelled",
    toast_downloading: "Downloading {count} files",
    toast_error: "Error: {msg}",
    preview_load_error: "Unable to load preview",
    unnamed_file: "Unnamed file",
    reconnecting: "Reconnecting...",
  },
};

const STORAGE_KEY = "pdfOcrLocale";

function detectLocale(): Locale {
  const saved = localStorage.getItem(STORAGE_KEY);
  if (saved && (LOCALES as readonly string[]).includes(saved)) return saved as Locale;
  const lang = (navigator.language || "").toLowerCase();
  if (lang.startsWith("en")) return "en";
  if (lang.includes("hans") || lang === "zh-cn" || lang === "zh-sg") return "zh-CN";
  return "zh-TW";
}

interface I18nValue {
  locale: Locale;
  setLocale: (l: Locale) => void;
  t: (key: string, params?: Record<string, string | number>) => string;
}

const I18nContext = createContext<I18nValue | null>(null);

export function I18nProvider({ children }: { children: ReactNode }) {
  const [locale, setLocaleState] = useState<Locale>(detectLocale);
  const setLocale = useCallback((l: Locale) => {
    localStorage.setItem(STORAGE_KEY, l);
    document.documentElement.lang = l;
    setLocaleState(l);
  }, []);
  const t = useCallback(
    (key: string, params?: Record<string, string | number>) => {
      let text = STRINGS[locale][key] ?? STRINGS["zh-TW"][key] ?? key;
      if (params) {
        for (const [k, v] of Object.entries(params)) text = text.replace(`{${k}}`, String(v));
      }
      return text;
    },
    [locale],
  );
  const value = useMemo(() => ({ locale, setLocale, t }), [locale, setLocale, t]);
  return <I18nContext value={value}>{children}</I18nContext>;
}

export function useI18n(): I18nValue {
  const ctx = useContext(I18nContext);
  if (!ctx) throw new Error("useI18n outside provider");
  return ctx;
}
