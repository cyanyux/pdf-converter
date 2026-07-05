import { expect, test } from "vite-plus/test";
import { contentDisposition, safeResolve, sanitizeDownloadName } from "./safe-resolve.ts";

test("safeResolve allows paths within the base", () => {
  expect(safeResolve("/base", "file.pdf")).toBe("/base/file.pdf");
  expect(safeResolve("/base", "sub/file.pdf")).toBe("/base/sub/file.pdf");
});

test("safeResolve rejects traversal, absolute escape, and null bytes", () => {
  expect(() => safeResolve("/base", "../etc/passwd")).toThrow();
  expect(() => safeResolve("/base", "/abs/escape")).toThrow();
  expect(() => safeResolve("/base", "a\0b")).toThrow();
});

test("sanitizeDownloadName strips quotes and path separators", () => {
  expect(sanitizeDownloadName('a/b"c.pdf')).toBe("a_b'c.pdf");
  expect(sanitizeDownloadName("  ..hidden  ")).not.toContain("/");
  expect(sanitizeDownloadName("")).toBe("download");
});

test("contentDisposition percent-encodes RFC 5987 delimiters in filename*", () => {
  // A double quote becomes an apostrophe via sanitizeDownloadName; the ext-value must
  // percent-encode it (%27), not emit a literal ' that an RFC 5987 parser splits on.
  const header = contentDisposition('Q1 "final".pdf');
  const star = /filename\*=UTF-8''(.+)$/.exec(header)?.[1] ?? "";
  expect(star).not.toContain("'");
  expect(star).toContain("%27");
  expect(header).toContain('filename="');
});

test("contentDisposition emits an ASCII fallback for non-Latin1 names", () => {
  // Node rejects non-Latin1 header bytes, so the fallback must be pure ASCII while the
  // RFC 5987 value carries the original name percent-encoded (keeps the .pdf extension).
  const header = contentDisposition("測試文件.pdf");
  expect(/filename="([^"]*)"/.exec(header)?.[1]).toBe("download.pdf");
  expect(header).toContain("filename*=UTF-8''");
  expect(header).not.toContain("測");
});

test("length cap truncates by code point so an astral name can't 500 the download", () => {
  // A CJK Ext-B glyph (U+2137C 𡍼, a surrogate pair) straddling the 200-unit cap must not be
  // bisected: a lone surrogate makes contentDisposition's encodeURIComponent throw (→ HTTP 500).
  const name = `${"a".repeat(199)}𡍼𡍼.pdf`;
  const sanitized = sanitizeDownloadName(name);
  const loneSurrogate = /[\uD800-\uDBFF](?![\uDC00-\uDFFF])|(?<![\uD800-\uDBFF])[\uDC00-\uDFFF]/;
  expect(loneSurrogate.test(sanitized)).toBe(false);
  expect(() => contentDisposition(name)).not.toThrow();
  expect(contentDisposition(name)).toContain("filename*=UTF-8''");
});
