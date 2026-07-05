/** Stable key for de-duplicating selected files. */
export function fileKey(f: { name: string; size: number; lastModified: number }): string {
  return `${f.name}|${f.size}|${f.lastModified}`;
}

/** Human-readable byte size. */
export function formatSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

// Map a server skip-reason code to an i18n key. The server tags too_many_files with
// a trailing detail (e.g. "too_many_files (max 20 per request)"), so match on the
// leading token; unknown codes fall back to a generic message.
const SKIP_REASON_KEYS: Record<string, string> = {
  too_large: "skip_too_large",
  invalid_pdf: "skip_invalid_pdf",
  not_pdf: "skip_not_pdf",
  encrypted_pdf: "skip_encrypted_pdf",
  empty_pdf: "skip_empty_pdf",
  unsupported: "skip_unsupported",
  too_many_files: "skip_too_many_files",
};

/** i18n key for a server-reported skip reason (see SKIP_REASON_KEYS). */
export function skipReasonKey(reason: string): string {
  const code = reason.split(" ", 1)[0];
  return SKIP_REASON_KEYS[code] ?? "skip_unknown";
}
