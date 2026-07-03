import { type Job, type Mode } from "@pdf-ocr/shared";
import { type ReactElement, useCallback, useRef, useState } from "react";
import { JobCard } from "./components/JobCard.tsx";
import { LanguageMenu } from "./components/LanguageMenu.tsx";
import { PreviewModal } from "./components/PreviewModal.tsx";
import { fetchPreview, triggerDownload } from "./lib/api.ts";
import { fileKey, formatSize } from "./lib/format.ts";
import { useI18n } from "./lib/i18n.tsx";
import { type ToastKind, useJobStore } from "./lib/jobs.ts";
import { useToast } from "./lib/toast.tsx";

const FORMATS: { mode: Mode; titleKey: string; descKey: string; cls: string }[] = [
  { mode: "pdf", titleKey: "fmt_pdf", descKey: "fmt_pdf_desc", cls: "pdf" },
  { mode: "markdown", titleKey: "fmt_md", descKey: "fmt_md_desc", cls: "md" },
  { mode: "word", titleKey: "fmt_word", descKey: "fmt_word_desc", cls: "word" },
];

// Line-art format icons; stroke color is set per-format in CSS (.format-icon.<cls> svg).
const FORMAT_ICONS: Record<Mode, ReactElement> = {
  pdf: (
    <svg
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={2}
      strokeLinecap="round"
      strokeLinejoin="round"
    >
      <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" />
      <polyline points="14 2 14 8 20 8" />
      <line x1="16" y1="13" x2="8" y2="13" />
      <line x1="16" y1="17" x2="8" y2="17" />
    </svg>
  ),
  markdown: (
    <svg
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={2}
      strokeLinecap="round"
      strokeLinejoin="round"
    >
      <path d="M4 4h16c1.1 0 2 .9 2 2v12c0 1.1-.9 2-2 2H4c-1.1 0-2-.9-2-2V6c0-1.1.9-2 2-2z" />
      <polyline points="7 15 10 12 7 9" />
      <line x1="14" y1="15" x2="17" y2="15" />
    </svg>
  ),
  word: (
    <svg
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={2}
      strokeLinecap="round"
      strokeLinejoin="round"
    >
      <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" />
      <polyline points="14 2 14 8 20 8" />
      <path d="M9 15l2 2 4-4" />
    </svg>
  ),
};

// File/upload glyph for the empty drop zone.
const DROP_ZONE_ICON = (
  <svg
    viewBox="0 0 24 24"
    fill="none"
    stroke="currentColor"
    strokeWidth={1.5}
    strokeLinecap="round"
    strokeLinejoin="round"
  >
    <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" />
    <polyline points="14 2 14 8 20 8" />
    <line x1="12" y1="18" x2="12" y2="12" />
    <line x1="9" y1="15" x2="15" y2="15" />
  </svg>
);

