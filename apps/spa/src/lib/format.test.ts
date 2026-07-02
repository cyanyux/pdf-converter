import { expect, test } from "vite-plus/test";
import { fileKey, formatSize } from "./format.ts";

test("formatSize", () => {
  expect(formatSize(512)).toBe("512 B");
  expect(formatSize(2048)).toBe("2.0 KB");
  expect(formatSize(5 * 1024 * 1024)).toBe("5.0 MB");
});

test("fileKey combines name, size, lastModified", () => {
  expect(fileKey({ name: "a.pdf", size: 10, lastModified: 5 })).toBe("a.pdf|10|5");
});
