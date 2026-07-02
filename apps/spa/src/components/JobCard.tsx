import { type Job, isTerminal } from "@pdf-ocr/shared";
import { useI18n } from "../lib/i18n.tsx";

const FORMAT_LABEL: Record<Job["mode"], string> = { pdf: "PDF", markdown: "MD", word: "Word" };
const FORMAT_CLASS: Record<Job["mode"], string> = {
  pdf: "pdf",
  markdown: "markdown",
  word: "word",
};

interface Props {
  job: Job;
  onCancel: (id: string) => void;
  onRemove: (id: string) => void;
  onDownload: (job: Job) => void;
  onPreview: (id: string) => void;
}

export function JobCard({ job, onCancel, onRemove, onDownload, onPreview }: Props) {
  const { t } = useI18n();
  const p = job.progress;
  const statusText =
    job.status === "processing"
      ? p?.status === "waiting"
        ? t("status_waiting")
        : t("status_processing")
      : ({
          done: t("status_done"),
          error: t("status_error"),
          cancelled: t("status_cancelled"),
          queued: t("status_waiting"),
          saving: t("status_processing"),
          cancel_requested: t("status_processing"),
        }[job.status] ?? job.status);

  const pageInfo = job.result
    ? job.result.imagesCount && job.result.imagesCount > 0
      ? t("page_image_info", { pages: job.result.totalPages, images: job.result.imagesCount })
      : t("page_info", { pages: job.result.totalPages })
    : "";

  return (
    <div className={`job-card ${job.status}`}>
      <div className="job-header">
        <span className="job-filename">
          {job.filename}
          <span className={`job-format-badge ${FORMAT_CLASS[job.mode]}`}>
            {FORMAT_LABEL[job.mode]}
          </span>
        </span>
        <span className={`job-status ${job.status}`}>{statusText}</span>
      </div>

      {!isTerminal(job.status) && (
        <div className="job-progress">
          <div className="progress-bar">
            <div className="progress-fill" style={{ width: `${p?.percent ?? 0}%` }} />
          </div>
          <div className="progress-text">{p?.message || t("status_processing")}</div>
          <button type="button" className="btn-cancel" onClick={() => onCancel(job.id)}>
            {t("btn_cancel")}
          </button>
        </div>
      )}

      {job.status === "done" && job.result && (
        <>
          <div className="job-result">
            <div className="job-result-text">
              <h3>{t("result_done")}</h3>
              <p>{pageInfo}</p>
            </div>
          </div>
          <div className="job-buttons">
            <button
              type="button"
              className={`btn-download ${FORMAT_CLASS[job.mode]}`}
              onClick={() => onDownload(job)}
            >
              {t("btn_download")}
            </button>
            {job.mode === "markdown" && (
              <button type="button" className="btn-secondary" onClick={() => onPreview(job.id)}>
                {t("btn_preview")}
              </button>
            )}
            <button type="button" className="btn-dismiss" onClick={() => onRemove(job.id)}>
              {t("btn_remove")}
            </button>
          </div>
        </>
      )}

      {job.status === "error" && (
        <>
          <div className="error-message">{job.error || t("error_default")}</div>
          <div className="job-buttons">
            <button type="button" className="btn-dismiss" onClick={() => onRemove(job.id)}>
              {t("btn_remove")}
            </button>
          </div>
        </>
      )}

      {job.status === "cancelled" && (
        <div className="job-buttons">
          <button type="button" className="btn-dismiss" onClick={() => onRemove(job.id)}>
            {t("btn_remove")}
          </button>
        </div>
      )}
    </div>
  );
}