export function App() {
  const { t, locale } = useI18n();
  const show = useToast();

  const onToast = useCallback(
    (kind: ToastKind, job: Job) => {
      if (kind === "done") show(t("toast_done"));
      else if (kind === "cancelled") show(t("toast_cancelled"));
      else show(t("toast_failed", { error: job.error || "" }));
    },
    [show, t],
  );

  const { jobs, submit, cancel, remove } = useJobStore(locale, onToast);

  const [files, setFiles] = useState<File[]>([]);
  const [modes, setModes] = useState<Set<Mode>>(new Set());
  const [dragOver, setDragOver] = useState(false);
  const [busy, setBusy] = useState(false);
  const [preview, setPreview] = useState<{ open: boolean; title: string; content: string }>({
    open: false,
    title: "",
    content: "",
  });
  const inputRef = useRef<HTMLInputElement>(null);

  const addFiles = useCallback((list: FileList | null) => {
    if (!list) return;
    setFiles((prev) => {
      const seen = new Set(prev.map(fileKey));
      const next = [...prev];
      for (const f of Array.from(list)) {
        const isSupported = /\.pdf$/i.test(f.name) || f.type === "application/pdf";
        if (isSupported && !seen.has(fileKey(f))) {
          seen.add(fileKey(f));
          next.push(f);
        }
      }
      return next;
    });
  }, []);

  const toggleMode = (m: Mode) =>
    setModes((prev) => {
      const next = new Set(prev);
      if (next.has(m)) next.delete(m);
      else next.add(m);
      return next;
    });

  const process = async () => {
    if (files.length === 0 || modes.size === 0) return;
    setBusy(true);
    try {
      await submit(files, [...modes]);
      setFiles([]);
    } catch (e) {
      show(t("toast_error", { msg: e instanceof Error ? e.message : String(e) }));
    } finally {
      setBusy(false);
    }
  };

  const onDownload = useCallback((job: Job) => triggerDownload(job), []);
  const onPreview = useCallback(
    async (id: string) => {
      try {
        const data = await fetchPreview(id);
        setPreview({ open: true, title: data.filename, content: data.content });
      } catch {
        show(t("preview_load_error"));
      }
    },
    [show, t],
  );

  const completed = jobs.filter((j) => j.status === "done");
  const downloadAll = () => {
    for (const j of completed) triggerDownload(j);
    if (completed.length) show(t("toast_downloading", { count: completed.length }));
  };
  const clearAll = () => {
    for (const j of completed) void remove(j.id);
  };

  return (
    <div className="container">
      <LanguageMenu />

      <header className="header">
        <h1>{t("title")}</h1>
        <p>{t("subtitle")}</p>
      </header>

      <button
        type="button"
        className={`drop-zone ${dragOver ? "dragover" : ""}`}
        onClick={() => inputRef.current?.click()}
        onDragOver={(e) => {
          e.preventDefault();
          setDragOver(true);
        }}
        onDragLeave={() => setDragOver(false)}
        onDrop={(e) => {
          e.preventDefault();
          setDragOver(false);
          addFiles(e.dataTransfer.files);
        }}
      >
        <div className="drop-zone-icon">{DROP_ZONE_ICON}</div>
        <div className="drop-zone-text">{t("drop_zone_text")}</div>
        <div className="drop-zone-hint">{t("drop_zone_hint")}</div>
        <input
          ref={inputRef}
          type="file"
          accept=".pdf,application/pdf"
          multiple
          hidden
          onChange={(e) => addFiles(e.target.files)}
        />
      </button>

      {files.length > 0 && (
        <div className="file-list">
          {files.map((f, i) => (
            <div className="file-item" key={fileKey(f)}>
              <span className="file-item-name">{f.name}</span>
              <span className="file-item-size">{formatSize(f.size)}</span>
              <button
                type="button"
                className="file-item-remove"
                aria-label="Remove"
                onClick={() => setFiles((prev) => prev.filter((_, idx) => idx !== i))}
              >
                &times;
              </button>
            </div>
          ))}
        </div>
      )}

      <div className="section-title">{t("output_format")}</div>
      <div className="format-options">
        {FORMATS.map((f) => (
          <label key={f.mode} className={`format-option ${modes.has(f.mode) ? "selected" : ""}`}>
            <input
              type="checkbox"
              checked={modes.has(f.mode)}
              onChange={() => toggleMode(f.mode)}
            />
            <div className={`format-icon ${f.cls}`}>{FORMAT_ICONS[f.mode]}</div>
            <div className="format-title">{t(f.titleKey)}</div>
            <div className="format-desc">{t(f.descKey)}</div>
          </label>
        ))}
      </div>

      <button
        type="button"
        className={`btn-process ${busy ? "loading" : ""}`}
        disabled={files.length === 0 || modes.size === 0 || busy}
        onClick={process}
      >
        {t("btn_process")}
      </button>

      {jobs.length > 0 && (
        <section className="jobs-section">
          <div className="jobs-header">
            <span className="jobs-title">
              {completed.length > 0
                ? t("completed_count", { count: completed.length })
                : t("header_processing")}
            </span>
            {completed.length > 0 && (
              <div className="jobs-actions">
                <button type="button" className="btn-action" onClick={downloadAll}>
                  {t("btn_download_all")}
                </button>
                <button type="button" className="btn-action" onClick={clearAll}>
                  {t("btn_clear_all")}
                </button>
              </div>
            )}
          </div>
          <div className="jobs-container">
            {jobs.map((job) => (
              <JobCard
                key={job.id}
                job={job}
                onCancel={cancel}
                onRemove={remove}
                onDownload={onDownload}
                onPreview={onPreview}
              />
            ))}
          </div>
        </section>
      )}

      <PreviewModal
        open={preview.open}
        title={preview.title}
        content={preview.content}
        onClose={() => setPreview((p) => ({ ...p, open: false }))}
      />
    </div>
  );
}
