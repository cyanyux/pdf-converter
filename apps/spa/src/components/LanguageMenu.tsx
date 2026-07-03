import { LOCALES, type Locale } from "@pdf-ocr/shared";
import { type ReactElement, useEffect, useRef, useState } from "react";
import { useI18n } from "../lib/i18n.tsx";

const LOCALE_LABEL: Record<Locale, string> = {
  "zh-TW": "繁體中文",
  "zh-CN": "简体中文",
  en: "English",
};

/**
 * Language switcher as an app-styled dropdown instead of a native <select> (whose popup
 * the OS renders with its own chrome). We own the trigger + menu markup so it matches the
 * app; closes on outside-click and Escape.
 */
export function LanguageMenu(): ReactElement {
  const { locale, setLocale } = useI18n();
  const [open, setOpen] = useState(false);
  const rootRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    const onPointerDown = (e: MouseEvent) => {
      if (rootRef.current && !rootRef.current.contains(e.target as Node)) setOpen(false);
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setOpen(false);
    };
    document.addEventListener("mousedown", onPointerDown);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onPointerDown);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  return (
    <div className="lang-switcher" ref={rootRef}>
      <button
        type="button"
        className="lang-trigger"
        aria-haspopup="listbox"
        aria-expanded={open}
        aria-label="Language"
        onClick={() => setOpen((o) => !o)}
      >
        <span>{LOCALE_LABEL[locale]}</span>
        <svg className="lang-chevron" viewBox="0 0 12 12" aria-hidden="true">
          <path
            d="M2.5 4.5 L6 8 L9.5 4.5"
            fill="none"
            stroke="currentColor"
            strokeWidth={1.5}
            strokeLinecap="round"
            strokeLinejoin="round"
          />
        </svg>
      </button>
      {open && (
        <div className="lang-menu" role="listbox" aria-label="Language">
          {LOCALES.map((l) => (
            <button
              key={l}
              type="button"
              role="option"
              aria-selected={l === locale}
              className={`lang-item ${l === locale ? "selected" : ""}`}
              onClick={() => {
                setLocale(l);
                setOpen(false);
              }}
            >
              <span className="lang-check" aria-hidden="true">
                {l === locale ? "✓" : ""}
              </span>
              {LOCALE_LABEL[l]}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}
