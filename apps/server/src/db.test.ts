import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { DatabaseSync } from "node:sqlite";
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
  const id = s.enqueue({
    mode: "pdf",
    filename: "a.pdf",
    locale: "en",
    engine: "auto",
    uploadPath: "/tmp/a.pdf",
  });
  const job = s.get(id);
  expect(job?.status).toBe("queued");
  expect(job?.mode).toBe("pdf");
  expect(job?.engine).toBe("auto");
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
    engine: "auto",
    uploadPath: "/tmp/b.pdf",
  });
  expect(s.requestCancel(id)).toBe("cancelled");
  expect(s.requestCancel("missing")).toBe(null);
  s.remove(id);
  expect(s.get(id)).toBe(null);
  s.close();
});

test("requestCancel on a running job asks it to stop", () => {
  const s = newStore();
  const id = s.enqueue({
    mode: "pdf",
    filename: "c.pdf",
    locale: "en",
    engine: "auto",
    uploadPath: "/tmp/c.pdf",
  });
  // Simulate the worker having claimed the job (queued -> processing).
  s.db.prepare("UPDATE jobs SET status='processing' WHERE id=?").run(id);
  expect(s.requestCancel(id)).toBe("cancel_requested");
  expect(s.get(id)?.status).toBe("cancel_requested");
  s.close();
});

test("requestCancel does not clobber a terminal status the worker committed", () => {
  const s = newStore();
  const id = s.enqueue({
    mode: "pdf",
    filename: "d.pdf",
    locale: "en",
    engine: "auto",
    uploadPath: "/tmp/d.pdf",
  });
  // Stand in for the worker committing 'done' on its own connection just before the
  // cancel lands: the terminal-guarded UPDATE must leave the row terminal and report it.
  s.db.prepare("UPDATE jobs SET status='done' WHERE id=?").run(id);
  expect(s.requestCancel(id)).toBe("done");
  expect(s.get(id)?.status).toBe("done");
  s.close();
});

test("migrate() adds the engine column to a pre-engine database", () => {
  const dir = mkdtempSync(join(tmpdir(), "pdfocr-"));
  const path = join(dir, "old.db");
  // Stand up a jobs table matching schema.sql's DDL BEFORE the engine column existed:
  // schema.sql runs via CREATE TABLE IF NOT EXISTS, so this old table is left untouched
  // by the schema exec and only the constructor's migrate() can add engine.
  const seed = new DatabaseSync(path);
  seed.exec(
    "CREATE TABLE jobs (" +
      "id TEXT PRIMARY KEY, group_id TEXT, mode TEXT NOT NULL, filename TEXT NOT NULL, " +
      "locale TEXT NOT NULL DEFAULT 'zh-TW', status TEXT NOT NULL DEFAULT 'queued', " +
      "attempts INTEGER NOT NULL DEFAULT 0, upload_path TEXT, download_id TEXT, " +
      "result_json TEXT, error TEXT, created_at REAL NOT NULL, updated_at REAL NOT NULL, " +
      "heartbeat_at REAL)",
  );
  seed.close();

  // Opening JobStore on the old file must not throw, and engine must now be queryable.
  const s = new JobStore(path, SCHEMA);
  const cols = s.db.prepare("PRAGMA table_info(jobs)").all() as { name: string }[];
  expect(cols.some((c) => c.name === "engine")).toBe(true);
  // A round-trip through enqueue/get proves the migrated column is usable, defaulting to 'auto'.
  const id = s.enqueue({
    mode: "markdown",
    filename: "old.pdf",
    locale: "en",
    engine: "vl",
    uploadPath: "/tmp/old.pdf",
  });
  expect(s.get(id)?.engine).toBe("vl");
  s.close();
});
