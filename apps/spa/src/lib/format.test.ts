import { expect, test } from "vite-plus/test";
import { fileKey, formatSize, skipReasonKey } from "./format.ts";

test("formatSize", () => {
  expect(formatSize(512)).toBe("512 B");
  expect(formatSize(2048)).toBe("2.0 KB");
  expect(formatSize(5 * 1024 * 1024)).toBe("5.0 MB");
});

test("fileKey combines name, size, lastModified", () => {
  expect(fileKey({ name: "a.pdf", size: 10, lastModified: 5 })).toBe("a.pdf|10|5");
});

test("skipReasonKey maps known reason codes", () => {
  expect(skipReasonKey("too_large")).toBe("skip_too_large");
  expect(skipReasonKey("invalid_pdf")).toBe("skip_invalid_pdf");
  expect(skipReasonKey("not_pdf")).toBe("skip_not_pdf");
  expect(skipReasonKey("encrypted_pdf")).toBe("skip_encrypted_pdf");
  expect(skipReasonKey("empty_pdf")).toBe("skip_empty_pdf");
  expect(skipReasonKey("unsupported")).toBe("skip_unsupported");
});

test("skipReasonKey matches too_many_files despite trailing detail", () => {
  expect(skipReasonKey("too_many_files (max 20 per request)")).toBe("skip_too_many_files");
});

test("skipReasonKey falls back for unknown codes", () => {
  expect(skipReasonKey("something_new")).toBe("skip_unknown");
  expect(skipReasonKey("")).toBe("skip_unknown");
});
