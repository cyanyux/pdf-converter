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
