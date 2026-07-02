import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { expect, test } from "vite-plus/test";
import { JobStore } from "./db.ts";

const SCHEMA = fileURLToPath(new URL("../../../db/schema.sql", import.meta.url));

function newStore(): JobStore {
  const dir = mkdtempSync(join(tmpdir(), "pdfocr-"));
  return new JobStore(join(dir, "t.db"), SCHEMA);
}

test("enqueue / get / list / queueDepth", () => {
  const s = newStore();
  const id = s.enqueue({ mode: "pdf", filename: "a.pdf", locale: "en", uploadPath: "/tmp/a.pdf" });
  const job = s.get(id);
  expect(job?.status).toBe("queued");
  expect(job?.mode).toBe("pdf");
  expect(job?.progress?.status).toBe("queued");
  expect(s.list()).toHaveLength(1);
  expect(s.queueDepth()).toBe(1);
  expect(s.activeCount()).toBe(1);
  s.close();
});

test("requestCancel on a queued job cancels it; remove deletes it", () => {
  const s = newStore();
  const id = s.enqueue({
    mode: "markdown",
    filename: "b.pdf",
    locale: "zh-TW",
    uploadPath: "/tmp/b.pdf",
  });
  expect(s.requestCancel(id)).toBe("cancelled");
  expect(s.requestCancel("missing")).toBe(null);
  s.remove(id);
  expect(s.get(id)).toBe(null);
  s.close();
});
