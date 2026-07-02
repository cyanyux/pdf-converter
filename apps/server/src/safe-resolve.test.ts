import { expect, test } from "vite-plus/test";
import { safeResolve, sanitizeDownloadName } from "./safe-resolve.ts";

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
