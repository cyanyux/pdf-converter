import { useEffect } from "react";
import Markdown from "react-markdown";
import { useI18n } from "../lib/i18n.tsx";

interface Props {
  open: boolean;
  title: string;
  content: string;
  onClose: () => void;
}

export function PreviewModal({ open, title, content, onClose }: Props) {
  const { t } = useI18n();
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [open, onClose]);

  if (!open) return null;
  return (
    <div
      className="modal show"
      onClick={(e) => {
        if (e.target === e.currentTarget) onClose();
      }}
    >
      <div className="modal-content">
        <div className="modal-header">
          <span className="modal-title">{title || t("preview_title")}</span>
          <button type="button" className="modal-close" onClick={onClose} aria-label="Close">
            &times;
          </button>
        </div>
        <div className="modal-body">
          <div className="markdown-preview">
            <Markdown>{content}</Markdown>
          </div>
        </div>
      </div>
    </div>
  );
}
