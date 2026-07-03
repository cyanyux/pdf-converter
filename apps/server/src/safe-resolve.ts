import { resolve, sep } from "node:path";

/**
 * Resolve `name` under `baseDir`, rejecting anything that escapes it
 * (path traversal / zip-slip). Rejects null bytes and absolute escapes.
 * Returns the absolute resolved path; throws on violation.
 */
export function safeResolve(baseDir: string, name: string): string {
  if (name.includes("\0")) throw new Error("invalid path: null byte");
  const base = resolve(baseDir);
  const target = resolve(base, name);
  if (target !== base && !target.startsWith(base + sep)) {
    throw new Error(`path escapes base directory: ${name}`);
  }
  return target;
}

// ASCII control chars U+0000-U+001F and U+007F (built via RegExp to keep the
// source free of literal control bytes). Stripping them is the intent.
// oxlint-disable-next-line no-control-regex
const CONTROL_CHARS = new RegExp("[\\u0000-\\u001F\\u007F]", "g");

/**
 * Sanitize a user-supplied filename for a Content-Disposition header: strip
 * control chars, quotes, and path separators; collapse whitespace; cap length;
 * never empty. (Ports the Flask app's sanitize_download_name.)
 */
export function sanitizeDownloadName(filename: string, maxLength = 200): string {
  let out = filename
    .replace(CONTROL_CHARS, "")
    .replace(/"/g, "'")
    .replace(/[\\/]/g, "_")
    .replace(/[_\s]+/g, "_")
    .replace(/^[\s._]+|[\s._]+$/g, "");
  if (out.length > maxLength) out = out.slice(0, maxLength).replace(/[\s._]+$/g, "");
  return out || "download";
}

/**
 * Build a full `Content-Disposition` header value for a download. Node rejects
 * non-Latin1 bytes in header values (ERR_INVALID_CHAR), so a raw CJK/emoji
 * filename would throw an HTTP 500. This emits both an ASCII `filename="…"`
 * fallback (sanitized, then any remaining non-ASCII bytes dropped, keeping the
 * extension) and an RFC 5987 `filename*=UTF-8''…` carrying the original name
 * percent-encoded for modern clients. e.g. `文件.pdf` → fallback `download.pdf`.
 */
export function contentDisposition(filename: string): string {
  const sanitized = sanitizeDownloadName(filename);
  const dot = sanitized.lastIndexOf(".");
  const stem = dot > 0 ? sanitized.slice(0, dot) : sanitized;
  const ext = dot > 0 ? sanitized.slice(dot) : "";
  // Drop any non-ASCII bytes Node's header writer would reject; if the stem is
  // left empty (e.g. an all-CJK name) fall back to "download" but keep the ext.
  const asciiStem = stem.replace(/[^\x20-\x7E]/g, "") || "download";
  const asciiExt = ext.replace(/[^\x20-\x7E]/g, "");
  const ascii = `${asciiStem}${asciiExt}`;
  // RFC 5987 ext-value: encodeURIComponent leaves `' ( ) *` unescaped, but those are NOT
  // attr-char and `'` is the charset/language delimiter — a name with a quote (or a `"` that
  // sanitizeDownloadName turned into `'`) would produce a malformed header a strict parser
  // truncates. Percent-encode those four too. (`!` and `~` are attr-char, so they stay.)
  const encoded = encodeURIComponent(sanitized).replace(
    /['()*]/g,
    (ch) => `%${ch.charCodeAt(0).toString(16).toUpperCase()}`,
  );
  return `attachment; filename="${ascii}"; filename*=UTF-8''${encoded}`;
}
